"""Integration tests for prompt_submit.py hook behavior.

Runs the hook as a subprocess with an isolated fake HOME so each test
gets a clean ~/.recall-mcp environment. PYTHONPATH is set to the repo
root so `from kb_recall.hooks.hook_helpers import ...` resolves correctly.
"""

import json
import os
import sys
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parent.parent
HOOK = REPO_ROOT / "kb_recall" / "hooks" / "prompt_submit.py"


def _git(args, cwd):
    subprocess.run(["git"] + args, cwd=cwd, capture_output=True, check=True)


def _make_env(tmp_path, branch=None):
    """Base env factory. If branch is given, initializes a git repo on that branch."""
    project_dir = tmp_path / "my-project"
    project_dir.mkdir()
    (project_dir / "CLAUDE.md").write_text("recall-mcp")

    if branch:
        _git(["init"], project_dir)
        _git(["config", "user.email", "t@t.com"], project_dir)
        _git(["config", "user.name", "T"], project_dir)
        _git(["checkout", "-b", branch], project_dir)

    kb_root = tmp_path / ".recall-mcp"
    kb_root.mkdir()
    (kb_root / "config.json").write_text(json.dumps({"projects": [str(project_dir)]}))

    return {
        "home": tmp_path,
        "project": project_dir,
        "kb_root": kb_root,
        "state_file": kb_root / "my-project" / "session-state",
    }


@pytest.fixture
def env(tmp_path):
    """Minimal env with no git repo (branch detection silently skipped)."""
    e = _make_env(tmp_path)
    proj_kb = e["kb_root"] / "my-project"
    proj_kb.mkdir()
    (proj_kb / "features.md").write_text(
        "# Feature Knowledge Base Index\n\n"
        "| Feature | Slug | Summary |\n"
        "|---|---|---|\n"
        "| Test Feature | test-feat | A test feature |\n"
    )
    return e


@pytest.fixture
def env_matched(tmp_path):
    """Env with git repo on a branch that matches a KB slug."""
    e = _make_env(tmp_path, branch="feat/test-feature")
    proj_kb = e["kb_root"] / "my-project"
    proj_kb.mkdir()
    (proj_kb / "features.md").write_text(
        "# Feature Knowledge Base Index\n\n"
        "| Feature | Slug | Ticket(s) | Branch(es) | Summary | Last Updated |\n"
        "|---|---|---|---|---|---|\n"
        "| Test Feature | test-feat | | feat/test-feature | A test feature | 2026-06-27 |\n"
    )
    return e


@pytest.fixture
def env_unmatched(tmp_path):
    """Env with git repo on a feature branch that has no KB."""
    e = _make_env(tmp_path, branch="feat/unknown-feature")
    proj_kb = e["kb_root"] / "my-project"
    proj_kb.mkdir()
    (proj_kb / "features.md").write_text(
        "# Feature Knowledge Base Index\n\n"
        "| Feature | Slug | Ticket(s) | Branch(es) | Summary | Last Updated |\n"
        "|---|---|---|---|---|---|\n"
        "| Test Feature | test-feat | | feat/test-feature | A test feature | 2026-06-27 |\n"
    )
    return e


@pytest.fixture
def env_malformed_row(tmp_path):
    """Env with git repo where features.md has a row missing a cell (corrupted)."""
    e = _make_env(tmp_path, branch="feat/test-feature")
    proj_kb = e["kb_root"] / "my-project"
    proj_kb.mkdir()
    (proj_kb / "features.md").write_text(
        "# Feature Knowledge Base Index\n\n"
        "| Feature | Slug | Ticket(s) | Branch(es) | Summary | Last Updated |\n"
        "|---|---|---|---|---|---|\n"
        # Missing the blank Ticket(s) cell — 5 cols instead of 6.
        "| Test Feature | test-feat | feat/test-feature | A test feature | 2026-06-27 |\n"
    )
    return e


def run_hook(env, session_id, cwd=None, transcript_path=None, prompt=""):
    payload = {"session_id": session_id, "prompt": prompt}
    if transcript_path is not None:
        payload["transcript_path"] = str(transcript_path)
    result = subprocess.run(
        [sys.executable, str(HOOK)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "HOME": str(env["home"]),
            "PYTHONPATH": str(REPO_ROOT),
        },
        cwd=str(cwd or env["project"]),
    )
    return result.stdout


@pytest.fixture
def env_missing_snippet(tmp_path):
    """Env where the project's CLAUDE.md has no recall-mcp section at all."""
    e = _make_env(tmp_path)
    (e["project"] / "CLAUDE.md").write_text("# Just a normal project\n")
    proj_kb = e["kb_root"] / "my-project"
    proj_kb.mkdir()
    (proj_kb / "features.md").write_text(
        "# Feature Knowledge Base Index\n\n"
        "| Feature | Slug | Summary |\n"
        "|---|---|---|\n"
        "| Test Feature | test-feat | A test feature |\n"
    )
    return e


@pytest.fixture
def env_unconfigured(tmp_path):
    """Env where a project IS registered, but cwd points elsewhere (unregistered)."""
    e = _make_env(tmp_path)
    proj_kb = e["kb_root"] / "my-project"
    proj_kb.mkdir()
    (proj_kb / "features.md").write_text(
        "# Feature Knowledge Base Index\n\n"
        "| Feature | Slug | Summary |\n"
        "|---|---|---|\n"
        "| Secret Feature | secret-feat | Should never leak to other dirs |\n"
    )
    other_dir = tmp_path / "unrelated-project"
    other_dir.mkdir()
    e["other"] = other_dir
    return e


# ---------------------------------------------------------------------------
# Feature index — inject once per session
# ---------------------------------------------------------------------------


class TestInjectOnce:
    def test_injects_index_on_first_prompt(self, env):
        out = run_hook(env, "s1")
        assert "Feature Knowledge Base Index" in out

    def test_does_not_inject_on_subsequent_prompts(self, env):
        run_hook(env, "s1")
        out = run_hook(env, "s1")
        assert "Feature Knowledge Base Index" not in out

    def test_injects_again_for_new_session(self, env):
        run_hook(env, "s1")
        out = run_hook(env, "s2")
        assert "Feature Knowledge Base Index" in out


# ---------------------------------------------------------------------------
# CLAUDE.local.md/CLAUDE.md missing recall-mcp section — warn once per session
# ---------------------------------------------------------------------------


class TestClaudeMdWarning:
    def test_warns_when_section_missing(self, env_missing_snippet):
        out = run_hook(env_missing_snippet, "s1")
        assert "has no recall-mcp section" in out

    def test_does_not_warn_when_section_present(self, env):
        out = run_hook(env, "s1")
        assert "has no recall-mcp section" not in out

    def test_does_not_repeat_on_subsequent_prompts_same_session(
        self, env_missing_snippet
    ):
        run_hook(env_missing_snippet, "s1")
        out = run_hook(env_missing_snippet, "s1")
        assert "has no recall-mcp section" not in out

    def test_warns_again_for_new_session(self, env_missing_snippet):
        run_hook(env_missing_snippet, "s1")
        out = run_hook(env_missing_snippet, "s2")
        assert "has no recall-mcp section" in out


# ---------------------------------------------------------------------------
# Turn counter — save reminder every SAVE_REMINDER_INTERVAL turns
# ---------------------------------------------------------------------------


class TestTurnCounter:
    def _seed_slug(self, env, session_id, slug):
        """Write a slug into session-state so active_slug is detected."""
        with env["state_file"].open("a") as f:
            f.write(json.dumps({"session_id": session_id, "slug": slug}) + "\n")

    def test_no_reminder_before_interval(self, env):
        self._seed_slug(env, "s1", "test-feat")
        for i in range(7):
            out = run_hook(env, "s1")
            assert "save_memory" not in out, f"Unexpected reminder at turn {i + 1}"

    def test_reminder_fires_at_8th_turn(self, env):
        self._seed_slug(env, "s1", "test-feat")
        for _ in range(7):
            run_hook(env, "s1")
        out = run_hook(env, "s1")  # 8th turn
        assert "save_memory" in out

    def test_no_reminder_between_intervals(self, env):
        self._seed_slug(env, "s1", "test-feat")
        for _ in range(8):
            run_hook(env, "s1")  # 8th turn fires reminder
        out = run_hook(env, "s1")  # 9th turn — should not fire
        assert "save_memory" not in out

    def test_reminder_repeats_at_next_interval(self, env):
        self._seed_slug(env, "s1", "test-feat")
        for _ in range(15):
            run_hook(env, "s1")
        out = run_hook(env, "s1")  # 16th turn
        assert "save_memory" in out


# ---------------------------------------------------------------------------
# active_slug is picked deterministically when 2+ KBs are active in a session
# ---------------------------------------------------------------------------


class TestActiveSlugDeterminism:
    def test_names_the_alphabetically_first_slug_when_multiple_active(self, env):
        with env["state_file"].open("a") as f:
            f.write(json.dumps({"session_id": "s1", "slug": "zzz-feat"}) + "\n")
            f.write(json.dumps({"session_id": "s1", "slug": "aaa-feat"}) + "\n")
        for _ in range(7):
            run_hook(env, "s1")
        out = run_hook(env, "s1")  # 8th turn — reminder names the active KB
        assert "KB 'aaa-feat' active" in out
        assert "zzz-feat" not in out


# ---------------------------------------------------------------------------
# Turn counter — miss reminder every MISS_REMINDER_INTERVAL turns
# ---------------------------------------------------------------------------


class TestMissTurnCounter:
    def _seed_slug(self, env, session_id, slug):
        with env["state_file"].open("a") as f:
            f.write(json.dumps({"session_id": session_id, "slug": slug}) + "\n")

    def test_no_miss_reminder_before_interval(self, env):
        self._seed_slug(env, "s1", "test-feat")
        for i in range(15):
            out = run_hook(env, "s1")
            assert "report_miss" not in out, f"Unexpected miss reminder at turn {i + 1}"

    def test_miss_reminder_fires_at_16th_turn(self, env):
        self._seed_slug(env, "s1", "test-feat")
        for _ in range(15):
            run_hook(env, "s1")
        out = run_hook(env, "s1")  # 16th turn
        assert "report_miss" in out

    def test_no_miss_reminder_between_intervals(self, env):
        self._seed_slug(env, "s1", "test-feat")
        for _ in range(16):
            run_hook(env, "s1")  # 16th turn fires reminder
        out = run_hook(env, "s1")  # 17th turn — should not fire
        assert "report_miss" not in out

    def test_miss_reminder_repeats_at_next_interval(self, env):
        self._seed_slug(env, "s1", "test-feat")
        for _ in range(31):
            run_hook(env, "s1")
        out = run_hook(env, "s1")  # 32nd turn
        assert "report_miss" in out

    def test_both_reminders_fire_together_at_16th_turn(self, env):
        """16 is divisible by both SAVE_REMINDER_INTERVAL (8) and MISS_REMINDER_INTERVAL (16)."""
        self._seed_slug(env, "s1", "test-feat")
        for _ in range(15):
            run_hook(env, "s1")
        out = run_hook(env, "s1")  # 16th turn
        assert "save_memory" in out
        assert "report_miss" in out


# ---------------------------------------------------------------------------
# Branch match → suggest loading KB
# ---------------------------------------------------------------------------


class TestBranchMatch:
    def test_suggests_load_on_first_prompt(self, env_matched):
        out = run_hook(env_matched, "s1")
        assert "matches feature KB 'test-feat'" in out
        assert "/recall:load" in out

    def test_suggests_load_only_once_per_session(self, env_matched):
        run_hook(env_matched, "s1")
        out = run_hook(env_matched, "s1")
        assert "matches feature KB" not in out

    def test_suggests_load_again_for_new_session(self, env_matched):
        run_hook(env_matched, "s1")
        out = run_hook(env_matched, "s2")
        assert "matches feature KB 'test-feat'" in out


# ---------------------------------------------------------------------------
# Branch with no KB → prompt to create or link
# ---------------------------------------------------------------------------


class TestBranchNoKB:
    def test_prompts_create_or_link(self, env_unmatched):
        out = run_hook(env_unmatched, "s1")
        assert "has no feature KB" in out
        assert "Create a new KB" in out
        assert "/recall:link-feature" in out

    def test_prompts_only_once_per_session(self, env_unmatched):
        run_hook(env_unmatched, "s1")
        out = run_hook(env_unmatched, "s1")
        assert "has no feature KB" not in out

    def test_prompts_again_for_new_session(self, env_unmatched):
        run_hook(env_unmatched, "s1")
        out = run_hook(env_unmatched, "s2")
        assert "has no feature KB" in out


# ---------------------------------------------------------------------------
# features.md has a row with a different column count than its header —
# surface it instead of letting branch/slug detection fail silently.
# ---------------------------------------------------------------------------


class TestMalformedRowWarning:
    def test_warns_when_row_has_wrong_column_count(self, env_malformed_row):
        out = run_hook(env_malformed_row, "s1")
        assert "different column count" in out

    def test_message_includes_header_and_actionable_fix_instruction(
        self, env_malformed_row
    ):
        out = run_hook(env_malformed_row, "s1")
        assert "Header:" in out
        assert "Bad row:" in out
        assert (
            "| Feature | Slug | Ticket(s) | Branch(es) | Summary | Last Updated |"
            in out
        )
        assert "Read " in out and "features.md" in out
        assert "fix that row" in out

    def test_does_not_warn_for_well_formed_table(self, env_matched):
        out = run_hook(env_matched, "s1")
        assert "different column count" not in out

    def test_does_not_repeat_same_session(self, env_malformed_row):
        run_hook(env_malformed_row, "s1")
        out = run_hook(env_malformed_row, "s1")
        assert "different column count" not in out

    def test_warns_again_for_new_session(self, env_malformed_row):
        run_hook(env_malformed_row, "s1")
        out = run_hook(env_malformed_row, "s2")
        assert "different column count" in out

    def test_does_not_offer_to_create_duplicate_kb_when_row_is_malformed(
        self, env_malformed_row
    ):
        # The branch's row exists but is malformed — must not suggest creating
        # a new KB (that would risk a real duplicate); must redirect to fixing
        # the row first instead.
        out = run_hook(env_malformed_row, "s1")
        assert "has no feature KB" not in out
        assert "ASK the user" not in out
        assert "Create a new KB" not in out
        assert "may be caused by the malformed row flagged above" in out
        assert "before offering to create a new KB" in out

    def test_normal_no_kb_flow_unaffected_when_table_is_well_formed(
        self, env_unmatched
    ):
        out = run_hook(env_unmatched, "s1")
        assert "has no feature KB" in out
        assert "Create a new KB" in out
        assert "may be caused by the malformed row" not in out


# ---------------------------------------------------------------------------
# Unconfigured directory — no cross-project leak, one-time hint per directory
# ---------------------------------------------------------------------------


class TestUnconfiguredProject:
    def test_does_not_leak_other_projects_index(self, env_unconfigured):
        out = run_hook(env_unconfigured, "s1", cwd=env_unconfigured["other"])
        assert "secret-feat" not in out
        assert "Secret Feature" not in out

    def test_shows_not_configured_hint(self, env_unconfigured):
        out = run_hook(env_unconfigured, "s1", cwd=env_unconfigured["other"])
        assert "is not configured" in out
        assert "recall setup" in out

    def test_hint_does_not_repeat_for_new_session(self, env_unconfigured):
        run_hook(env_unconfigured, "s1", cwd=env_unconfigured["other"])
        out = run_hook(env_unconfigured, "s2", cwd=env_unconfigured["other"])
        assert "is not configured" not in out

    def test_hint_fires_without_a_git_repo(self, env_unconfigured):
        # other_dir has no .git at all — hint must not depend on branch detection
        out = run_hook(env_unconfigured, "s1", cwd=env_unconfigured["other"])
        assert "is not configured" in out

    def test_independent_hint_per_directory(self, env_unconfigured):
        other2 = env_unconfigured["home"] / "another-unrelated"
        other2.mkdir()
        run_hook(env_unconfigured, "s1", cwd=env_unconfigured["other"])
        out = run_hook(env_unconfigured, "s2", cwd=other2)
        assert "is not configured" in out


# ---------------------------------------------------------------------------
# Session ID file — written by hook, keyed by PPID
# ---------------------------------------------------------------------------


class TestSessionIdFile:
    def test_writes_current_session_file(self, env):
        run_hook(env, "test-session-abc")
        session_file = env["kb_root"] / "current-session"
        assert session_file.exists(), "current-session file not created"
        assert session_file.read_text().strip() == "test-session-abc"

    def test_overwrites_on_new_session(self, env):
        run_hook(env, "session-1")
        run_hook(env, "session-2")
        session_file = env["kb_root"] / "current-session"
        assert session_file.read_text().strip() == "session-2"

    def test_no_file_written_without_session_id(self, env):
        subprocess.run(
            [sys.executable, str(HOOK)],
            input=json.dumps({}),  # no session_id key
            capture_output=True,
            text=True,
            env={"HOME": str(env["home"]), "PYTHONPATH": str(REPO_ROOT)},
            cwd=str(env["project"]),
        )
        session_file = env["kb_root"] / "current-session"
        assert not session_file.exists()


# ---------------------------------------------------------------------------
# session-state is scoped per project (KB_ROOT/<project>/session-state) —
# one project's activity must not evict another's dedup guards/turn count.
# ---------------------------------------------------------------------------


@pytest.fixture
def env_fresh_project(tmp_path):
    """Registered project with no KB_ROOT/<project> dir yet (never ran init_feature)."""
    project_dir = tmp_path / "brand-new-project"
    project_dir.mkdir()
    (project_dir / "CLAUDE.md").write_text("recall-mcp")

    kb_root = tmp_path / ".recall-mcp"
    kb_root.mkdir()
    (kb_root / "config.json").write_text(json.dumps({"projects": [str(project_dir)]}))
    # Deliberately no kb_root/"brand-new-project" dir — simulates `recall setup`
    # having run but init_feature never called for this project yet.

    return {"home": tmp_path, "project": project_dir, "kb_root": kb_root}


@pytest.fixture
def env_two_projects(tmp_path):
    """Two independently registered projects sharing one ~/.recall-mcp root."""
    kb_root = tmp_path / ".recall-mcp"
    kb_root.mkdir()

    projects = {}
    for name in ("project-a", "project-b"):
        proj_dir = tmp_path / name
        proj_dir.mkdir()
        (proj_dir / "CLAUDE.md").write_text("recall-mcp")
        proj_kb = kb_root / name
        proj_kb.mkdir()
        (proj_kb / "features.md").write_text(
            "# Feature Knowledge Base Index\n\n"
            "| Feature | Slug | Summary |\n"
            "|---|---|---|\n"
            f"| {name} feat | {name}-feat | desc |\n"
        )
        projects[name] = {"home": tmp_path, "project": proj_dir, "kb_root": kb_root}

    (kb_root / "config.json").write_text(
        json.dumps(
            {
                "projects": [
                    str(projects["project-a"]["project"]),
                    str(projects["project-b"]["project"]),
                ]
            }
        )
    )
    return projects


class TestPerProjectStateFile:
    def test_creates_project_kb_dir_if_missing(self, env_fresh_project):
        proj_kb_dir = env_fresh_project["kb_root"] / "brand-new-project"
        assert not proj_kb_dir.exists()
        run_hook(env_fresh_project, "s1")
        assert (proj_kb_dir / "session-state").exists()

    def test_two_projects_have_independent_state_files(self, env_two_projects):
        run_hook(env_two_projects["project-a"], "s1")
        run_hook(env_two_projects["project-b"], "s1")
        kb_root = env_two_projects["project-a"]["kb_root"]
        assert (kb_root / "project-a" / "session-state").exists()
        assert (kb_root / "project-b" / "session-state").exists()

    def test_heavy_activity_in_one_project_does_not_affect_another(
        self, env_two_projects
    ):
        state_a = (
            env_two_projects["project-a"]["kb_root"] / "project-a" / "session-state"
        )
        with state_a.open("a") as f:
            f.write(json.dumps({"session_id": "s1", "slug": "project-a-feat"}) + "\n")
        for _ in range(7):
            run_hook(env_two_projects["project-a"], "s1")
        out_a = run_hook(env_two_projects["project-a"], "s1")  # 8th turn
        assert "save_memory" in out_a  # project A's own reminder fired

        # project B's very first turn — must be unaffected by project A's history
        out_b = run_hook(env_two_projects["project-b"], "s1")
        assert "save_memory" not in out_b
        state_b = (
            env_two_projects["project-b"]["kb_root"] / "project-b" / "session-state"
        )
        b_lines = state_b.read_text().splitlines()
        assert len(b_lines) <= 2  # just this session's own __index__ + __turn__


# ---------------------------------------------------------------------------
# Idle-gap hard-block — gated by config.json's block_idle flag
# ---------------------------------------------------------------------------


def _write_expensive_cold_start_transcript(path, idle_minutes=56, tokens=150_000):
    """Fake transcript JSONL: a real user turn `idle_minutes` ago, followed by
    an assistant reply whose usage totals `tokens` — the combination the
    idle-gap guard treats as an expensive cold-start.
    """
    old_ts = (datetime.now(timezone.utc) - timedelta(minutes=idle_minutes)).isoformat()
    lines = [
        json.dumps(
            {
                "type": "user",
                "timestamp": old_ts,
                "message": {"content": "earlier message"},
            }
        ),
        json.dumps(
            {
                "type": "assistant",
                "timestamp": old_ts,
                "message": {
                    "usage": {
                        "input_tokens": tokens,
                        "cache_read_input_tokens": 0,
                        "cache_creation_input_tokens": 0,
                    }
                },
            }
        ),
    ]
    path.write_text("\n".join(lines) + "\n")


@pytest.fixture
def env_expensive_cold_start(tmp_path):
    """Env with a fake transcript showing idle > IDLE_BLOCK_SECONDS and
    context > CONTEXT_BLOCK_TOKENS — the block condition, absent a config
    override.
    """
    e = _make_env(tmp_path)
    proj_kb = e["kb_root"] / "my-project"
    proj_kb.mkdir()
    (proj_kb / "features.md").write_text(
        "# Feature Knowledge Base Index\n\n"
        "| Feature | Slug | Summary |\n"
        "|---|---|---|\n"
        "| Test Feature | test-feat | A test feature |\n"
    )
    transcript = tmp_path / "transcript.jsonl"
    _write_expensive_cold_start_transcript(transcript)
    e["transcript"] = transcript
    return e


def _set_block_idle(env, value):
    cfg_file = env["kb_root"] / "config.json"
    cfg = json.loads(cfg_file.read_text())
    cfg["block_idle"] = value
    cfg_file.write_text(json.dumps(cfg))


class TestBlockIdleToggle:
    def test_blocks_by_default(self, env_expensive_cold_start):
        out = run_hook(
            env_expensive_cold_start,
            "s1",
            transcript_path=env_expensive_cold_start["transcript"],
        )
        assert '"decision": "block"' in out

    def test_does_not_block_when_disabled(self, env_expensive_cold_start):
        _set_block_idle(env_expensive_cold_start, False)
        out = run_hook(
            env_expensive_cold_start,
            "s1",
            transcript_path=env_expensive_cold_start["transcript"],
        )
        assert '"decision": "block"' not in out

    def test_warn_still_fires_when_block_disabled(self, env_expensive_cold_start):
        _set_block_idle(env_expensive_cold_start, False)
        out = run_hook(
            env_expensive_cold_start,
            "s1",
            transcript_path=env_expensive_cold_start["transcript"],
        )
        assert "min since your last turn" in out

    def test_blocks_again_when_re_enabled(self, env_expensive_cold_start):
        _set_block_idle(env_expensive_cold_start, False)
        run_hook(
            env_expensive_cold_start,
            "s1",
            transcript_path=env_expensive_cold_start["transcript"],
        )
        _set_block_idle(env_expensive_cold_start, True)
        out = run_hook(
            env_expensive_cold_start,
            "s2",
            transcript_path=env_expensive_cold_start["transcript"],
        )
        assert '"decision": "block"' in out
