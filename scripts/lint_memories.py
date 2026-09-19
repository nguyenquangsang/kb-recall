#!/usr/bin/env python3
"""Deterministic lint over recall-mcp memories files — no LLM involved.

Server-side save_memory validation (see server.py::_validate_memory_content)
only catches bad content at write time. This script catches drift in what's
already on disk — entries written before the validation existed, or that
slip past it in ways validation can't see (dangling ID references, missing
Why/Apply structure).

Checks, all mechanical / regex-based:
1. Invalid or non-standard leading tag (reuses server.py's own tag whitelist).
2. Content too short after the tag (reuses server.py's own threshold).
3. Dangling [supersedes:XXXX] / [resolved:XXXX] references — ID doesn't match
   any [id:XXXX] entry in the same memories file.
4. Category tags ([gotcha]/[bug]/[decision]/[constraint]/[rule]) missing both
   "Why:" and "Apply:" markers — the documented What/Why/Apply structure.
5. Duplicate [id:XXXX] — two entries in the same file sharing one ID. Confirmed
   to happen live (KB `recall-mcp`, id:3236) before entry IDs were widened from
   4 to 6 hex chars (2026-07-27) — a [supersedes:XXXX]/[resolved:XXXX] referencing
   a duplicated ID hides ALL entries with that ID, not just the intended one.

Deliberately NOT doing near-duplicate/semantic-similarity detection — see
TODO.md item #29: text-overlap heuristics are prone to false positives, and an
LLM-based similarity check would double the cost of every save. Out of scope
for a "dumb but reliable" lint pass.

Read-only. Does not modify any file.
"""

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from kb_recall import server  # noqa: E402 — needs sys.path adjustment above first

ENTRY_SPLIT_RE = re.compile(r"(?m)^(?=- \*\*\d{4}-\d{2}-\d{2}\*\* )")
# report_miss entries insert an extra "[MISS]" tag between the id and the
# colon (server.py:907: "[id:{entry_id}] [MISS]: {description}") — a
# different content contract than save_memory's tagged What/Why/Apply, so
# they're parsed but excluded from the tag/structure checks below.
ENTRY_RE = re.compile(
    r"^- \*\*(\d{4}-\d{2}-\d{2})\*\* \[id:([0-9a-f]+)\](?:\s*\[(MISS)\])?:\s*(.*)",
    re.DOTALL,
)
REF_RE = re.compile(r"\[(?:supersedes|resolved):([0-9a-f]+)\]")

# Category tags that are documented to carry the full What/Why/Apply structure.
# [idea]/[pattern]/[resolved] are intentionally excluded — they're allowed to
# be shorter (an idea is a proposal, a resolved entry is a one-line closure).
STRUCTURED_TAGS = {"gotcha", "bug", "decision", "constraint", "rule"}


def parse_entries(text):
    """Yield (date, entry_id, is_miss, content) for each top-level memory entry."""
    for chunk in ENTRY_SPLIT_RE.split(text):
        chunk = chunk.strip()
        if not chunk.startswith("- **"):
            continue
        m = ENTRY_RE.match(chunk)
        if m:
            yield m.group(1), m.group(2), bool(m.group(3)), m.group(4)


def lint_file(path):
    """Return a list of finding strings for one memories*.md file."""
    text = path.read_text()
    entries = list(parse_entries(text))
    known_ids = {entry_id for _, entry_id, _, _ in entries}

    findings = []

    id_dates = {}
    for entry_date, entry_id, _, _ in entries:
        id_dates.setdefault(entry_id, []).append(entry_date)
    for entry_id, dates in sorted(id_dates.items()):
        if len(dates) > 1:
            findings.append(
                f"[id:{entry_id}] used by {len(dates)} entries ({', '.join(dates)}) — duplicate ID, "
                f"[supersedes:{entry_id}]/[resolved:{entry_id}] would hide ALL of them, not just one"
            )

    for entry_date, entry_id, is_miss, content in entries:
        tag = f"[id:{entry_id}]" + (" [MISS]" if is_miss else "")

        if is_miss:
            # report_miss's `description` is free-form prose, not the
            # save_memory tag/What-Why-Apply contract — only check references.
            for ref_id in REF_RE.findall(content):
                if ref_id not in known_ids:
                    findings.append(
                        f"{entry_date} {tag}: references [supersedes/resolved:{ref_id}] "
                        f"which does not exist in this file (dangling reference)"
                    )
            continue

        error = server._validate_memory_content(content)
        if error:
            findings.append(f"{entry_date} {tag}: {error}")
            continue  # skip further checks — content already flagged invalid

        for ref_id in REF_RE.findall(content):
            if ref_id not in known_ids:
                findings.append(
                    f"{entry_date} {tag}: references [supersedes/resolved:{ref_id}] "
                    f"which does not exist in this file (dangling reference)"
                )

        tag_m = re.match(r"^\*{0,2}\[([a-zA-Z]+)", content.strip())
        primary_tag = tag_m.group(1) if tag_m else ""
        if primary_tag in STRUCTURED_TAGS:
            has_why = "Why:" in content
            has_apply = "Apply:" in content
            if not (has_why and has_apply):
                missing = [
                    n
                    for n, present in (("Why:", has_why), ("Apply:", has_apply))
                    if not present
                ]
                findings.append(
                    f"{entry_date} {tag}: [{primary_tag}] entry missing {', '.join(missing)} "
                    f"— incomplete What/Why/Apply structure"
                )

    return findings


def supersede_stats(path):
    """Return (total, superseded_count, same_day_count) for one memories file.

    "superseded" = entry is the TARGET of a [supersedes:XXXX]/[resolved:XXXX]
    reference from some other entry — i.e. it was later revised or retired.
    "same_day" = the earliest entry referencing it was saved the same date as
    the original — a rough proxy for "caught and fixed almost immediately"
    (more likely a real correction) vs. a later supersede (more likely natural
    decision evolution, not necessarily an error). Not a proxy for truth, just
    a directional signal — see TODO.md item on measurability (2026-07-14).
    """
    entries = list(parse_entries(path.read_text()))
    by_id = {eid: entry_date for entry_date, eid, _, _ in entries}
    total = len(entries)
    if total == 0:
        return 0, 0, 0

    earliest_ref_date = {}
    for entry_date, _, _, content in entries:
        for ref_id in REF_RE.findall(content):
            if ref_id in by_id:
                if (
                    ref_id not in earliest_ref_date
                    or entry_date < earliest_ref_date[ref_id]
                ):
                    earliest_ref_date[ref_id] = entry_date

    same_day = sum(
        1
        for target_id, ref_date in earliest_ref_date.items()
        if by_id[target_id] == ref_date
    )
    return total, len(earliest_ref_date), same_day


def main():
    kb_root = server.KB_ROOT
    total_files = 0
    total_entries = 0
    total_findings = 0
    total_superseded = 0
    total_same_day = 0

    for project_dir in sorted(kb_root.iterdir()):
        if not project_dir.is_dir():
            continue
        for memories_file in sorted(project_dir.glob("*/memories-*.md")):
            total_files += 1
            entries = list(parse_entries(memories_file.read_text()))
            total_entries += len(entries)
            findings = lint_file(memories_file)
            if findings:
                total_findings += len(findings)
                rel = memories_file.relative_to(kb_root)
                print(f"\n{rel}  ({len(findings)} finding(s))")
                for f in findings:
                    print(f"  - {f}")

            _, superseded, same_day = supersede_stats(memories_file)
            total_superseded += superseded
            total_same_day += same_day

    print(f"\n{'=' * 70}")
    print(f"Scanned {total_files} memories files, {total_entries} entries total.")
    print(f"{total_findings} finding(s) across all files.")
    if total_entries:
        print(f"({100 * total_findings / total_entries:.1f}% of entries flagged)")

    print("\n--- Supersede/revision rate (see 2026-07-14 measurability proposal) ---")
    if total_entries:
        print(
            f"{total_superseded}/{total_entries} entries later superseded/resolved "
            f"({100 * total_superseded / total_entries:.1f}%)"
        )
        print(
            f"  of which {total_same_day} same-day ({100 * total_same_day / max(1, total_superseded):.1f}% "
            f"of superseded) — rough proxy for fast self-correction vs. later natural revision"
        )
        print(
            f"  same-day / all entries = {100 * total_same_day / total_entries:.1f}% "
            f"— directional error-rate estimate, not a precise measurement"
        )


if __name__ == "__main__":
    main()
