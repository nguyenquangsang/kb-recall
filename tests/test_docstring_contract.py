"""Regression guards for the MCP tool docstring contract.

Why these tests exist: Claude Code's ToolSearch truncates a deferred MCP
tool's description at ~2000-2150 chars with no error signal. The `Args:`
section carries the only per-parameter descriptions (the JSON schema has
none), so if it lands past the cut the model silently loses what a parameter
means. CLAUDE.md's "Docstring length" section documents the rules these tests
pin down.

These tests measure `tool.description`, not `inspect.getdoc(fn)`: the server
serves the raw `fn.__doc__` (see MCPServer's Tool.from_function), which keeps
the source indentation and is therefore ~130-250 chars LONGER than the
`inspect.getdoc` figure CLAUDE.md measures. The served value is the one that
actually hits the truncation.
"""

import inspect

from kb_recall import server

# Hard safety floor: `Args:` must close before the truncation zone begins.
# Truncation was measured at 2011-2154 and 2100-2154 chars; CLAUDE.md's
# "comfortably under ~1900" target leaves margin on top of this floor.
ARGS_SAFE_LIMIT = 2000

# Descriptions this long (served) or longer must put `Args:` before `OUTPUT:`.
# Shorter ones fit whole before the cut, so `Args:` may sit last.
LONG_DOC_THRESHOLD = 1800

_SECTIONS = ("WHEN:", "FORMAT:", "Args:", "OUTPUT:", "EXAMPLES:")


def _tools():
    """All registered MCP tools, by name — includes any added later."""
    return {t.name: t for t in server.mcp._tool_manager.list_tools()}


def _section_offsets(doc):
    """Char offset of each section marker's line, first occurrence only."""
    offsets = {}
    offset = 0
    for line in doc.splitlines(keepends=True):
        stripped = line.strip()
        for marker in _SECTIONS:
            if stripped.startswith(marker) and marker not in offsets:
                offsets[marker] = offset
        offset += len(line)
    return offsets


def _args_end_offset(doc):
    """Char offset just past the Args: block (next section, or end of doc)."""
    offsets = _section_offsets(doc)
    args_start = offsets.get("Args:")
    if args_start is None:
        return None
    following = [
        offsets[m]
        for m in ("OUTPUT:", "EXAMPLES:")
        if m in offsets and offsets[m] > args_start
    ]
    return min(following) if following else len(doc)


def _served_doc(tool):
    return tool.description or ""


def test_every_tool_has_a_nonempty_description():
    for name, tool in _tools().items():
        assert _served_doc(tool).strip(), f"{name}: empty tool description"


def test_every_tool_has_an_args_section():
    for name, tool in _tools().items():
        assert "Args:" in _served_doc(tool), f"{name}: missing Args: section"


def test_args_section_closes_before_truncation():
    for name, tool in _tools().items():
        end = _args_end_offset(_served_doc(tool))
        assert end is not None, f"{name}: no Args: block"
        assert end <= ARGS_SAFE_LIMIT, (
            f"{name}: Args: closes at char {end}, over the {ARGS_SAFE_LIMIT} "
            "safe limit — a truncating client would lose parameter descriptions"
        )


def test_long_descriptions_put_args_before_output():
    for name, tool in _tools().items():
        doc = _served_doc(tool)
        if len(doc) < LONG_DOC_THRESHOLD:
            continue
        offsets = _section_offsets(doc)
        assert "Args:" in offsets and "OUTPUT:" in offsets, (
            f"{name}: missing Args:/OUTPUT:"
        )
        assert offsets["Args:"] < offsets["OUTPUT:"], (
            f"{name}: Args: after OUTPUT: in a long description"
        )


def test_every_parameter_is_documented():
    for name, tool in _tools().items():
        doc = _served_doc(tool)
        args_block = doc.split("Args:", 1)[1] if "Args:" in doc else ""
        for param in inspect.signature(tool.fn).parameters:
            assert param in args_block, (
                f"{name}: parameter '{param}' not named in Args:"
            )
