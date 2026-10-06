"""Tests for the GitHub Copilot adapter hook.

Two layers, because they fail differently:

  - Subprocess tests against a fake HOME (same approach as test_prompt_submit.py)
    cover the stdout contract, which is the part Copilot is unforgiving about: it
    concatenates all of stdout and runs ONE json.loads over it, so a second JSON
    object invalidates the whole output silently. Only a real process proves the
    hook writes one object and nothing else.
  - The budget-cut contract is exercised through a KB fixture with a single
    oversized section: the sessionStart body must stay under Copilot's budget and
    drop whole sections rather than hard-truncating one mid-entry.
"""

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from kb_recall.adapters.copilot import hook as copilot_hook

REPO_ROOT = Path(__file__).parent.parent
HOOK_MODULE = "kb_recall.adapters.copilot.hook"

BRANCH = "feat/demo"
SLUG = "demo"
PROJECT_NAME = "my-project"
SID = "sess-1"


def _git(args, cwd):
    subprocess.run(["git", *args], cwd=cwd, capture_output=True, check=True)


def _write_features_md(kb_root: Path) -> None:
    proj_kb = kb_root / PROJECT_NAME
    proj_kb.mkdir(parents=True, exist_ok=True)
    (proj_kb / "features.md").write_text(
        "# Feature Knowledge Base Index\n\n"
        "| Feature | Slug | Ticket(s) | Branch(es) | Summary | Last Updated |\n"
        "|---|---|---|---|---|---|\n"
        f"| Demo | {SLUG} |  | {BRANCH} | A demo feature | 2026-01-01 |\n"
    )


def _write_kb(
    kb_root: Path,
    readme_body: str = "Some feature context.\n",
    readme: str | None = None,
) -> None:
    feature = kb_root / PROJECT_NAME / SLUG
    feature.mkdir(parents=True, exist_ok=True)
    (feature / "README.md").write_text(
        readme
        if readme is not None
        else (
            "# Demo — Knowledge Base\n\n"
            "<overview>\nA demo feature.\n</overview>\n\n"
            f"<architecture>\n{readme_body}</architecture>\n"
        )
    )


def _make_env(tmp_path: Path, branch: str = BRANCH, with_kb: bool = True) -> dict:
    """Build a fake HOME: a git repo + the matching ~/.recall-mcp layout."""
    project = tmp_path / PROJECT_NAME
    project.mkdir()

    _git(["init"], project)
    _git(["config", "user.email", "t@t.com"], project)
    _git(["config", "user.name", "T"], project)
    # `git init` may already leave us on the requested branch name.
    _git(["checkout", "-b", branch], project)

    kb_root = tmp_path / ".recall-mcp"
    kb_root.mkdir()
    (kb_root / "config.json").write_text(json.dumps({"projects": [str(project)]}))
    _write_features_md(kb_root)
    if with_kb:
        _write_kb(kb_root)

    return {"home": tmp_path, "project": project, "kb_root": kb_root}


def run_hook(env, event="sessionStart", payload=None, raw=None, cwd=None):
    """Invoke the hook as a subprocess and return the CompletedProcess."""
    stdin = (
        raw if raw is not None else json.dumps(payload or {"cwd": str(env["project"])})
    )
    return subprocess.run(
        [sys.executable, "-m", HOOK_MODULE, event],
        input=stdin,
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "HOME": str(env["home"]),
            "PYTHONPATH": str(REPO_ROOT),
        },
        cwd=str(cwd or env["project"]),
        check=False,  # assertions read returncode/status; a raise would mask them
    )


def parse_single_object(stdout: str) -> dict:
    """Parse stdout as exactly one JSON value — the contract Copilot relies on."""
    return json.loads(stdout)


def copilot_state_path(env) -> Path:
    """The Copilot adapter's own counter file — pinned here as a contract.

    Deliberately NOT `<project>/session-state`, which is Claude's: two adapters
    counting into one file would each corrupt the other's cadence.
    """
    return env["kb_root"] / PROJECT_NAME / "copilot-session-state"


def seed_tool_calls(env, sid: str, n: int) -> None:
    """Pre-fill the counter so a test reaches call N in a single subprocess.

    Spawning the hook N times would test the same arithmetic for ~40x the wall
    clock. The counter's on-disk shape is Claude's `__turn__` entry — the helper
    functions are reused verbatim, only the unit they count differs.
    """
    with copilot_state_path(env).open("a") as f:
        for _ in range(n):
            f.write(json.dumps({"session_id": sid, "slug": "__turn__"}) + "\n")


def post_tool_payload(env, sid: str | None = SID, key: str = "session_id") -> dict:
    payload = {"cwd": str(env["project"])}
    if sid is not None:
        payload[key] = sid
    return payload


@pytest.fixture
def env(tmp_path):
    return _make_env(tmp_path)


class TestSessionStart:
    def test_injects_the_branchs_kb(self, env):
        result = run_hook(env)

        assert result.returncode == 0, result.stderr
        output = parse_single_object(result.stdout)

        assert output["hookSpecificOutput"]["hookEventName"] == "SessionStart"
        context = output["hookSpecificOutput"]["additionalContext"]
        # Byte-for-byte the same renderer Claude Code uses — same header, same body.
        assert context.startswith(f"# Feature context: {PROJECT_NAME}/{SLUG}")
        assert "A demo feature." in context

    def test_stdout_carries_exactly_one_json_object(self, env):
        """A second object makes Copilot's single json.loads fail — and it fails
        silently, so nothing else would catch it."""
        result = run_hook(env)

        output = parse_single_object(result.stdout)  # raises on trailing data
        assert list(output.keys()) == ["hookSpecificOutput"]

    def test_event_is_routed_by_argv_not_by_the_payload(self, env):
        """`hook_event_name` only exists in the PascalCase payload; the camelCase
        payload the native Copilot CLI sends has no such field. Routing on it would
        silently misroute those calls, so argv must win."""
        # argv says userPromptTransformed (an event this adapter does not implement)
        # → no injection, even though the payload claims SessionStart.
        unsupported = run_hook(
            env,
            event="userPromptTransformed",
            payload={"hook_event_name": "SessionStart"},
        )
        assert parse_single_object(unsupported.stdout) == {}

        # argv says sessionStart → injects, even though the payload claims otherwise.
        supported = run_hook(
            env, event="sessionStart", payload={"hook_event_name": "PostToolUse"}
        )
        assert "hookSpecificOutput" in parse_single_object(supported.stdout)

    def test_unmapped_branch_injects_a_one_line_link_hint(self, tmp_path):
        """The unmapped-branch gap must not be silent on Copilot: Claude's
        UserPromptSubmit hook prints "ASK the user" with the two options, and
        this hint is the Copilot carrier of the same decision point."""
        env = _make_env(tmp_path, branch="chore/unmapped")
        result = run_hook(env)

        assert result.returncode == 0
        output = parse_single_object(result.stdout)
        context = output["hookSpecificOutput"]["additionalContext"]
        assert "Branch 'chore/unmapped' has no feature KB" in context
        assert "/recall-link-feature" in context
        # The no-KB-mapping decision is the user's — and the wording must close
        # the "loading is neutral" loophole that lets a model call
        # load_feature_context for a candidate slug before the user chooses
        # (observed in a real Copilot session, 2026-09-26: branch hint said
        # "offer the choice", CLAUDE.md said "don't ask first" for related
        # features, the model picked the latter and loaded the candidate).
        assert "do not decide for them" in context
        assert "loading presumes the mapping" in context
        # The hint is one short paragraph — it shares the 10 KB cap with the
        # full KB body, so it must stay a rounding error next to it.
        assert len(context) < 400

    def test_unmapped_branch_hint_is_asked_once_per_session(self, tmp_path):
        """Resume/replay must not re-deliver the hint for the same (session,
        branch) — load_asked/mark_asked gate it, same convention as Claude's
        `__new__<branch>` key."""
        env = _make_env(tmp_path, branch="chore/unmapped")
        payload = {"cwd": str(env["project"]), "session_id": SID}

        first = run_hook(env, payload=payload)
        assert (
            "has no feature KB"
            in parse_single_object(first.stdout)["hookSpecificOutput"][
                "additionalContext"
            ]
        )

        second = run_hook(env, payload=payload)
        assert parse_single_object(second.stdout) == {}

        # A different session on the same branch gets the hint again —
        # ask-once is per (session, branch), not per branch forever.
        third = run_hook(env, payload={**payload, "session_id": "sess-2"})
        assert (
            "has no feature KB"
            in parse_single_object(third.stdout)["hookSpecificOutput"][
                "additionalContext"
            ]
        )

    def test_unmapped_branch_hint_without_session_id_still_fires(self, tmp_path):
        """No session_id in the payload → the ask-once gate is skipped (not
        collapsed onto key "" — load_asked("") matches entries written with
        session_id "", which would wedge the hint off permanently). The hint
        fires on every id-less invocation instead: better a repeat than a
        silent gap. VS Code payloads always carry session_id, so this is the
        defensive path."""
        env = _make_env(tmp_path, branch="chore/unmapped")
        # No session_id key at all — only cwd.
        first = run_hook(env, payload={"cwd": str(env["project"])})
        second = run_hook(env, payload={"cwd": str(env["project"])})

        for result in (first, second):
            assert result.returncode == 0, result.stderr
            context = parse_single_object(result.stdout)["hookSpecificOutput"][
                "additionalContext"
            ]
            assert "has no feature KB" in context

    @pytest.mark.parametrize("branch", ["main", "master", "develop", "dev"])
    def test_shared_branches_are_never_auto_loaded(self, tmp_path, branch):
        env = _make_env(tmp_path, branch=branch)
        # Map the shared branch to a KB to prove the skip, not just a lookup miss.
        _write_features_md(env["kb_root"])
        (env["kb_root"] / PROJECT_NAME / "features.md").write_text(
            "# Feature Knowledge Base Index\n\n"
            "| Feature | Slug | Ticket(s) | Branch(es) | Summary | Last Updated |\n"
            "|---|---|---|---|---|---|\n"
            f"| Demo | {SLUG} |  | {branch} | A demo feature | 2026-01-01 |\n"
        )
        result = run_hook(env)

        assert parse_single_object(result.stdout) == {}

    def test_project_with_no_kb_store_injects_nothing(self, tmp_path):
        env = _make_env(tmp_path)
        # Drop ~/.recall-mcp/<project> entirely — this project name has no KB
        # store, so the hook must skip the server import and inject nothing.
        shutil.rmtree(env["kb_root"] / PROJECT_NAME)
        result = run_hook(env)

        assert result.returncode == 0
        assert parse_single_object(result.stdout) == {}


class TestPostToolUse:
    """Per-turn reminders.

    The unit under test is a TOOL CALL, not a turn: postToolUse has no turn id, so
    the counter counts calls and the intervals are scaled to match. Seeding the
    counter directly (rather than invoking the hook N times) keeps these tests
    fast without weakening what they assert — the arithmetic is the same.

    `prior_calls` is how many calls the session already made, so the call under
    test is number `prior_calls + 1`: the hook appends its own entry before it
    counts, which is what makes the counter include the current call.
    """

    @pytest.mark.parametrize(
        "prior_calls, expect_save, expect_miss",
        [
            (0, False, False),
            (copilot_hook.SAVE_REMINDER_EVERY_TOOL_CALLS - 2, False, False),
            (copilot_hook.SAVE_REMINDER_EVERY_TOOL_CALLS - 1, True, False),
            (copilot_hook.MISS_REMINDER_EVERY_TOOL_CALLS - 2, False, False),
            (copilot_hook.MISS_REMINDER_EVERY_TOOL_CALLS - 1, True, True),
        ],
    )
    def test_cadence(self, tmp_path, prior_calls, expect_save, expect_miss):
        env = _make_env(tmp_path)
        seed_tool_calls(env, SID, prior_calls)

        output = parse_single_object(
            run_hook(env, event="postToolUse", payload=post_tool_payload(env)).stdout
        )

        if not (expect_save or expect_miss):
            assert output == {}
            return

        assert output["hookSpecificOutput"]["hookEventName"] == "PostToolUse"
        context = output["hookSpecificOutput"]["additionalContext"]
        assert SLUG in context
        # Distinct cadences: a miss check must not ride along on a save reminder.
        assert ("Save check" in context) is expect_save
        assert ("Miss check" in context) is expect_miss

    def test_counter_lives_in_its_own_file(self, env):
        """Claude keeps its turn counter in `<project>/session-state`. Sharing that
        file would corrupt both cadences, so this asserts the separation rather
        than trusting it."""
        seed_tool_calls(env, SID, copilot_hook.SAVE_REMINDER_EVERY_TOOL_CALLS - 1)

        run_hook(env, event="postToolUse", payload=post_tool_payload(env))

        assert copilot_state_path(env).exists()
        assert not (env["kb_root"] / PROJECT_NAME / "session-state").exists()

    def test_camelcase_session_id_is_counted(self, tmp_path):
        """The native Copilot CLI sends `sessionId`, VS Code sends `session_id`.
        Reading only the snake_case key would silently pool every session onto one
        counter, so the cadence would never reset."""
        env = _make_env(tmp_path)
        seed_tool_calls(env, SID, copilot_hook.SAVE_REMINDER_EVERY_TOOL_CALLS - 1)

        output = parse_single_object(
            run_hook(
                env,
                event="postToolUse",
                payload=post_tool_payload(env, key="sessionId"),
            ).stdout
        )

        assert "hookSpecificOutput" in output

    @pytest.mark.parametrize("branch", ["main", "master", "develop", "dev"])
    def test_shared_branches_never_remind(self, tmp_path, branch):
        env = _make_env(tmp_path, branch=branch)
        # Map the shared branch to a KB, so this proves the skip and not a lookup miss.
        (env["kb_root"] / PROJECT_NAME / "features.md").write_text(
            "# Feature Knowledge Base Index\n\n"
            "| Feature | Slug | Ticket(s) | Branch(es) | Summary | Last Updated |\n"
            "|---|---|---|---|---|---|\n"
            f"| Demo | {SLUG} |  | {branch} | A demo feature | 2026-01-01 |\n"
        )
        seed_tool_calls(env, SID, copilot_hook.MISS_REMINDER_EVERY_TOOL_CALLS - 1)

        output = parse_single_object(
            run_hook(env, event="postToolUse", payload=post_tool_payload(env)).stdout
        )

        assert output == {}

    def test_project_without_a_kb_store_reminds_nothing_and_writes_nothing(
        self, tmp_path
    ):
        env = _make_env(tmp_path)
        shutil.rmtree(env["kb_root"] / PROJECT_NAME)

        result = run_hook(env, event="postToolUse", payload=post_tool_payload(env))

        assert result.returncode == 0, result.stderr
        assert parse_single_object(result.stdout) == {}
        # The early return must come BEFORE any state write, not after.
        assert not (env["kb_root"] / PROJECT_NAME).exists()

    def test_reminder_stays_far_below_the_shared_cap(self, tmp_path):
        """`additionalContext` is merged across all hooks and capped at 10 KB
        together, so this text shares budget with the KB body rather than owning
        it — the reason Claude's four-line phrasing is not reused here."""
        env = _make_env(tmp_path)
        seed_tool_calls(env, SID, copilot_hook.MISS_REMINDER_EVERY_TOOL_CALLS - 1)

        context = parse_single_object(
            run_hook(env, event="postToolUse", payload=post_tool_payload(env)).stdout
        )["hookSpecificOutput"]["additionalContext"]

        assert len(context) < copilot_hook.COPILOT_CONTEXT_CAP_CHARS // 4

    @pytest.mark.parametrize("raw", ["not json at all", "", "{", "[]"])
    def test_malformed_stdin_never_crashes(self, env, raw):
        result = run_hook(env, event="postToolUse", raw=raw)

        assert result.returncode == 0, result.stderr
        assert isinstance(parse_single_object(result.stdout), dict)


def test_matcher_is_a_valid_anchored_regex():
    """An invalid matcher makes the harness skip the whole hook entry SILENTLY.
    The regex the config generator writes is compiled and exercised here, because
    a config file a tool wrote is exactly the kind nobody hand-checks."""
    pattern = re.compile(f"^(?:{copilot_hook.POST_TOOL_USE_MATCHER})$")

    for name in copilot_hook.POST_TOOL_USE_MATCHER.split("|"):
        assert pattern.match(name), name
    assert not pattern.match("read")
    assert not pattern.match("bashful")  # anchoring must reject prefixes/suffixes


class TestStdoutContract:
    @pytest.mark.parametrize(
        "raw",
        [
            "not json at all",
            "",
            "[]",  # valid JSON, wrong type
            '"a string"',
            "\x00\x01\x02 random bytes",
            "{",
        ],
    )
    def test_malformed_stdin_never_crashes(self, env, raw):
        """A crashing hook must not take the user's session with it.

        Deliberately does NOT assert an empty object: with an unreadable payload
        the hook falls back to the process cwd, which here IS the git repo, so
        injecting the KB is the correct answer. The contract under test is that
        stdout stays a single parseable object and the exit code stays 0.
        """
        result = run_hook(env, raw=raw)

        assert result.returncode == 0, result.stderr
        assert isinstance(parse_single_object(result.stdout), dict)

    def test_missing_cwd_falls_back_to_process_cwd(self, env):
        result = run_hook(env, payload={})

        # cwd is the git repo, so the branch still resolves.
        assert "hookSpecificOutput" in parse_single_object(result.stdout)


class TestContextCap:
    def test_large_kb_is_budget_cut_with_an_omission_notice(self, tmp_path):
        """load_feature_context's own ceiling is 40k chars — 4x Copilot's 10k budget,
        so without the max_chars budget the harness would cut the KB mid-entry,
        silently. The body is sized above the budget but below the 40k ceiling, so
        the response is a real KB rather than the "too large" message; the oversized
        section is dropped whole and an omission notice names it.

        The bulk goes in the LOWEST-priority section (`overview`): the renderer stops
        at the first unit that does not fit, so sizing a higher-priority section over
        the budget would (correctly) drop `overview` too and leave no body text to
        check the directive's position against.
        """
        env = _make_env(tmp_path)
        _write_kb(
            env["kb_root"],
            readme=(
                "# Demo — Knowledge Base\n\n"
                "<critical_warnings>\nA demo feature.\n</critical_warnings>\n\n"
                f"<overview>\n{'word ' * 5_000}\n</overview>\n"
            ),
        )
        result = run_hook(env)

        assert result.returncode == 0, result.stderr
        context = parse_single_object(result.stdout)["hookSpecificOutput"][
            "additionalContext"
        ]

        assert len(context) <= copilot_hook.SESSION_START_BUDGET_CHARS
        assert context.startswith(f"# Feature context: {PROJECT_NAME}/{SLUG}")
        assert "KB cut to fit the injection budget" in context
        assert "overview" in context  # the directive names the dropped section
        assert "word " not in context  # dropped whole, not cut mid-entry
        # The directive leads, ABOVE the body: the model must learn the KB is
        # partial before reading it, not from a note at the point it stops reading.
        assert context.index("⚠️") < context.index("A demo feature.")

    def test_small_kb_is_not_budget_cut(self, env):
        context = parse_single_object(run_hook(env).stdout)["hookSpecificOutput"][
            "additionalContext"
        ]

        assert "⚠️" not in context
