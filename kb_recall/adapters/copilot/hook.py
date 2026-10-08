#!/usr/bin/env python3
"""recall-mcp hook for GitHub Copilot — `sessionStart` auto-load, `postToolUse` reminders.

Copilot's equivalent of Claude Code's UserPromptSubmit hook, restricted to what the
Copilot hook protocol can actually do:

  `sessionStart`  → `hookSpecificOutput.additionalContext`  — auto-load the KB
  `postToolUse`   → `hookSpecificOutput.additionalContext`  — per-turn reminders

Both are real channels on the VS Code harness: `sessionStart` was verified E2E on
VS Code 1.139.1 / copilot-agent 0.67.0 (the model received the injected text and
quoted it back verbatim), `postToolUse` per the VS Code hooks reference. The wrapper
differs by harness — VS Code nests `additionalContext` under `hookSpecificOutput`,
while the Copilot CLI takes it as a plain top-level key — so this adapter emits the
VS Code shape, matching the only harness tested here. Payload CASING is the signal
that distinguishes the two, should CLI support be added later.

What this adapter deliberately does NOT do, and why:

  - No idle-gap cost guard. The prompt-time event is mutation-only by spec — it can
    rewrite prompt text but cannot block a turn — so the `decision:block` + exit-2
    guard Claude Code relies on has no Copilot equivalent here. Also note VS Code's
    own prompt event is `UserPromptSubmit` (NOT the Copilot CLI's
    `userPromptTransformed`), and its output schema is "common output only", so it
    could not inject context even under the right name.
  - No reload when the branch changes mid-session. `sessionStart` runs once; see
    `_session_start`.

Per-turn reminders come from `postToolUse` — but NOT counted the way Claude counts
them. Claude's `UserPromptSubmit` fires exactly once per user turn, which is what
makes its counter a true turn count. `postToolUse` fires once per *matched tool call*
and the payload carries no turn id to recover the distinction (`tool_use_id`
identifies one call, not one turn), so the counter here counts tool calls and the
interval is scaled to match — see SAVE_REMINDER_EVERY_TOOL_CALLS.

Two failure modes drive most of the design here, both silent:

  1. Copilot concatenates everything left on stdout and runs ONE `json.loads`
     over it. A second JSON object makes the whole output invalid and it is
     discarded without an error — so there is exactly one write point (`_emit`),
     and no code path may print anything else.
  2. `additionalContext` is capped by the harness — documented at 10 KB (joined
     across hooks) for postToolUse; the sessionStart cap is undocumented, so this
     adapter applies a self-imposed ~9 KB budget as a safety margin. The harness
     counts bytes, not chars, so a 10,000-char ASCII budget already leaves
     headroom. The body is requested through `load_feature_context(max_chars=...)`,
     whose budget render drops whole low-priority sections instead of hard-
     truncating one, so nothing is ever cut mid-entry.
"""

import json
import subprocess
import sys
from pathlib import Path

from kb_recall.hooks.hook_helpers import (
    append_turn,
    count_turns,
    find_slug_for_branch,
    load_asked,
    mark_asked,
)
from kb_recall.stdio import force_utf8_stdio

KB_ROOT = Path.home() / ".recall-mcp"

# Copilot's additionalContext cap, documented at 10 KB for postToolUse (joined
# across hooks). The sessionStart cap is undocumented, so this adapter treats the
# same 10 KB as its budget and leaves a ~1 KB margin for other hooks and for the
# chars-vs-bytes mismatch. Passed as `max_chars` to load_feature_context.
COPILOT_CONTEXT_CAP_CHARS = 10_000
# The sessionStart budget: Copilot's cap minus the ~1 KB margin above. Passed as
# `max_chars` to load_feature_context, whose budget render drops whole sections
# instead of hard-truncating one mid-entry.
SESSION_START_BUDGET_CHARS = COPILOT_CONTEXT_CAP_CHARS - 1000

# Mirrors prompt_submit.py: shared branches never auto-load a KB.
SKIPPED_BRANCHES = ("main", "master", "develop", "dev")

GIT_TIMEOUT_SECONDS = 3

# Reminder cadence, in TOOL CALLS — not turns. See the module docstring: postToolUse
# has no turn id, so a turn counter cannot be built here. Claude reminds every
# SAVE_REMINDER_INTERVAL = 8 user turns; the interval here is scaled to match
# 8 turns × measured calls-per-turn.
#
# CAVEAT (2026-09-26): VS Code Local parses `matcher` and ignores its VALUE, so
# every tool call advances the counter — not just matcher-matching ones. Measured
# 2026-09-26: ~8.8 tool calls per user turn (709 hook runs / ~81 turns), so 8 turns
# ≈ 70 calls. The old 16 (8 turns × an assumed ~2 calls/turn) fired ~4.4x denser
# than intended. Retuned to 70 on that measurement (OI-4's "measure first" is now
# satisfied). Misses stay far rarer than save-worthy insights (5 report_miss vs 224
# save_memory, all-time), so miss cadence stays coarse at 5x SAVE.
SAVE_REMINDER_EVERY_TOOL_CALLS = 70
MISS_REMINDER_EVERY_TOOL_CALLS = 5 * SAVE_REMINDER_EVERY_TOOL_CALLS

# Matcher for the generated postToolUse entry. Where matcher values are honored, the
# harness tests it against the whole tool name anchored as `^(?:PATTERN)$`, and an
# invalid regex makes it skip the entry SILENTLY — so it is kept trivial and asserted
# in tests.
#
# VS Code Local does NOT honor the value: it parses the field for Claude compatibility
# and then ignores it, running every nested command for the event. So this does not
# reduce how often the hook runs there; branch on the event input inside the handler if
# real filtering is ever needed. Kept anyway — other harnesses do honor it, and it
# documents intent.
POST_TOOL_USE_MATCHER = "bash|edit|create"

# Both events are dispatched on argv[1]. Named here so `main` reads as a table.
SUPPORTED_EVENTS = ("sessionStart", "postToolUse")


def _emit(payload: dict) -> None:
    """Write exactly ONE JSON object to stdout — the hook's only write point.

    Kept as a single function on purpose: Copilot runs one JSON parse over the
    whole stdout, so a second object anywhere makes every object invalid and the
    entire output is dropped silently.
    """
    sys.stdout.write(json.dumps(payload) + "\n")


def _current_branch(cwd: Path) -> str:
    """Return the current git branch, or "" when git is unavailable.

    Not in `hook_helpers.py`: that module's contract is pure functions with no
    side effects, and this shells out. Mirrors prompt_submit.py's invocation.
    """
    try:
        return subprocess.run(
            ["git", "branch", "--show-current"],
            capture_output=True,
            text=True,
            timeout=GIT_TIMEOUT_SECONDS,
            cwd=str(cwd),
            check=False,  # a missing git or a detached HEAD just yields ""
        ).stdout.strip()
    except Exception:
        return ""


def _project_name(cwd: Path) -> str:
    """Return the `project=` value for the MCP tools — the bare directory name.

    The server's `_filter` matches on `Path.name` (or a full path string), so the
    name is what it expects; a path would work too but is not what it prefers.
    """
    return cwd.name


def _features_index(cwd: Path) -> Path:
    """Path to this project's features.md — the KB store's existence check.

    Its absence is the cheap, git-free "this project has no KB" guard, used by both
    the sessionStart and postToolUse paths before either does anything expensive.
    """
    return KB_ROOT / _project_name(cwd) / "features.md"


def _session_id(payload: dict) -> str:
    """Return the session id under either payload casing.

    VS Code emits `session_id` (snake_case); the native Copilot CLI emits
    `sessionId`. Reading the wrong one yields "" — no crash, but every session then
    collapses onto the same counter key, so the cadence would carry over between
    sessions instead of resetting, and the first reminder of a long-lived key would
    come at an unpredictable point.
    """
    return payload.get("session_id") or payload.get("sessionId") or ""


def _slug_for(cwd: Path, branch: str | None = None) -> str | None:
    """Map the current branch to a feature slug using this project's features.md.

    Reuses `find_slug_for_branch` rather than matching a branch-name convention:
    branch-to-slug is *data* in features.md (a `Branch(es)` cell), so there is no
    pattern to match on.

    `branch` may be passed by a caller that already resolved it (`_session_start`
    needs the branch even when the lookup misses, for the unmapped-branch hint);
    passing None shells out to git here.

    Doubles as the cheap guard for the expensive `kb_recall.server` import — no
    features.md for this project means no KB, so the import is skipped entirely.
    """
    if branch is None:
        branch = _current_branch(cwd)
    if not branch or branch in SKIPPED_BRANCHES:
        return None

    index = _features_index(cwd)
    if not index.exists():
        return None

    try:
        return find_slug_for_branch(index.read_text(encoding="utf-8"), branch)
    except Exception:
        return None


def _session_start(payload: dict) -> dict:
    """Return the sessionStart hook output, or {} when there is nothing to inject.

    {} is the correct answer for "this project has no KB for this branch" — it
    injects nothing and is still valid JSON, so the harness has nothing to report.

    NOTE: because this runs once per session, switching branches mid-session does
    NOT re-load a different KB. There is no prompt-time event to hook on Copilot.

    An UNMAPPED branch (a features.md exists for this project but this branch has
    no row) is not silent: one short hint is injected, once per (session, branch),
    pointing at the link-feature flow — this restores the environment-delivered
    "ask the user" framing that Claude's UserPromptSubmit hook prints on the same
    discovery. Without it, the decision collapses onto the agent's own judgment
    (observed 2026-09-26: the gap surfaced mid-diagnosis and got framed as a
    remedy to apply rather than a choice to present). Shared branches stay fully
    silent — "on main" is a deliberate non-feature state, not a gap to fix.
    """
    cwd = Path(payload.get("cwd") or Path.cwd())
    branch = _current_branch(cwd)
    slug = _slug_for(cwd, branch)
    if not slug:
        if not branch or branch in SKIPPED_BRANCHES:
            return {}
        if not _features_index(cwd).exists():
            return {}  # no KB store for this project — nothing to link to

        session_id = _session_id(payload)
        state_file = KB_ROOT / _project_name(cwd) / "copilot-session-state"
        # Without a session id the ask-once key collapses to "" — and
        # load_asked("") DOES match entries written with session_id "" (the
        # filter is plain ==), which would wedge the hint off for every
        # future id-less invocation. So the gate applies only with a real id:
        # better a repeated hint than a silent gap. VS Code payloads always
        # carry session_id, so this branch is the defensive path.
        if session_id:
            asked_key = "__new__" + branch.replace("/", "-").replace("_", "-")
            if asked_key in load_asked(session_id, state_file):
                return {}  # hint already delivered for this branch this session
            mark_asked(session_id, asked_key, state_file)
        return {
            "hookSpecificOutput": {
                "hookEventName": "SessionStart",
                "additionalContext": (
                    f"[recall-mcp] Branch '{branch}' has no feature KB. If this "
                    "branch belongs to an existing feature, run /recall-link-feature "
                    "to link it (keep both branches or replace the old name); if "
                    "it is a new feature, /recall-init creates a KB. This is the "
                    "user's call — offer the choice, do not decide for them. "
                    "Do not load a candidate slug until the user chooses — "
                    "loading presumes the mapping."
                ),
            }
        }

    # Imported here, not at module level: printing on import would corrupt the
    # JSON on stdout, and the import costs ~420ms that a project with no KB
    # should not pay.
    from kb_recall import server

    # Pass `project=` explicitly. server._SESSION_PROJECT is computed at IMPORT
    # time from the process cwd — which is not the payload's cwd — and relying on
    # it fails silently as "feature not found". Reads the payload's cwd only.
    # `max_chars` keeps the injected body under Copilot's additionalContext budget
    # with a ~1 KB margin (the harness counts bytes, not chars); the budget render
    # drops whole low-priority sections instead of hard-truncating one.
    context = server.load_feature_context(
        slug,
        project=_project_name(cwd),
        max_chars=SESSION_START_BUDGET_CHARS,
    )
    if not context.startswith("# Feature context:"):
        return {}  # not found / too large — inject nothing rather than an error

    return {
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": context,
        }
    }


def _reminder_text(slug: str, calls: int, save_due: bool, miss_due: bool) -> str:
    """Build the reminder line(s) — deliberately terse.

    `additionalContext` from every registered hook is merged with blank lines and
    capped at 10 KB together, and this text lands on a tool result the model reads
    mid-task. Claude's equivalent can afford four lines and a randomised phrasing
    list; here the shortest wording that still forces an explicit output wins. The
    randomised phrasings are NOT reused for that reason.
    """
    lines = []
    if save_due:
        lines.append(
            f"[recall-mcp] {calls} tool calls in — KB '{slug}' active. This turn only, "
            'output exactly one line first: "[recall-mcp] Save check: <what is worth '
            'saving>" or "[recall-mcp] Save check: nothing this round." Then call '
            f"save_memory(slug='{slug}') if something qualifies."
        )
    if miss_due:
        lines.append(
            f"[recall-mcp] {calls} tool calls in — miss check for '{slug}'. Output one "
            'more line: "[recall-mcp] Miss check: <KB gap that let a mistake through>" '
            'or "[recall-mcp] Miss check: nothing this round." Then call '
            f"report_miss(slug='{slug}', description='...') if something qualifies."
        )
    return "\n".join(lines)


def _post_tool_use(payload: dict) -> dict:
    """Return the postToolUse hook output, or {} when no reminder is due.

    Every call increments the counter, but the branch lookup — the only part that
    shells out to git — runs only when a reminder is actually due. One turn can
    produce dozens of tool calls, so resolving the slug unconditionally would spawn
    a `git` process per call to obtain a value needed on roughly one call in forty.
    """
    cwd = Path(payload.get("cwd") or Path.cwd())

    project_dir = KB_ROOT / _project_name(cwd)
    if not (project_dir / "features.md").exists():
        return {}  # no KB store for this project — nothing to remind about

    # NOT Claude's `session-state`: prompt_submit.py counts its own turns there, and
    # two adapters counting into one file would each corrupt the other's cadence.
    state_file = project_dir / "copilot-session-state"
    session_id = _session_id(payload)

    append_turn(session_id, state_file)
    calls = count_turns(session_id, state_file)

    save_due = calls > 0 and calls % SAVE_REMINDER_EVERY_TOOL_CALLS == 0
    miss_due = calls > 0 and calls % MISS_REMINDER_EVERY_TOOL_CALLS == 0
    if not (save_due or miss_due):
        return {}

    slug = _slug_for(cwd)
    if not slug:
        return {}  # shared branch or unmapped branch — no KB is active

    return {
        "hookSpecificOutput": {
            "hookEventName": "PostToolUse",
            "additionalContext": _reminder_text(slug, calls, save_due, miss_due),
        }
    }


def main() -> None:
    """Read one hook payload from stdin and write one JSON object to stdout.

    The event name comes from `sys.argv[1]`, NOT from the payload. `hook_event_name`
    exists only in the PascalCase payload shape; the camelCase shape that the
    native Copilot CLI sends has no such field, so reading it from the payload
    would silently misroute every camelCase invocation to the same handler.

    Never raises. A hook that crashes must not take the user's session with it —
    an empty object is always a safe answer.
    """
    # The payload arrives as UTF-8 on stdin; a Windows pipe would hand it to us
    # as cp1252, where a non-ASCII prompt either mangles or raises.
    force_utf8_stdio()
    event = sys.argv[1] if len(sys.argv) > 1 else SUPPORTED_EVENTS[0]

    try:
        raw = sys.stdin.read()
    except Exception:
        raw = ""

    try:
        payload = json.loads(raw or "{}")
    except Exception:
        payload = {}
    if not isinstance(payload, dict):
        payload = {}

    try:
        if event == "sessionStart":
            result = _session_start(payload)
        elif event == "postToolUse":
            result = _post_tool_use(payload)
        else:
            result = {}
    except Exception:
        result = {}

    _emit(result)


if __name__ == "__main__":
    main()
