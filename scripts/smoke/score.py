#!/usr/bin/env python3
"""Score a recorded run against a scenario's expected tool-call signature.

This is the conformance half of the smoke suite — the descriptive half is
`scripts/analyze_effectiveness.py`, which makes no assumptions and just reports
what the data contains. Here a scenario states what *should* have happened, so
an absent call scores as a failure instead of looking like silence.

The scoring function is pure: usage records in, verdict out. That is deliberate
— model behaviour is non-deterministic and can only be measured as a rate, but
the scorer itself is ordinary code and is unit-tested in
`tests/test_smoke_scorer.py`. Keep the two apart; do not move the model into a
pytest assertion.

Usage:
    uv run python scripts/smoke/score.py --scenario scenarios/load-on-branch.json run1.jsonl run2.jsonl
"""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.smoke.harness import called_tools, load_usage_file

SCENARIO_DIR = Path(__file__).parent / "scenarios"


def is_subsequence(needle, haystack) -> bool:
    """True if `needle` appears in `haystack` in order, gaps allowed.

    Gaps are allowed because an extra exploratory call (`list_features` before
    the real one) is not a failure — the signature describes what must happen,
    not everything that may.
    """
    remaining = iter(haystack)
    return all(any(called == wanted for called in remaining) for wanted in needle)


def score_run(records, scenario) -> tuple:
    """Return (passed, reasons). Empty reasons means passed."""
    called = called_tools(records)
    reasons = []

    for tool in scenario.get("forbids", []):
        if tool in called:
            reasons.append(f"called a forbidden tool: {tool}")

    for sequence in scenario.get("requires", []):
        if not is_subsequence(sequence, called):
            reasons.append("missing sequence: " + " -> ".join(sequence))

    minimum = scenario.get("min_calls")
    if minimum is not None and len(called) < minimum:
        reasons.append(f"only {len(called)} tool call(s), expected >= {minimum}")

    return (not reasons, reasons)


def load_scenario(path) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenario", required=True, help="Scenario JSON file")
    parser.add_argument(
        "runs",
        nargs="+",
        help="usage.jsonl files recorded for this scenario (one per trial)",
    )
    parser.add_argument("--json", action="store_true", help="Machine-readable output")
    args = parser.parse_args()

    scenario = load_scenario(args.scenario)
    results = []
    for run in args.runs:
        records = load_usage_file(run)
        passed, reasons = score_run(records, scenario)
        results.append(
            {
                "run": str(run),
                "calls": called_tools(records),
                "passed": passed,
                "reasons": reasons,
            }
        )

    passed_count = sum(1 for r in results if r["passed"])
    total = len(results)
    rate = (passed_count / total) if total else 0.0

    if args.json:
        print(
            json.dumps(
                {
                    "scenario": scenario.get("name", str(args.scenario)),
                    "passed": passed_count,
                    "total": total,
                    "rate": round(rate, 3),
                    "runs": results,
                },
                indent=2,
            )
        )
    else:
        print(f"Scenario: {scenario.get('name', args.scenario)}")
        print(f"  {scenario.get('description', '')}\n")
        for result in results:
            mark = "PASS" if result["passed"] else "FAIL"
            print(f"  [{mark}] {result['run']}")
            print(f"         calls: {', '.join(result['calls']) or '(none)'}")
            for reason in result["reasons"]:
                print(f"         - {reason}")
        print(f"\n  rate: {passed_count}/{total} ({rate:.0%})")
        if total < 5:
            print("  ⚠ under 5 trials — a rate this small is anecdote, not signal.")

    return 0 if passed_count == total else 1


if __name__ == "__main__":
    raise SystemExit(main())
