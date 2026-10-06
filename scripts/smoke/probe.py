#!/usr/bin/env python3
"""Minimal capability probe: can this model drive the recall MCP tools at all?

Answers exactly one question, and is meant to be run before anything else in
this directory. If the model cannot make a single well-formed tool call in this
harness, every richer scenario is uninterpretable — a failure there would say
nothing about the KB's instruction surface.

Deliberately narrow: one prompt, no branch or KB seeding, one assertion. The
general conformance checker is `score.py`; this is the gate in front of it.

Usage:
    uv run python scripts/smoke/probe.py                     # default flash model
    uv run python scripts/smoke/probe.py --runs 5 --keep     # 5 trials, keep the stores
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.smoke.harness import (
    called_tools,
    isolated_home,
    load_usage,
    run_claude,
    write_mcp_config,
)

REPO = Path(__file__).resolve().parents[2]

PROMPT = (
    "Call the recall MCP tool `list_features` with no arguments, then report "
    "exactly what it returned. Do not call any other tool."
)

# The whole probe: one call, named precisely.
EXPECTED = "list_features"

# Claude Code warns about an unrecognised model for its background
# session-title query on *every* run, passing ones included. Quoting the last
# stderr line therefore reports this warning as the cause of failures it has
# nothing to do with — which is exactly what a first version of this probe did.
NOISE = ("generate_session_title", "unrecognized_model")


def _error_detail(proc) -> str:
    """Pick the line that actually explains a non-zero exit."""
    for stream in (proc.stderr, proc.stdout):
        lines = [line for line in (stream or "").splitlines() if line.strip()]
        real = [line for line in lines if not any(n in line for n in NOISE)]
        if real:
            return real[-1]
    return "no diagnostic output (only known session-title noise)"


def probe_once(model: str, keep: bool) -> tuple:
    """Run one trial. Returns (passed, tools_called, detail)."""
    with isolated_home(REPO, keep=keep) as home:
        mcp_config = write_mcp_config(home, REPO)
        try:
            proc = run_claude(
                home=home,
                repo=REPO,
                prompt=PROMPT,
                model=model,
                mcp_config=mcp_config,
                allowed_tools=["mcp__recall"],
            )
        except Exception as exc:  # noqa: BLE001 - surfaced as the probe's result
            return (False, [], f"harness error: {exc}")

        tools = called_tools(load_usage(home))

        if proc.returncode != 0:
            return (
                False,
                tools,
                f"claude exited {proc.returncode}: {_error_detail(proc)}",
            )

        if EXPECTED not in tools:
            return (
                False,
                tools,
                f"model did not call {EXPECTED} (called: {', '.join(tools) or 'nothing'})",
            )

        return (True, tools, "")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--model",
        default="deepseek-v4-flash",
        help="Model to probe (default: deepseek-v4-flash)",
    )
    parser.add_argument("--runs", type=int, default=1, help="Trials (default: 1)")
    parser.add_argument(
        "--keep",
        action="store_true",
        help="Keep each isolated HOME so usage.jsonl can be inspected",
    )
    args = parser.parse_args()

    print(f"Probe: does {args.model} call `{EXPECTED}` in Claude Code?\n")
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
            "  passes — check that the model supports tool use in this harness\n"
            "  before reading anything into the scenario results."
        )
    elif passed < args.runs:
        print(
            "\n  Partial. Tool use works but is not reliable at this model — treat\n"
            "  scenario rates as noisy and raise --runs."
        )

    return 0 if passed == args.runs else 1


if __name__ == "__main__":
    raise SystemExit(main())
