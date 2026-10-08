"""No text stream in this repo may rely on the locale's default encoding.

Windows hands a *piped* stream the locale encoding — cp1252 on most installs, not
UTF-8 — so a bare `open()` / `read_text()` / `write_text()` does not mean "UTF-8";
it means "whatever this machine happens to use". Measured 2026-10-07: `recall
setup` died in `_write_copilot_instructions` reading its own template with

    UnicodeDecodeError: 'charmap' codec can't decode byte 0x8f in position 6346

because `templates/copilot-instructions.md` carries `⚠️` and 0x8F is one of the
five bytes cp1252 leaves undefined (0x81, 0x8D, 0x8F, 0x90, 0x9D). The same
assumption breaks the streams in the other direction: a hook printing KB text
carrying `—` / `→` / `✓` / `⚠` raises `UnicodeEncodeError`, and because hooks
swallow every exception by design the symptom is an empty injection, not an error.

Reproduce the whole class at once rather than one site at a time:

    PYTHONUTF8=0 LC_ALL=C uv run pytest -q     # 123 failed before, 455 passed after

This guard is a test and not a lint rule on purpose: ruff's `PLW1514`
(unspecified-encoding) covers the same ground but is still a preview rule, and
turning preview mode on would change how the stable rules already pinned in
`pyproject.toml` behave.
"""

from __future__ import annotations

import ast
import os
import pathlib
import subprocess
import sys

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
SOURCE_DIRS = ("kb_recall", "tests", "scripts")
WRAPPERS = {"read_text", "write_text"}


def _call_name(node: ast.Call) -> str | None:
    func = node.func
    if isinstance(func, ast.Attribute):
        return func.attr
    if isinstance(func, ast.Name):
        return func.id
    return None


def _open_mode(node: ast.Call, is_builtin: bool) -> str:
    """The mode string of an `open()` call; binary modes are not our business."""
    index = 1 if is_builtin else 0
    if len(node.args) > index and isinstance(node.args[index], ast.Constant):
        mode = node.args[index].value
        if isinstance(mode, str):
            return mode
    return "r"


def _sites_missing_encoding(path: pathlib.Path) -> list[str]:
    found = []
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if not isinstance(node, ast.Call):
            continue
        name = _call_name(node)
        if name not in WRAPPERS and name != "open":
            continue
        if any(keyword.arg == "encoding" for keyword in node.keywords):
            continue
        if name == "open" and "b" in _open_mode(node, isinstance(node.func, ast.Name)):
            continue
        found.append(f"{path.relative_to(REPO_ROOT)}:{node.lineno}: {name}()")
    return found


def test_every_text_stream_names_its_encoding():
    offenders = [
        site
        for directory in SOURCE_DIRS
        for path in sorted((REPO_ROOT / directory).rglob("*.py"))
        for site in _sites_missing_encoding(path)
    ]
    assert not offenders, (
        "text I/O without encoding= falls back to the locale's encoding, which is "
        "cp1252 on Windows and fails on bytes UTF-8 content contains:\n  "
        + "\n  ".join(offenders)
    )


def test_force_utf8_stdio_round_trips_non_ascii_under_a_non_utf8_locale(tmp_path):
    """The stream guard, exercised in a child process forced to a non-UTF-8 locale.

    The locale has to be forced in the child: on a developer machine that already
    defaults to UTF-8 the probe would pass with the fix removed, which is how the
    original bug stayed invisible.

    The probe compares the text it read and writes a *literal* from its own source
    rather than echoing stdin back. Both details are load-bearing: stdio defaults
    to `errors="surrogateescape"`, so undecodable input comes back as lone
    surrogates that re-encode to the original bytes — an echo would round-trip
    perfectly with the fix removed and the test would guard nothing.
    """
    probe = tmp_path / "probe.py"
    probe.write_text(
        "import sys\n"
        "from kb_recall.stdio import force_utf8_stdio\n"
        "\n"
        "force_utf8_stdio()\n"
        "expected = 'ấ→⚠'\n"
        "sys.stdout.write('ok' if sys.stdin.read() == expected else 'mangled')\n"
        "sys.stdout.write(expected)\n",
        encoding="utf-8",
    )
    result = subprocess.run(
        [sys.executable, str(probe)],
        input="ấ→⚠",
        capture_output=True,
        encoding="utf-8",
        check=True,
        env={
            **os.environ,
            "PYTHONUTF8": "0",
            "LC_ALL": "C",
            "PYTHONPATH": str(REPO_ROOT),
        },
    )
    assert result.stdout == "okấ→⚠"


def test_copilot_instructions_survive_a_non_utf8_locale(tmp_path):
    """The reported failure, end to end: read the real template, not a stubbed one.

    `tests/test_cli.py` covers this function against a template it writes itself,
    which is ASCII — so it cannot reproduce the bug, because the bug needs the
    packaged file's `⚠️` (UTF-8 `e2 9a a0 ef b8 8f`, and 0x8F is undefined in
    cp1252). Only the real `SCRIPT_DIR/templates` carries that byte, and only a
    child process can force the locale that makes it fatal.

    stdout is redirected so the child's own `✓` progress line cannot fail the run
    first: `force_utf8_stdio()` lives in `main()`, and this probe calls the write
    helper directly, so the print would be the locale's problem rather than the
    file read's.
    """
    project = tmp_path / "project"
    project.mkdir()
    probe = tmp_path / "probe.py"
    probe.write_text(
        "import contextlib, io, pathlib, sys\n"
        "from kb_recall.cli import _write_copilot_instructions\n"
        "\n"
        "with contextlib.redirect_stdout(io.StringIO()):\n"
        "    _write_copilot_instructions(pathlib.Path(sys.argv[1]))\n",
        encoding="utf-8",
    )
    subprocess.run(
        [sys.executable, str(probe), str(project)],
        check=True,
        capture_output=True,
        encoding="utf-8",
        env={
            **os.environ,
            "PYTHONUTF8": "0",
            "LC_ALL": "C",
            "PYTHONPATH": str(REPO_ROOT),
        },
    )
    written = (project / ".github" / "copilot-instructions.md").read_text(encoding="utf-8")
    assert "⚠️ Promotion skipped" in written
