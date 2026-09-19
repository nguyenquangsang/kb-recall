#!/usr/bin/env python3
"""recall-mcp UserPromptSubmit hook.

Problem: Claude has no memory of which features exist or which one is active.
Without this hook, every session starts blind — the user has to manually load
context or Claude works without it.

This hook fires on every user prompt and injects context automatically:

  1. Feature index (first prompt of session only)
     Prints features.md so Claude knows what KBs exist and their slugs.
     Required for Claude to recognize feature references and call load_feature_context.

  2. Branch auto-load
     Matches the current git branch against features.md. If matched and not yet
     loaded this session, tells Claude to load the KB before answering.
     Solves the common case: user opens IDE, starts coding, forgets to load KB.

  3. Init prompt (no KB for branch)
     If on a feature branch with no KB yet, asks the user once whether to create one.

  4. Turn counter reminders (every SAVE_REMINDER_INTERVAL / MISS_REMINDER_INTERVAL turns)
     Reminds Claude to call save_memory (every 8 turns) and separately to call
     report_miss (every 16 turns — misses are much rarer, checking as often as saves
     would be mostly noise) if a KB is active. Both require an explicit output even
     when nothing qualifies (forcing function — proves the check actually ran, not
     silently skipped), rotated across a few phrasings so it isn't the same dry line
     every time. Zero token overhead — injected into the user's prompt, not a separate turn.

  5. Idle-gap / expensive-cold-start guard (registered projects only)
     Independent of #4 — #4 answers "have we done enough work this session to
     distill something?"; this answers "is a prompt-cache cold-start (1h TTL)
     imminent or already paid for?". Reads real turn timestamps + real
     cache/context usage stats from the transcript JSONL (`transcript_path`,
     via idle_gap_seconds/parse_transcript_idle in hook_helpers.py), falling
     back to a self-written `ts` on the __turn__ counter if the transcript is
     missing or its (undocumented) schema doesn't match. Past IDLE_WARN_SECONDS
     with no size data, or size below CONTEXT_BLOCK_TOKENS: non-blocking
     reminder only. Past IDLE_BLOCK_SECONDS AND context already over
     CONTEXT_BLOCK_TOKENS: hard-blocks the turn (JSON decision:"block" + reason
     on stdout, exit 0) so Claude is never invoked for that expensive cold-start
     — reason goes to the user via the structured `reason` field (more reliably
     surfaced across frontends than raw stderr text), asking them to /compact or
     /recall:save first, then resend.
     Scoped narrowly to avoid reintroducing the manual-step friction this
     mechanism exists to remove: only blocks when the gap AND the size are
     both already expensive, never on a merely-long idle gap alone. Escape
     hatch: a message prefixed with ESCAPE_HATCH_PREFIX skips the block once
     (the prefix stays in the message Claude sees — the hook doesn't strip it).
     The hard-block itself can be turned off entirely via `recall block-idle false`
     (writes `block_idle: false` to config.json, checked before the block
     condition) — the warn-only reminders below keep firing either way.
     A third, idle-independent check (SIZE_WARN_TOKENS) fires a non-blocking
     nudge once context alone crosses the threshold, then re-fires every
     SIZE_WARN_STEP_TOKENS of further growth — covers a continuously active
     session that never hits an idle gap, where cache-READ cost (paid every
     turn) is the bigger driver the idle/size block above can't see.

Exits silently when config doesn't exist: the hook is registered globally and runs
in every project; silence is correct for projects that don't use recall-mcp.

Session state: ~/.recall-mcp/<project>/session-state for registered projects
(JSONL, append-only, capped at 500 lines PER PROJECT — scoping avoids one
project's activity evicting another's dedup guards/turn count from the cap).
Falls back to the shared ~/.recall-mcp/session-state when no project is
detected (no project to scope by; also the home of the persistent
__unconfigured__ hint, which is deliberately cross-project/cross-session).
Each line: {"session_id": "<id>", "slug": "<slug>"}

Reserved slug keys (dedup guards):
  __badrow__{project} — "features.md has a malformed row" warning already
                        shown (per session)
  __claudemd__{dir}  — "CLAUDE.local.md missing recall-mcp section" warning
                        already shown (per session)
  __index__          — feature index already injected (per session)
  __new__{branch}    — "no KB for branch" prompt already shown (per session)
  __proj__{dir}      — "project not configured" hint already shown (once per
                        directory, persisted across sessions under the sentinel
                        session_id "__unconfigured__" — not a real Claude Code session)
  __turn__           — written every run, NOT deduped; raw count = turn number (per session)
"""

import json
import os
import random
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from kb_recall.hooks.hook_helpers import (
    append_turn,
    count_turns,
    find_malformed_rows,
    find_slug_for_branch,
    idle_gap_seconds,
    last_size_warn_tokens,
    load_asked,
    mark_asked,
    mark_size_warn,
    pick_active_slug,
)

SAVE_REMINDER_INTERVAL = 8  # remind every N turns when KB is active
MISS_REMINDER_INTERVAL = 16  # remind every N turns — separate cadence: misses are
# much rarer than save-worthy insights (5 report_miss calls vs 224 save_memory calls,
# all-time), so checking as often as Save check would be mostly noise.

IDLE_WARN_SECONDS = 3000  # 50 min — non-blocking heads-up; some false positives OK
IDLE_BLOCK_SECONDS = 3300  # 55 min — below the 1h TTL, so this can fire slightly
# before the cache has actually expired; accepted trade-off, chosen deliberately
# over the theoretically-safer ~62-65 min (see KB context-cost-optimization).
CONTEXT_BLOCK_TOKENS = 100_000  # ...don't block cheap cold-starts, only expensive ones

SIZE_WARN_TOKENS = 250_000  # non-blocking, idle-independent: catches the case where
# a session stays continuously active (no idle gap ever crosses IDLE_WARN_SECONDS)
# but the thread has grown large enough that cache-READ cost alone (paid on every
# turn, not just cold-starts) is now the bigger driver — see KB
# context-cost-optimization: cache read was 57% of one user's monthly cost vs 25%
# for cache write, yet the idle-gap guard above only ever targets the write side.
# Set above that case study's own ~230k avg tokens/request, so it doesn't fire on
# what's already normal for a heavy user — only once a thread grows past it.
SIZE_WARN_STEP_TOKENS = 50_000  # re-fires every additional 50k of growth past the
# last size-nudge, instead of only once per session — a single one-time nudge
# would go silent right when a ballooning thread needs it most.

ESCAPE_HATCH_PREFIX = "!!force"  # prefix a message with this to skip the block once

# Rotated randomly so the "nothing to save/report" case doesn't feel like the same
# dry line every N turns — still an exact-match requirement (forcing function
# that proves the check actually ran), just less repetitive to read.
NOTHING_TO_SAVE_LINES = [
    "[recall-mcp] Save check: nothing this round.",
    "[recall-mcp] Save check: quiet round — KB stays as-is.",
    "[recall-mcp] Save check: nothing juicy enough to bottle up.",
    "[recall-mcp] Save check: nada, moving on.",
    "[recall-mcp] Save check: clean run, nothing to stash away.",
]
NOTHING_TO_REPORT_LINES = [
    "[recall-mcp] Miss check: nothing this round.",
    "[recall-mcp] Miss check: no misses spotted — KB held up fine.",
    "[recall-mcp] Miss check: clean streak, nothing to report.",
    "[recall-mcp] Miss check: KB did its job this round.",
]

KB_ROOT = Path.home() / ".recall-mcp"
CFG = KB_ROOT / "config.json"


def _extract_section(text: str, section: str) -> str:
    start = text.find(f"<{section}>")
    end = text.find(f"</{section}>")
    if start == -1 or end == -1:
        return ""
    return text[start + len(f"<{section}>") : end]


def _kb_stats(kb_root: Path, project_name: str, slug: str) -> str:
    """Return a compact protection summary: '15 decisions · 14 warnings · 4 past mistakes'."""
    try:
        readme = kb_root / project_name / slug / "README.md"
        if not readme.exists():
            return ""
        text = readme.read_text()

        arch = _extract_section(text, "architecture")
        decisions = len(re.findall(r"^\*\*\[", arch, re.MULTILINE))

        warn_sec = _extract_section(text, "critical_warnings")
        warnings = len(re.findall(r"^\*\*\[", warn_sec, re.MULTILINE))

        misses = 0
        for mem_file in (kb_root / project_name / slug).glob("memories*.md"):
            misses += len(
                re.findall(r"^- \*\*.*\[MISS\]", mem_file.read_text(), re.MULTILINE)
            )

        parts = []
        if decisions:
            parts.append(f"{decisions} decisions (won't re-litigate)")
        if warnings:
            parts.append(f"{warnings} warnings (known traps)")
        if misses:
            parts.append(f"{misses} past mistakes on record")

        return " · ".join(parts) if parts else ""
    except Exception:
        return ""


def main() -> None:
    if not CFG.exists():
        exit()

    # Read session_id from stdin (Claude Code passes JSON on UserPromptSubmit)
    payload = {}
    session_id = ""
    try:
        payload = json.loads(sys.stdin.read())
        session_id = payload.get("session_id", "")
    except Exception:
        pass

    prompt = payload.get("prompt", "")
    transcript_path = payload.get("transcript_path", "")

    if session_id:
        try:
            (KB_ROOT / "current-session").write_text(session_id)
        except Exception:
            pass

    try:
        data = json.loads(CFG.read_text())
    except Exception as e:
        print(f"[recall-mcp] ⚠ config.json malformed ({e}) — fix {CFG}")
        exit()
    projects = [Path(p).resolve() for p in data.get("projects", [])]
    block_idle = data.get("block_idle", True)

    # Detect current project from cwd
    cwd = Path(os.getcwd()).resolve()
    current = next(
        (p for p in projects if cwd == p or p in cwd.parents),
        None,
    )

    # Scoped per project so one project's activity can't evict another's dedup
    # guards/turn count from the shared 500-line cap. No project to scope by in
    # the unconfigured-directory case, so that one stays on the global file.
    if current:
        project_kb_dir = KB_ROOT / current.name
        project_kb_dir.mkdir(parents=True, exist_ok=True)
        state_file = project_kb_dir / "session-state"
    else:
        state_file = KB_ROOT / "session-state"

    asked = load_asked(session_id, state_file)

    # --- Idle-gap / expensive-cold-start guard ---
    # Runs first, before any other stdout: a block (exit code 2) discards this turn
    # entirely — Claude is never invoked — so nothing else this hook would print
    # matters once that decision is made.
    if current:
        idle_seconds, context_tokens, idle_source = idle_gap_seconds(
            session_id, state_file, transcript_path
        )
        if idle_seconds is not None:
            idle_minutes = int(idle_seconds // 60)
            forced = prompt.strip().lower().startswith(ESCAPE_HATCH_PREFIX)
            if (
                block_idle
                and idle_seconds > IDLE_BLOCK_SECONDS
                and context_tokens is not None
                and context_tokens > CONTEXT_BLOCK_TOKENS
                and not forced
            ):
                # Blocking discards this turn — echo the user's own message back in
                # the block reason so they can copy it straight from there instead
                # of retyping/re-pasting it. Uses decision:block + reason (JSON on
                # stdout, exit 0) instead of stderr + exit(2): decision/reason is a
                # structured field the protocol guarantees gets surfaced to the user,
                # whereas raw stderr text is only guaranteed to reach a debug log —
                # some frontends (e.g. the VSCode extension) don't render it in-chat.
                print(
                    json.dumps(
                        {
                            "decision": "block",
                            "reason": (
                                f"[recall-mcp] Blocked: {idle_minutes} min idle and "
                                f"~{context_tokens:,} context tokens already in play "
                                f"(source: {idle_source}) — this turn would trigger an "
                                "expensive prompt-cache cold-start (cache write, ~2x "
                                "input price) on top of an already-large context.\n"
                                "Pick one:\n"
                                "  1. Run /compact, then resend your message.\n"
                                "  2. Start a new session, run /recall:load, then resend "
                                "your message.\n"
                                f"  3. Prefix your message with '{ESCAPE_HATCH_PREFIX}' "
                                "and resend to proceed anyway (skips this check once).\n"
                                f"--- your message ---\n{prompt}"
                            ),
                        }
                    )
                )
                sys.exit(0)
            elif idle_seconds > IDLE_WARN_SECONDS:
                print(
                    f"[recall-mcp] {idle_minutes} min since your last turn "
                    f"(source: {idle_source}) — the prompt cache (1h TTL) may have "
                    "already expired, so this turn likely paid full cache-write price. "
                    "If there's anything worth keeping from this session, save it now; "
                    "consider /compact if the thread has a lot of resolved content."
                )
            elif context_tokens is not None and context_tokens > SIZE_WARN_TOKENS:
                # Idle-independent: fires even in a continuously active session with
                # no idle gap, because cache-READ cost (paid every turn) scales with
                # context size regardless of idle time — the block/warn above only
                # ever catches the cache-WRITE side. Re-fires every SIZE_WARN_STEP_TOKENS
                # of further growth (not just once per session) — a one-time nudge
                # would go silent right when a ballooning thread needs it most.
                last_warned = last_size_warn_tokens(session_id, state_file)
                if (
                    last_warned is None
                    or context_tokens >= last_warned + SIZE_WARN_STEP_TOKENS
                ):
                    mark_size_warn(session_id, state_file, context_tokens)
                    print(
                        f"[recall-mcp] ~{context_tokens:,} context tokens in this "
                        "thread (no idle gap involved — this fires from size alone) — "
                        "every turn from here on re-reads that much context from "
                        "cache, so cache-read cost keeps climbing regardless of idle "
                        "time. Consider /compact if the thread has a lot of resolved "
                        "content."
                    )

    # Check CLAUDE.local.md setup (falls back to legacy CLAUDE.md for pre-existing installs)
    # Deduped per session (like __index__) — otherwise this reprints the full snippet
    # on every single turn for a project that hasn't added it yet.
    CLAUDE_MD_KEY = f"__claudemd__{cwd}"
    if current and CLAUDE_MD_KEY not in asked:
        local_md = current / "CLAUDE.local.md"
        legacy_md = current / "CLAUDE.md"
        has_section = (
            "recall-mcp" in local_md.read_text() if local_md.exists() else False
        ) or ("recall-mcp" in legacy_md.read_text() if legacy_md.exists() else False)
        if not has_section:
            mark_asked(session_id, CLAUDE_MD_KEY, state_file)
            snippet_path = (
                Path(__file__).parent.parent / "templates" / "claude-md-snippet.md"
            )
            snippet = snippet_path.read_text().strip()
            print(f"[recall-mcp] ⚠  {local_md} has no recall-mcp section.")
            print(
                f"Add this to {local_md} manually (gitignored — per-developer, not team-shared):\n"
            )
            print(snippet)
            print()

    # --- Inject feature index (first message of session only, registered projects only) ---

    INDEX_KEY = "__index__"
    if INDEX_KEY not in asked and current:
        mark_asked(session_id, INDEX_KEY, state_file)
        index = KB_ROOT / current.name / "features.md"
        if index.exists():
            print(f"[recall-mcp: {current.name}]")
            print(index.read_text().strip())
            print()

    # --- Unconfigured-project hint (once per directory, persists across sessions) ---

    UNCONFIGURED_SID = (
        "__unconfigured__"  # sentinel — not a real Claude Code session_id
    )

    if not current:
        unconfigured_seen = load_asked(UNCONFIGURED_SID, state_file)
        proj_key = f"__proj__{cwd}"
        if proj_key not in unconfigured_seen:
            mark_asked(UNCONFIGURED_SID, proj_key, state_file)
            print(f"[recall-mcp] Project '{cwd.name}' is not configured.")
            print(
                "ASK the user: \"This project isn't set up for recall-mcp yet. "
                'Run `recall setup` now?"'
            )
            print(
                "If yes: run `recall setup` via Bash, then report the output and tell the user to reload Claude Code to activate it."
            )
            print("If no: do nothing.")

    # --- Branch-based KB prompt (registered projects only) ---

    matched = None
    if current:
        try:
            branch = subprocess.run(
                ["git", "branch", "--show-current"],
                capture_output=True,
                text=True,
                timeout=3,
                cwd=str(cwd),
            ).stdout.strip()

            if branch and branch not in ("main", "master", "develop", "dev"):
                # Look up branch in features.md index
                index_file = KB_ROOT / current.name / "features.md"
                malformed = []
                if index_file.exists():
                    table_text = index_file.read_text()
                    matched = find_slug_for_branch(table_text, branch)
                    malformed = find_malformed_rows(table_text)

                    # Surface table corruption instead of letting it fail silently
                    # (e.g. a miscounted freehand edit from /recall:link-feature).
                    bad_row_key = f"__badrow__{current.name}"
                    if malformed and bad_row_key not in asked:
                        mark_asked(session_id, bad_row_key, state_file)
                        header_line = next(
                            (
                                line.strip()
                                for line in table_text.splitlines()
                                if line.strip().startswith("|")
                            ),
                            "",
                        )
                        print(
                            f"[recall-mcp] ⚠ {index_file} has a row with a different "
                            "column count than its header — branch/slug detection for "
                            f"that row may silently fail.\n"
                            f"Header:  {header_line}\n"
                            f"Bad row: {malformed[0]}"
                        )
                        print(
                            f"Read {index_file}, fix that row so its `|`-delimited cell "
                            "count matches the header shown above (check for a "
                            "dropped/extra cell, e.g. a blank Ticket(s) cell rendered as "
                            "`| |`), then tell the user what was corrected."
                        )

                if matched:
                    if matched not in asked:
                        mark_asked(session_id, matched, state_file)
                        stats = _kb_stats(KB_ROOT, current.name, matched)
                        print(
                            f"[recall-mcp] Branch '{branch}' matches feature KB '{matched}'."
                        )
                        print(
                            f'BEFORE answering the user, run /recall:load with argument "{matched}" — do NOT ask the user first.'
                        )
                        print(
                            f'After loading, notify the user: "[recall-mcp] Loaded KB `{matched}` for branch `{branch}`" followed by the token count from the response header. If maintenance hints appear in the header, include them.'
                        )
                        if stats:
                            print(
                                f'Then output exactly one line: "[recall-mcp] KB active: {stats}"'
                            )
                        print("Then answer their request.")
                        print(
                            "If the command fails, skip silently and answer normally."
                        )
                else:
                    branch_key = branch.replace("/", "-").replace("_", "-")
                    new_key = f"__new__{branch_key}"
                    if new_key not in asked:
                        mark_asked(session_id, new_key, state_file)
                        if malformed:
                            # Don't offer to create a duplicate KB — the mismatch
                            # may just be the malformed row flagged above, not a
                            # genuinely missing KB. Let Claude rule that out first.
                            print(
                                f"[recall-mcp] Branch '{branch}' didn't match any KB in "
                                "features.md — this may be caused by the malformed row "
                                "flagged above, not a genuinely missing KB."
                            )
                            print(
                                "Fix that row first, then re-check whether the branch now "
                                "matches, before offering to create a new KB or asking the "
                                "user anything."
                            )
                        else:
                            slug_hint = branch.split("/")[-1].replace("_", "-").lower()
                            name_hint = slug_hint.replace("-", " ").title()
                            print(f"[recall-mcp] Branch '{branch}' has no feature KB.")
                            print(
                                f"ASK the user: \"Branch '{branch}' has no feature KB yet. What would you like to do?\""
                            )
                            print("1. Create a new KB for this branch")
                            print("2. Link to an existing KB (branch was renamed)")
                            print(
                                f'If 1: run /recall:init with name="{name_hint}", slug="{slug_hint}", project="{current.name}" as arguments.'
                            )
                            print("If 2: run /recall:link-feature")
                            print("If neither: do nothing.")
        except Exception:
            pass

    # --- Turn counter save reminder ---

    append_turn(session_id, state_file)

    active_slug = matched or pick_active_slug(asked)

    if active_slug and current:
        turn_count = count_turns(session_id, state_file)
        if turn_count > 0 and turn_count % SAVE_REMINDER_INTERVAL == 0:
            nothing_line = random.choice(NOTHING_TO_SAVE_LINES)
            print(f"[recall-mcp] {turn_count} turns in — KB '{active_slug}' active.")
            print(
                "This turn only: output exactly one line first — "
                'either "[recall-mcp] Save check: <1-line of what\'s worth saving>" '
                f'or exactly "{nothing_line}" (no additional text).'
            )
            print(
                f"Then: if something qualifies, call save_memory(slug='{active_slug}')."
            )
            print("Then answer the user's question normally.")

        if turn_count > 0 and turn_count % MISS_REMINDER_INTERVAL == 0:
            nothing_report_line = random.choice(NOTHING_TO_REPORT_LINES)
            print(
                f"[recall-mcp] {turn_count} turns in — miss check for '{active_slug}'."
            )
            print(
                "This turn only: output exactly one more line — "
                'either "[recall-mcp] Miss check: <what KB gap let a mistake happen>" '
                f'or exactly "{nothing_report_line}" (no additional text).'
            )
            print(
                f"Then: if something qualifies, call report_miss(slug='{active_slug}', "
                "description='<what went wrong + what the KB should have said>')."
            )
            print("Then answer the user's question normally.")

    # Log slash command usage (user-typed only — after active_slug/current resolved)
    if prompt.strip().startswith("/recall:"):
        try:
            cmd = prompt.strip().split()[0].lstrip("/")  # e.g. "recall:save"
            ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            entry: dict = {"ts": ts, "command": cmd}
            if session_id:
                entry["session_id"] = session_id
            if active_slug:
                entry["slug"] = active_slug
            if current:
                entry["project"] = current.name
            with (KB_ROOT / "usage.jsonl").open("a") as f:
                f.write(json.dumps(entry) + "\n")
        except Exception:
            pass


if __name__ == "__main__":
    main()
