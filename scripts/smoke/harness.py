"""Shared plumbing for the smoke tests: a throwaway KB store and a headless
`claude -p` runner.

Two jobs:
  1. Build an isolated HOME so each run gets its own clean `usage.jsonl`.
  2. Locate and drive the `claude` binary without a human at the terminal.

Why isolation works this way — `kb_recall/server.py` hardcodes
`KB_ROOT = Path.home() / ".recall-mcp"` and reads no environment override, so
moving HOME is the only lever available. Auth survives the move because this
setup authenticates via inherited `ANTHROPIC_*` env vars
(`ANTHROPIC_BASE_URL` -> api.deepseek.com/anthropic), not with anything under
`~/.claude`. `settings.json` is *copied*, not symlinked: it carries the
`UserPromptSubmit` hook and the model choice — both things these probes exist to
exercise — and the child must never be able to write back over the real one.
"""

import json
import os
import re
import shutil
import subprocess
import tempfile
from contextlib import contextmanager
from pathlib import Path


def _version_key(dirname: str) -> tuple:
    """Sort key for an extension directory name.

    Numeric, not lexicographic: sorting the version strings as text puts
    `2.1.9` above `2.1.272`, so the newest bundle would lose to an older one.
    """
    match = re.search(r"claude-code-(\d+(?:\.\d+)*)", dirname)
    if not match:
        return (0,)
    return tuple(int(part) for part in match.group(1).split("."))


def find_claude() -> Path:
    """Locate the `claude` binary.

    PATH first, then `~/.claude/local/claude`, then the newest VS Code extension
    bundle. That last fallback is the normal case for extension users — the
    binary ships inside the bundle and is never put on PATH.
    """
    on_path = shutil.which("claude")
    if on_path:
        return Path(on_path)

    local = Path.home() / ".claude" / "local" / "claude"
    if local.exists():
        return local

    candidates = []
    for bundle in (Path.home() / ".vscode" / "extensions").glob(
        "anthropic.claude-code-*"
    ):
        binary = bundle / "resources" / "native-binary" / "claude"
        if binary.exists():
            candidates.append((_version_key(bundle.name), binary))

    if not candidates:
        raise FileNotFoundError(
            "claude binary not found — checked PATH, ~/.claude/local/claude, "
            "and ~/.vscode/extensions/anthropic.claude-code-*"
        )
    return max(candidates)[1]


@contextmanager
def isolated_home(repo: Path, keep: bool = False):
    """Yield a temp HOME whose `.recall-mcp` belongs to this run alone.

    The child inherits `ANTHROPIC_*` from the ambient environment, so pointing
    HOME elsewhere costs it nothing but its settings — which are copied in.
    """
    home = Path(tempfile.mkdtemp(prefix="recall-smoke-"))
    try:
        claude_dir = home / ".claude"
        claude_dir.mkdir()
        real_settings = Path.home() / ".claude" / "settings.json"
        if real_settings.exists():
            shutil.copy2(real_settings, claude_dir / "settings.json")

        store = home / ".recall-mcp"
        store.mkdir()
        # Pin the username so _resolve_username never shells out to `git config`
        # and never inherits the developer's own memories file.
        (store / "config.json").write_text(
            json.dumps({"projects": [str(repo)], "username": "smoke"}, indent=2) + "\n",
            encoding="utf-8",
        )
        yield home
    finally:
        if keep:
            print(f"[harness] kept isolated HOME: {home}")
        else:
            shutil.rmtree(home, ignore_errors=True)


def write_mcp_config(home: Path, repo: Path) -> Path:
    """Write an MCP config naming only this project's recall server."""
    server = repo / ".venv" / "bin" / "recall-server"
    if not server.exists():
        raise FileNotFoundError(
            f"{server} missing — run `uv sync` in {repo} so the probe drives the "
            "working tree's server rather than an installed one."
        )
    config = home / "mcp.json"
    config.write_text(
        json.dumps(
            {"mcpServers": {"recall": {"type": "stdio", "command": str(server)}}},
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return config


def run_claude(
    *,
    home: Path,
    repo: Path,
    prompt: str,
    model: str,
    mcp_config: Path,
    allowed_tools=(),
    timeout: int = 240,
):
    """Run one headless turn and return the CompletedProcess.

    `--strict-mcp-config` keeps every other configured MCP server out, so a
    stray `list_features` from an unrelated server can't be mistaken for a
    recall-mcp call.
    """
    env = dict(os.environ)
    # Both, not just HOME: Windows' ntpath.expanduser() reads USERPROFILE and
    # never consults HOME, so setting HOME alone would still resolve the KB paths
    # to the real profile. Mirrors tests/conftest.py::fake_home.
    env["HOME"] = str(home)
    env["USERPROFILE"] = str(home)
    env["ANTHROPIC_MODEL"] = model

    cmd = [
        str(find_claude()),
        "-p",
        prompt,
        "--model",
        model,
        "--strict-mcp-config",
        "--mcp-config",
        str(mcp_config),
        "--output-format",
        "json",
    ]
    if allowed_tools:
        cmd.extend(["--allowedTools", *allowed_tools])

    # check=False is deliberate: a non-zero exit is a probe *result* to report,
    # not an exception to raise — callers branch on returncode.
    return subprocess.run(
        cmd,
        cwd=repo,
        env=env,
        capture_output=True,
        encoding="utf-8",
        timeout=timeout,
        check=False,
    )


def _parse_jsonl(text: str) -> list:
    """Parse NDJSON, skipping any unreadable line."""
    records = []
    for line in text.splitlines():
        line = line.strip()
        if line:
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return records


def load_usage_file(path) -> list:
    """Parse one usage.jsonl file, skipping any unreadable line."""
    path = Path(path)
    if not path.exists():
        return []
    return _parse_jsonl(path.read_text(encoding="utf-8"))


def usage_offset(path) -> int:
    """Byte offset to resume reading from, i.e. the current end of the log.

    Callers pair this with `records_since` to read only what a run added. Byte
    offset, not line count: the log is appended to by every client on the
    machine, so "the last N lines" can straddle someone else's session.
    """
    path = Path(path)
    return path.stat().st_size if path.exists() else 0


def records_since(path, offset: int) -> list:
    """Records appended to a usage.jsonl past `offset`."""
    path = Path(path)
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8") as handle:
        handle.seek(offset)
        return _parse_jsonl(handle.read())


def find_copilot() -> Path:
    """Locate the `copilot` binary.

    PATH first, then the npm global prefix. The Copilot CLI is installed with
    `npm install -g @github/copilot`, so when PATH misses it the prefix is where
    npm put it — and under a node version manager that bin directory is
    version-specific, which is exactly the case where a bare `which` fails.
    """
    on_path = shutil.which("copilot")
    if on_path:
        return Path(on_path)

    prefixes = []
    npm = shutil.which("npm")
    if npm:
        proc = subprocess.run(
            [npm, "config", "get", "prefix"],
            capture_output=True,
            encoding="utf-8",
            check=False,
        )
        if proc.returncode == 0 and proc.stdout.strip():
            prefixes.append(Path(proc.stdout.strip()))

    versions = Path.home() / ".nvm" / "versions" / "node"
    if versions.is_dir():
        prefixes.extend(sorted(p for p in versions.iterdir() if p.is_dir()))

    for prefix in prefixes:
        binary = prefix / "bin" / "copilot"
        if binary.exists():
            return binary

    raise FileNotFoundError(
        "copilot binary not found — install it with "
        "`npm install -g @github/copilot`, then re-run"
    )


def run_copilot(
    *,
    repo: Path,
    prompt: str,
    mcp_config: Path,
    model: str = "",
    timeout: int = 240,
):
    """Run one headless Copilot turn and return the CompletedProcess.

    `--additional-mcp-config` is not optional here: a bare `copilot mcp list`
    in this repo shows only the builtin servers, so the workspace `.mcp.json`
    that `recall setup` generates is invisible unless it is injected.
    `--allow-all-tools` is likewise required — non-interactive mode refuses to
    start tool calls without it.
    """
    cmd = [
        str(find_copilot()),
        "-p",
        prompt,
        "--additional-mcp-config",
        f"@{mcp_config}",
        "--allow-all-tools",
        "-s",
    ]
    if model:
        cmd.extend(["--model", model])

    # check=False is deliberate, as in run_claude: a non-zero exit is a probe
    # *result* to report, not an exception to raise.
    return subprocess.run(
        cmd,
        cwd=repo,
        capture_output=True,
        encoding="utf-8",
        timeout=timeout,
        check=False,
    )


def load_usage(home: Path) -> list:
    """Every tool call recorded under an isolated HOME, in order."""
    return load_usage_file(Path(home) / ".recall-mcp" / "usage.jsonl")


def called_tools(records) -> list:
    """Tool names in call order, skipping records that name no tool.

    A record without a `tool` is not a call — counting it would inflate
    `min_calls` and let malformed log lines pad a failing run into passing.
    """
    return [name for name in (r.get("tool") for r in records) if name]
