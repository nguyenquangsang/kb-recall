#!/usr/bin/env python3
"""Capability probe for the Copilot CLI: can it drive the recall MCP tools?

The Copilot-side twin of `probe.py` — same one question, different harness.
Run it before reading anything into Copilot scenario results; if the CLI cannot
make one well-formed recall tool call, nothing richer is interpretable.

Scoring reads `usage.jsonl` rather than the CLI's stdout. The log is written by
the *server*, so it records a call no matter which client made it, while the
CLI's own output is prose. A byte offset marks the log's end before the run and
only what lands past it is counted.

Deliberately not isolated behind a temp HOME, unlike `probe.py`. `~/.copilot`
holds the CLI's config and its credential store lives outside HOME entirely, so
moving HOME would break auth in exchange for a clean log — and the byte offset
already gives a clean window. The cost is that nothing else may call recall
tools during a run, or its calls land in the same window; that is why this
probe is not safe to run alongside a busy Claude session.

Usage:
    uv run python scripts/smoke/probe_copilot.py
    uv run python scripts/smoke/probe_copilot.py --runs 5 --keep
"""

import argparse
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.smoke.harness import (
    records_since,
    run_copilot,
    usage_offset,
    write_mcp_config,
)

REPO = Path(__file__).resolve().parents[2]
USAGE = Path.home() / ".recall-mcp" / "usage.jsonl"

PROMPT = (
    "Call the recall MCP tool named 'list_features' with no arguments, then "
    "report exactly what it returned. Do not call any other tool."
)

# The whole probe: one call, named precisely.
EXPECTED = "list_features"


def probe_once(model: str, keep: bool) -> tuple:
    """Run one trial. Returns (passed, tools_called, detail)."""
    workdir = Path(tempfile.mkdtemp(prefix="recall-copilot-"))
    try:
        try:
            mcp_config = write_mcp_config(workdir, REPO)
        except FileNotFoundError as exc:
            return (False, [], str(exc))

        offset = usage_offset(USAGE)

        try:
            proc = run_copilot(
                repo=REPO, prompt=PROMPT, mcp_config=mcp_config, model=model
            )
        except Exception as exc:  # noqa: BLE001 - surfaced as the probe's result
            return (False, [], f"harness error: {exc}")

        # Read the log even on a non-zero exit: the CLI can call the tool and
        # still fail afterwards, and that distinction is the whole point.
        records = records_since(USAGE, offset)
        tools = [r.get("tool", "") for r in records if r.get("tool")]

        if EXPECTED not in tools:
            if proc.returncode != 0:
                detail = (proc.stderr or "").strip().splitlines()
                tail = detail[-1] if detail else "no stderr"
                return (False, tools, f"copilot exited {proc.returncode}: {tail}")
            called = ", ".join(tools) or "nothing"
            return (False, tools, f"model did not call {EXPECTED} (called: {called})")

        return (True, tools, "")
    finally:
        if keep:
            print(f"[probe_copilot] kept {workdir}")
        else:
            shutil.rmtree(workdir, ignore_errors=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--model",
        default="",
        help="Model to probe (default: let Copilot pick)",
    )
    parser.add_argument("--runs", type=int, default=1, help="Trials (default: 1)")
    parser.add_argument(
        "--keep",
        action="store_true",
        help="Keep each generated MCP config for inspection",
    )
    args = parser.parse_args()

    label = args.model or "the default model"
    print(f"Probe: does Copilot CLI ({label}) call `{EXPECTED}`?\n")

    passed = 0
    for i in range(args.runs):
        ok, tools, detail = probe_once(args.model, args.keep)
        passed += ok
        mark = "PASS" if ok else "FAIL"
        print(f"  [{mark}] run {i + 1}: {', '.join(tools) or '(no tool calls)'}")
        if detail:
            print(f"         {detail}")

    print(f"\n  rate: {passed}/{args.runs}")

    if passed == 0:
        print(
            "\n  Probe failed. Everything downstream is uninterpretable until this\n"
            "  passes — check auth (`copilot login`) and that the injected MCP\n"
            "  config names a working recall-server before reading anything into\n"
            "  scenario results."
        )
    elif passed < args.runs:
        print(
            "\n  Partial. Tool use works but is not reliable on this harness —\n"
            "  treat scenario rates as noisy and raise --runs."
        )

    return 0 if passed == args.runs else 1


if __name__ == "__main__":
    raise SystemExit(main())
