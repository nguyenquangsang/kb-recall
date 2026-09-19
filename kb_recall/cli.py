#!/usr/bin/env python3
"""recall CLI — setup and management commands."""

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

KB_ROOT = Path.home() / ".recall-mcp"
CLAUDE_SETTINGS = Path.home() / ".claude" / "settings.json"
SCRIPT_DIR = Path(__file__).resolve().parent


def _read_config() -> dict:
    cfg = KB_ROOT / "config.json"
    if not cfg.exists():
        return {}
    return json.loads(cfg.read_text())


def _write_config(data: dict) -> None:
    KB_ROOT.mkdir(parents=True, exist_ok=True)
    (KB_ROOT / "config.json").write_text(json.dumps(data, indent=2) + "\n")


def _recall_command() -> str:
    """The command Claude Code should run for the UserPromptSubmit hook.

    Prefers the absolute path to the `recall` console script over the bare name.
    Claude Code spawns hook commands through a shell that does not source the
    user's interactive rc (`.zshrc`), so a bare `recall` — which resolves only
    because `.zshrc` puts `~/.local/bin` on PATH — fails silently there: no
    stderr, no hint, the hook simply injects nothing. Falls back to the bare
    name only when the executable cannot be located at all.

    Deliberately does not resolve symlinks: `uv tool install` exposes the script
    as `~/.local/bin/recall` -> the tool venv's own copy, and the stable
    user-facing symlink is the better thing to bake into settings.json.
    """
    exe = shutil.which("recall")
    return f"{Path(exe).absolute()} prompt" if exe else "recall prompt"


def _server_command() -> list[str]:
    """argv that starts the MCP server, for `claude mcp add ... -- <argv>`.

    The console script is preferred for the same reason as the hook command —
    Claude Code may spawn the server without the user's interactive PATH, so the
    absolute path is baked in rather than the bare name. The `sys.executable -m`
    fallback covers "nothing on PATH at all": it reuses the very interpreter
    running this CLI, which by construction has the package importable, and
    needs no directory to point a `uv run --project` at.
    """
    exe = shutil.which("recall-server")
    if exe:
        return [str(Path(exe).absolute())]
    return [sys.executable, "-m", "kb_recall.server"]


def _extension_version(claude_path: Path) -> tuple[int, ...]:
    """Version tuple from the `anthropic.claude-code-*` dir enclosing claude_path.

    Sorts `2.1.272` above `2.1.9`, which a plain string compare gets backwards —
    VSCode keeps several versions around, so picking the newest matters.
    """
    match = re.search(r"-(\d+(?:\.\d+)*)-", claude_path.parents[2].name)
    return tuple(int(p) for p in match.group(1).split(".")) if match else (0,)


def _claude_cli() -> str | None:
    """Path to the `claude` CLI, or None when it cannot be found.

    A PATH install wins, since that is the binary the user actually invokes.
    Extension installs never put `claude` on PATH — the CLI ships inside the
    VSCode extension bundle — so that layout is searched too, otherwise the
    first setup step is guaranteed to fail for every extension user.
    """
    found = shutil.which("claude")
    if found:
        return found

    local = Path.home() / ".claude" / "local" / "claude"
    if local.is_file() and os.access(local, os.X_OK):
        return str(local)

    extensions = Path.home() / ".vscode" / "extensions"
    if extensions.is_dir():
        bundled = [
            path
            for path in extensions.glob(
                "anthropic.claude-code-*/resources/native-binary/claude"
            )
            if path.is_file() and os.access(path, os.X_OK)
        ]
        if bundled:
            return str(max(bundled, key=_extension_version))
    return None


def _is_recall_prompt(command: str) -> bool:
    """True for any command invoking `recall prompt`, however it is spelled.

    Matches the bare name and an absolute path alike, so re-running setup
    upgrades an existing entry in place instead of appending a duplicate.
    """
    parts = command.split()
    return len(parts) == 2 and parts[1] == "prompt" and Path(parts[0]).name == "recall"


def cmd_sync_commands(_args: list[str] | None = None) -> int:
    dest_dir = Path.home() / ".claude" / "commands" / "recall"
    dest_dir.mkdir(parents=True, exist_ok=True)
    src_dir = SCRIPT_DIR / "commands"

    copied = []
    linked = []
    for src in sorted(src_dir.glob("*.md")):
        dest = dest_dir / src.name
        if dest.is_symlink():
            dest.unlink()
        elif dest.exists():
            dest.unlink()
        try:
            dest.symlink_to(src)
            linked.append(src.name)
        except OSError:
            shutil.copy2(src, dest)
            copied.append(src.name)

    if linked:
        print(f"  ✓ Linked: {', '.join(linked)}")
    if copied:
        print(f"  ~ Copied (symlink unavailable): {', '.join(copied)}")
        print("    Re-run 'recall sync-commands' after updating commands.")
    return 0


def cmd_prompt(_args: list[str] | None = None) -> int:
    from kb_recall.hooks.prompt_submit import main as prompt_submit_main

    # Returns None and signals through SystemExit (exit() for early exits,
    # sys.exit(0) on the happy path), so there is no value to propagate —
    # a SystemExit raised here still reaches the console script unchanged.
    prompt_submit_main()
    return 0


def cmd_block_idle(args: list[str]) -> int:
    if not args or args[0] not in ("true", "false"):
        print("Usage: recall block-idle <true|false>", file=sys.stderr)
        return 1

    enabled = args[0] == "true"
    cfg = _read_config()
    cfg["block_idle"] = enabled
    _write_config(cfg)
    state = "enabled" if enabled else "disabled"
    print(f"Idle-gap hard-block {state}.")
    if not enabled:
        print(
            "The softer warn-only reminders (idle-gap and context-size) still fire."
        )
    return 0


def cmd_setup(_args: list[str]) -> int:
    project_dir = Path.cwd().resolve()

    print(f"\nSetting up recall-mcp for: {project_dir}\n")

    # Step 1 — Register MCP server
    print("[1/5] Registering MCP server...")
    mcp_add_args = ["mcp", "add", "--scope", "user", "recall", "--", *_server_command()]
    claude_cli = _claude_cli()
    # Show the path that actually works on this machine — extension users have no
    # `claude` on PATH, so a bare `claude mcp add ...` is not copy-pasteable for them.
    manual_hint = " ".join([claude_cli or "claude"] + mcp_add_args)

    if claude_cli is None:
        print("  — Could not find the `claude` CLI. Run manually:")
        print(f"    {manual_hint}")
    else:
        try:
            # Remove first, so this is idempotent. `mcp add` refuses to overwrite an
            # existing entry, and an entry that merely exists is not necessarily a
            # correct one — a stale command (e.g. a script path from before the
            # package was restructured) surfaces to the user only as "failed to
            # connect" from `claude mcp list`, with nothing pointing back at setup.
            subprocess.run(
                [claude_cli, "mcp", "remove", "--scope", "user", "recall"],
                capture_output=True,
                text=True,
            )
            result = subprocess.run(
                [claude_cli] + mcp_add_args, capture_output=True, text=True
            )
        except OSError:
            # A missing or non-executable binary raises before returncode exists —
            # without this, setup dies here and steps 2-5 never run.
            result = None

        if result is not None and result.returncode == 0:
            print("  ✓ Registered (reload Claude Code to activate)")
        else:
            print("  — Could not auto-register. Run manually:")
            print(f"    {manual_hint}")

    # Step 2 — Add project to config.json + update CLAUDE.md
    print("\n[2/5] Configuring project...")
    cfg = _read_config()
    projects = cfg.get("projects", [])
    project_str = str(project_dir)
    if project_str not in projects:
        projects.append(project_str)
        cfg["projects"] = projects
        _write_config(cfg)
        print(f"  ✓ Added to config: {project_str}")
    else:
        print("  — Project already in config")

    # Written to CLAUDE.local.md, not the team-shared CLAUDE.md — this is
    # per-developer opt-in instruction text, not something every teammate
    # should have forced on them just because one person uses recall-mcp.
    local_md = project_dir / "CLAUDE.local.md"
    legacy_md = project_dir / "CLAUDE.md"
    snippet = (SCRIPT_DIR / "templates" / "claude-md-snippet.md").read_text().strip()
    already_has_section = (
        "recall-mcp" in local_md.read_text() if local_md.exists() else False
    ) or ("recall-mcp" in legacy_md.read_text() if legacy_md.exists() else False)
    if already_has_section:
        print(
            "  — CLAUDE.local.md (or legacy CLAUDE.md) already has recall-mcp section"
        )
    elif local_md.exists():
        with local_md.open("a") as f:
            f.write(f"\n\n{snippet}\n")
        print(f"  ✓ Appended recall-mcp section to {local_md.name}")
    else:
        local_md.write_text(f"{snippet}\n")
        print(f"  ✓ Created {local_md.name} with recall-mcp section")

    gitignore = project_dir / ".gitignore"
    gitignore_text = gitignore.read_text() if gitignore.exists() else ""
    if "CLAUDE.local.md" not in gitignore_text.splitlines():
        with gitignore.open("a") as f:
            if gitignore_text and not gitignore_text.endswith("\n"):
                f.write("\n")
            f.write("CLAUDE.local.md\n")
        print("  ✓ Added CLAUDE.local.md to .gitignore")
    else:
        print("  — CLAUDE.local.md already in .gitignore")

    # Step 3 — Configure issue tracker (once per project; /recall:init falls
    # back to asking this itself only if it's still missing here)
    print("\n[3/5] Configuring issue tracker...")
    trackers = cfg.setdefault("issue_trackers", {})
    if project_str in trackers:
        print(f"  — Already set: {trackers[project_str]}")
    else:
        try:
            answer = (
                input(
                    "  What issue tracker does this project use? [J]ira / [O]ther or none: "
                )
                .strip()
                .lower()
            )
        except EOFError:
            # No TTY (e.g. Claude running this non-interactively via Bash) — don't
            # guess and persist a wrong default. Leave unset; /recall:init already
            # falls back to asking this itself when it's still missing here.
            print(
                "  — No input available (non-interactive) — skipping, /recall:init will ask later"
            )
        else:
            tracker = "jira" if answer.startswith("j") else "other"
            trackers[project_str] = tracker
            _write_config(cfg)
            print(f"  ✓ Saved: {tracker}")

    # Step 4 — Inject hooks into ~/.claude/settings.json
    print("\n[4/5] Installing hooks...")

    settings: dict = {}
    if CLAUDE_SETTINGS.exists():
        settings = json.loads(CLAUDE_SETTINGS.read_text())

    hooks = settings.setdefault("hooks", {})

    # UserPromptSubmit — feature index + branch auto-load + turn counter reminder.
    # Uses the `recall` console script (uv tool install, own isolated interpreter)
    # rather than a bare `python3 .../prompt_submit.py` or `sys.executable`-based
    # command — either of those only imports `kb_recall.hooks.hook_helpers` when
    # the ambient python3 happens to be recall-mcp's own venv, which silently
    # ModuleNotFoundError-crashes the hook (non-blocking, no visible warning) in
    # every other project's session. The console script sidesteps the interpreter
    # question entirely, but not the PATH one — see _recall_command().
    submit_hooks = hooks.setdefault("UserPromptSubmit", [])
    hook_command = _recall_command()
    already_installed = False

    for entry in submit_hooks:
        for h in entry.get("hooks", []):
            if h.get("type") != "command":
                continue
            cmd = h.get("command", "")
            is_recall_hook = "prompt_submit.py" in cmd or _is_recall_prompt(cmd)
            if not is_recall_hook:
                continue
            if cmd != hook_command:
                print(f"  ~ Migrating stale hook command: {cmd!r} → {hook_command!r}")
                h["command"] = hook_command
            already_installed = True

    if already_installed:
        print("  — UserPromptSubmit hook already installed")
    else:
        submit_hooks.append(
            {"matcher": "", "hooks": [{"type": "command", "command": hook_command}]}
        )
        print("  ✓ UserPromptSubmit hook installed")

    CLAUDE_SETTINGS.parent.mkdir(parents=True, exist_ok=True)
    CLAUDE_SETTINGS.write_text(json.dumps(settings, indent=2) + "\n")

    # Step 5 — Sync slash commands to ~/.claude/commands/recall/
    print("\n[5/5] Syncing slash commands...")
    cmd_sync_commands()

    print("\nSetup complete. Reload Claude Code to activate.\n")
    return 0


def main() -> None:
    args = sys.argv[1:]
    if not args or args[0] in ("-h", "--help"):
        print("Usage: recall <command>")
        print()
        print("Commands:")
        print(
            "  setup           Register MCP server, configure project, install hooks, sync commands"
        )
        print("  sync-commands   Re-sync slash commands to ~/.claude/commands/recall/")
        print(
            "  prompt          Run the UserPromptSubmit hook (called by settings.json, not by hand)"
        )
        print(
            "  block-idle <true|false>   Enable/disable the idle-gap hard-block (warn-only reminders stay on)"
        )
        sys.exit(0)

    command = args[0]
    if command == "setup":
        sys.exit(cmd_setup(args[1:]))
    elif command == "sync-commands":
        sys.exit(cmd_sync_commands(args[1:]))
    elif command == "prompt":
        sys.exit(cmd_prompt(args[1:]))
    elif command == "block-idle":
        sys.exit(cmd_block_idle(args[1:]))
    else:
        print(f"Unknown command: {command}", file=sys.stderr)
        print("Run 'recall --help' for usage.", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
