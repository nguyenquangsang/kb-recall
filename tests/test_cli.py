"""Unit tests for kb_recall/cli.py's block-idle command."""

import json
import re
import subprocess
import sys

import pytest

from kb_recall import cli
from kb_recall.adapters.copilot.hook import POST_TOOL_USE_MATCHER
from tests.conftest import fake_home


@pytest.fixture
def kb_root(tmp_path, monkeypatch):
    root = tmp_path / ".recall-mcp"
    root.mkdir()
    monkeypatch.setattr(cli, "KB_ROOT", root)
    return root


class TestCmdBlockIdle:
    def test_rejects_missing_arg(self, kb_root, capsys):
        assert cli.cmd_block_idle([]) == 1
        assert "Usage: recall block-idle <true|false>" in capsys.readouterr().err

    def test_rejects_invalid_arg(self, kb_root, capsys):
        assert cli.cmd_block_idle(["on"]) == 1
        assert "Usage: recall block-idle <true|false>" in capsys.readouterr().err

    def test_sets_true(self, kb_root):
        assert cli.cmd_block_idle(["true"]) == 0
        cfg = json.loads((kb_root / "config.json").read_text(encoding="utf-8"))
        assert cfg["block_idle"] is True

    def test_sets_false(self, kb_root):
        assert cli.cmd_block_idle(["false"]) == 0
        cfg = json.loads((kb_root / "config.json").read_text(encoding="utf-8"))
        assert cfg["block_idle"] is False

    def test_preserves_existing_config(self, kb_root):
        (kb_root / "config.json").write_text(
            json.dumps({"projects": ["/a/b"]}), encoding="utf-8"
        )
        cli.cmd_block_idle(["false"])
        cfg = json.loads((kb_root / "config.json").read_text(encoding="utf-8"))
        assert cfg["projects"] == ["/a/b"]
        assert cfg["block_idle"] is False


class TestRecallCommand:
    """The hook command must survive a shell that never sourced ~/.zshrc."""

    def test_uses_absolute_path_when_recall_resolves(self, monkeypatch, tmp_path):
        exe = tmp_path / "bin" / "recall"
        exe.parent.mkdir()
        exe.touch()
        monkeypatch.setattr(cli.shutil, "which", lambda _: str(exe))
        assert cli._recall_command() == f"{exe.absolute()} prompt"

    def test_keeps_the_symlink_rather_than_the_venv_it_points_at(
        self, monkeypatch, tmp_path
    ):
        """uv tool install exposes ~/.local/bin/recall as a symlink into its venv.

        The symlink is the stable user-facing path — resolving it would bake a
        venv-internal path that moves whenever the tool is reinstalled.
        """
        real = tmp_path / "uv" / "tools" / "kb-recall" / "bin" / "recall"
        real.parent.mkdir(parents=True)
        real.touch()
        link = tmp_path / "bin" / "recall"
        link.parent.mkdir()
        try:
            link.symlink_to(real)
        except OSError as exc:
            # Skipped rather than marked win32-only: symlink creation needs
            # SeCreateSymbolicLinkPrivilege on Windows, which some runners have
            # and some do not, so the OS refuses or permits it independently of
            # the platform name. Skipping on the actual refusal keeps the test
            # running wherever it can.
            pytest.skip(f"symlink creation not permitted here: {exc}")
        monkeypatch.setattr(cli.shutil, "which", lambda _: str(link))
        assert cli._recall_command() == f"{link} prompt"

    def test_falls_back_to_bare_name(self, monkeypatch):
        monkeypatch.setattr(cli.shutil, "which", lambda _: None)
        assert cli._recall_command() == "recall prompt"


class TestIsRecallPrompt:
    @pytest.mark.parametrize(
        "command",
        ["recall prompt", "/home/me/.local/bin/recall prompt"],
    )
    def test_matches_recall_prompt_spellings(self, command):
        assert cli._is_recall_prompt(command) is True

    @pytest.mark.parametrize(
        "command",
        [
            "recall setup",
            "recall",
            "recall prompt --verbose",
            "python3 /x/hooks/prompt_submit.py",
            "myrecall prompt",
        ],
    )
    def test_rejects_everything_else(self, command):
        assert cli._is_recall_prompt(command) is False


def _make_extension_binary(home, version):
    """Create a fake VSCode-extension `claude` binary under a version dir."""
    exe = (
        home
        / ".vscode"
        / "extensions"
        / f"anthropic.claude-code-{version}-darwin-arm64"
        / "resources"
        / "native-binary"
        / "claude"
    )
    exe.parent.mkdir(parents=True)
    exe.touch()
    exe.chmod(0o755)
    return exe


class TestClaudeCli:
    """Extension installs ship `claude` inside the VSCode bundle, off PATH."""

    @pytest.fixture(autouse=True)
    def no_path_claude(self, monkeypatch, tmp_path):
        monkeypatch.setattr(cli.shutil, "which", lambda _: None)
        for key, value in fake_home(tmp_path).items():
            monkeypatch.setenv(key, value)
        return tmp_path

    def test_prefers_path_install(self, monkeypatch):
        monkeypatch.setattr(cli.shutil, "which", lambda _: "/usr/local/bin/claude")
        assert cli._claude_cli() == "/usr/local/bin/claude"

    def test_returns_none_when_nothing_found(self):
        assert cli._claude_cli() is None

    def test_finds_bundled_extension_binary(self, no_path_claude):
        exe = _make_extension_binary(no_path_claude, "2.1.272")
        assert cli._claude_cli() == str(exe)

    def test_picks_newest_version_numerically_not_lexically(self, no_path_claude):
        """A string compare puts 2.1.9 above 2.1.272 — the wrong way round."""
        _make_extension_binary(no_path_claude, "2.1.9")
        newest = _make_extension_binary(no_path_claude, "2.1.272")
        assert cli._claude_cli() == str(newest)

    def test_local_install_beats_bundled_copy(self, no_path_claude):
        local = no_path_claude / ".claude" / "local" / "claude"
        local.parent.mkdir(parents=True)
        local.touch()
        local.chmod(0o755)
        _make_extension_binary(no_path_claude, "2.1.272")
        assert cli._claude_cli() == str(local)

    @pytest.mark.skipif(
        sys.platform == "win32",
        reason="Windows has no executable bit: os.access(X_OK) is True for any "
        "existing file and chmod(0o644) only toggles the read-only flag, so a "
        "candidate cannot be made non-executable the way this test needs.",
    )
    def test_ignores_non_executable_candidate(self, no_path_claude):
        exe = _make_extension_binary(no_path_claude, "2.1.272")
        exe.chmod(0o644)
        assert cli._claude_cli() is None


class TestCmdSetup:
    """A missing `claude` CLI must not abort setup — steps 2-5 still have work to do."""

    @pytest.fixture
    def project(self, tmp_path, monkeypatch):
        script_dir = tmp_path / "script"
        (script_dir / "templates").mkdir(parents=True)
        (script_dir / "templates" / "claude-md-snippet.md").write_text(
            "recall-mcp\n", encoding="utf-8"
        )

        project_dir = tmp_path / "project"
        project_dir.mkdir()
        monkeypatch.chdir(project_dir)

        monkeypatch.setattr(cli, "SCRIPT_DIR", script_dir)
        monkeypatch.setattr(cli, "KB_ROOT", tmp_path / "kb")
        monkeypatch.setattr(
            cli, "CLAUDE_SETTINGS", tmp_path / "claude" / "settings.json"
        )
        monkeypatch.setattr(cli, "cmd_sync_commands", lambda *_: 0)
        monkeypatch.setattr("builtins.input", lambda *_: "other")
        return project_dir

    def test_survives_missing_claude_cli(self, project, monkeypatch, capsys):
        monkeypatch.setattr(cli, "_claude_cli", lambda: None)
        assert cli.cmd_setup([]) == 0
        assert "Could not find the `claude` CLI" in capsys.readouterr().out
        cfg = json.loads((cli.KB_ROOT / "config.json").read_text(encoding="utf-8"))
        assert str(project) in cfg["projects"]
        assert (project / "CLAUDE.local.md").exists()

    def test_survives_oserror_raised_by_subprocess(self, project, monkeypatch, capsys):
        """subprocess.run raises FileNotFoundError before returncode exists."""

        def boom(*_args, **_kwargs):
            raise FileNotFoundError(2, "No such file or directory", "claude")

        monkeypatch.setattr(cli, "_claude_cli", lambda: "/nope/claude")
        monkeypatch.setattr(cli.subprocess, "run", boom)
        assert cli.cmd_setup([]) == 0
        out = capsys.readouterr().out
        assert "Could not auto-register" in out
        assert "/nope/claude mcp add --scope user recall" in out
        cfg = json.loads((cli.KB_ROOT / "config.json").read_text(encoding="utf-8"))
        assert str(project) in cfg["projects"]

    def test_reregisters_instead_of_leaving_a_stale_entry(self, project, monkeypatch):
        """`mcp add` refuses to overwrite, and an entry that exists may be stale.

        Registration is now a console script, but an older setup wrote a script
        path — the user only sees "failed to connect" from `claude mcp list`.
        """
        calls = []

        def record(argv, **_kwargs):
            calls.append(argv)
            return subprocess.CompletedProcess(argv, 0, "", "")

        monkeypatch.setattr(cli, "_claude_cli", lambda: "/bin/claude")
        monkeypatch.setattr(cli, "_server_command", lambda: ["/bin/recall-server"])
        monkeypatch.setattr(cli.subprocess, "run", record)

        assert cli.cmd_setup([]) == 0
        assert calls[0] == [
            "/bin/claude",
            "mcp",
            "remove",
            "--scope",
            "user",
            "recall",
        ]
        assert calls[1] == [
            "/bin/claude",
            "mcp",
            "add",
            "--scope",
            "user",
            "recall",
            "--",
            "/bin/recall-server",
        ]


class TestParsePlatform:
    def test_defaults_to_claude(self):
        """`setup` gained a flag; it did not change meaning. Every existing
        invocation — and every doc that says `recall setup` — keeps the behaviour
        it had before, so an existing user is never surprised by new files."""
        assert cli._parse_platform([]) == "claude"

    @pytest.mark.parametrize("platform", cli.PLATFORMS)
    def test_accepts_every_platform(self, platform):
        assert cli._parse_platform(["--platform", platform]) == platform

    def test_accepts_the_equals_form(self):
        assert cli._parse_platform(["--platform=copilot"]) == "copilot"

    @pytest.mark.parametrize(
        "args",
        [
            ["--platform"],  # flag with no value — reject, do not guess a platform
            ["--platform", "claude-code"],
            ["--platform="],
            ["--platform", "CLAUDE"],  # case-sensitive deliberately
        ],
    )
    def test_rejects_missing_or_unknown_values(self, args):
        """None, not a fallback to "claude": a silent fallback would run a
        Claude-only setup for someone who asked for Copilot, and report success."""
        assert cli._parse_platform(args) is None

    @pytest.mark.parametrize(
        "args",
        [
            ["--platfrom", "copilot"],  # transposed flag name
            ["-p", "copilot"],
            ["--platform", "copilot", "--verbose"],  # valid flag, stray extra
            ["copilot"],
        ],
    )
    def test_rejects_unrecognized_arguments(self, args):
        """An argument that is not `--platform` is rejected, not skipped.

        Skipping it made a mistyped flag behave as if it had not been passed at
        all, so `--platfrom copilot` ran the default Claude setup — the same
        wrong-platform failure the bad-value case above already guards against.
        """
        assert cli._parse_platform(args) is None


class TestCmdSetupCopilot:
    """`--platform copilot` is the one path that must never write into ~/.claude/.

    Claude and Copilot are configured independently on purpose: one platform's
    setup silently rewriting the other's live hook config is the failure this
    separation exists to prevent, so it is asserted rather than assumed.
    """

    @pytest.fixture
    def project(self, tmp_path, monkeypatch):
        script_dir = tmp_path / "script"
        (script_dir / "templates").mkdir(parents=True)
        (script_dir / "templates" / "claude-md-snippet.md").write_text(
            "recall-mcp\n", encoding="utf-8"
        )
        (script_dir / "templates" / "copilot-instructions.md").write_text(
            "recall-mcp for copilot\n",
            encoding="utf-8",
        )
        (script_dir / "skills" / "recall-load").mkdir(parents=True)
        (script_dir / "skills" / "recall-load" / "SKILL.md").write_text(
            "---\nname: recall-load\ndescription: load a KB\n---\n\n## When to use\n"
            "Never wrong.\n",
            encoding="utf-8",
        )

        project_dir = tmp_path / "project"
        project_dir.mkdir()
        monkeypatch.chdir(project_dir)

        monkeypatch.setattr(cli, "SCRIPT_DIR", script_dir)
        monkeypatch.setattr(cli, "KB_ROOT", tmp_path / "kb")
        monkeypatch.setattr(
            cli, "CLAUDE_SETTINGS", tmp_path / "claude" / "settings.json"
        )
        monkeypatch.setattr(cli, "COPILOT_SKILLS_DIR", tmp_path / "copilot" / "skills")
        monkeypatch.setattr(cli, "cmd_sync_commands", lambda *_: 0)
        monkeypatch.setattr("builtins.input", lambda *_: "other")
        # Under tmp_path rather than a literal "/fake/bin": _server_command()
        # bakes in str(Path(exe).absolute()), and on Windows a POSIX-style root
        # resolves against the current drive ("/fake/bin/x" -> "C:\fake\bin\x"),
        # so a hardcoded POSIX literal asserts something the code never returns
        # there. tmp_path is absolute on every platform, so it round-trips.
        fake_bin = tmp_path / "fake-bin"
        monkeypatch.setattr(cli.shutil, "which", lambda name: str(fake_bin / name))
        return project_dir

    def test_writes_the_mcp_config(self, project, tmp_path):
        assert cli.cmd_setup(["--platform", "copilot"]) == 0

        mcp_config = json.loads((project / ".mcp.json").read_text(encoding="utf-8"))
        server = mcp_config["mcpServers"]["recall"]
        assert server["type"] == "stdio"
        assert server["command"] == str(tmp_path / "fake-bin" / "recall-server")

    def test_writes_both_hook_events_with_the_event_on_argv(self, project):
        """Both events in one file: the adapter dispatches on argv[1], and the
        payload's casing says nothing about which event fired, so the event name
        has to be baked into the command string."""
        cli.cmd_setup(["--platform", "copilot"])

        hooks = json.loads(
            (project / ".github" / "hooks" / "recall.json").read_text(encoding="utf-8")
        )["hooks"]
        assert set(hooks) == {"sessionStart", "postToolUse"}
        assert hooks["sessionStart"][0]["command"].endswith(" sessionStart")
        assert hooks["postToolUse"][0]["command"].endswith(" postToolUse")

    def test_matcher_matches_the_adapter_it_configures(self, project):
        """The generator reads the matcher from the adapter rather than holding a
        second copy: an invalid regex makes the harness skip the whole hook entry
        SILENTLY, so a drifted copy would fail with no signal at all."""
        cli.cmd_setup(["--platform", "copilot"])

        entry = json.loads(
            (project / ".github" / "hooks" / "recall.json").read_text(encoding="utf-8")
        )["hooks"]["postToolUse"][0]
        assert entry["matcher"] == POST_TOOL_USE_MATCHER
        re.compile(f"^(?:{entry['matcher']})$")

    def test_never_touches_the_claude_tree(self, project, monkeypatch, capsys):
        monkeypatch.setattr(cli, "cmd_sync_commands", lambda *_: pytest.fail("called"))
        assert cli.cmd_setup(["--platform", "copilot"]) == 0

        assert not cli.CLAUDE_SETTINGS.exists()
        assert not (project / "CLAUDE.local.md").exists()
        assert "reload Claude Code" not in capsys.readouterr().out

    def test_never_looks_for_the_claude_cli(self, project, monkeypatch):
        def boom():
            raise AssertionError("the Copilot path must not probe for `claude`")

        monkeypatch.setattr(cli, "_claude_cli", boom)
        assert cli.cmd_setup(["--platform", "copilot"]) == 0

    def test_writes_the_instructions_file(self, project):
        cli.cmd_setup(["--platform", "copilot"])
        assert "recall-mcp for copilot" in (
            project / ".github" / "copilot-instructions.md"
        ).read_text(encoding="utf-8")

    def test_writes_the_skills_to_user_scope(self, project):
        cli.cmd_setup(["--platform", "copilot"])
        skill = cli.COPILOT_SKILLS_DIR / "recall-load" / "SKILL.md"
        assert skill.is_symlink() or skill.exists()
        assert "name: recall-load" in skill.read_text(encoding="utf-8")

    def test_skills_are_user_scoped_not_project_scoped(self, project):
        """Skills serve every project, so they install once under ~/.copilot/
        (like Claude commands under ~/.claude/), not into the project."""
        cli.cmd_setup(["--platform", "copilot"])
        assert not (project / ".github" / "skills").exists()
        assert (cli.COPILOT_SKILLS_DIR / "recall-load").exists()

    def test_gitignores_only_the_machine_specific_files(self, project):
        """The hook command bakes in an absolute path to this machine's console
        script, so the config files are per-machine. The instructions file is not:
        it carries no machine-specific path and is the only channel Copilot reads
        from the repo, so it stays committable."""
        cli.cmd_setup(["--platform", "copilot"])

        ignored = (project / ".gitignore").read_text(encoding="utf-8").splitlines()
        assert ".mcp.json" in ignored
        assert ".github/hooks/recall.json" in ignored
        assert not any("copilot-instructions" in entry for entry in ignored)

    def test_rerunning_does_not_duplicate_gitignore_entries(self, project):
        cli.cmd_setup(["--platform", "copilot"])
        cli.cmd_setup(["--platform", "copilot"])

        ignored = (project / ".gitignore").read_text(encoding="utf-8").splitlines()
        assert ignored.count(".mcp.json") == 1

    def test_writes_instructions_even_when_claude_md_already_carries_recall(
        self, project
    ):
        """The Claude snippet omits the Copilot-only corrections (no index
        injection, list_features needed), so Copilot still gets its own file even
        when CLAUDE.local.md already has recall-mcp — `--platform all` must not
        leave Copilot holding only the Claude-flavoured guidance."""
        (project / "CLAUDE.local.md").write_text("recall-mcp\n", encoding="utf-8")

        cli.cmd_setup(["--platform", "copilot"])

        assert (project / ".github" / "copilot-instructions.md").exists()
        assert "recall-mcp for copilot" in (
            project / ".github" / "copilot-instructions.md"
        ).read_text(encoding="utf-8")

    def test_mcp_config_preserves_other_servers(self, project):
        """`.mcp.json` is the shared workspace-root MCP config — a recall setup
        must merge into it, not overwrite it, or every other server there is lost."""
        (project / ".mcp.json").write_text(
            json.dumps(
                {
                    "mcpServers": {
                        "other-team-server": {"type": "stdio", "command": "/bin/other"}
                    }
                }
            ),
            encoding="utf-8",
        )

        cli.cmd_setup(["--platform", "copilot"])

        servers = json.loads((project / ".mcp.json").read_text(encoding="utf-8"))[
            "mcpServers"
        ]
        assert servers["other-team-server"] == {
            "type": "stdio",
            "command": "/bin/other",
        }
        assert "recall" in servers

    def test_instructions_appends_instead_of_clobbering(self, project):
        """A .github/copilot-instructions.md without the recall-mcp marker is the
        team's own content — setup must append to it, never replace it."""
        (project / ".github").mkdir(parents=True, exist_ok=True)
        (project / ".github" / "copilot-instructions.md").write_text(
            "# Team rules\nAlways run make lint.\n",
            encoding="utf-8",
        )

        cli.cmd_setup(["--platform", "copilot"])

        text = (project / ".github" / "copilot-instructions.md").read_text(
            encoding="utf-8"
        )
        assert "Always run make lint." in text
        assert "recall-mcp for copilot" in text

    def test_hook_config_preserves_other_events(self, project):
        """A hook file may carry events for other tools — merge recall's events in,
        preserving the rest."""
        (project / ".github" / "hooks").mkdir(parents=True, exist_ok=True)
        (project / ".github" / "hooks" / "recall.json").write_text(
            json.dumps(
                {
                    "version": 1,
                    "hooks": {
                        "preToolUse": [{"type": "command", "command": "/bin/other"}]
                    },
                }
            ),
            encoding="utf-8",
        )

        cli.cmd_setup(["--platform", "copilot"])

        hooks = json.loads(
            (project / ".github" / "hooks" / "recall.json").read_text(encoding="utf-8")
        )["hooks"]
        assert hooks["preToolUse"] == [{"type": "command", "command": "/bin/other"}]
        assert "sessionStart" in hooks

    def test_malformed_mcp_json_is_left_untouched(self, project, capsys):
        """A .mcp.json we can't parse must not be clobbered — refuse rather than
        destroy whatever is in it."""
        (project / ".mcp.json").write_text("{ not valid json", encoding="utf-8")

        cli.cmd_setup(["--platform", "copilot"])

        assert (project / ".mcp.json").read_text(encoding="utf-8") == "{ not valid json"
        assert "isn't valid JSON" in capsys.readouterr().out

    def test_claude_platform_writes_no_copilot_files(self, project, monkeypatch):
        monkeypatch.setattr(cli, "_claude_cli", lambda: None)
        assert cli.cmd_setup(["--platform", "claude"]) == 0

        assert not (project / ".mcp.json").exists()
        assert not (project / ".github").exists()

    def test_all_writes_both(self, project, monkeypatch):
        monkeypatch.setattr(cli, "_claude_cli", lambda: None)
        assert cli.cmd_setup(["--platform", "all"]) == 0

        assert (project / "CLAUDE.local.md").exists()
        assert (project / ".mcp.json").exists()
        assert (project / ".github" / "hooks" / "recall.json").exists()
        # Copilot still gets its own instructions file: the Claude snippet omits
        # the Copilot-only corrections.
        assert (project / ".github" / "copilot-instructions.md").exists()

    def test_invalid_platform_exits_nonzero_and_writes_nothing(self, project, capsys):
        assert cli.cmd_setup(["--platform", "wsl"]) == 1

        assert "--platform claude|copilot|all" in capsys.readouterr().err
        assert not (project / ".mcp.json").exists()
        assert not (cli.KB_ROOT / "config.json").exists()

    def test_mistyped_flag_writes_nothing(self, project, capsys):
        """`--platfrom` must abort, not fall through to the default platform.

        Before unknown arguments were rejected, this ran a full Claude setup —
        registering the MCP server and writing Claude files — while the user
        believed they had asked for Copilot.
        """
        assert cli.cmd_setup(["--platfrom", "copilot"]) == 1

        assert "--platform claude|copilot|all" in capsys.readouterr().err
        assert not (project / ".mcp.json").exists()
        assert not (project / ".github").exists()
        assert not (project / "CLAUDE.local.md").exists()


class TestHelpFlag:
    """`--help` must print usage instead of running the command.

    Only `args[0]` used to be checked, so `recall setup --help` reached
    `cmd_setup`, which ignored the flag it did not recognize — registering the
    MCP server and installing hooks for someone who asked for help.
    """

    @pytest.mark.parametrize(
        "argv",
        [
            ["recall", "--help"],
            ["recall", "-h"],
            ["recall", "setup", "--help"],
            ["recall", "block-idle", "--help"],
        ],
    )
    def test_prints_usage_without_dispatching(self, argv, monkeypatch, capsys):
        monkeypatch.setattr(sys, "argv", argv)

        def _explode(*_args, **_kwargs):
            raise AssertionError(f"dispatched for {argv}")

        monkeypatch.setattr(cli, "cmd_setup", _explode)
        monkeypatch.setattr(cli, "cmd_block_idle", _explode)

        with pytest.raises(SystemExit) as exc:
            cli.main()

        assert exc.value.code == 0
        assert "Usage: recall <command>" in capsys.readouterr().out


class TestServerCommand:
    """The MCP server must be launchable without pointing at a file path."""

    def test_prefers_the_console_script(self, monkeypatch, tmp_path):
        exe = tmp_path / "bin" / "recall-server"
        exe.parent.mkdir()
        exe.touch()
        monkeypatch.setattr(cli.shutil, "which", lambda _: str(exe))
        assert cli._server_command() == [str(exe.absolute())]

    def test_falls_back_to_the_running_interpreter(self, monkeypatch):
        """Nothing on PATH must not degrade to a directory-based uv invocation.

        `uv run --project <SCRIPT_DIR>` resolved to site-packages once the wheel
        shipped the repo root, which built a .venv inside site-packages.
        """
        monkeypatch.setattr(cli.shutil, "which", lambda _: None)
        assert cli._server_command() == [sys.executable, "-m", "kb_recall.server"]
