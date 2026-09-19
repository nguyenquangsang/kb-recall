#!/usr/bin/env python3
"""Diagnostic pass over recall-mcp usage data — discovery first, no fixed
metric committed upfront.

Two data sources:
1. ~/.recall-mcp/usage.jsonl       — recall-mcp's own tool-call log.
2. ~/.claude/projects/*/*.jsonl    — Claude Code session transcripts, mined for
   the recall-mcp hook's "Save check:" / "Miss check:" lines and whether an
   actual save_memory/report_miss tool_use followed before the user's next turn.

Read-only. Does not call any MCP tool, does not modify any file.
"""

import json
import re
from collections import Counter, defaultdict
from pathlib import Path

USAGE_LOG = Path.home() / ".recall-mcp" / "usage.jsonl"
TRANSCRIPT_ROOT = Path.home() / ".claude" / "projects"

# "Nothing found" prefixes, derived from the hook's own prompt text (grepped
# from transcripts: `or exactly \"...\"`) plus older/rotated wordings seen in
# practice (e.g. "nothing new to save this round"). Matched by startswith on
# the lowercased text, not exact equality — the model sometimes appends extra
# rationale after the canned phrase (e.g. "nothing this round — still
# exploring"), which is still a quiet outcome, not a claimed finding.
NEGATIVE_PREFIXES = (
    "nothing",
    "quiet round",
    "clean run",
    "clean streak",
    "nada, moving on",
    "no misses spotted",
    "kb did its job",
)

# Anchored: a genuine hook firing is the model's own standalone line, never
# embedded in a longer sentence/quote (rules out meta-discussion like
# 'Turn 18: "[recall-mcp] Save check: ..." — NOT a tool call' or documentation
# text quoting the hook's own placeholder, e.g. '`[recall-mcp] Save check:
# <1-line...>`'). Confirmed by inspecting the excluded matches by hand.
SAVE_RE = re.compile(r"^\[recall-mcp\] Save check:\s*(.+)$")
MISS_RE = re.compile(r"^\[recall-mcp\] Miss check:\s*(.+)$")


def is_negative(claimed_text: str) -> bool:
    return claimed_text.strip().lower().startswith(NEGATIVE_PREFIXES)


def load_usage(path):
    records = []
    if not path.exists():
        return records
    with path.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return records


def analyze_usage(records):
    # Call volume: count from ALL records — legacy rows missing `status` still
    # have `tool`, so excluding them here would silently undercount real call
    # volume (e.g. report_miss showed as "1 call" when 4/5 of its calls
    # predate the status field, all bundled into the "legacy" total instead).
    with_status = [r for r in records if "status" in r]
    without_status = len(records) - len(with_status)

    by_tool = Counter(r.get("tool", "?") for r in records)
    # Error rate needs `status` to be meaningful — computed on the with_status
    # subset only, and reported as a fraction of that subset per tool, not of
    # total calls (a legacy call has unknown status, not "ok").
    errors_by_tool = Counter(
        r.get("tool", "?") for r in with_status if r.get("status") == "error"
    )
    status_known_by_tool = Counter(r.get("tool", "?") for r in with_status)
    by_project = Counter(r.get("project", "") for r in with_status)

    durations = defaultdict(list)
    for r in with_status:
        if "duration_ms" in r:
            durations[r.get("tool", "?")].append(r["duration_ms"])

    total_errors = sum(errors_by_tool.values())

    print("=" * 70)
    print("SOURCE 1: usage.jsonl")
    print("=" * 70)
    print(
        f"Total records: {len(records)}  (legacy/no-status: {without_status}, "
        f"usable for error-rate: {len(with_status)})"
    )
    print(
        f"Overall error rate (usable records only): "
        f"{total_errors}/{len(with_status)} = "
        f"{100 * total_errors / max(1, len(with_status)):.1f}%"
    )
    print()
    print(
        f"{'tool':<28}{'calls':>8}{'w/status':>10}{'errors':>8}{'err%':>8}{'p50 ms':>10}{'p95 ms':>10}"
    )
    for tool, count in by_tool.most_common():
        known = status_known_by_tool.get(tool, 0)
        errs = errors_by_tool.get(tool, 0)
        ds = sorted(durations.get(tool, []))
        p50 = ds[len(ds) // 2] if ds else 0
        p95 = ds[int(len(ds) * 0.95)] if ds else 0
        err_pct = f"{100 * errs / known:.1f}%" if known else "n/a"
        print(f"{tool:<28}{count:>8}{known:>10}{errs:>8}{err_pct:>8}{p50:>10}{p95:>10}")
    print()
    print("By project (status-known records only):")
    for proj, count in by_project.most_common():
        print(f"  {proj or '(empty)'}: {count}")
    print()


def is_real_user_turn(entry):
    """True if this 'user' entry is an actual typed prompt, not a tool_result echo."""
    msg = entry.get("message", {})
    content = msg.get("content")
    if isinstance(content, str):
        return True
    if isinstance(content, list):
        # a real user turn has no tool_result blocks (those are our own tool
        # outputs being fed back to the assistant, not user input)
        return not any(
            b.get("type") == "tool_result" for b in content if isinstance(b, dict)
        )
    return False


def scan_transcript(path):
    """Return list of dicts: {kind: 'save'|'miss', claimed: bool, complied: bool}.

    Save check and Miss check can both fire in the same turn (both lines
    printed before either tool call happens), so they're tracked as two
    independent pending slots — closing one must never discard the other.
    """
    events = []
    pending = {"save": None, "miss": None}

    def finalize(kind):
        if pending[kind] is not None:
            events.append(pending[kind])
            pending[kind] = None

    def finalize_all():
        finalize("save")
        finalize("miss")

    try:
        with path.open() as f:
            lines = f.readlines()
    except OSError:
        return events

    for raw in lines:
        raw = raw.strip()
        if not raw:
            continue
        try:
            entry = json.loads(raw)
        except json.JSONDecodeError:
            continue

        etype = entry.get("type")

        if etype == "user" and is_real_user_turn(entry):
            # a new genuine user prompt closes out any unresolved checks
            finalize_all()
            continue

        if etype != "assistant":
            continue

        content = entry.get("message", {}).get("content")
        if not isinstance(content, list):
            continue

        for block in content:
            if not isinstance(block, dict):
                continue
            btype = block.get("type")

            if btype == "text":
                text = block.get("text", "")
                for line in text.splitlines():
                    m_save = SAVE_RE.match(line.strip())
                    m_miss = MISS_RE.match(line.strip())
                    if m_save:
                        finalize("save")  # close prior unresolved save check only
                        claimed = not is_negative(m_save.group(1))
                        pending["save"] = {
                            "kind": "save",
                            "claimed": claimed,
                            "complied": not claimed,
                        }
                    elif m_miss:
                        finalize("miss")
                        claimed = not is_negative(m_miss.group(1))
                        pending["miss"] = {
                            "kind": "miss",
                            "claimed": claimed,
                            "complied": not claimed,
                        }

            elif btype == "tool_use":
                name = block.get("name", "")
                p_save = pending["save"]
                p_miss = pending["miss"]
                if p_save is not None and p_save["claimed"] and not p_save["complied"]:
                    if name == "mcp__recall__save_memory":
                        p_save["complied"] = True
                if p_miss is not None and p_miss["claimed"] and not p_miss["complied"]:
                    if name == "mcp__recall__report_miss":
                        p_miss["complied"] = True

    finalize_all()
    return events


def analyze_transcripts(root):
    print("=" * 70)
    print("SOURCE 2: Claude Code transcripts — hook trigger vs actual call")
    print("=" * 70)

    if not root.exists():
        print(f"(no transcript root at {root})")
        return

    all_events = []
    files_scanned = 0
    for project_dir in sorted(root.iterdir()):
        if not project_dir.is_dir():
            continue
        for jf in project_dir.glob("*.jsonl"):
            files_scanned += 1
            all_events.extend(scan_transcript(jf))

    print(f"Session files scanned: {files_scanned}")
    print(f"Total Save/Miss check firings observed: {len(all_events)}")
    print()

    for kind in ("save", "miss"):
        subset = [e for e in all_events if e["kind"] == kind]
        claimed = [e for e in subset if e["claimed"]]
        complied = [e for e in claimed if e["complied"]]
        quiet = len(subset) - len(claimed)
        label = "Save check" if kind == "save" else "Miss check"
        tool = "save_memory" if kind == "save" else "report_miss"
        print(
            f"{label}: {len(subset)} firings — {quiet} quiet, {len(claimed)} claimed a finding"
        )
        if claimed:
            rate = 100 * len(complied) / len(claimed)
            print(
                f"  -> of claimed findings, {len(complied)}/{len(claimed)} "
                f"({rate:.1f}%) were followed by an actual {tool} call"
            )
            gap = len(claimed) - len(complied)
            if gap:
                print(
                    f"  -> {gap} claimed finding(s) NEVER resulted in a {tool} call "
                    f"(compliance gap)"
                )
        print()


def main():
    print("recall-mcp effectiveness — discovery pass (read-only)\n")
    analyze_usage(load_usage(USAGE_LOG))
    analyze_transcripts(TRANSCRIPT_ROOT)


if __name__ == "__main__":
    main()
