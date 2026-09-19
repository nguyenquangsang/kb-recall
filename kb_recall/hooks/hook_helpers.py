"""Pure helper functions for the recall-mcp UserPromptSubmit hook.

Extracted for testability — no module-level side effects here.
All functions receive their dependencies as parameters.
"""

import json
from datetime import datetime, timezone
from pathlib import Path


def load_asked(sid: str, state_file: Path) -> set:
    """Return the set of slugs already prompted-about this session (deduped)."""
    if not state_file.exists():
        return set()
    asked = set()
    for line in state_file.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
            if entry.get("session_id") == sid:
                asked.add(entry.get("slug", ""))
        except Exception:
            pass
    return asked


def mark_asked(sid: str, slug: str, state_file: Path) -> None:
    """Append slug to session-state (dedup guard — prevents repeating prompts)."""
    with state_file.open("a") as f:
        f.write(json.dumps({"session_id": sid, "slug": slug}) + "\n")
    lines = state_file.read_text().splitlines()
    if len(lines) > 500:
        state_file.write_text("\n".join(lines[-500:]) + "\n")


def append_turn(sid: str, state_file: Path) -> None:
    """Append one __turn__ entry — intentionally not deduped, used as a counter.

    Also stamps a wall-clock `ts` (ISO 8601 UTC) — a self-controlled fallback for
    idle-gap detection when the transcript JSONL can't be read/parsed (its schema
    isn't officially documented by Claude Code, so it shouldn't be the only source).
    """
    entry = {
        "session_id": sid,
        "slug": "__turn__",
        "ts": datetime.now(timezone.utc).isoformat(),
    }
    with state_file.open("a") as f:
        f.write(json.dumps(entry) + "\n")
    lines = state_file.read_text().splitlines()
    if len(lines) > 500:
        state_file.write_text("\n".join(lines[-500:]) + "\n")


def last_turn_ts(sid: str, state_file: Path):
    """Return the datetime of the most recent __turn__ entry for this session.

    Self-tracked fallback for idle-gap detection — used only when the transcript
    JSONL is missing or doesn't parse as expected.
    """
    if not state_file.exists():
        return None
    last = None
    for line in state_file.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except Exception:
            continue
        if (
            entry.get("session_id") == sid
            and entry.get("slug") == "__turn__"
            and entry.get("ts")
        ):
            try:
                last = datetime.fromisoformat(entry["ts"])
            except Exception:
                continue
    return last


def last_size_warn_tokens(sid: str, state_file: Path):
    """Return the context_tokens value from the most recent __sizewarn__ entry
    for this session, or None if it was never fired.

    Used to re-fire the idle-independent size nudge every SIZE_WARN_STEP_TOKENS
    of further growth, instead of only once per session.
    """
    if not state_file.exists():
        return None
    last = None
    for line in state_file.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except Exception:
            continue
        if (
            entry.get("session_id") == sid
            and entry.get("slug") == "__sizewarn__"
            and "tokens" in entry
        ):
            last = entry["tokens"]
    return last


def mark_size_warn(sid: str, state_file: Path, tokens: int) -> None:
    """Record that the size-only nudge fired at this context_tokens value."""
    entry = {"session_id": sid, "slug": "__sizewarn__", "tokens": tokens}
    with state_file.open("a") as f:
        f.write(json.dumps(entry) + "\n")
    lines = state_file.read_text().splitlines()
    if len(lines) > 500:
        state_file.write_text("\n".join(lines[-500:]) + "\n")


def parse_transcript_idle(transcript_path: str):
    """Read a Claude Code transcript JSONL for the last real user-turn timestamp
    and the last assistant usage stats (context size).

    Returns (timestamp, usage_dict) — either may be None if the file is missing,
    unparseable, or doesn't contain the expected fields. Distinguishes genuine
    user prompts (message.content is a string, or a list with no tool_result
    items) from tool-result entries, which are also stored with type=="user".
    """
    if not transcript_path:
        return None, None
    path = Path(transcript_path)
    if not path.exists():
        return None, None
    try:
        lines = path.read_text().splitlines()
    except Exception:
        return None, None

    last_user_ts = None
    last_usage = None
    for line in reversed(lines):
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except Exception:
            continue

        if last_user_ts is None and entry.get("type") == "user":
            content = entry.get("message", {}).get("content")
            is_real_prompt = isinstance(content, str) or (
                isinstance(content, list)
                and content
                and all(
                    isinstance(c, dict) and c.get("type") != "tool_result"
                    for c in content
                )
            )
            if is_real_prompt:
                ts_str = entry.get("timestamp", "")
                try:
                    last_user_ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                except Exception:
                    pass

        if last_usage is None and entry.get("type") == "assistant":
            usage = entry.get("message", {}).get("usage")
            if isinstance(usage, dict):
                last_usage = usage

        if last_user_ts is not None and last_usage is not None:
            break

    return last_user_ts, last_usage


def idle_gap_seconds(sid: str, state_file: Path, transcript_path: str):
    """Return (idle_seconds, context_tokens, source) describing the gap since the
    previous turn.

    Tries the transcript JSONL first — real timestamps plus real cache/context
    usage stats, so callers can gate blocking on actual context size instead of
    estimating. Falls back to the self-written __turn__ ts (source=="self") if
    the transcript is missing, unparseable, or lacks the expected fields. All
    three return values are None when nothing is available (e.g. first turn of
    a session).
    """
    now = datetime.now(timezone.utc)
    last_ts, last_usage = parse_transcript_idle(transcript_path)
    source = "transcript" if last_ts else None
    if last_ts is None:
        last_ts = last_turn_ts(sid, state_file)
        source = "self" if last_ts else None
    if last_ts is None:
        return None, None, None

    if last_ts.tzinfo is None:
        last_ts = last_ts.replace(tzinfo=timezone.utc)
    idle_seconds = (now - last_ts).total_seconds()

    context_tokens = None
    if last_usage:
        context_tokens = (
            last_usage.get("input_tokens", 0)
            + last_usage.get("cache_read_input_tokens", 0)
            + last_usage.get("cache_creation_input_tokens", 0)
        )
    return idle_seconds, context_tokens, source


def parse_pipe_table(text: str) -> list[dict]:
    """Parse a markdown pipe table into a list of {lowercased_header: cell} dicts.

    The first `|`-starting line is treated as the header row. Skips
    non-table lines, the separator row (e.g. `|---|---|`), and any data
    row with fewer cells than the header (malformed/truncated row).
    """
    rows = []
    header = None
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("|"):
            continue
        cols = [c.strip() for c in line.split("|")[1:-1]]
        if not cols:
            continue
        if header is None:
            header = [c.lower() for c in cols]
            continue
        if all(not c.replace("-", "").replace(":", "").strip() for c in cols):
            continue  # separator row
        if len(cols) < len(header):
            continue  # malformed row — fewer cells than header
        rows.append(dict(zip(header, cols)))
    return rows


def find_slug_for_branch(table_text: str, branch: str) -> str | None:
    """Look up which feature slug a git branch maps to, from a features.md-style table.

    Requires a header column named exactly "slug" and any column whose name
    contains "branch" (e.g. "Branch(es)"). A branch cell may list multiple
    comma-separated branches. Returns None if the header/column/match is missing.
    """
    rows = parse_pipe_table(table_text)
    if not rows:
        return None
    header = list(rows[0].keys())
    if "slug" not in header:
        return None
    branch_col = next((h for h in header if "branch" in h), None)
    if branch_col is None:
        return None
    for row in rows:
        branches = [b.strip() for b in row.get(branch_col, "").split(",") if b.strip()]
        if branch in branches:
            return row.get("slug")
    return None


def find_malformed_rows(text: str) -> list[str]:
    """Return raw text of data rows whose cell count doesn't match the header.

    `parse_pipe_table` silently drops rows like this (by design — a
    corrupted row shouldn't crash the hook), which means table corruption
    (e.g. a miscounted freehand edit to features.md's Branch(es) cell) would
    otherwise have zero visible signal. This surfaces that signal instead of
    swallowing it. Only flags cell-count MISMATCHES, not "genuinely no
    match" rows — a well-formed row that just doesn't list a given branch is
    normal, not corruption.
    """
    header = None
    malformed = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped.startswith("|"):
            continue
        cols = [c.strip() for c in stripped.split("|")[1:-1]]
        if not cols:
            continue
        if header is None:
            header = cols
            continue
        if all(not c.replace("-", "").replace(":", "").strip() for c in cols):
            continue  # separator row
        if len(cols) != len(header):
            malformed.append(stripped)
    return malformed


def pick_active_slug(asked: set) -> str | None:
    """Deterministically pick a real (non-dunder) slug out of `asked`.

    Sorted, not hash-order: prompt_submit.py runs as a fresh subprocess every
    turn, and CPython randomizes str hashing per-process, so iterating `asked`
    (a set) directly would pick a different slug turn to turn whenever 2+ are
    active in the same session, even with unchanged state.
    """
    for s in sorted(asked):
        if not s.startswith("__"):
            return s
    return None


def count_turns(sid: str, state_file: Path) -> int:
    if not state_file.exists():
        return 0
    count = 0
    for line in state_file.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
            if entry.get("session_id") == sid and entry.get("slug") == "__turn__":
                count += 1
        except Exception:
            pass
    return count
