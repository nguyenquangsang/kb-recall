#!/usr/bin/env python3
"""recall CLI — setup and management commands."""

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

# The postToolUse matcher is a hook-protocol detail, so it lives with the hook that
# has to honour it rather than being restated here. A second copy is not harmless:
# an invalid regex makes the harness skip the whole hook entry SILENTLY, so the two
# copies would have to be kept in sync by hand with no failure signal if they drift.
from kb_recall.adapters.copilot.hook import POST_TOOL_USE_MATCHER

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
        if dest.is_symlink() or dest.exists():
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

    # Returns None and signals early exits through SystemExit (sys.exit()), so
    # there is no value to propagate — a SystemExit raised here still reaches the
    # console script unchanged.
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
        print("The softer warn-only reminders (idle-gap and context-size) still fire.")
    return 0


PLATFORMS = ("claude", "copilot", "all")
COPILOT_HOOK_SCRIPT = "recall-copilot-hook"
COPILOT_HOOK_TIMEOUT_SEC = 30
COPILOT_POST_TOOL_TIMEOUT_SEC = 15
COPILOT_SKILLS_DIR = Path.home() / ".copilot" / "skills"


def _parse_platform(args: list[str]) -> str | None:
    """Return the requested platform, or None when the value is missing/invalid.

    Defaults to "claude": `recall setup` gained a flag, it did not change meaning,
    so every existing invocation keeps the behaviour it had.
    """
    platform = "claude"
    i = 0
    while i < len(args):
        arg = args[i]
        if arg == "--platform":
            if i + 1 >= len(args):
                return None  # flag with no value — reject rather than guess
            platform = args[i + 1]
            i += 2
            continue
        if arg.startswith("--platform="):
            platform = arg.split("=", 1)[1]
        i += 1
    return platform if platform in PLATFORMS else None


def _ensure_gitignored(project_dir: Path, entries: list[str]) -> None:
    """Append any of `entries` missing from .gitignore, one per line."""
    gitignore = project_dir / ".gitignore"
    text = gitignore.read_text() if gitignore.exists() else ""
    missing = [entry for entry in entries if entry not in text.splitlines()]
    if not missing:
        print(f"  — Already gitignored: {', '.join(entries)}")
        return
    with gitignore.open("a") as f:
        if text and not text.endswith("\n"):
            f.write("\n")
        for entry in missing:
            f.write(f"{entry}\n")
    print(f"  ✓ Added to .gitignore: {', '.join(missing)}")


def _copilot_hook_command(event: str) -> str:
    """One hook-command string for .github/hooks/recall.json.

    Absolute path for the same reason as _recall_command(): the harness spawns hook
    commands through a shell that never sources the user's rc, so a bare console
    script name resolves to nothing and the hook fails SILENTLY — no stderr, no
    hint, the hook simply injects nothing.

    The event travels on argv rather than being read from the payload, because the
    payload's casing says nothing about which event fired (see the adapter's `main`).
    """
    exe = shutil.which(COPILOT_HOOK_SCRIPT)
    base = str(Path(exe).absolute()) if exe else COPILOT_HOOK_SCRIPT
    return f"{base} {event}"


def _copilot_hook_config() -> dict:
    """The body of .github/hooks/recall.json — both events this adapter implements.

    The `matcher` key placement is verified: VS Code Local accepts the entry and fires
    the hook. Its VALUE, however, is documented to be ignored there — Local parses
    `matcher` for Claude compatibility and then runs every nested command for the
    event. So the key is carried for harnesses that honor it, not to make this process
    cheaper on Local: every tool call still spawns it, and the counter counts those
    too. To actually filter on Local, branch on the event input inside the handler.
    """
    return {
        "version": 1,
        "hooks": {
            "sessionStart": [
                {
                    "type": "command",
                    "command": _copilot_hook_command("sessionStart"),
                    "timeoutSec": COPILOT_HOOK_TIMEOUT_SEC,
                }
            ],
            "postToolUse": [
                {
                    "type": "command",
                    "command": _copilot_hook_command("postToolUse"),
                    "matcher": POST_TOOL_USE_MATCHER,
                    "timeoutSec": COPILOT_POST_TOOL_TIMEOUT_SEC,
                }
            ],
        },
    }


def _copilot_mcp_config() -> dict:
    """The body of .mcp.json — VS Code reads this at the workspace root.

    VS Code Stable ignores a workspace-root .mcp.json unless
    `chat.mcp.workspaceRootConfig.enabled` is true (it defaults to false outside
    Insiders), so setup says so rather than letting the user discover it by the
    absence of tools. Reuses _server_command() so the path baked in is the same one
    Claude Code gets.
    """
    argv = _server_command()
    server: dict = {"type": "stdio", "command": argv[0]}
    if len(argv) > 1:
        server["args"] = argv[1:]
    return {"mcpServers": {"recall": server}}


def _read_json_dict(path: Path) -> dict | None:
    """Read `path` as a JSON object; `{}` if absent, `None` if present but not one.

    `None` (not `{}`) distinguishes "malformed / not an object — leave it alone"
    from "absent — write fresh". Clobbering a file we cannot parse would destroy
    whatever is in it, so callers treat `None` as refuse-to-touch.
    """
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text())
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def _write_copilot_instructions(project_dir: Path) -> None:
    """Write .github/copilot-instructions.md unless it already has the recall-mcp section.

    This file carries the Copilot-specific corrections — no index injection, so
    `list_features()` is needed; per-tool-call reminders; no idle-gap guard — that the
    Claude-side snippet omits or states differently. It is therefore written even when
    `CLAUDE.local.md` already carries recall-mcp: `--platform all` must not leave Copilot
    holding only the Claude-flavoured guidance. A file that already exists without the
    recall-mcp marker is the team's own instructions — append to it, never overwrite.
    """
    target = project_dir / ".github" / "copilot-instructions.md"
    if target.exists() and "recall-mcp" in target.read_text():
        print("  — .github/copilot-instructions.md already has the recall-mcp section")
        return

    snippet = (SCRIPT_DIR / "templates" / "copilot-instructions.md").read_text()
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        existing = target.read_text().rstrip()
        target.write_text(f"{existing}\n\n---\n\n{snippet}" if existing else snippet)
        print(f"  ✓ Appended to {target.relative_to(project_dir)}")
    else:
        target.write_text(snippet)
        print(f"  ✓ Created {target.relative_to(project_dir)}")


def _write_copilot_skills() -> None:
    """Symlink kb_recall/skills/* into ~/.copilot/skills/, mirroring cmd_sync_commands.

    Skills are user-scope, not project-scope, for the same reason the Claude
    commands are: the same three skills serve every project, so installing them
    once under ~/.copilot/ makes them available everywhere instead of copying
    them into each project's .github/. Symlinked rather than copied — the choice
    cmd_sync_commands already makes — so kb_recall/skills/ stays the single
    source of truth and edits to it take effect live, with no second copy to
    drift.
    """
    src_root = SCRIPT_DIR / "skills"
    if not src_root.is_dir():
        return
    linked: list[str] = []
    copied: list[str] = []
    for src in sorted(src_root.glob("*/SKILL.md")):
        dest_dir = COPILOT_SKILLS_DIR / src.parent.name
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / "SKILL.md"
        if dest.is_symlink() or dest.exists():
            dest.unlink()
        try:
            dest.symlink_to(src)
            linked.append(src.parent.name)
        except OSError:
            shutil.copy2(src, dest)
            copied.append(src.parent.name)
    if linked:
        print(f"  ✓ Copilot skills (symlinked): {', '.join(linked)}")
    if copied:
        print(f"  ~ Copilot skills (copied, symlink unavailable): {', '.join(copied)}")
        print("    Re-run 'recall setup --platform copilot' after updating skills.")


def _setup_copilot(project_dir: Path) -> None:
    """Write the Copilot config: per-machine MCP + hook config into the project,
    committable instructions, and user-scope skills under ~/.copilot/. Never
    touches ~/.claude/."""
    print("\nWriting Copilot MCP config...")
    mcp_file = project_dir / ".mcp.json"
    existing_mcp = _read_json_dict(mcp_file)
    if existing_mcp is None:
        print(f"  — {mcp_file.name} exists but isn't valid JSON; leaving it untouched")
    else:
        # Merge, never overwrite: .mcp.json is the shared workspace-root MCP config,
        # so any other server configured there must survive a recall setup.
        servers = existing_mcp.get("mcpServers", {})
        if not isinstance(servers, dict):
            servers = {}
        servers["recall"] = _copilot_mcp_config()["mcpServers"]["recall"]
        existing_mcp["mcpServers"] = servers
        mcp_file.write_text(json.dumps(existing_mcp, indent=2) + "\n")
        suffix = " (merged with existing servers)" if len(servers) > 1 else ""
        print(f"  ✓ {mcp_file.name} → recall (stdio){suffix}")
    print(
        "    Requires `chat.mcp.workspaceRootConfig.enabled: true` in VS Code "
        "settings — Stable defaults it to false and ignores this file silently."
    )

    print("\nWriting Copilot hook config...")
    hooks_file = project_dir / ".github" / "hooks" / "recall.json"
    hooks_file.parent.mkdir(parents=True, exist_ok=True)
    existing_hooks = _read_json_dict(hooks_file)
    if existing_hooks is None:
        print(f"  — {hooks_file.name} exists but isn't valid JSON; leaving it untouched")
    else:
        # Preserve other event keys a user may have added; replace only recall's own.
        merged_hooks = existing_hooks.get("hooks", {})
        if not isinstance(merged_hooks, dict):
            merged_hooks = {}
        for event, entries in _copilot_hook_config()["hooks"].items():
            merged_hooks[event] = entries
        existing_hooks["hooks"] = merged_hooks
        existing_hooks.setdefault("version", 1)
        hooks_file.write_text(json.dumps(existing_hooks, indent=2) + "\n")
        print(
            f"  ✓ {hooks_file.relative_to(project_dir)} "
            "(sessionStart auto-load + postToolUse reminders)"
        )

    print("\nWriting Copilot instructions...")
    _write_copilot_instructions(project_dir)

    print("\nWriting Copilot skills...")
    _write_copilot_skills()

    print("\nUpdating .gitignore...")
    _ensure_gitignored(project_dir, [".mcp.json", ".github/hooks/recall.json"])


def cmd_setup(args: list[str]) -> int:
    platform = _parse_platform(args)
    if platform is None:
        print(
            f"  Invalid --platform. Expected one of: {', '.join(PLATFORMS)}.",
            file=sys.stderr,
        )
        print("  Usage: recall setup [--platform claude|copilot|all]", file=sys.stderr)
        return 1

    do_claude = platform in ("claude", "all")
    do_copilot = platform in ("copilot", "all")

    project_dir = Path.cwd().resolve()

    # Printed unconditionally: with --platform all, which files were written where
    # is the difference between "it worked" and "I clobbered something".
    print(f"\nSetting up recall-mcp for: {project_dir}")
    print(f"Platform: {platform}\n")

    # Step 1 — Register MCP server (Claude Code only; Copilot gets .mcp.json below)
    if do_claude:
        print("[1/5] Registering MCP server...")
        mcp_add_args = [
            "mcp",
            "add",
            "--scope",
            "user",
            "recall",
            "--",
            *_server_command(),
        ]
        claude_cli = _claude_cli()
        # Show the path that actually works on this machine — extension users have no
        # `claude` on PATH, so a bare `claude mcp add ...` is not copy-pasteable for them.
        manual_hint = " ".join([claude_cli or "claude", *mcp_add_args])

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
                    check=False,  # best-effort: a missing entry is not a failure
                )
                result = subprocess.run(
                    [claude_cli, *mcp_add_args],
                    capture_output=True,
                    text=True,
                    check=False,  # the returncode is inspected below, not raised
                )
            except OSError:
                # A missing or non-executable binary raises before returncode exists —
                # without this, setup dies here and every later step never runs.
                result = None

            if result is not None and result.returncode == 0:
                print("  ✓ Registered (reload Claude Code to activate)")
            else:
                print("  — Could not auto-register. Run manually:")
                print(f"    {manual_hint}")

    # Step 2 — Add project to config.json, then Claude's instruction file
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

    if do_claude:
        # Written to CLAUDE.local.md, not the team-shared CLAUDE.md — this is
        # per-developer opt-in instruction text, not something every teammate
        # should have forced on them just because one person uses recall-mcp.
        local_md = project_dir / "CLAUDE.local.md"
        legacy_md = project_dir / "CLAUDE.md"
        snippet = (
            (SCRIPT_DIR / "templates" / "claude-md-snippet.md").read_text().strip()
        )
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

        _ensure_gitignored(project_dir, ["CLAUDE.local.md"])

    # Step 3 — Configure issue tracker (shared; /recall:init falls back to asking
    # this itself only if it's still missing here)
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

    # Step 4 — Inject hooks into ~/.claude/settings.json + sync slash commands
    if do_claude:
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
                    print(
                        f"  ~ Migrating stale hook command: {cmd!r} → {hook_command!r}"
                    )
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

    # Step 5 — GitHub Copilot config. Deliberately never touches ~/.claude/: the two
    # platforms keep separate hook configs, and mixing them would have one platform's
    # setup silently rewrite the other's live configuration.
    if do_copilot:
        _setup_copilot(project_dir)

    print()
    if do_claude:
        print("Claude Code: reload to activate.")
    if do_copilot:
        print(
            "Copilot: reload VS Code and start a NEW chat session — the hook runs at "
            "session start, so an open session will not pick it up."
        )
    print()
    return 0


def main() -> None:
    args = sys.argv[1:]
    if not args or args[0] in ("-h", "--help"):
        print("Usage: recall <command>")
        print()
        print("Commands:")
        print("  setup [--platform claude|copilot|all]")
        print(
            "                  Register MCP server, configure project, install hooks, sync commands"
        )
        print(
            "                  (default: claude — copilot writes .mcp.json + .github/, never ~/.claude/)"
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
