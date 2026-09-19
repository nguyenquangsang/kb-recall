"""Unit tests for kb_recall/cli.py's block-idle command."""

import json
import subprocess
import sys

import pytest

from kb_recall import cli


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
        cfg = json.loads((kb_root / "config.json").read_text())
        assert cfg["block_idle"] is True

    def test_sets_false(self, kb_root):
        assert cli.cmd_block_idle(["false"]) == 0
        cfg = json.loads((kb_root / "config.json").read_text())
        assert cfg["block_idle"] is False

    def test_preserves_existing_config(self, kb_root):
        (kb_root / "config.json").write_text(json.dumps({"projects": ["/a/b"]}))
        cli.cmd_block_idle(["false"])
        cfg = json.loads((kb_root / "config.json").read_text())
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
        link.symlink_to(real)
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
        monkeypatch.setenv("HOME", str(tmp_path))
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
        (script_dir / "templates" / "claude-md-snippet.md").write_text("recall-mcp\n")

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
        cfg = json.loads((cli.KB_ROOT / "config.json").read_text())
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
        cfg = json.loads((cli.KB_ROOT / "config.json").read_text())
        assert str(project) in cfg["projects"]

    def test_reregisters_instead_of_leaving_a_stale_entry(
        self, project, monkeypatch
    ):
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
            "/bin/claude", "mcp", "remove", "--scope", "user", "recall",
        ]
        assert calls[1] == [
            "/bin/claude", "mcp", "add", "--scope", "user", "recall", "--",
            "/bin/recall-server",
        ]


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
