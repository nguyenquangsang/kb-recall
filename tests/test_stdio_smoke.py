"""Stdio smoke test: speak the MCP JSON-RPC protocol to a real `recall-server`
subprocess and assert it answers.

The stdio transport is newline-delimited JSON (NDJSON) — one JSON-RPC message
per line, no Content-Length framing. This is the one path the in-process unit
tests never touch: the actual wire protocol the client harness uses.
"""

import json
import os
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from tests.conftest import fake_home

REPO_ROOT = Path(__file__).resolve().parents[1]
EXPECTED_TOOLS = {
    "init_feature",
    "list_features",
    "load_feature_context",
    "report_miss",
    "save_memory",
    "search_features",
    "update_feature_index",
    "update_readme",
}


def _send(proc, obj):
    proc.stdin.write(json.dumps(obj) + "\n")
    proc.stdin.flush()


def _read_line(proc, timeout=10.0):
    """Read one NDJSON line, bounded by a timeout that also works on Windows.

    Deliberately not `select.select`: on Windows it accepts sockets only and
    raises OSError on a pipe, so this smoke test could not run there at all. A
    reader thread is the portable equivalent — it performs the blocking
    `readline()` while the main thread bounds the wait. The thread is a daemon
    so a hung server cannot keep the test process alive after a failure.
    """
    lines: list[str] = []

    def pump() -> None:
        line = proc.stdout.readline()
        if line:
            lines.append(line)

    reader = threading.Thread(target=pump, daemon=True)
    reader.start()
    reader.join(timeout)
    if reader.is_alive():
        raise TimeoutError("recall-server did not respond")
    if not lines:
        raise EOFError("recall-server closed stdout")
    return json.loads(lines[0])


def _initialize(proc):
    _send(
        proc,
        {
            "jsonrpc": "2.0",
            "id": 0,
            "method": "initialize",
            "params": {
                "protocolVersion": "2026-07-28",
                "capabilities": {},
                "clientInfo": {"name": "smoke-test", "version": "0"},
            },
        },
    )
    init = _read_line(proc)
    assert init["id"] == 0 and "result" in init, init
    _send(proc, {"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}})


@pytest.fixture
def server_proc(tmp_path):
    """A live recall-server subprocess with an isolated HOME (fake KB store)."""
    home = tmp_path / "home"
    (home / ".recall-mcp").mkdir(parents=True)
    (home / ".recall-mcp" / "config.json").write_text(
        json.dumps({"projects": [str(tmp_path / "myproj")]}),
        encoding="utf-8",
    )
    env = {**os.environ, **fake_home(home)}
    proc = subprocess.Popen(
        [sys.executable, "-m", "kb_recall.server"],
        cwd=REPO_ROOT,
        env=env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        encoding="utf-8",
    )
    try:
        yield home, proc
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()


def test_server_lists_all_tools_over_stdio(server_proc):
    _, proc = server_proc
    _initialize(proc)
    _send(proc, {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}})
    resp = _read_line(proc)
    names = {t["name"] for t in resp["result"]["tools"]}
    assert names == EXPECTED_TOOLS, names


def test_server_calls_init_feature_over_stdio(server_proc):
    home, proc = server_proc
    _initialize(proc)
    _send(
        proc,
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {
                "name": "init_feature",
                "arguments": {
                    "name": "Smoke",
                    "slug": "smoke",
                    "summary": "stdio smoke test",
                    "project": "myproj",
                    "username": "alice",
                },
            },
        },
    )
    resp = _read_line(proc)
    result = resp["result"]
    assert "Created feature KB 'smoke'" in json.dumps(result), result
    assert (home / ".recall-mcp" / "myproj" / "smoke" / "README.md").exists()
