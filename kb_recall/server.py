#!/usr/bin/env python3
"""Recall MCP — Feature Knowledge Base server for Claude Code.

Tools:
  list_features        — list all features across configured projects
  search_features      — keyword search across all features' README + memories
  load_feature_context — load full context for a specific feature
  save_memory          — prepend an insight to a feature's memories.md
  update_readme        — replace or append content in a README section
  init_feature         — create a new feature KB directory
  report_miss          — record a context miss to memories.md
"""

import difflib
import json
import re
import secrets
import subprocess
import time
from datetime import date, datetime, timezone
from pathlib import Path
from string import Template

from mcp.server.mcpserver import MCPServer

CONFIG_PATH = Path.home() / ".recall-mcp" / "config.json"
KB_ROOT = Path.home() / ".recall-mcp"
LOG_FILE = KB_ROOT / "usage.log"
LOG_JSONL = KB_ROOT / "usage.jsonl"
TEMPLATES_DIR = Path(__file__).parent / "templates"

mcp = MCPServer("recall")

SECTION_PRIORITY = {
    "critical_warnings": 4,
    "business_rules": 3,
    "architecture": 2,
    "technical_stack": 2,
    "key_files": 2,
    "overview": 1,
    "open_items": 1,
    "checklist": 1,
    "related_tickets": 1,
}
# memories is not a README <section>; the budget render admits it as its own
# top-level block at the same tier as the low-value sections, and LAST among ties
# — it is the bulkiest and most redundant with the promoted README content.
_MEMORIES_PRIORITY = 1
MAX_SEARCH_RESULTS = 20
SEARCH_SNIPPET_HALF_WINDOW = 100  # chars of context each side of a match in a snippet
SEARCH_RESULT_CHAR_LIMIT = (
    20_000  # defensive total-size backstop, even with per-hit caps
)
# OR-matched keywords this common would otherwise dominate hit counts on their own
# (measured: "a" alone matched 280 lines, "not" 221, in a real 272-hit query where the
# actual distinctive keyword matched only 3) — dropped before matching, not just advised
# against, since FORMAT guidance alone didn't stop it in ~39 real calls.
_SEARCH_STOPWORDS = frozenset(
    {
        "a",
        "an",
        "the",
        "is",
        "are",
        "was",
        "were",
        "be",
        "been",
        "being",
        "of",
        "to",
        "in",
        "on",
        "at",
        "for",
        "with",
        "not",
        "no",
        "do",
        "does",
        "did",
        "this",
        "that",
        "it",
        "its",
        "as",
        "by",
        "or",
        "and",
        "from",
        "has",
        "have",
        "had",
        "but",
        "if",
        "so",
        "than",
        "then",
        "there",
        "their",
        "into",
        "about",
        "can",
        "will",
        "would",
        "could",
        "should",
        "i",
        "you",
        "we",
        "they",
    }
)

# KB health hint thresholds (used by _kb_health_hints in load_feature_context)
KB_MEMORIES_COMPACT_THRESHOLD = 20_000  # visible chars → hint /recall:compact
KB_SECTION_TIDY_ENTRY_THRESHOLD = 10  # entries in critical_warnings → hint /recall:tidy
KB_UNPROMOTED_GATING_THRESHOLD = 8  # live gating memories → hint to promote to README

# architecture/business_rules use char thresholds, not entry counts — architecture
# is prose by design (18/22 real KBs), so counting "**[" tag markers
# there systematically undercounts. business_rules IS tag-based like
# critical_warnings, so its threshold is pegged to the same 10-entry budget
# (avg real entry size on this machine: business_rules 364 chars/entry, vs.
# critical_warnings 566 chars/entry x 10 = 5,660 chars) — not to a population
# quantile, since that produced a threshold that fired far earlier than
# critical_warnings' for the same kind of content. architecture has no
# entries to peg to, so it's set near the midpoint of the other two budgets.
KB_ARCHITECTURE_TIDY_CHAR_THRESHOLD = 4_600  # ~midpoint of the two budgets below
KB_BUSINESS_RULES_TIDY_THRESHOLD = 3_600  # ~10 entries x 364 chars/entry avg

# S4 dedup: Jaccard similarity over distinctive tokens above which a promotion is
# skipped as near-duplicate. Unmeasured default — the 0.5/0.6/0.7 probe
# range was never run, so this errs toward high precision (only clearly
# near-identical blocks) to avoid suppressing legitimate promotions. Tune after
# OI-2's measurement. The gate is promotion-only: it never blocks the save itself,
# and it never blocks a [supersedes] correction (net-negative, replaces a block).
KB_PROMOTE_DEDUP_JACCARD_THRESHOLD = 0.7

# Save-time dedup (S4's sibling, over live memory entries instead of README
# blocks): the "Never call twice for the same insight" docstring, enforced.
# Set HIGHER than the promotion threshold (0.7) deliberately — the promotion
# gate compares a promoted body against a section's existing blocks and can
# afford a lower bar (worst case is a skipped promotion, which is loud), while
# this one hard-rejects a save, so a false positive here costs a real entry.
# 0.8 only fires on near-verbatim repeats; a genuine same-domain but distinct
# insight scores far below it (see decision 9a6ea7: real pairwise Jaccard tops
# out at 0.215 on the README corpus).
KB_SAVE_DEDUP_JACCARD_THRESHOLD = 0.8

# The single response-size ceiling. Empirically bisected on this harness: a
# load_feature_context response of 48,146 chars succeeded, 56,078 failed with
# "exceeds maximum allowed tokens" (KB `improve` hit this for real — 144,596 chars,
# hard failure, no recovery path since /recall:compact/tidy both depend on this
# same call). Kept under the lowest observed success point (48k), not just the
# failure point, since real text can tokenize denser than the synthetic content
# used to bisect.
# This ONE number is every size gate — the unbudgeted render cap, the budgeted
# render's ceiling, the save-side hard limit, and the index-all threshold — so
# they cannot drift apart. Copilot never uses the unbudgeted path; its hook passes
# max_chars=SESSION_START_BUDGET_CHARS (~9k).
KB_FULL_RENDER_LIMIT_CHARS = 45_000

# Growth ceiling on FULL-BODY size, independent of index-all. Index-all makes a
# KB over the hard limit loadable via titles, but it also removes the growth
# pressure the hard limit used to provide — full-body could grow unbounded while
# the title-only gate metric stays small. This caps that: a write whose full-body
# (README + all live memory bodies + the new entry) would exceed this is refused,
# so compaction is still forced even when the KB renders fine as titles.
# The ceiling must never sit above KB_FULL_RENDER_LIMIT_CHARS: once full-body
# passes that point the unbudgeted load refuses to render, so growth past it makes
# the KB unloadable with no save-side refusal. Derived, not a second literal, so
# the two cannot drift apart.
KB_FULL_BODY_GROWTH_LIMIT_CHARS = KB_FULL_RENDER_LIMIT_CHARS

# Per-ENTRY size guard, shared by both memory writers (save_memory, report_miss).
# Every gate above measures the whole KB, so a single oversized write passes
# silently on a small KB and then eats a large slice of the budget, breaks the
# Jaccard dedup (a blob matches nothing), and makes expand_ids return a huge
# block. Two tiers, mirroring the dedup philosophy of "hard-reject errs toward
# precision": a soft limit that only appends an advisory (never loses a real
# entry), and a hard ceiling that rejects the "model dumped a whole file" case.
# Calibrated on the live store (2026-10-03: 57 substantive entries) — a well-formed
# What/Why/Apply entry lands ~1,200 chars with its three parts balanced at ~400
# each, so 1,500 flags only the upper tail (p90 of substantive = 1,799) and 4,000
# caps one write at under 9% of KB_FULL_RENDER_LIMIT_CHARS.
KB_MEMORY_ENTRY_SOFT_LIMIT_CHARS = 1_500
KB_MEMORY_ENTRY_HARD_LIMIT_CHARS = 4_000

# Cap on the memories index (title-only list) reserved in a budgeted render's
# fixed frame. The index is always shown (it's the pointer to expand_ids bodies),
# so without a cap it grows with entry count and starves README sections of room
# in Copilot's ~9k budget. Titles are newest-first (entries are date-descending),
# so the cap keeps the most recent; the rest are reachable via search_features.
KB_INDEX_TITLE_BUDGET_CHARS = 2_000


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


def _projects() -> list[Path]:
    if not CONFIG_PATH.exists():
        return []
    data = json.loads(CONFIG_PATH.read_text())
    return [Path(p).expanduser().resolve() for p in data.get("projects", [])]


def _filter(projects: list[Path], name: str) -> list[Path]:
    if not name:
        return projects
    return [p for p in projects if p.name == name or str(p) == name]


def _kb_root(proj: Path) -> Path:
    return KB_ROOT / proj.name


def _detect_session_project() -> str:
    """Detect current project from CWD at server startup."""
    cwd = Path.cwd().resolve()
    for proj in _projects():
        if proj == cwd or cwd.is_relative_to(proj):
            return proj.name
    return ""


_SESSION_PROJECT: str = _detect_session_project()


def _active_project(project: str = "") -> list[Path]:
    """Resolve which configured project(s) a slug-scoped tool call is limited to.

    Falls back to the session's detected project when `project` is omitted.
    If neither is available (cwd didn't match any configured project, and no
    explicit project= was passed), returns an empty list — fail closed
    instead of silently widening to every configured project.
    """
    name = project or _SESSION_PROJECT
    if not name:
        return []
    return _filter(_projects(), name)


def _has_recall_setup(project_path: Path) -> bool:
    for name in ("CLAUDE.local.md", "CLAUDE.md"):
        f = project_path / name
        if f.exists() and "recall-mcp" in f.read_text():
            return True
    return False


def _feature_dirs(slug: str, projects: list[Path]) -> list[tuple[Path, Path]]:
    """Return all (project_path, feature_dir) pairs matching this slug across projects."""
    results = []
    for proj in projects:
        d = _kb_root(proj) / slug
        if d.is_dir():
            results.append((proj, d))
    return results


def _available_slugs(projects: list[Path]) -> list[str]:
    seen: set[str] = set()
    slugs = []
    for proj in projects:
        for sub in sorted(_kb_root(proj).glob("*")):
            if sub.is_dir() and (sub / "README.md").exists():
                s = sub.name
                if s not in seen:
                    seen.add(s)
                    slugs.append(s)
    return sorted(slugs)


def _resolve_slug(
    slug: str, projects: list[Path]
) -> tuple[Path | None, str, bool] | str:
    """Return (feature_dir, resolved_slug, was_fuzzy), or an error string if ambiguous.

    Tries exact match first. If the slug exists in multiple projects and no
    project filter is active, returns an error string asking to specify project.
    Falls back to fuzzy auto-resolve (≥0.8) when not found.
    """
    matches = _feature_dirs(slug, projects)
    if len(matches) > 1:
        names = ", ".join(f"'{p.name}'" for p, _ in matches)
        return f"Slug '{slug}' exists in multiple projects: {names}. Specify project= to disambiguate."
    if len(matches) == 1:
        return matches[0][1], slug, False

    available = _available_slugs(projects)
    close = difflib.get_close_matches(slug, available, n=1, cutoff=0.8)
    if close:
        resolved = close[0]
        resolved_matches = _feature_dirs(resolved, projects)
        if len(resolved_matches) > 1:
            names = ", ".join(f"'{p.name}'" for p, _ in resolved_matches)
            return f"Slug '{resolved}' (resolved from '{slug}') exists in multiple projects: {names}. Specify project= to disambiguate."
        if len(resolved_matches) == 1:
            return resolved_matches[0][1], resolved, True

    return None, slug, False


def _not_found_msg(slug: str, projects: list[Path]) -> str:
    available = _available_slugs(projects)
    suggestions = difflib.get_close_matches(slug, available, n=3, cutoff=0.5)
    opts = ", ".join(available) if available else "none"
    if suggestions:
        hint = ", ".join(f"'{s}'" for s in suggestions)
        return f"Feature '{slug}' not found. Did you mean: {hint}?\nAvailable: {opts}"
    return f"Feature '{slug}' not found. Available: {opts}"


def _resolved_project(projects: list[Path], raw: str) -> str:
    return projects[0].name if len(projects) == 1 else raw


def _today_iso() -> str:
    """Today's local calendar date as `YYYY-MM-DD`, for KB entry stamps.

    Deliberately local rather than UTC: the KB is read by the person who wrote
    it, and an entry saved at 08:00 in UTC+7 would carry yesterday's date if the
    stamp were taken from UTC. A naive `date` is exactly what a calendar-date
    stamp wants — hence the DTZ011 suppression rather than a tz-aware datetime.
    """
    return date.today().isoformat()  # noqa: DTZ011


def _current_session_id() -> str:
    try:
        return (KB_ROOT / "current-session").read_text().strip()
    except Exception:  # noqa: BLE001 — no session file simply means no id
        return ""


def _log(tool: str, **kwargs) -> None:
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    session_id = _current_session_id()
    fields = dict(kwargs)
    try:
        parts = " ".join(
            f"{k}={v}" for k, v in fields.items() if v is not None and v != ""
        )
        with LOG_FILE.open("a") as f:
            f.write(f"{ts}  {tool:<22}  {parts}\n")
        entry: dict = {"ts": ts, "tool": tool, **fields}
        if session_id:
            entry["session_id"] = session_id
        with LOG_JSONL.open("a") as f:
            f.write(json.dumps(entry) + "\n")
    except Exception:  # noqa: BLE001, S110 — the logger cannot log its own failure
        pass


def _log_reject(log_kwargs: dict, reason: str) -> None:
    """Mark a call as a deliberate rejection and record why.

    `log_kwargs` defaults to `status="rejected"` (a guard refused — correct
    behaviour), and a crash is flipped to `status="error"` by the `except`
    clause each tool wraps its body in. This just records a short reason so a
    rejection and a crash stay distinguishable in usage.jsonl after the fact,
    instead of both being one undifferentiated `status=error`.
    """
    log_kwargs["status"] = "rejected"
    log_kwargs["message"] = reason


def _ensure_git_repo(project_root: Path) -> None:
    """Lazily git-init this project's KB directory (KB_ROOT/<project-name>) so
    it's ready whenever a user wants to manually commit/push to share just
    THAT project's KBs with its team — scoped per project, not one repo for
    every project under KB_ROOT, since different projects have different
    collaborators. recall-mcp itself never auto-commits (no rollback/revert
    workflow relies on git history, and no auto-push/pull exists to make
    per-call commits useful for sharing)."""
    try:
        if not (project_root / ".git").exists():
            subprocess.run(
                ["git", "-C", str(project_root), "init"],
                capture_output=True,
                timeout=5,
                check=False,
            )
    except Exception:  # noqa: BLE001, S110 — git may be absent; the KB still works
        pass


def _read_config() -> dict:
    if not CONFIG_PATH.exists():
        return {}
    return json.loads(CONFIG_PATH.read_text())


def _write_config(data: dict) -> None:
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(data, indent=2) + "\n")


def _atomic_write(path: Path, text: str) -> None:
    """Write `text` to `path` atomically: a sibling temp file, then `Path.replace`.

    `write_text()` writes in place, so a crash mid-write can leave a torn or
    truncated file that every later read mis-parses. Writing a unique temp file
    in the SAME directory and then atomically renaming it over the target means
    a reader only ever sees the old complete file or the new complete file —
    never a partial one. The temp name is derived from `time.monotonic_ns()`,
    not `secrets.token_hex` — the latter is the entry-id generator and tests
    pin its output, so reusing it here would collide. The target itself still
    needs a lock for true lost-update safety — level 2, deliberately deferred.
    """
    tmp = path.with_name(f"{path.name}.tmp-{time.monotonic_ns()}")
    tmp.write_text(text)
    tmp.replace(path)


def _resolve_username(confirmed: str = "") -> str:
    """Return username to use for memories files.

    Priority: confirmed arg > config > git config > 'unknown'.
    If confirmed is provided, saves it to config.
    """
    if confirmed:
        username = confirmed.strip().lower().replace(" ", "-")
        data = _read_config()
        data["username"] = username
        _write_config(data)
        return username

    data = _read_config()
    if data.get("username"):
        return data["username"]

    try:
        result = subprocess.run(
            ["git", "config", "user.name"],
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        )
        name = result.stdout.strip()
        if name:
            return name.lower().replace(" ", "-")
    except Exception:  # noqa: BLE001, S110 — no git identity falls back to "unknown"
        pass
    return "unknown"


def _memories_files(feature_dir: Path) -> list[Path]:
    """Return all memories files for a feature (memories-{username}.md pattern)."""
    return list(feature_dir.glob("memories-*.md"))


def _entry_id(block: str) -> str:
    """Extract [id:XXXX] from an entry's first line, or empty string if absent."""
    m = re.search(r"\[id:([a-f0-9]{4,6})\]", block.splitlines()[0], re.IGNORECASE)
    return m.group(1).lower() if m else ""


_ENTRY_HEADER_RE = re.compile(
    r"^- \*\*\d{4}-\d{2}-\d{2}\*\* \[id:[a-f0-9]{4,6}\]:\s*", re.IGNORECASE
)


def _entry_content(block: str) -> str:
    """The saved content of an entry — its block with the date/id header stripped.

    Inverse of the `save_memory` prefix `- **{date}** [id:XXXX]: `, so it returns
    exactly the `content` string that was saved (used to re-derive a promoted
    block's verbatim body for capture detection).
    """
    lines = block.splitlines()
    if not lines:
        return ""
    return "\n".join([_ENTRY_HEADER_RE.sub("", lines[0], count=1), *lines[1:]])


def _entry_date(block: str) -> str:
    """The `YYYY-MM-DD` from an entry's header, or "" if the header is malformed.

    "" is the ordinary miss for a block that is not an entry at all, and it is
    also what a malformed header degrades to: `_promoted_marker` then writes a
    dateless marker, which is the pre-2026-10-03 shape rather than a broken one.
    """
    lines = block.splitlines()
    if not lines:
        return ""
    m = _ENTRY_DATE_RE.match(lines[0])
    return m.group(1) if m else ""


def _superseded_ids(entries: list[tuple[str, str]]) -> set[str]:
    """Collect IDs referenced by [supersedes:XXXX] tags in any entry."""
    ids: set[str] = set()
    for _, block in entries:
        for m in re.finditer(
            r"\[supersedes:([a-f0-9]{4,6})\]", block.splitlines()[0], re.IGNORECASE
        ):
            ids.add(m.group(1).lower())
    return ids


def _resolved_ids(entries: list[tuple[str, str]]) -> set[str]:
    """Collect IDs referenced by [resolved:XXXX] tags in any entry."""
    ids: set[str] = set()
    for _, block in entries:
        for m in re.finditer(
            r"\[resolved:([a-f0-9]{4,6})\]", block.splitlines()[0], re.IGNORECASE
        ):
            ids.add(m.group(1).lower())
    return ids


# Tags safe to keep as title-only index entries in load_feature_context instead
# of full body — missing one costs re-investigation, not a correctness bug.
# Every other tag (including bare [resolved:XXXX] closures, which have no
# leading category tag by design) defaults to the always-full gating tier.
INDEX_ONLY_TAGS = frozenset({"idea", "pattern"})

# The mechanical tag -> section map (S1). Promotion is this pure function, so it
# needs no LLM judgement — which is the point: the five-step promote lifecycle it
# replaces was an LLM-compliance chain (~25% per step, ~0.4% joint), while a
# dict lookup either happens or doesn't.
# A live entry with one of these tags still sitting in memories means promotion
# has not caught up — invariant knowledge is full-body in the cold tier (bloat)
# instead of the must-read README tier. `_count_unpromoted_gating` counts that
# backlog for a hint.
PROMOTE_SECTIONS = {
    "gotcha": "critical_warnings",
    "constraint": "critical_warnings",
    "rule": "business_rules",
    "decision": "architecture",
}

# Derived, never written twice — the promotable set and the gating (full-load)
# set are the same set by design, so they must not be able to drift apart.
PROMOTE_TAGS = frozenset(PROMOTE_SECTIONS)

# Every section promotion can touch, deduplicated. A *pure closure* entry —
# `[resolved:XXXX]` with no promotable tag of its own, the shape the promotion
# protocol's own closure line takes — names no section, so its target block is
# found by scanning these.
PROMOTABLE_SECTIONS = tuple(dict.fromkeys(PROMOTE_SECTIONS.values()))

_ENTRY_TAG_RE = re.compile(r"\[id:[a-f0-9]{4,6}\]:\s*\*{0,2}\[([a-zA-Z]+)")


def _entry_tag(block: str) -> str:
    """Extract the leading category tag (e.g. 'idea') from an entry's first line.

    Returns "" for bare [resolved:XXXX] closures and malformed entries — both
    fall outside INDEX_ONLY_TAGS, so they default to the gating tier.
    """
    m = _ENTRY_TAG_RE.search(block.splitlines()[0])
    return m.group(1).lower() if m else ""


def _memory_tag_for_id(d: Path, entry_id: str) -> str:
    """Leading tag of the memory entry `entry_id` references, or "" if absent.

    Reads the raw memories files *unfiltered* — `_collect_memory_entries` hides
    [supersedes]/[resolved] targets, but a supersede ref may point at an entry
    that is itself retired, and `_apply_promotion` still needs its tag to judge
    whether a missing README block is suspicious.
    """
    needle = f"[id:{entry_id.lower()}]"
    for fpath in _memories_files(d):
        for line in fpath.read_text().splitlines():
            if needle in line:
                return _entry_tag(line)
    return ""


def _id_is_retired(d: Path, target_id: str, exclude_id: str = "") -> bool:
    """Whether `target_id` is referenced by a [supersedes:X]/[resolved:X] ref on
    some raw entry's first line — i.e. it was already retired, so its README
    block (if any) was replaced on purpose, not lost to a hand edit.

    `exclude_id` skips the entry that is itself being saved: its own
    [supersedes:target] ref is the supersede being processed, not a prior
    retirement signal.
    """
    supersedes = f"[supersedes:{target_id.lower()}]"
    resolved = f"[resolved:{target_id.lower()}]"
    exclude = f"[id:{exclude_id.lower()}]" if exclude_id else ""
    for fpath in _memories_files(d):
        for line in fpath.read_text().splitlines():
            if not line.strip().startswith(_ENTRY_OPEN):
                continue
            if exclude and exclude in line:
                continue
            if supersedes in line or resolved in line:
                return True
    return False


def _count_unpromoted_gating(entries: list[tuple[str, str]]) -> int:
    """Count live gating-tag entries (gotcha/constraint/rule/decision) in memories.

    `entries` is already filtered by `_collect_memory_entries` (resolved/
    superseded targets hidden), so every hit here is a *live* invariant still
    full-body in memories. A high count means promotion backlog: the knowledge
    is in the cold tier instead of the must-read README sections it maps to.
    """
    return sum(1 for _d, block in entries if _entry_tag(block) in PROMOTE_TAGS)


def _cap_index_titles(titles: list[str], budget: int) -> tuple[str, int]:
    """Render `titles` (raw entry first-lines, newest first) into a capped index.

    Returns (text, dropped_count). Each title becomes a "  • " bullet; the cap is
    on total rendered chars so the index can't starve README sections of room in a
    budgeted render. The first title is always kept even if it alone exceeds the
    budget — one oversized title is better than an empty index that hides the
    pointer to expand_ids bodies.
    """
    kept: list[str] = []
    used = 0
    dropped = 0
    for title in titles:
        bullet = f"  • {title}"
        cost = len(bullet) + 1  # +1 for the separator between bullets
        if kept and used + cost > budget:
            dropped += 1
            continue
        kept.append(bullet)
        used += cost
    return "\n".join(kept), dropped


_ENTRY_OPEN = "- **"
# Just the date half of the header. Separate from `_ENTRY_HEADER_RE` because a
# promoted block carries the date into the README while the id goes to the
# marker — the two halves are read independently, so neither regex may assume
# the other matched. Read by both `_flush_memory_entry` here and `_entry_date`.
_ENTRY_DATE_RE = re.compile(r"^- \*\*(\d{4}-\d{2}-\d{2})\*\*")


def _flush_memory_entry(buf: list[str], entries: list[tuple[str, str]]) -> int:
    """Close the accumulated entry into `entries`; return 1 if it had no date.

    An empty buffer returns 0: the buffer is empty before the first opener and
    after the last entry, which is the normal case, not a malformed one.
    """
    block = "\n".join(buf).strip()
    if not block:
        return 0
    m = _ENTRY_DATE_RE.match(block.splitlines()[0])
    if m:
        entries.append((m.group(1), block))
        return 0
    entries.append(("0000-00-00", block))
    return 1


def _collect_memory_entries(
    files: list[Path], readme_text: str = ""
) -> tuple[list[tuple[str, str]], int]:
    """Parse, sort (date descending), and filter memory entries from files.

    An entry opens at every line beginning with "- **" and owns every line
    until the next such line. Blank lines are content, not delimiters: an
    entry body may contain them freely, because save_memory writes the model's
    content verbatim and a blank line between What/Why/Apply paragraphs is
    ordinary formatting. Anything before the first opener is file preamble
    (title, separator) and is skipped.

    Consequence worth knowing: a body line that itself begins with "- **"
    opens a new entry, splitting the entry it sits in — today only when a
    blank line precedes it, now unconditionally. That is deliberate: the
    split tail is counted as malformed and sorts to the bottom, so it is
    visible, whereas the blank-line delimiter it replaced lost the body
    silently (measured 2026-09-26: 31 entries / 14,874 chars across the KB
    store had their body dropped this way).

    Filtering rules (raw files are never modified):
    - [supersedes:XXXX]: the TARGET entry (id:XXXX) is hidden; the replacement
      entry containing [supersedes:XXXX] stays visible — it holds the new content.
    - [resolved:XXXX]: the TARGET entry (id:XXXX) is hidden; the closure record
      containing [resolved:XXXX] stays visible — Claude needs it to know what was
      resolved and why, so it doesn't re-investigate a fixed issue.
    - captured: an entry whose promoted README block (carrying `from:XXXX`) is still
      a verbatim copy is hidden — the README block is the source of truth. Applied
      only when `readme_text` is passed; the verbatim check keeps a trimmed block
      from hiding detail that now survives only in the memory.

    Returns (entries, malformed_count) as (date, block) pairs, newest first.
    An entry whose first line doesn't match the "- **YYYY-MM-DD**" prefix
    save_memory always writes (e.g. a body line that begins with "- **", or one
    hand-edited into the file without a date) sorts as "0000-00-00" — oldest —
    instead of erroring; malformed_count lets the caller surface this instead
    of it happening silently.
    """
    entries: list[tuple[str, str]] = []  # (date, block)
    malformed = 0

    for fpath in files:
        buf: list[str] = []
        for line in fpath.read_text().splitlines():
            if line.strip().startswith(_ENTRY_OPEN):
                malformed += _flush_memory_entry(buf, entries)
                buf = [line.strip()]
            elif buf:
                buf.append(line)
        malformed += _flush_memory_entry(buf, entries)

    entries.sort(key=lambda x: x[0], reverse=True)

    filtered = _superseded_ids(entries) | _resolved_ids(entries)
    if readme_text:
        filtered |= _captured_ids(readme_text, entries)
    if filtered:
        entries = [(d, b) for d, b in entries if _entry_id(b) not in filtered]

    return entries, malformed


def _merge_memories(files: list[Path], readme_text: str = "") -> tuple[str, int]:
    """Merge entries from multiple memories files into one text blob, sorted
    by date descending. See _collect_memory_entries for filtering rules.

    Returns (merged_text, malformed_count).
    """
    entries, malformed = _collect_memory_entries(files, readme_text)
    merged = "\n\n".join(block for _, block in entries) if entries else ""
    return merged, malformed


# HTML comments EXCEPT the `<!-- from:XXXX -->` provenance marker. The marker is
# kept in the model-facing render (so a captured block's source memory can still be
# superseded by id), while template placeholders and any other comment are stripped.
# The lookahead mirrors `_PROMOTED_MARKER_RE` including the optional date: any
# marker shape this fails to exclude is stripped as an ordinary comment, which
# silently orphans the block and makes a later supersede append a duplicate.
_NON_MARKER_COMMENT_RE = re.compile(
    r"<!--(?!\s*from:[a-f0-9]{4,6}(?:\s+\S+)?\s*-->).*?-->",
    re.DOTALL,
)


def _strip_readme_for_context(text: str, keep_markers: bool = False) -> str:
    """Strip HTML comments and empty XML sections before returning to Claude.

    The stored README keeps comments (useful for human editing). `keep_markers`
    preserves provenance `<!-- from:XXXX -->` markers (needed so a captured block's
    source memory can still be superseded); other comments are always stripped.
    """
    stripped = (
        _NON_MARKER_COMMENT_RE.sub("", text)
        if keep_markers
        else _strip_html_comments(text)
    )

    def _drop_if_empty(m: re.Match) -> str:
        return "" if not m.group(2).strip() else m.group(0)

    stripped = re.sub(r"<(\w+)>(.*?)</\1>", _drop_if_empty, stripped, flags=re.DOTALL)
    stripped = re.sub(r"\n{3,}", "\n\n", stripped)
    return stripped.strip()


_SECTION_BLOCK_RE = re.compile(r"<(\w+)>(.*?)</\1>", re.DOTALL)


def _split_readme_sections(text: str) -> tuple[str, list[tuple[str, str]]]:
    """Split stripped README text into (preamble, [(name, block), ...]) in file order.

    The budget render reorders and rejoins blocks with "\\n\\n" separators, so this
    does not preserve the exact inter-section whitespace — it only extracts the
    preamble (title/feature line) and the non-empty `<section>` blocks. The default
    (unbudgeted) render never calls this, so its bit-identical output is unaffected.
    """
    sections = [(m.group(1), m.group(0)) for m in _SECTION_BLOCK_RE.finditer(text)]
    first = _SECTION_BLOCK_RE.search(text)
    preamble = text[: first.start()].strip() if first else ""
    return preamble, sections


def _omission_directive(
    slug: str, section_names: list[str], omitted_memories: bool
) -> str:
    """Imperative header for what the budget render dropped.

    Placed at the TOP of the render, before any body text, so the caller knows the
    KB is partial *before* reading it rather than discovering a trailing note at the
    point it stops reading. States the follow-up call for each kind of omission:
    `sections=[...]` for dropped sections (a targeted render that stays inside a
    tool result), the budget-free default render for memories (which can only be
    fetched as part of a full render).
    """
    omitted = ", ".join(section_names + (["memories"] if omitted_memories else []))
    lines = [
        (
            "⚠️ KB cut to fit the injection budget — priority subset only, whole "
            "sections, nothing half-shown."
        ),
        "Load the rest before relying on this KB:",
    ]
    if section_names:
        args = ", ".join(repr(n) for n in section_names)
        lines.append(f"  load_feature_context('{slug}', sections=[{args}])")
    if omitted_memories:
        lines.append(f"  load_feature_context('{slug}')  — full render, incl. memories")
    lines.append(f"Omitted: {omitted}")
    return "\n".join(lines)


def _render_budgeted(
    *,
    slug: str,
    header: str,
    hints: str,
    readme_stripped: str,
    memories: list[str],
    expanded: list[str],
    index_part: str,
    footer: str,
    sections_filter: list[str] | None,
    max_chars: int | None,
) -> str:
    """Assemble the context under a sections allow-list and/or a char budget.

    Units are admitted in strict `SECTION_PRIORITY` order and the first one that
    does not fit ends the render, so the admitted set is always a PREFIX of that
    order — a high-priority unit is never dropped in favour of a smaller
    lower-priority one. Whole units only, never a mid-entry cut. Header, hints,
    omission directive, README heading/preamble, index, and footer are reserved
    first — the directive is charged to the budget it reports on, so it can never
    push the render over the limit it exists to enforce.

    `memories` is the list of gating memory entries, already newest-first (from
    `_collect_memory_entries`). Each entry is its own unit, so a budget that
    cannot carry the whole memories tier admits a PREFIX of the newest entries
    instead of dropping them all — an oversized KB can still load its most recent
    load-bearing memories. All memory units share the reserved name "memories"
    so the omission directive reports them as one category.

    `expanded` are the bodies the caller explicitly requested via `expand_ids`.
    They are reserved (always shown) rather than budget-limited — an explicit
    request must survive even a budget below the fixed frame, otherwise the
    index's "call expand_ids to fetch a body" instruction can never be honoured.
    """
    preamble, section_list = _split_readme_sections(readme_stripped)

    if sections_filter is not None:
        want = set(sections_filter)
        section_list = [(n, b) for n, b in section_list if n in want]
        memories = []
        expanded = []
        index_part = ""

    units = [(SECTION_PRIORITY.get(n, 0), n, b) for n, b in section_list]
    # Memories are split per entry (already newest-first): each is its own unit,
    # so the admit loop below takes a PREFIX of the newest entries rather than
    # dropping the whole tier. They all carry the reserved name "memories" so
    # omission detection and the directive treat them as one category.
    units.extend((_MEMORIES_PRIORITY, "memories", block) for block in memories)
    units.sort(key=lambda u: -u[0])  # stable → ties keep file order

    budget = (
        KB_FULL_RENDER_LIMIT_CHARS
        if max_chars is None
        else min(max_chars, KB_FULL_RENDER_LIMIT_CHARS)
    )

    fixed_parts = [header]
    if hints:
        fixed_parts.append(hints)
    if preamble:
        fixed_parts.append(f"## README.md\n\n{preamble}")
    if expanded:
        # Explicitly-requested bodies are reserved (always shown), like the frame —
        # an expand_ids request must survive even a budget below the fixed frame.
        fixed_parts.append("## memories\n\n" + "\n\n".join(expanded))
    suffix_parts = [p for p in (index_part, footer) if p]
    # Reserve the WORST-CASE directive (every section + memories omitted) rather than
    # a guessed constant: the real one is always a subset, so this over-reserves by a
    # few dozen chars and never under-reserves. The directive's own length depends on
    # what gets omitted, which depends on the budget — reserving first breaks the
    # circularity in the safe direction.
    directive_reserve = len(
        _omission_directive(slug, [n for n, _ in section_list], bool(memories))
    )
    fixed_len = (
        len("\n\n".join(fixed_parts))
        + len("\n\n".join(suffix_parts))
        + directive_reserve
        + 2  # the "\n\n" separator before the directive itself
    )
    room = budget - fixed_len

    included = []
    omitted_sections = []
    omitted_memories = False
    for i, (_pri, name, block) in enumerate(units):
        if room - (len(block) + 2) >= 0:  # +2 for the "\n\n" separator before it
            included.append((name, block))
            room -= len(block) + 2
            continue
        # The first unit that does not fit ENDS the render — do not skip it and try
        # smaller later units instead. Skipping makes SIZE beat priority: measured
        # 2026-09-26 on a real KB, `max_chars=8900` dropped `business_rules`
        # (priority 3, 756 chars) while keeping `overview` + `related_tickets`
        # (priority 1), and below ~8200 it dropped `critical_warnings` (priority 4)
        # the same way — the silent-loss mode MISS 2fedf3 recorded, and a direct
        # contradiction of the directive's own "priority subset only" wording.
        # Breaking keeps the admitted set a prefix of SECTION_PRIORITY order.
        rest = units[i:]
        omitted_sections = [n for _p, n, _b in rest if n != "memories"]
        omitted_memories = any(n == "memories" for _p, n, _b in rest)
        break

    readme_content = []
    if preamble:
        readme_content.append(preamble)
    readme_content.extend(b for n, b in included if n != "memories")

    parts = [header]
    if hints:
        parts.append(hints)
    if omitted_sections or omitted_memories:
        parts.append(_omission_directive(slug, omitted_sections, omitted_memories))
    if readme_content:
        parts.append("## README.md\n\n" + "\n\n".join(readme_content))
    memories_blocks = list(expanded) + [b for n, b in included if n == "memories"]
    if memories_blocks:
        parts.append("## memories\n\n" + "\n\n".join(memories_blocks))
    if index_part:
        parts.append(index_part)
    parts.append(footer)
    return "\n\n".join(parts)


_KEY_FILES_LINE_RE = re.compile(
    r"^-\s"
)  # any markdown bullet line — backtick may appear
# anywhere on the line (e.g. "- Tests: `file.py`"), not just right after the dash; the
# per-token _LOOKS_LIKE_PATH_RE check below still filters what counts as a real path
_BACKTICK_TOKEN_RE = re.compile(r"`([^`]+)`")
_KEY_FILES_PLACEHOLDER_RE = re.compile(
    r"[{}]"
)  # template vars, e.g. `{project-name}/features.md`
# Has a symbol (`::`), ends in a directory slash, or ends in a file extension.
# Deliberately does NOT treat a bare mid-token `/` alone as path-like — that
# also matches non-path prose like `twc-reanalysis/v1` (a data identifier) or
# `try/finally` (a Python idiom), neither of which is a file.
_LOOKS_LIKE_PATH_RE = re.compile(r"::|/$|\.[A-Za-z0-9]{1,5}$")
KEY_FILES_HUB_THRESHOLD = 3  # a path referenced by this many features (or more) is a
# hub/entry-point file, not evidence any two specific features are related
# NOTE: boilerplate filenames (__init__.py, pyproject.toml, ci.yaml, ...) are
# deliberately NOT filtered here. That's a "technically true but low-value"
# match, not missing/wrong data — Claude reading the footer can already
# discount an obviously-generic shared filename on its own; the footer's
# prompt text (see load_feature_context) carries that instruction instead of
# a maintained code-level denylist.


def _key_files_index(
    projects: list[Path],
) -> tuple[dict[str, list[str]], dict[str, list[str]]]:
    """Map each key_files path (and path::symbol) to the slugs referencing it.

    Parses every backtick-quoted token per <key_files> bullet line (not just
    the first — a bullet can legitimately list several real paths on one
    line, e.g. "`a.py`, `b.py` — ..."), but only tokens that look like a path:
    contain `/`, `::`, or end in a file extension. This excludes inline code
    mentioned in a bullet's free-text description (symbol/class names like
    `` `RegistryAdapter` `` or a bare concept name like `` `_shared` ``) which
    would otherwise be misread as extra key files. Tokens containing `{`/`}`
    are also skipped — those are illustrative template placeholders (e.g.
    `{project-name}/features.md`), not real paths. Returns (file_index,
    symbol_index): file_index keys are bare paths (`::symbol` suffix
    stripped, so any symbol in a file counts toward that file's overlap);
    symbol_index keys are the full `path::symbol` string, kept separately as
    a finer-grained signal for _related_by_key_files to upgrade a
    shared-file match into a shared-exact-symbol match. Computed live on
    every call — not cached, since key_files bullets change often enough that
    a cache would need its own invalidation logic for little benefit at this
    scale. Best-effort: only as accurate as each feature's key_files
    bullets, which can drift from the real code — don't treat a hit here as
    verified without checking the actual file (see _unverified_key_files for
    a soft, non-excluding staleness check).
    """
    file_index: dict[str, list[str]] = {}
    symbol_index: dict[str, list[str]] = {}
    for proj in projects:
        kb_root = _kb_root(proj)
        if not kb_root.is_dir():
            continue
        for feature_dir in sorted(kb_root.glob("*")):
            readme = feature_dir / "README.md"
            if not feature_dir.is_dir() or not readme.exists():
                continue
            slug = feature_dir.name
            text = _strip_html_comments(readme.read_text())
            m = re.search(r"<key_files>(.*?)</key_files>", text, re.DOTALL)
            if not m:
                continue
            for line in m.group(1).splitlines():
                stripped = line.strip()
                if not _KEY_FILES_LINE_RE.match(stripped):
                    continue
                for token in _BACKTICK_TOKEN_RE.findall(stripped):
                    token = token.strip()
                    if not token or _KEY_FILES_PLACEHOLDER_RE.search(token):
                        continue
                    if not _LOOKS_LIKE_PATH_RE.search(token):
                        continue
                    path, _, symbol = token.partition("::")
                    path = path.strip()
                    if not path:
                        continue
                    bucket = file_index.setdefault(path, [])
                    if slug not in bucket:
                        bucket.append(slug)
                    if symbol:
                        sbucket = symbol_index.setdefault(token, [])
                        if slug not in sbucket:
                            sbucket.append(slug)
    return file_index, symbol_index


def _unverified_key_files(paths: list[str], projects: list[Path]) -> set[str]:
    """Bare paths (no ::symbol) that don't exist on disk under any given project root.

    Informational only — never used to drop a match. Most key_files bullets
    are repo-relative, but some legitimately point elsewhere (e.g. this
    project's own `~/.recall-mcp/config.json`), so a missing-on-disk result
    isn't reliable enough to exclude — only to flag as worth a second look.
    """
    unverified = set()
    for p in paths:
        bare = p.split("::", 1)[0]
        if not any((proj / bare).exists() for proj in projects):
            unverified.add(p)
    return unverified


def _related_by_key_files(
    slug: str,
    file_index: dict[str, list[str]],
    symbol_index: dict[str, list[str]],
) -> tuple[dict[str, list[str]], dict[str, list[str]]]:
    """Return (strong, weak) — {other_slug: [shared_paths]} split by confidence.

    `strong` holds ordinary key_files overlap: a real signal that two
    features are related. `weak` holds a path referenced by
    KEY_FILES_HUB_THRESHOLD+ features (hub/entry-point file) — not strong
    evidence on its own, but unlike before, still surfaced (annotated, ranked
    after `strong`) instead of silently disappearing.

    Symbol-level matches (both features reference the exact same
    `path::symbol`) always count as `strong`, even when the bare file landed
    in `weak` — a specific shared symbol is meaningful signal on its own, so
    it "rescues" the pair out of `weak` for that path.
    """
    strong: dict[str, list[str]] = {}
    weak: dict[str, list[str]] = {}
    for path, slugs in file_index.items():
        if slug not in slugs:
            continue
        target = weak if len(slugs) >= KEY_FILES_HUB_THRESHOLD else strong
        for other in slugs:
            if other != slug:
                target.setdefault(other, []).append(path)

    for full_key, slugs in symbol_index.items():
        if slug not in slugs:
            continue
        bare_path = full_key.split("::", 1)[0]
        for other in slugs:
            if other == slug:
                continue
            strong_paths = strong.setdefault(other, [])
            if bare_path in strong_paths:
                strong_paths[strong_paths.index(bare_path)] = full_key
                continue
            weak_paths = weak.get(other)
            if weak_paths and bare_path in weak_paths:
                weak_paths.remove(bare_path)
                if not weak_paths:
                    del weak[other]
            if full_key not in strong_paths:
                strong_paths.append(full_key)

    strong = {other: paths for other, paths in strong.items() if paths}
    weak = {other: paths for other, paths in weak.items() if paths}
    return strong, weak


def _weak_match_reason(path: str, file_index: dict[str, list[str]]) -> str:
    """Human-readable reason a path landed in _related_by_key_files' `weak` bucket."""
    bare = path.split("::", 1)[0]
    count = len(file_index.get(bare, []))
    return f"hub — {count} features"


def _key_files_format_hint(content: str) -> str:
    """Flag key_files bullets with no backtick-quoted path-shaped token at all.

    A prose-prefixed bullet ("- Tests: `file.py`") is still readable — the
    backtick can appear anywhere on the line. A bullet with NO backtick token
    at all (a bare path, e.g. "- src/foo.py — description") can't be recovered
    by any parser change: there's no token to find. Advisory only — never
    blocks the write, since the content itself is still valid documentation.
    """
    bad_lines = 0
    for line in _strip_html_comments(content).splitlines():
        stripped = line.strip()
        if not _KEY_FILES_LINE_RE.match(stripped):
            continue
        found_path = any(
            _LOOKS_LIKE_PATH_RE.search(token.strip())
            for token in _BACKTICK_TOKEN_RE.findall(stripped)
            if token.strip() and not _KEY_FILES_PLACEHOLDER_RE.search(token)
        )
        if not found_path:
            bad_lines += 1
    if not bad_lines:
        return ""
    return (
        f"\n\n**key_files format:** {bad_lines} bullet(s) have no backtick-quoted "
        f'path — wrap the path itself in backticks (e.g. "- `src/foo.py` — ..."), '
        f"otherwise the related-kb signal can't see it."
    )


def _kb_health_hints(
    readme_text: str, combined_memories: str, unpromoted_gating: int = 0
) -> list[str]:
    """Return maintenance hints if KB exceeds size thresholds."""
    hints = []

    mem_chars = len(combined_memories)
    if mem_chars > KB_MEMORIES_COMPACT_THRESHOLD:
        hints.append(
            f"**KB maintenance:** memories are large ({mem_chars:,} chars) — "
            f"run `/recall:compact` to reduce token cost."
        )

    if unpromoted_gating > KB_UNPROMOTED_GATING_THRESHOLD:
        hints.append(
            f"**KB maintenance:** {unpromoted_gating} gating memories "
            f"(gotcha/constraint/rule/decision) still live in memories, not promoted "
            f"to README — they predate auto-promotion or were skipped at save. Re-save "
            f"each with `[supersedes:<its-own-id>]` so S1 re-promotes it with a "
            f"`<!-- from:XXXX -->` marker; the old body is then superseded and the new "
            f"one auto-hides once captured. Hand-writing the same content instead works "
            f"too, but copy the memory's body verbatim — a verbatim block is recognised "
            f"and gets its marker attached, while a paraphrase stays an orphan that no "
            f"later supersede can replace."
        )

    tidy_sections = []
    m = re.search(
        r"<critical_warnings>(.*?)</critical_warnings>", readme_text, re.DOTALL
    )
    if m:
        count = len(re.findall(r"\*\*\[", m.group(1)))
        if count > KB_SECTION_TIDY_ENTRY_THRESHOLD:
            tidy_sections.append(f"critical_warnings ({count} entries)")

    for section, threshold in (
        ("architecture", KB_ARCHITECTURE_TIDY_CHAR_THRESHOLD),
        ("business_rules", KB_BUSINESS_RULES_TIDY_THRESHOLD),
    ):
        m = re.search(f"<{section}>(.*?)</{section}>", readme_text, re.DOTALL)
        if m and len(m.group(1)) > threshold:
            tidy_sections.append(f"{section} ({len(m.group(1)):,} chars)")

    if tidy_sections:
        hints.append(
            f"**KB maintenance:** README sections are large ({', '.join(tidy_sections)}) — "
            f"run `/recall:tidy` to reduce token cost."
        )

    return hints


def _health_hint_suffix(d: Path) -> str:
    """Recompute KB size hints right after a write and return a suffix to append
    to the caller's response (empty string if the KB is still under threshold).

    `load_feature_context` already runs this check, but a long working session
    often calls `save_memory`/`update_readme` many times per `load_feature_context`
    call (or doesn't call it again at all after the initial load) — so a KB can
    cross the maintenance threshold, or even the hard load limit, entirely
    between load calls, with no signal until the next (possibly much later)
    load fails outright. Checking on every write closes that gap.
    """
    readme = d / "README.md"
    readme_text = readme.read_text() if readme.exists() else ""
    entries, _ = _collect_memory_entries(_memories_files(d), readme_text)
    combined = "\n\n".join(block for _, block in entries) if entries else ""
    hints = _kb_health_hints(readme_text, combined, _count_unpromoted_gating(entries))
    return ("\n\n" + "\n".join(hints)) if hints else ""


def _retire_ids_from_entry(new_entry: str) -> set[str]:
    """Extract [resolved:XXXX]/[supersedes:XXXX] target ids from a single entry.

    Safe on an empty string (returns empty set) — used by the save gate to know
    which existing entries this new entry would hide.
    """
    if not new_entry:
        return set()
    ids: set[str] = set()
    for m in re.finditer(
        r"\[(?:resolved|supersedes):([a-f0-9]{4,6})\]",
        new_entry.splitlines()[0],
        re.IGNORECASE,
    ):
        ids.add(m.group(1).lower())
    return ids


def _render_size_chars(
    readme_text: str,
    entries: list[tuple[str, str]],
    *,
    effective_full_limit: int = KB_FULL_RENDER_LIMIT_CHARS,
) -> int:
    """The unbudgeted render size, exactly as `load_feature_context` computes it.

    Single source of truth for the render gate. Load renders this inline (it needs
    the strings, not just their length), but the save-side `_post_save_total_chars`
    predicts the post-save size through this function so the two cannot drift. The
    pre-0.2 bug was two hand-rolled copies that disagreed on two things — the
    `<!-- from:XXXX -->` markers (save stripped them, load kept them) and the
    `"\\n\\n"` separators between memory blocks (load joined them, save did not) —
    so a save could push the KB past `KB_FULL_RENDER_LIMIT_CHARS` with no refusal.
    """
    readme_chars = len(_strip_readme_for_context(readme_text, keep_markers=True))
    index_all = readme_chars + sum(len(b) for _, b in entries) > effective_full_limit
    combined_blocks: list[str] = []
    index_lines: list[str] = []
    for _date, block in entries:
        if index_all or _entry_tag(block) in INDEX_ONLY_TAGS:
            index_lines.append(block.splitlines()[0])
        else:
            combined_blocks.append(block)
    combined = "\n\n".join(combined_blocks)
    index_text, _cut_titles = _cap_index_titles(
        index_lines, KB_INDEX_TITLE_BUDGET_CHARS
    )
    return readme_chars + len(combined) + len(index_text)


def _full_body_total_chars(d: Path, new_entry: str) -> int:
    """The KB's full-body context size (README + all live memory bodies + entry),
    ignoring index-all's title-only compression. This is the on-disk weight that
    `_merge_memories`/`search_features`/`expand_ids` must still read in full, so
    it needs its own growth cap independent of the title-only load metric. Markers
    are kept and bodies joined with the same `"\\n\\n"` separator `_merge_memories`
    uses, so the figure is the real weight, not a stripped estimate."""
    readme = d / "README.md"
    readme_text = (
        _strip_readme_for_context(readme.read_text(), keep_markers=True)
        if readme.exists()
        else ""
    )
    entries, _ = _collect_memory_entries(_memories_files(d))
    retired = _retire_ids_from_entry(new_entry)
    bodies = [b for _, b in entries if _entry_id(b) not in retired]
    if new_entry:
        bodies.append(new_entry)
    return len(readme_text) + (len("\n\n".join(bodies)) if bodies else 0)


def _post_save_total_chars(d: Path, new_entry: str) -> int:
    """Predict the KB's post-save render size via `_render_size_chars`, so it always
    agrees with what `load_feature_context`'s gate measures — including the markers
    and separators the old hand-rolled copy dropped.

    `_collect_memory_entries` is passed `readme_text` for the same reason load
    does: entries already captured in a verbatim README block are hidden from the
    render, so their bodies must not be counted here either. The pending entry is
    prepended as the newest, matching save_memory's write order.
    """
    readme = d / "README.md"
    readme_text = readme.read_text() if readme.exists() else ""
    entries, _ = _collect_memory_entries(_memories_files(d), readme_text)
    retired = _retire_ids_from_entry(new_entry)
    live = [(dt, b) for dt, b in entries if _entry_id(b) not in retired]
    if new_entry:
        live = [("9999-99-99", new_entry), *live]
    return _render_size_chars(readme_text, live)


def _save_blocked_msg(slug: str, total: int) -> str:
    """Blocking directive for a save that would push the KB over the hard limit.

    Mirrors the load gate: refuse the write, name the remedy, state that the
    entry was not written (so the caller knows to re-save after compacting).
    """
    return (
        f"⚠️ Save blocked — '{slug}' is at the hard size limit.\n"
        f"Writing this entry would bring the KB to ~{total:,} chars, over the "
        f"~{KB_FULL_RENDER_LIMIT_CHARS:,} limit, making it unloadable.\n"
        f"This entry was NOT written. Run `/recall:compact` (and `/recall:tidy` "
        f"if README is large) to shrink the KB, then save again."
    )


def _full_body_growth_blocked_msg(slug: str, total: int) -> str:
    """Blocking directive for a save whose FULL-BODY would exceed the growth cap.

    Distinct from `_save_blocked_msg`: the KB is still loadable (index-all renders
    titles), so this is not about unloadability — it is about the on-disk weight
    that merge/search/expand still read in full, and about compaction no longer
    being forced. States that the KB is loadable but the entry was not written.
    """
    return (
        f"⚠️ Save blocked — '{slug}' full-body is at the growth ceiling.\n"
        f"The KB still loads (memories render as titles), but writing this entry "
        f"would bring full-body (README + memory bodies) to ~{total:,} chars, over "
        f"the ~{KB_FULL_BODY_GROWTH_LIMIT_CHARS:,} ceiling.\n"
        f"This entry was NOT written. Run `/recall:compact` (and `/recall:tidy` "
        f"if README is large) to shrink the KB, then save again."
    )


def _memory_too_long_msg(content_len: int, hard: bool) -> str:
    """Reject (hard) or advisory note (soft) for an oversized single entry.

    Shared by `save_memory` and `report_miss`: both write one entry to a memories
    file and both are capped by the same per-entry guard, so the wording stays
    neutral about which field was sent.

    Hard is a genuine refusal — a memory is one insight, not a document, and a
    blob past the ceiling is never an insight. Soft never rejects: it appends a
    note to a write that already succeeded, so a false positive costs nothing.
    """
    if hard:
        return (
            f"Rejected: content is {content_len:,} chars — over the per-entry "
            f"limit of {KB_MEMORY_ENTRY_HARD_LIMIT_CHARS:,}. A memory is a single "
            f"insight, not a document. Condense it, or move long-form detail to "
            f"the README via update_readme."
        )
    return (
        f"\n\n⚠️ Entry is {content_len:,} chars (soft limit "
        f"{KB_MEMORY_ENTRY_SOFT_LIMIT_CHARS:,}) — memories are single insights; "
        f"condense it, or move long-form detail to the README via update_readme."
    )


def _growth_gate(d: Path, slug: str, new_entry: str) -> str:
    """The blocking message when writing `new_entry` grows the KB past a ceiling,
    else "".

    Shared by both memories writers (`save_memory`, `report_miss`) so the two
    cannot drift — the same reasoning that derives both ceilings from one
    literal. `report_miss` grew without it until 2026-10-03, which let a MISS
    push an already-large KB past the load limit with no refusal.

    Two ceilings, mirroring the load path. The hard one is the mirror of the load
    gate: a write must never push the KB past it, since growth should be
    compacted first rather than allowed to reach the point where the KB stops
    loading. The full-body one caps the on-disk weight that
    `_merge_memories`/`search_features`/`expand_ids` must still read in full,
    independent of index-all's title-only compression.

    Growth-only: a retire/supersede closure shrinks the KB (net-negative), so it
    must always be allowed through — otherwise the gate deadlocks, blocking the
    very closure/compact that would recover an oversized KB.
    """
    current_total = _post_save_total_chars(d, "")
    post_save_total = _post_save_total_chars(d, new_entry)
    if post_save_total > KB_FULL_RENDER_LIMIT_CHARS and post_save_total > current_total:
        return _save_blocked_msg(slug, post_save_total)

    current_full = _full_body_total_chars(d, "")
    post_full = _full_body_total_chars(d, new_entry)
    if post_full > KB_FULL_BODY_GROWTH_LIMIT_CHARS and post_full > current_full:
        return _full_body_growth_blocked_msg(slug, post_full)
    return ""


def _readme_write_blocked(d: Path, new_readme_text: str) -> tuple[int, int] | None:
    """(projected, limit) when writing `new_readme_text` would grow the KB past a
    ceiling; None when it shrinks or still fits.

    README is injected in full on every load, and `update_readme` is the only
    writer the save gates never see — so without this, a section write could push
    the always-on cost past the load limit entirely between two saves.

    Growth-only, matching `_apply_promotion` and both save gates: a replace that
    shrinks the README must never be blocked, or the remedy for an oversized KB
    would itself be refused. Both metrics are checked, as in `save_memory` —
    README counts toward the load metric and toward full-body alike, and both
    reuse `save_memory`'s own helpers rather than re-deriving the arithmetic.
    """
    readme = d / "README.md"
    old_len = (
        len(_strip_readme_for_context(readme.read_text(), keep_markers=True))
        if readme.exists()
        else 0
    )
    new_len = len(_strip_readme_for_context(new_readme_text, keep_markers=True))
    delta = new_len - old_len
    if delta <= 0:
        return None
    # Both projections reuse `save_memory`'s own helpers with an empty pending
    # entry, so this gate cannot drift from the save gates' arithmetic: the empty
    # entry contributes 0 chars and retires nothing, leaving the current README
    # length as the only thing `delta` adjusts.
    full = _full_body_total_chars(d, "") + delta
    if full > KB_FULL_BODY_GROWTH_LIMIT_CHARS:
        return full, KB_FULL_BODY_GROWTH_LIMIT_CHARS
    projected = _post_save_total_chars(d, "") + delta
    if projected > KB_FULL_RENDER_LIMIT_CHARS:
        return projected, KB_FULL_RENDER_LIMIT_CHARS
    return None


def _readme_blocked_msg(slug: str, section: str, total: int, limit: int) -> str:
    """Blocking directive for an `update_readme` that would grow the README past a
    ceiling. Shrinking is always allowed, so the remedy is named instead."""
    return (
        f"⚠️ Update blocked — writing <{section}> would grow '{slug}' to "
        f"~{total:,} chars, over the ~{limit:,} ceiling.\n"
        f"README is injected in full on every load, so this is the KB's always-on "
        f"cost. Nothing was written. Run `/recall:tidy` to shrink the section, or "
        f"write less in this call."
    )


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


@mcp.tool()
def list_features(project: str = "") -> str:
    """List all feature knowledge bases across configured projects.

    WHEN: Do NOT call on every turn or as an orientation step before other tools.
    Call only when you genuinely need slug discovery: you don't know what KBs exist,
    or load_feature_context returned "not found" and you need to find the correct slug.

    OUTPUT: Returns a features.md table per project (name, slug, tickets, branches,
    summary). Returns all projects when project= is omitted. Use the slug column as
    the exact value for load_feature_context, save_memory, and update_readme calls.

    Args:
        project: Project name or path to filter results. Always pass when the current
            project is known — omit only when you genuinely don't know which project
            to target.
    """
    t0 = time.monotonic()
    # Unlike other tools, an omitted project= means "show everything" here,
    # not "guess from session cwd" — don't route through _active_project.
    projects = _filter(_projects(), project) if project else _projects()
    log_kwargs: dict = {
        "project": _resolved_project(projects, project),
        "count": 0,
        "status": "rejected",
    }
    try:
        if not projects:
            log_kwargs["status"] = "ok"
            return "No projects configured. Add paths to ~/.recall-mcp/config.json."

        sections = []
        for proj in projects:
            index = _kb_root(proj) / "features.md"
            if not index.exists():
                continue
            sections.append(f"## {proj.name}\n\n{index.read_text().strip()}")

        log_kwargs["count"] = sum(
            1
            for proj in projects
            for sub in _kb_root(proj).iterdir()
            if sub.is_dir() and (sub / "README.md").exists()
        )
        log_kwargs["status"] = "ok"
        return (
            "\n\n".join(sections) if sections else "No feature knowledge bases found."
        )
    except Exception:
        log_kwargs["status"] = "error"
        raise
    finally:
        log_kwargs["duration_ms"] = int((time.monotonic() - t0) * 1000)
        _log("list_features", **log_kwargs)


@mcp.tool()
def search_features(query: str, project: str = "") -> str:
    """Search every feature's README.md and memories files for keyword matches.

    WHEN: Call when you need to verify or discover a SPECIFIC FACT that might live
    in a feature you are not currently working in — e.g. "was X already done
    elsewhere", "did anyone already hit this bug", "is there a constraint about Z
    in another feature". Never call this to find which slug to load —
    session-start `list_features` already covers that.

    FORMAT: query is space-separated keywords, OR-matched case-insensitively as
    whole words (substring-inside-another-word doesn't count, e.g. "term" won't
    match "determines") — a line counts as a hit if it contains ANY keyword.
    Filler words (a, the, not, of, is, ...) are auto-dropped before matching —
    they'd otherwise dominate every hit. Never pass a full sentence expecting
    AND-semantics; use 2-4 distinctive keywords, not more — each is OR'd
    independently. Searches README.md + every memories-*.md (all
    contributors) per feature in scope, minus [resolved:XXXX]/
    [supersedes:XXXX] entries.

    Args:
        query: Space-separated keywords, OR-matched, case-insensitive.
        project: Optional project name or path — defaults to the active session
            project. Comma-separate names for a subset, or "all" for every
            project — only if the user asks; never infer this or pick
            subset-vs-all yourself — ask, offering real project names as
            choices.

    OUTPUT: Results are lightweight snippets grouped by slug, ranked by relevance
    (most distinct keywords matched first) — NOT full KB content. Each hit shows
    "(N/M kw)"; N==M is the strongest signal. For any slug that looks relevant,
    call load_feature_context(slug) before relying on the snippet alone. Capped
    at MAX_SEARCH_RESULTS; if truncated, shown hits are already the best —
    narrow the query, don't assume a better one was cut. Zero hits ≠ confirmed
    absence — retry a broader keyword first.

    Examples:
        SEARCH "kubernetes istio envoy" -> 0 hits -> WHY: absent isn't proof
        -> APPLY: retry with a synonym.
    """
    t0 = time.monotonic()
    if project == "all":
        projects = _projects()
    elif "," in project:
        names = {p.strip() for p in project.split(",") if p.strip()}
        projects = [p for p in _projects() if p.name in names]
    else:
        projects = _active_project(project)
    multi_project = len(projects) > 1
    log_kwargs: dict = {
        "query": query,
        "project": _resolved_project(projects, project),
        "hit_count": 0,
        "status": "rejected",
    }
    try:
        if not projects:
            log_kwargs["status"] = "ok"
            return "No projects configured. Add paths to ~/.recall-mcp/config.json."

        keywords = [w.lower() for w in query.split() if w.strip()]
        if not keywords:
            _log_reject(log_kwargs, "empty query")
            return "Empty query — provide at least one keyword."
        significant = [kw for kw in keywords if kw not in _SEARCH_STOPWORDS]
        dropped_stopwords = [kw for kw in keywords if kw in _SEARCH_STOPWORDS]
        if significant:
            keywords = significant
        else:
            dropped_stopwords = []  # query was stopwords-only — keep it as-is, don't zero it out
        keyword_patterns = [
            re.compile(rf"(?<![a-zA-Z0-9]){re.escape(kw)}(?![a-zA-Z0-9])")
            for kw in keywords
        ]
        total_keywords = len(set(keywords))  # distinct count — dedupes a repeated word

        def _matched_keywords(text: str) -> set[str]:
            low = text.lower()
            return {
                kw
                for kw, pat in zip(keywords, keyword_patterns, strict=True)
                if pat.search(low)
            }

        def _matches(text: str) -> bool:
            return bool(_matched_keywords(text))

        def _snippet(text: str) -> str:
            """Window ~SEARCH_SNIPPET_HALF_WINDOW chars around the first match —
            never a prefix slice, which can cut past a match that lands late in
            a long line and silently drop the reason the hit was returned."""
            window = SEARCH_SNIPPET_HALF_WINDOW * 2
            if len(text) <= window:
                return text
            low = text.lower()
            pos = next(
                (m.start() for pat in keyword_patterns if (m := pat.search(low))),
                None,
            )
            if pos is None:
                start, end = 0, window
            else:
                start = max(0, pos - SEARCH_SNIPPET_HALF_WINDOW)
                end = min(len(text), pos + SEARCH_SNIPPET_HALF_WINDOW)
            # Snap to word boundaries so the cut doesn't land mid-word.
            if start > 0:
                space = text.rfind(" ", 0, start)
                if space != -1:
                    start = space + 1
            if end < len(text):
                space = text.find(" ", end)
                if space != -1:
                    end = space
            return (
                ("…" if start > 0 else "")
                + text[start:end]
                + ("…" if end < len(text) else "")
            )

        hits_by_key: dict[tuple[str, str], list[str]] = {}
        # Distinct keywords matched per (proj, slug) — ranks which slugs are shown
        # first when total hits exceed MAX_SEARCH_RESULTS, instead of alphabetical
        # order (which made truncation arbitrary rather than relevance-based).
        matched_keywords_by_key: dict[tuple[str, str], set[str]] = {}

        for proj in projects:
            kb_root = _kb_root(proj)
            if not kb_root.is_dir():
                continue
            for feature_dir in sorted(kb_root.glob("*")):
                if not feature_dir.is_dir():
                    continue
                readme = feature_dir / "README.md"
                if not readme.exists():
                    continue
                slug = feature_dir.name
                slug_hits: list[str] = []
                slug_matched: set[str] = set()

                readme_text = _strip_html_comments(readme.read_text())
                for tag_m in re.finditer(r"<(\w+)>(.*?)</\1>", readme_text, re.DOTALL):
                    section = tag_m.group(1)
                    for line in tag_m.group(2).splitlines():
                        line = line.strip()
                        kws = _matched_keywords(line) if line else set()
                        if kws:
                            slug_hits.append(
                                f"[{section}] {_snippet(line)} "
                                f"({len(kws)}/{total_keywords} kw)"
                            )
                            slug_matched |= kws

                combined, _malformed = _merge_memories(
                    _memories_files(feature_dir), readme.read_text()
                )
                for block in re.split(r"\n\s*\n", combined):
                    block = block.strip()
                    if not block:
                        continue
                    line_kws = [
                        (line.strip(), _matched_keywords(line))
                        for line in block.splitlines()
                    ]
                    matching_entries = [(line, kws) for line, kws in line_kws if kws]
                    if not matching_entries:
                        continue
                    for _, kws in matching_entries:
                        slug_matched |= kws
                    entry_id = _entry_id(block)
                    label = f"id:{entry_id}" if entry_id else "memory"
                    shown_entries = matching_entries[:2]
                    shown_kws: set[str] = set()
                    for _, kws in shown_entries:
                        shown_kws |= kws
                    snippets = (_snippet(line) for line, _ in shown_entries)
                    slug_hits.append(
                        f"[{label}] "
                        + " / ".join(snippets)
                        + f" ({len(shown_kws)}/{total_keywords} kw)"
                    )

                if slug_hits:
                    matched_keywords_by_key[(proj.name, slug)] = slug_matched
                    hits_by_key.setdefault((proj.name, slug), []).extend(slug_hits)

        if not hits_by_key:
            log_kwargs["status"] = "ok"
            note = (
                f" (ignored common word(s): {', '.join(dropped_stopwords)})"
                if dropped_stopwords
                else ""
            )
            return (
                f"No matches for '{query}'{note} across {len(projects)} project(s).\n"
                "This does not confirm the fact doesn't exist anywhere — "
                "word-boundary OR-match still misses synonyms and rephrasing. "
                "Retry with a different or broader keyword before concluding "
                'absence; don\'t state "not done anywhere" from one search miss.'
            )

        # Rank by how many distinct keywords a slug matched (descending) before
        # truncating — otherwise the shown MAX_SEARCH_RESULTS hits are whichever
        # happen to sort first alphabetically, not the most relevant ones.
        ranked_keys = sorted(
            hits_by_key,
            key=lambda k: (-len(matched_keywords_by_key.get(k, set())), k[0], k[1]),
        )
        flat_hits = [(key, hit) for key in ranked_keys for hit in hits_by_key[key]]
        total_hits = len(flat_hits)
        shown_hits = flat_hits[:MAX_SEARCH_RESULTS]
        truncated = total_hits > MAX_SEARCH_RESULTS

        hit_lines = [
            f"- **{proj_name + '/' if multi_project else ''}{slug}** {hit}"
            for (proj_name, slug), hit in shown_hits
        ]
        # Defensive backstop: per-hit snippet windowing already bounds size in
        # practice, but guard the total the same way load_feature_context guards
        # KB_FULL_RENDER_LIMIT_CHARS, rather than trust the per-hit cap alone.
        while (
            hit_lines
            and sum(len(line) + 1 for line in hit_lines) > SEARCH_RESULT_CHAR_LIMIT
        ):
            hit_lines.pop()
            shown_hits.pop()
            truncated = True

        stopword_note = (
            f" (ignored common word(s): {', '.join(dropped_stopwords)})"
            if dropped_stopwords
            else ""
        )
        parts = [
            (
                f"# Search: '{query}'{stopword_note} — {total_hits} hit(s) across "
                f"{len(hits_by_key)} feature(s), ranked by relevance"
            )
        ]
        parts.extend(hit_lines)
        if truncated:
            parts.append(
                f"\n(truncated at {len(shown_hits)} of {total_hits} total hits "
                "— narrow the query for full coverage)"
            )
        parts.append(
            "\n---\n"
            "These are lightweight snippets, not full content — do not answer from "
            "the snippet text alone.\n"
            "**Redirect signal:** if a hit's text says something like \"Wrong-KB "
            'duplicate" or names an authoritative KB for this topic, load THAT '
            "slug — the snippet is telling you where the real answer lives, not "
            "the one it happened to match in.\n"
            "**Noise check:** before loading a slug, check whether the matched "
            'keyword is only a substring inside an unrelated word (e.g. "term" '
            'inside "determines") or the snippet is clearly off-topic — skip '
            "those, don't spend a load_feature_context call on them.\n"
            "**Multiple slugs hit:** load the slug(s) whose snippet most directly "
            "answers your actual question first — you don't need to load every "
            "slug that appeared."
        )
        if multi_project:
            parts.append(
                "**Cross-project results — ask, don't auto-load:** each result "
                "above is prefixed `<project>/<slug>`. Do not decide yourself "
                "which to load — show the user the matched project/slug list "
                "and let them pick. Once they do, pass that project "
                'explicitly — load_feature_context(slug, project="<project>") '
                "— never call it bare, which defaults to your own session's "
                "project and could silently load an unrelated same-named "
                "feature instead of erroring."
            )

        log_kwargs["hit_count"] = total_hits
        log_kwargs["status"] = "ok"
        return "\n".join(parts)
    except Exception:
        log_kwargs["status"] = "error"
        raise
    finally:
        log_kwargs["duration_ms"] = int((time.monotonic() - t0) * 1000)
        _log("search_features", **log_kwargs)


def _arg_type_directive(name: str, value: object, expected: str) -> str:
    """Directive for a `load_feature_context` arg whose *type* is wrong.

    Distinct from the value-level guards next to it (`Invalid max_chars`,
    `Unknown section(s)`): those speak to a well-formed value that is out of
    range, this one to a malformed one. Both name the fix, because a model that
    sent a string where the schema said array cannot act on "expected array"
    alone — it needs to see the call it should have made instead.
    """
    return (
        f"Invalid {name}: got {type(value).__name__}, expected {expected}. "
        f"Re-issue the call with the correct type — retrying this call unchanged "
        f"will fail the same way."
    )


def _coerce_positive_int(value: object) -> int | None:
    """`value` as an int where one was asked for, else None.

    `bool` is rejected explicitly before the int check: `isinstance(True, int)`
    is True in Python, so without that guard `max_chars=True` would sail through
    as 1 — a budget small enough to read as a bug rather than a typo. A numeric
    string is accepted because it is unambiguous and common; anything else
    returns None so the caller can send a directive instead of crashing.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError:
            return None
    return None


def _coerce_str_list(value: object) -> list[str] | None:
    """`value` as a list of non-empty strings where one was asked for, else None.

    A bare string is accepted because it is unambiguous — a single name, a
    comma-joined list and a whitespace-joined list all mean exactly one thing,
    and rejecting them would only cost a round trip on a case that has a
    correct reading. A list is accepted only when every element is a string;
    anything else (a dict, a list of ints) returns None to be rejected rather
    than silently `str()`-coerced into something the caller never asked for.
    """
    if isinstance(value, str):
        return [p for p in re.split(r"[,\s]+", value) if p]
    if isinstance(value, list) and all(isinstance(p, str) for p in value):
        return [p.strip() for p in value if p.strip()]
    return None


@mcp.tool()
def load_feature_context(
    slug: str,
    project: str = "",
    expand_ids: list[str] | None = None,
    max_chars: int | None = None,
    sections: list[str] | None = None,
) -> str:
    """Load README.md and all memories files for a specific feature.

    WHEN: Call at the start of any task that touches a feature. Read the full
    output before acting — critical_warnings, business_rules, and architecture
    constrain every decision this session. Don't skip this; missing context
    causes repeated mistakes.

    FORMAT: slug must match the directory name exactly (e.g. 'payment-gateway',
    not 'feature-payment-gateway'). Fuzzy resolution (difflib >=0.8) is a
    fallback only — never pass a path or prefix. expand_ids is a list of
    entry ids (e.g. ["a1b2c3"]) — pass only ids seen in a "memories index"
    section from a prior call on the same slug; never guess an id. The three
    optional args also accept their obvious string form (max_chars="9000",
    sections="critical_warnings,overview"); a wrong *type* is rejected with a
    directive naming the form to re-send, never silently ignored.

    Args:
        slug: Feature slug (e.g. 'payment-gateway'). Exact match preferred.
        project: Optional — only needed if this slug exists in >1 project.
        expand_ids: Optional entry ids to force full body for — use when an
            index-only [idea]/[pattern] title looks relevant to the current
            task. Omit on the first call for a slug.
        max_chars: Optional char budget for the whole output — sections are
            dropped lowest-priority first, cut only at whole sections. An
            over-budget render leads with a directive naming what was dropped
            and how to fetch it. Default None = full render.
        sections: Optional allow-list of README section names to render only
            those (e.g. ['critical_warnings']); omits the memories block. Default
            None = all sections.

    OUTPUT:
    - "too large to load safely": this blocks only the unfiltered, unbudgeted
      render — retry with `sections=[...]` or `max_chars=` for a partial one.
      Never read memories-*.md directly
      (skips this tool's [supersedes:XXXX]/[resolved:XXXX] filtering — retired
      entries would look current). Ask the user before running /recall:compact +
      /recall:tidy — user-request-only, never automatic.
    - Apply critical_warnings, business_rules, and architecture immediately —
      the footer repeats this every call.
    - "*(resolved from 'x')*" in the header means fuzzy match was used — verify
      it's the right feature before proceeding.
    - "memories index" section: [idea]/[pattern] entries are title-only here to
      save context. If one looks relevant, call load_feature_context(slug,
      expand_ids=[...]) again now for its full body — don't guess from the
      title alone, and don't wait to be asked.
    - expand_ids miss: if a requested id matches no live entry, the footer says
      so and names it — the body was NOT included. Re-check the id against the
      index; a mistyped and a retired id are indistinguishable from here.
    - Related features (from the response, or a request touching a known feature
      KB) → call load_feature_context for it now, don't ask first; notify after:
      "[recall-mcp] Loaded KB `{slug}` — detected your request touches {reason}."
      Exception: a KB you are considering LINKING the current unmapped branch to
      is not "related" — the link is the user's call; ask before loading it.
    - The footer also repeats the related-features rule (plus what to do when
      uncertain), the KB self-consistency rule, and the save-memory rule every
      call — no need to memorize them from this docstring.
    - key_files bootstrap: if empty/placeholder-only, grep the codebase for the
      main entry points and call update_readme(section="key_files", ...)
      immediately — don't ask, don't defer.
    - Stale value guard: grep the codebase to verify any quoted literal/constant/
      number in the KB before relying on it — stale values fail silently with
      no error signal.

    Example: "external API enforces idempotency keys per 24h window — retry
        within it silently replays the stale response" -> non-obvious
        external constraint -> save_memory now. "Added retry with backoff"
        -> visible in diff -> don't save.
    """
    t0 = time.monotonic()
    projects = _active_project(project)
    log_kwargs: dict = {
        "slug": slug,
        "project": _resolved_project(projects, project),
        "readme_chars": 0,
        "memories_chars": 0,
        "memories_count": 0,
        "memories_date_parse_failures": 0,
        "expand_ids_missing": 0,
        "status": "rejected",
    }
    try:
        # Type guards run BEFORE the value guards below, and that order is the
        # point: the value guards are written for well-formed values and behave
        # badly on malformed ones. Measured on the three union-typed optionals
        # (`expand_ids` / `max_chars` / `sections`, declared `list[str] | None`
        # and `int | None`, so MCP emits `anyOf: [{type: array|integer},
        # {type: null}]` — a shape a client or provider may flatten, after which
        # a small model emits a bare string):
        #   - `max_chars="9000"` -> the `<= 0` check ITSELF raised TypeError, so
        #     the "Invalid max_chars" message below was never reachable.
        #   - `sections="critical_warnings"` -> iterated character-by-character,
        #     reporting `Unknown section(s): 'c', 'r', 'i', ...`.
        #   - `expand_ids="a1b2c3"` -> iterated character-by-character, matched
        #     no id, and returned a NORMAL successful render with the requested
        #     body silently absent — no error signal at all, so nothing to
        #     self-correct from.
        # Coercion, not blanket rejection: the string forms above have exactly
        # one sensible reading, so accepting them costs nothing and removes a
        # round trip on a small model's most likely mistake. Only the shapes
        # with no correct reading get a directive. Note the harness, not this
        # server, is what records "the model sent a string where an array was
        # declared" — coercion here must not be read as evidence the schema
        # reached the model intact.
        if max_chars is not None:
            coerced_max_chars = _coerce_positive_int(max_chars)
            if coerced_max_chars is None:
                _log_reject(log_kwargs, "bad max_chars type")
                return _arg_type_directive(
                    "max_chars", max_chars, "a positive integer (or omit it)"
                )
            max_chars = coerced_max_chars
        if max_chars is not None and max_chars <= 0:
            _log_reject(log_kwargs, "invalid max_chars value")
            return f"Invalid max_chars {max_chars}: must be a positive integer or None."
        if sections is not None:
            coerced_sections = _coerce_str_list(sections)
            if coerced_sections is None:
                _log_reject(log_kwargs, "bad sections type")
                return _arg_type_directive(
                    "sections", sections, "a list of section-name strings (or omit it)"
                )
            sections = coerced_sections
            invalid = [s for s in sections if s not in VALID_SECTIONS]
            if invalid:
                _log_reject(log_kwargs, "unknown section(s)")
                return (
                    f"Unknown section(s): {', '.join(repr(s) for s in invalid)}. "
                    f"Valid sections: {', '.join(VALID_SECTIONS)}."
                )
        if expand_ids is not None:
            coerced_ids = _coerce_str_list(expand_ids)
            if coerced_ids is None:
                _log_reject(log_kwargs, "bad expand_ids type")
                return _arg_type_directive(
                    "expand_ids", expand_ids, "a list of entry-id strings (or omit it)"
                )
            expand_ids = coerced_ids
        original_slug = slug
        result = _resolve_slug(slug, projects)
        if isinstance(result, str):
            _log_reject(log_kwargs, "slug ambiguous")
            return result
        d, slug, was_fuzzy = result
        if not d:
            _log_reject(log_kwargs, "slug not found")
            return _not_found_msg(slug, projects)

        # Read content first so token estimate can be included in the header
        readme = d / "README.md"
        readme_text = ""
        readme_stripped = ""
        related_hint = ""
        if readme.exists():
            readme_text = readme.read_text()
            readme_stripped = _strip_readme_for_context(readme_text, keep_markers=True)
            m = re.search(
                r"<related_tickets>\s*(.*?)\s*</related_tickets>",
                readme_text,
                re.DOTALL,
            )
            if m:
                content = _strip_html_comments(m.group(1))
                lines = [line.strip() for line in content.splitlines() if line.strip()]
                if lines:
                    bullets = "\n".join(f"  • {line}" for line in lines)
                    related_hint = f"**Related features — check before answering:**\n{bullets}\nIf the current task touches any of these, call `load_feature_context('<slug>')` NOW — do not wait.\n"

        key_files_hint = ""
        file_index, symbol_index = _key_files_index(projects)
        strong_related, weak_related = _related_by_key_files(
            slug, file_index, symbol_index
        )
        if strong_related or weak_related:
            ranked = sorted(
                set(strong_related) | set(weak_related),
                key=lambda other: (-len(strong_related.get(other, [])), other),
            )
            bullet_lines = []
            for other in ranked:
                strong_paths = strong_related.get(other, [])
                weak_paths = weak_related.get(other, [])
                all_paths = strong_paths + weak_paths
                unverified = _unverified_key_files(all_paths, projects)
                rendered_parts = []
                for p in all_paths:
                    if p in weak_paths:
                        rendered_parts.append(
                            f"`{p}` ({_weak_match_reason(p, file_index)}, weak signal)"
                        )
                    else:
                        rendered_parts.append(
                            f"`{p}`" + (" (unverified)" if p in unverified else "")
                        )
                rendered = ", ".join(rendered_parts)
                if strong_paths and weak_paths:
                    count_label = f"{len(strong_paths)} shared + {len(weak_paths)} weak"
                elif strong_paths:
                    count_label = f"{len(strong_paths)} shared"
                else:
                    count_label = f"{len(weak_paths)} weak"
                bullet_lines.append(f"  • {other} ({count_label}): shares {rendered}")
            bullets = "\n".join(bullet_lines)
            key_files_hint = (
                f"**Possibly related (shared key_files, unconfirmed):**\n{bullets}\n"
                f"This is a structural signal, not yet a curated link — "
                f"only load one of these if the current task actually touches the shared "
                f"file(s); don't auto-load on this hint alone. Paths marked '(hub — N "
                f"features, weak signal)' are low-confidence. Use judgment on the rest too: "
                f"a shared boilerplate filename (`__init__.py`, `pyproject.toml`, `ci.yaml`, "
                f"a version/ID-like string, etc.) is usually coincidence, not evidence — "
                f"weigh a specific, substantive shared path or symbol much higher. If it "
                f"turns out relevant, promote it into this KB's own related_tickets section "
                f"via update_readme(section='related_tickets', mode='append') — a short "
                f"`slug: reason` pointer, same format as a curated related_tickets entry, "
                f"not a copy of the other KB's content.\n"
            )

        combined = ""
        index_text = ""
        gating_blocks: list[str] = []
        expanded_blocks: list[str] = []
        unpromoted_gating = 0
        index_all = False
        cut_titles = 0
        missing_expand_ids: list[str] = []
        memory_files = _memories_files(d)
        if memory_files:
            entries, log_kwargs["memories_date_parse_failures"] = (
                _collect_memory_entries(memory_files, readme_text)
            )
            unpromoted_gating = _count_unpromoted_gating(entries)
            expand_id_set = {e.strip().lower() for e in (expand_ids or [])}
            # Index-all threshold is platform-dependent: Claude (max_chars=None)
            # keeps full-body memories up to the full-render ceiling; Copilot (a
            # budgeted call) demotes to titles as soon as full body exceeds its
            # injection budget — almost always — so it gets README + titles and
            # fetches bodies via expand_ids.
            effective_full_limit = (
                max_chars if max_chars is not None else KB_FULL_RENDER_LIMIT_CHARS
            )
            index_all = (
                len(readme_stripped) + sum(len(b) for _, b in entries)
                > effective_full_limit
            )
            index_lines = []
            combined_blocks = []
            for _entry_date, block in entries:
                eid = _entry_id(block)
                if eid in expand_id_set:
                    # Explicitly requested — full body, guaranteed in the render.
                    expanded_blocks.append(block)
                    combined_blocks.append(block)
                elif index_all or _entry_tag(block) in INDEX_ONLY_TAGS:
                    index_lines.append(block.splitlines()[0])
                else:
                    gating_blocks.append(block)
                    combined_blocks.append(block)
            combined = "\n\n".join(combined_blocks)
            index_text, cut_titles = _cap_index_titles(
                index_lines, KB_INDEX_TITLE_BUDGET_CHARS
            )
            # An id that matched nothing used to be the quietest failure of the
            # three: the render came back normal, successful, and simply without
            # the requested body. A mistyped id and a retired id look identical
            # from here, so this reports the miss and names the id rather than
            # guessing which it was.
            live_ids = {_entry_id(b).lower() for _, b in entries if _entry_id(b)}
            missing_expand_ids = sorted(expand_id_set - live_ids)
            log_kwargs["expand_ids_missing"] = len(missing_expand_ids)

        readme_loaded = len(readme_stripped)
        memories_loaded = len(combined) + len(index_text)
        total_chars = readme_loaded + memories_loaded
        # Cosmetic estimate for the header only — NOT the safety gate below,
        # which deliberately compares total_chars directly (see
        # KB_FULL_RENDER_LIMIT_CHARS' comment: bisected on real char counts,
        # not tokens). Don't "fix" this by making the gate token-based.
        total_tokens = total_chars // 4

        if (
            max_chars is None
            and not sections
            and total_chars > KB_FULL_RENDER_LIMIT_CHARS
        ):
            log_kwargs["status"] = "oversized"
            log_kwargs["readme_chars"] = readme_loaded
            log_kwargs["memories_chars"] = memories_loaded
            return (
                f"KB '{slug}' is too large to load safely: README ~{readme_loaded:,} chars + "
                f"memories ~{memories_loaded:,} chars = ~{total_chars:,} chars total "
                f"(limit ~{KB_FULL_RENDER_LIMIT_CHARS:,}). Returning this would exceed the "
                f"tool-result size cap and fail outright — do not retry this call unchanged, "
                f"it will fail the same way.\n"
                f"If you only need part of it, retry with a filter — "
                f"load_feature_context('{slug}', sections=['critical_warnings']) or "
                f"load_feature_context('{slug}', max_chars=9000) both render fine at this "
                f"size. Only the unfiltered, unbudgeted render is blocked.\n"
                f"Do NOT read `{d}/memories-*.md` directly as a workaround — those raw files "
                f"don't have this tool's [supersedes:XXXX]/[resolved:XXXX] filtering applied, "
                f"so retired conclusions would look just as valid as current ones.\n"
                f"Tell the user: KB '{slug}' has grown too large to load and needs maintenance "
                f"— ask whether to run /recall:compact (shrinks memories) and /recall:tidy "
                f"(shrinks README) now. Do not run them without asking first — both are "
                f"explicitly user-request-only, never automatic. If the user declines or this "
                f"turn moves on without running them, load_feature_context('{slug}') stays "
                f"blocked and this KB cannot be loaded at all until it's shrunk — encourage "
                f"running it rather than letting the KB stay unusable. Once back under the "
                f"limit, re-run load_feature_context('{slug}')."
            )

        header = (
            f"# Feature context: {d.parent.name}/{slug}"
            f" (~{total_tokens:,} tokens: README ~{readme_loaded // 4:,} + memories ~{memories_loaded // 4:,})"
        )
        if was_fuzzy:
            header += f"\n*(resolved from '{original_slug}')*"

        hints = _kb_health_hints(readme_text, combined + index_text, unpromoted_gating)
        hints_text = "\n".join(hints) if hints else ""

        index_part = ""
        if index_text:
            scope = "all memories" if index_all else "[idea]/[pattern]"
            tail_note = (
                f"; {cut_titles} older titles omitted — use search_features to find them"
                if cut_titles
                else ""
            )
            index_part = (
                f"## memories index ({scope}{tail_note} — title-only to save context)\n\n"
                f"{index_text}\n\n"
                f"If one of these titles looks relevant to the current task, call "
                f"load_feature_context('{slug}', expand_ids=['<id>']) now for its full "
                f"body — don't guess from the title alone, and don't wait to be asked."
            )

        footer = "---\n"
        if missing_expand_ids:
            footer += (
                f"**⚠️ `expand_ids` matched no live entry: "
                f"{', '.join(missing_expand_ids)}** — those ids were ignored and the "
                f'body was NOT included. Check the id against the "memories index" '
                f"list from a prior render (ids are 6-char hex, e.g. `a1b2c3`); a "
                f"mistyped or retired id matches nothing.\n"
            )
        if related_hint:
            footer += related_hint + "\n"
        if key_files_hint:
            footer += key_files_hint + "\n"
        footer += (
            f"**Apply immediately:** critical_warnings, business_rules, and architecture "
            f"above constrain every decision this session — apply them before acting, don't "
            f"just skim past them.\n"
            f"**Stale-check:** if a memory above carries [supersedes:XXXX] and the "
            f"replaced claim still sits verbatim in critical_warnings, "
            f"business_rules, architecture, or open_items, that section is stale — "
            f"fix it now via update_readme, don't wait for /recall:save. A "
            f"[resolved:XXXX] memory is the opposite case: its block in README is "
            f"the live copy, so leave it alone.\n"
            f"**Inline save rule for this session:** the moment you discover a bug root cause, "
            f"non-obvious constraint, gotcha, or rejected approach — call "
            f"`save_memory(slug='{slug}', ...)` immediately at that point. "
            f"Do not defer to end of session — deferred saves are forgotten.\n"
            f"**Cross-feature uncertainty:** when unsure whether a related/mapped feature "
            f"KB actually applies to the current task, load it anyway — missing "
            f"cross-feature context costs more than one extra `load_feature_context` call. "
            f"Exception: a KB you are considering LINKING the current unmapped branch to is "
            f"not 'related' — linking is the user's call, so ask before loading it."
        )

        if max_chars is None and not sections:
            # Default path — bit-for-bit identical to the pre-budget render.
            parts = [header]
            if hints_text:
                parts.append(hints_text)
            if readme_stripped:
                parts.append(f"## README.md\n\n{readme_stripped}")
            if combined:
                parts.append(f"## memories\n\n{combined}")
            if index_part:
                parts.append(index_part)
            parts.append(footer)
            result = "\n\n".join(parts)
        else:
            result = _render_budgeted(
                slug=slug,
                header=header,
                hints=hints_text,
                readme_stripped=readme_stripped,
                memories=gating_blocks,
                expanded=expanded_blocks,
                index_part=index_part,
                footer=footer,
                sections_filter=sections,
                max_chars=max_chars,
            )

        log_kwargs["slug"] = slug
        log_kwargs["readme_chars"] = readme_loaded
        log_kwargs["memories_chars"] = memories_loaded
        log_kwargs["memories_count"] = sum(
            1 for ln in combined.splitlines() if ln.startswith("- **")
        )
        log_kwargs["status"] = "ok"
        return result
    except Exception:
        log_kwargs["status"] = "error"
        raise
    finally:
        log_kwargs["duration_ms"] = int((time.monotonic() - t0) * 1000)
        _log("load_feature_context", **log_kwargs)


MEMORY_TAGS = ("gotcha", "bug", "decision", "constraint", "rule", "idea", "pattern")
# "resolved" is a valid standalone leading tag too — the closure format is
# `[resolved:XXXX] <one-line reason>` with no category tag before it (see
# commands/compact.md Step 4). "supersedes" never leads alone — the replace
# format keeps the original category tag first: "[tag][supersedes:XXXX] ...".
FIRST_TAG_ALLOWED = (*MEMORY_TAGS, "resolved")

# Non-Latin script characters: CJK, Kana, Hangul, Cyrillic, Arabic, Devanagari,
# Thai, plus the Latin-1 / Latin Extended blocks that carry Vietnamese and other
# European diacritics (đ ă ơ ư ạ ả ậ ố …). The "English only" save_memory rule
# is enforced against this set rather than left as a docstring.
_NON_ENGLISH_RE = re.compile(
    "["
    "\u00c0-\u024f"  # Latin-1 Supplement + Latin Extended-A/B — Vietnamese/European diacritics
    "\u1e00-\u1eff"  # Latin Extended Additional — Vietnamese ạ/ả/ậ/ố series
    "\u3040-\u30ff"  # Hiragana + Katakana
    "\u3400-\u4dbf"  # CJK Unified Ideographs Extension A
    "\u4e00-\u9fff"  # CJK Unified Ideographs
    "\uf900-\ufaff"  # CJK Compatibility Ideographs
    "\uac00-\ud7af"  # Hangul syllables
    "\u0400-\u04ff"  # Cyrillic
    "\u0600-\u06ff"  # Arabic
    "\u0900-\u097f"  # Devanagari
    "\u0e00-\u0e7f"  # Thai
    "]"
)

# Tolerance, not strict ≥1 character: a proper noun or a single quoted foreign
# term must not fail the gate, so it fires only when the content is substantially
# non-English — the real failure mode is a model writing the whole entry in
# another language, which produces dozens of matches, not one or two.
KB_NON_ENGLISH_CHAR_THRESHOLD = 5


def _validate_memory_content(content: str) -> str:
    """Return an error message if content fails the hard quality gate, else "".

    Server-side enforcement, not just docstring guidance — catches the empty/
    near-empty save_memory call regardless of whether the caller read or
    followed the prompt instructions (observed twice in real usage: a "quiet
    round" Save-check line followed by a reflexive save_memory call anyway).
    Also enforces "English only" against `_NON_ENGLISH_RE`, with tolerance
    (`KB_NON_ENGLISH_CHAR_THRESHOLD`) so a proper noun or quoted foreign term
    doesn't trip it.
    """
    stripped = content.strip()
    if len(stripped) < 15:
        return (
            "content is empty or too short (<15 chars) to be a real insight. "
            "If there's nothing to save, do not call save_memory at all — just "
            "print the Save-check line and stop."
        )
    # Leading "**" (markdown bold, e.g. "**[gotcha] title**") is common and valid —
    # strip it before checking for the opening bracket.
    tag_m = re.match(r"^\*{0,2}\[([a-zA-Z]+)", stripped)
    if not tag_m:
        return "content must start with a [tag] — one of: " + ", ".join(
            f"[{t}]" for t in MEMORY_TAGS
        )
    if tag_m.group(1) not in FIRST_TAG_ALLOWED:
        return f"first tag [{tag_m.group(1)}] is not recognized. Allowed: " + ", ".join(
            f"[{t}]" for t in FIRST_TAG_ALLOWED
        )
    non_english = _NON_ENGLISH_RE.findall(stripped)
    if len(non_english) >= KB_NON_ENGLISH_CHAR_THRESHOLD:
        return (
            "content must be written in English — found "
            f"{len(non_english)} non-Latin character(s)"
            f" (e.g. {''.join(non_english[:5])})."
        )
    return ""


_HTML_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)


def _strip_html_comments(text: str) -> str:
    """Remove template placeholder comments (e.g. '<!-- Example: ... -->') and trim.

    Without this, a freshly-init_feature'd section's "existing" content is the
    template's placeholder comments, not truly empty — so the first real
    append preserves that placeholder text verbatim instead of treating the
    section as empty (observed in the wild across multiple real KBs).
    """
    return _HTML_COMMENT_RE.sub("", text).strip()


def _strip_readme_for_diff(text: str) -> str:
    """`_strip_html_comments`, but keeping `from:` provenance markers.

    The preview variant: `update_readme`'s most consequential effect on a section
    is often a marker change, not a wording change — a dropped marker returns the
    matching memory's body to the next load, and an added one means a later
    supersede will replace the block wholesale. Diffing comment-stripped text
    hides exactly that, so the approved diff would not be the change being made.
    """
    return _NON_MARKER_COMMENT_RE.sub("", text).strip()


# ---------------------------------------------------------------------------
# S1 — mechanical promotion (tag -> README section), run at save time
# ---------------------------------------------------------------------------

# `<!-- from:XXXX YYYY-MM-DD -->`, appended to every promoted README block. It
# names the memory entry the block came from, so when that entry is later
# superseded the machinery can find the block and REPLACE it instead of
# appending a second, contradictory one. HTML comments are stripped by
# `_strip_readme_for_context`, so the marker is invisible in context and costs
# no budget — it lives on disk only, where the supersede path reads it.
#
# The date is the source entry's own date, carried because it is the one field
# `_entry_content` strips on the way out and nothing else re-adds: a promoted
# block would otherwise lose when the knowledge was learned. Optional — markers
# written before 2026-10-03 have no date, and those READMEs are read-only as far
# as this regex is concerned.
#
# The date group is `\S+`, not a date shape, on purpose: the id is what makes a
# block supersedeable, so a hand-mangled date must cost the date and never the
# id. Pinning a shape here means a marker like `<!-- from:X 01/01/2026 -->` stops
# parsing at all — provenance is lost silently, and the next supersede appends a
# duplicate instead of replacing.
_PROMOTED_MARKER_RE = re.compile(r"<!--\s*from:([a-f0-9]{4,6})(?:\s+(\S+))?\s*-->")

# A README entry block opens at a line starting with `**[tag] ...**` — the shape
# both save_memory's content format and the existing sections already use.
_BLOCK_OPENER_RE = re.compile(r"^\*\*\[")


def _promoted_marker(entry_id: str, date: str = "") -> str:
    """The provenance marker for a block promoted from `entry_id`.

    `date` is the source entry's date. Empty means "unknown" — `_promoted_marker`
    is also the re-attach path for markers recovered from an existing README, and
    a pre-2026-10-03 marker carries no date to recover.
    """
    return f"<!-- from:{entry_id} {date} -->" if date else f"<!-- from:{entry_id} -->"


# `[supersedes:XXXX]` / `[resolved:XXXX]` on a promoted entry's opening line.
_CLOSURE_REF_RE = re.compile(
    r"\[(?:supersedes|resolved):[a-f0-9]{4,6}\]", re.IGNORECASE
)


def _promoted_body(content: str) -> str:
    """The README text for a promoted entry: its content, closure refs stripped.

    A closure ref names a memory entry that is not visible in the README, so it
    is bookkeeping that leaks — the `from:` marker already carries the real
    provenance. Stripping it also keeps the block's opening line equal to what a
    natural rewrite produces; leaving a dangling `[supersedes:XXXX]` in the title
    is precisely the edit a rewording model makes, which would drop the marker
    (`_carry_promoted_markers` matches openers verbatim) and silently break
    replace-on-supersede. Purely mechanical — no judgement, no LLM.
    """
    lines = content.strip().splitlines()
    lines[0] = re.sub(r"\s{2,}", " ", _CLOSURE_REF_RE.sub("", lines[0])).strip()
    return "\n".join(lines)


def _marked_source_ids(block: str) -> set[str]:
    """Entry ids named by `from:` markers inside one block (usually 0 or 1)."""
    return {m.group(1).lower() for m in _PROMOTED_MARKER_RE.finditer(block)}


def _marked_markers(block: str) -> list[str]:
    """The literal `from:` marker strings inside one block, in order.

    Literal, not rebuilt from the id: a carry-forward re-attach must preserve the
    marker's date, and `_promoted_marker(sid)` cannot reconstruct one that the
    memory no longer holds (a superseded source, a hand-added marker).
    """
    return [m.group(0) for m in _PROMOTED_MARKER_RE.finditer(block)]


def _marker_id(marker: str) -> str:
    """The entry id a literal `from:` marker names, or "" if it names none."""
    m = _PROMOTED_MARKER_RE.search(marker)
    return m.group(1).lower() if m else ""


def _normalized_block_body(block_text: str) -> str:
    """A block's body, markers removed and stripped — the ONE normalization
    shared by both sides of the provenance question.

    `_captured_ids` asks it on read ("is this README block still the memory's
    text?"); `_recognized_sources` asks it on write ("is this text a memory's
    body?"). They must not normalize differently: a one-character disagreement
    attaches or drops a `from:` marker silently, and a dropped marker makes a
    later supersede append a duplicate instead of replacing the block.
    """
    return _PROMOTED_MARKER_RE.sub("", block_text).strip()


def _readme_block_spans(section_text: str) -> list[tuple[int, int]]:
    """Line spans of `**[tag] ...**` blocks inside a README section body.

    A block opens at any line starting with `**[` and owns every line up to the
    next opener or the end of the section. This matches how promoted entries are
    written and how the entry sections already look. A prose paragraph that
    happens to open with bold text reads as a block too — harmless, because it
    is the marker lookup, not the span, that identifies a *promoted* block.
    """
    spans: list[tuple[int, int]] = []
    start: int | None = None
    for i, line in enumerate(section_text.splitlines()):
        if _BLOCK_OPENER_RE.match(line.strip()):
            if start is not None:
                spans.append((start, i))
            start = i
    if start is not None:
        spans.append((start, len(section_text.splitlines())))
    return spans


def _find_promoted_span(section_text: str, source_id: str) -> tuple[int, int] | None:
    """Line span of the block carrying `from:source_id`, or None if absent.

    None is the ordinary miss, not an error: the source entry may never have been
    promoted, or a user may have reworded its block through update_readme — which
    drops the marker on purpose, because a reworded block is a hand edit and the
    block is then the user's to own.
    """
    lines = section_text.splitlines()
    for start, end in _readme_block_spans(section_text):
        if source_id in _marked_source_ids("\n".join(lines[start:end])):
            return start, end
    return None


def _captured_ids(readme_text: str, entries: list[tuple[str, str]]) -> set[str]:
    """Memory ids fully captured by a live, verbatim README block.

    A memory X is *captured* when its promoted block (carrying `<!-- from:X -->`)
    still lives in a promotable README section AND that block's body still equals
    `_promoted_body(_entry_content(X))` — i.e. no `update_readme` trim has happened
    since promotion. The comparison is verbatim, not semantic: a trimmed or
    paraphrased block fails it, so X stays live (its Why/Apply detail survives only
    in the memory). Captured memories are hidden on read exactly like
    `[resolved:XXXX]` targets — the README block is the source of truth.
    """
    content_by_id = {
        _entry_id(block): _entry_content(block)
        for _, block in entries
        if _entry_id(block)
    }
    if not content_by_id:
        return set()

    captured: set[str] = set()
    for section in PROMOTABLE_SECTIONS:
        span = _section_span(readme_text, section)
        if span is None:
            continue
        section_body = readme_text[span[0] : span[1]]
        for start, end in _readme_block_spans(section_body):
            block_text = "\n".join(section_body.splitlines()[start:end])
            source_ids = _marked_source_ids(block_text)
            if not source_ids:
                continue
            block_body = _normalized_block_body(block_text)
            for sid in source_ids:
                if (
                    sid in content_by_id
                    and _promoted_body(content_by_id[sid]).strip() == block_body
                ):
                    captured.add(sid)
    return captured


def _block_opener(lines: list[str], start: int) -> str:
    """A block's opening line, for naming it back to the caller in a footer."""
    return lines[start].strip()[:120] if start < len(lines) else "(empty block)"


def _section_span(text: str, section: str) -> tuple[int, int] | None:
    """Char offsets of a section body (between its tags), or None if not found."""
    open_tag, close_tag = f"<{section}>", f"</{section}>"
    start, end = text.find(open_tag), text.find(close_tag)
    if start == -1 or end == -1:
        return None
    return start + len(open_tag), end


def _closure_ids(first_line: str, kind: str) -> set[str]:
    """Target ids of `[kind:XXXX]` on an entry's first line (kind = supersede/resolved)."""
    return {
        m.group(1).lower()
        for m in re.finditer(
            rf"\[{kind}:([a-f0-9]{{4,6}})\]", first_line, re.IGNORECASE
        )
    }


def _carry_promoted_markers(old_section: str, new_content: str) -> str:
    """Re-attach provenance markers across an `update_readme` section rewrite.

    `update_readme` replaces a section wholesale with model-supplied content, so
    without this every marker dies on the first rewrite — after which a later
    `[supersedes:Y]` can no longer find its block and appends a duplicate
    instead, silently. Markers are matched back by the block's opening line
    verbatim: a block whose opener the user reworded keeps no marker, which is
    the intended reading — they rewrote it, so it is theirs.

    The whole literal marker moves across, date included — it is recovered from
    the README, not rebuilt from the id, since a superseded source's memory no
    longer carries the date the block was promoted with.
    """
    old_lines = old_section.splitlines()
    carried: dict[str, str | None] = {}  # opener -> literal marker; None = ambiguous
    for start, end in _readme_block_spans(old_section):
        opener = old_lines[start].strip()
        for marker in _marked_markers("\n".join(old_lines[start:end])):
            prior = carried.get(opener)
            if opener in carried and _marker_id(prior or "") != _marker_id(marker):
                carried[opener] = None  # same title, different sources — don't guess
            else:
                carried[opener] = marker
    if not any(carried.values()):
        return new_content

    lines = new_content.splitlines()
    inserts: list[tuple[int, str]] = []
    for start, end in _readme_block_spans(new_content):
        carried_marker = carried.get(lines[start].strip())
        if carried_marker and not _marked_source_ids("\n".join(lines[start:end])):
            inserts.append((end - 1, carried_marker))
    for idx, marker in reversed(inserts):  # reverse: earlier indices stay valid
        lines[idx] = lines[idx] + "\n" + marker
    return "\n".join(lines)


def _marker_ids_in(text: str) -> set[str]:
    """Every `from:` id carried by any block in a section body."""
    lines = text.splitlines()
    return {
        sid
        for start, end in _readme_block_spans(text)
        for sid in _marked_source_ids("\n".join(lines[start:end]))
    }


def _recognized_sources(
    entries: list[tuple[str, str]], new_content: str
) -> tuple[dict[tuple[int, int], str], list[str], list[str]]:
    """Map a block's line span -> memory id, for blocks that ARE a memory's
    promoted body.

    Recognition is verbatim (`_normalized_block_body`), never fuzzy: an attached
    marker means a later `[supersedes:X]` replaces that block *wholesale*, so it
    is only safe while the text is still byte-identical to the memory's body — a
    replacement is then a no-op. A paraphrased block gets no marker: it is a hand
    edit, and leaving the memory live keeps its Why/Apply detail in the render.

    Keyed by the block's `(start, end)` span rather than by its opening line,
    unlike `_carry_promoted_markers`: two blocks can share a title (the duplicate
    shape this needs to see), and an opener-keyed map would mark both with one id.
    The span also lets `_attach_recognized_markers` look the block up without
    assuming its own `_readme_block_spans` pass produced identical indices.

    Returns (recognized, ambiguous, duplicated). Ambiguity — two memories sharing
    one body — resolves to no marker, matching `_carry_promoted_markers`' "same
    title, different sources" rule. `duplicated` names a second block claiming an
    id already claimed: it has no source of its own, so it is surfaced instead of
    marked.
    """
    by_body: dict[str, set[str]] = {}
    for _, block in entries:
        eid = _entry_id(block)
        if eid:
            by_body.setdefault(
                _promoted_body(_entry_content(block)).strip(), set()
            ).add(eid)

    lines = new_content.splitlines()
    recognized: dict[tuple[int, int], str] = {}
    ambiguous: list[str] = []
    duplicated: list[str] = []
    claimed: set[str] = set()
    for start, end in _readme_block_spans(new_content):
        opener = lines[start].strip()
        ids = by_body.get(_normalized_block_body("\n".join(lines[start:end])))
        if not ids:
            continue
        if len(ids) > 1:
            ambiguous.append(opener)
            continue
        sid = next(iter(ids))
        if sid in claimed:
            duplicated.append(opener)
            continue
        claimed.add(sid)
        recognized[(start, end)] = sid
    return recognized, ambiguous, duplicated


def _attach_recognized_markers(
    new_content: str,
    recognized: dict[tuple[int, int], str],
    dates: dict[str, str] | None = None,
) -> str:
    """Append a `from:` marker to every recognized block that lacks one.

    `dates` maps memory id -> that entry's date, so a newly attached marker
    carries when the knowledge was learned. A missing id (or no map at all)
    writes the dateless marker rather than skipping the marker.

    Shares `_carry_promoted_markers`' insertion shape — block-end index, spliced
    back-to-front — so the two mechanisms cannot disagree about where a marker
    belongs inside a block.
    """
    if not recognized:
        return new_content
    dates = dates or {}
    lines = new_content.splitlines()
    inserts: list[tuple[int, str]] = []
    for (start, end), sid in recognized.items():
        if not _marked_source_ids("\n".join(lines[start:end])):
            inserts.append((end - 1, _promoted_marker(sid, dates.get(sid, ""))))
    for idx, marker in sorted(inserts, reverse=True):  # earlier indices stay valid
        lines[idx] = lines[idx] + "\n" + marker
    return "\n".join(lines)


def _finalize_readme_content(
    d: Path, section: str, old_body: str, new_content: str
) -> tuple[str, str]:
    """The one place README marker policy is applied: (final_content, notes).

    `update_readme`'s preview and its write both call this, so the text a user
    approves is the text that lands. Previously the preview diffed the model's
    raw content while only the write path ran `_carry_promoted_markers`, so
    provenance could change between approval and write with nothing showing it.

    Two mechanisms, in order:
    1. Recognition (content-based) — a block that is verbatim a live memory's
       body gets that memory's marker. This is what makes a copied memory
       supersedeable instead of an orphan no `[supersedes:X]` can reach.
    2. Carry-forward (opener-based, `_carry_promoted_markers`) — the fallback for
       blocks whose opening line survived a rewrite.

    Entries are read WITHOUT `readme_text`: the captured filter would hide
    exactly the memories whose blocks are in the README, which are the ones
    worth recognizing. Closure filtering (`[supersedes]`/`[resolved]` targets)
    still applies, so a retired memory is never re-marked.
    """
    if section not in PROMOTABLE_SECTIONS:
        # overview/key_files/open_items/... have no promoting memory behind them,
        # so nothing there is provenance-bearing. Carry still runs — it is what
        # the write path did for every section before recognition existed.
        return _carry_promoted_markers(old_body, new_content), ""

    entries, _ = _collect_memory_entries(_memories_files(d))
    recognized, ambiguous, duplicated = _recognized_sources(entries, new_content)
    dates = {
        eid: _entry_date(block) for _, block in entries if (eid := _entry_id(block))
    }
    content = _attach_recognized_markers(new_content, recognized, dates)
    content = _carry_promoted_markers(old_body, content)

    notes: list[str] = []
    if recognized:
        notes.append(
            f"Provenance: {len(recognized)} block(s) recognised as promoted from a "
            f"memory — marker attached ({', '.join(sorted(recognized.values()))})."
        )
    if ambiguous:
        notes.append(
            f"⚠️ Provenance: {len(ambiguous)} block(s) match more than one memory — "
            "no marker attached; supersede by hand if it is a duplicate."
        )
    if duplicated:
        notes.append(
            f"⚠️ {len(duplicated)} block(s) duplicate another block's content here — a "
            "marker on only the first means a later supersede leaves the copy behind."
        )
    dropped = _marker_ids_in(old_body) - _marker_ids_in(content)
    if dropped:
        notes.append(
            f"Provenance: {len(dropped)} marker(s) dropped ({', '.join(sorted(dropped))}) "
            "— the matching memory's body returns to the next load."
        )
    return content, "\n".join(notes)


def _tokenize(text: str) -> frozenset[str]:
    """Lowercased alphanumeric tokens of `text`, stopwords dropped (stdlib only)."""
    return frozenset(
        t for t in re.findall(r"[a-z0-9_]+", text.lower()) if t not in _SEARCH_STOPWORDS
    )


def _distinctive_tokens(blocks: list[str]) -> frozenset[str]:
    """Tokens appearing in at most half of `blocks` — the set Jaccard runs over.

    A token present in every block discriminates nothing; keeping it would make
    two same-domain blocks read as near-identical on shared vocabulary alone.
    Dropping >50%-frequency tokens is the stdlib (TS-1) stand-in for IDF. The
    `max(1, …)` floor keeps a one-block section at plain Jaccard rather than
    dropping every token and silently disabling the gate on tiny sections.
    """
    freq: dict[str, int] = {}
    for block in blocks:
        for t in _tokenize(block):
            freq[t] = freq.get(t, 0) + 1
    if not blocks:
        return frozenset()
    cap = max(1, len(blocks) // 2)
    return frozenset(t for t, n in freq.items() if n <= cap)


def _jaccard(a: frozenset[str], b: frozenset[str]) -> float:
    """Jaccard similarity over distinctive tokens; 0.0 when both sets are empty."""
    if not a and not b:
        return 0.0
    return len(a & b) / len(a | b)


def _find_near_duplicate(
    d: Path, section: str, incoming: str
) -> tuple[str, float] | None:
    """The existing block in `section` most similar to `incoming`, if near-duplicate.

    Returns (block opener, jaccard) of the best existing block at/above
    `KB_PROMOTE_DEDUP_JACCARD_THRESHOLD`, else None. Compares the incoming
    *promoted* body (closure refs stripped — the text that would land) against
    each existing block's text, over distinctive tokens so same-domain blocks
    don't false-positive. Reads the README as it stands, before `_apply_promotion`
    writes. Pure function — no judgement, no LLM (the S4/S1 symmetry: S1 decides
    to promote, S4 decides not to).
    """
    readme = d / "README.md"
    if not readme.exists():
        return None
    text = readme.read_text()
    span = _section_span(text, section)
    if span is None:
        return None
    section_body = text[span[0] : span[1]]
    lines = section_body.splitlines()
    spans = _readme_block_spans(section_body)
    if not spans:
        return None

    existing = ["\n".join(lines[s:e]) for s, e in spans]
    distinctive = _distinctive_tokens(existing)
    inc = _tokenize(_promoted_body(incoming)) & distinctive

    best: tuple[str, float] | None = None
    for s, e in spans:
        b = _tokenize("\n".join(lines[s:e])) & distinctive
        sim = _jaccard(inc, b)
        if sim >= KB_PROMOTE_DEDUP_JACCARD_THRESHOLD and (
            best is None or sim > best[1]
        ):
            best = (_block_opener(lines, s), sim)
    return best


def _find_near_duplicate_memory(
    entries: list[tuple[str, str]], incoming: str
) -> tuple[str, float] | None:
    """The live memory entry most similar to `incoming`, if near-duplicate.

    Returns (entry id, jaccard) of the best live entry at/above
    `KB_SAVE_DEDUP_JACCARD_THRESHOLD`, else None. Mirrors `_find_near_duplicate`
    (the S4 promotion gate) but runs over live memory entries instead of README
    blocks, and at a higher threshold — this one hard-rejects a save, so it must
    err toward precision. `entries` is already filtered by
    `_collect_memory_entries`, so retired (superseded/resolved) entries never
    block a new save. Pure function — no judgement, no LLM.
    """
    if not entries:
        return None
    bodies = [_entry_content(block) for _, block in entries]
    distinctive = _distinctive_tokens(bodies)
    inc = _tokenize(incoming) & distinctive
    if not inc:
        return None

    best: tuple[str, float] | None = None
    for (_, block), body in zip(entries, bodies, strict=True):
        eid = _entry_id(block)
        if not eid:
            continue
        b = _tokenize(body) & distinctive
        sim = _jaccard(inc, b)
        if sim >= KB_SAVE_DEDUP_JACCARD_THRESHOLD and (best is None or sim > best[1]):
            best = (eid, sim)
    return best


def _apply_promotion(
    d: Path,
    section: str | None,
    entry_id: str,
    content: str,
    date: str,
    supersede_ids: set[str],
) -> str:
    """Write/replace promoted blocks; return a footer note ("" = quiet).

    `section` is the tag's mapped target, or None for a pure `[supersedes:XXXX]`
    closure entry with no promotable tag of its own — then there is nothing to
    append, only a block to replace.

    `[resolved:XXXX]` never reaches this function. It means "dealt with, and the
    knowledge lives in the README", so it hides the source memory on the read
    side (`_resolved_ids`) and must leave the README alone: the marked block is
    the live copy the receipt points at, not a stale one it retires. Deleting on
    that ref destroys the very thing the receipt asserts exists.

    Raises on any failure — the caller treats promotion as best-effort, because
    writing the memory entry is the primary act and must not fail with it.
    """
    readme = d / "README.md"
    text = readme.read_text()

    spans: dict[str, tuple[int, int]] = {}
    bodies: dict[str, list[str]] = {}
    for cand in PROMOTABLE_SECTIONS:
        cs = _section_span(text, cand)
        if cs:
            spans[cand] = cs
            bodies[cand] = text[cs[0] : cs[1]].splitlines()
    if section and section not in spans:
        # Auto-create the missing section rather than skip: init_feature makes
        # every section, so a miss means an older KB or a hand-delete, and
        # skipping would strand the memory as unpromoted (cold tier).
        if text and not text.endswith("\n"):
            text += "\n"
        text += f"<{section}>\n</{section}>\n"
        cs = _section_span(text, section)
        assert cs is not None, "just appended the section tags above"
        spans[section] = cs
        bodies[section] = text[cs[0] : cs[1]].splitlines()

    replaced: list[str] = []
    replaced_ids: set[str] = set()
    insert_at: dict[str, int] = {}
    freed = 0

    # The marker, not the tag, is the authority on where a source entry was
    # promoted — hence the scan. One entry may replace blocks in two different
    # sections (e.g. a gotcha and a decision superseded together).
    for sid in sorted(supersede_ids):
        for cand, lines in bodies.items():
            found = _find_promoted_span("\n".join(lines), sid)
            if found:
                replaced_ids.add(sid)
                replaced.append(_block_opener(lines, found[0]))
                freed += len("\n".join(lines[found[0] : found[1]]))
                if cand == section and (
                    cand not in insert_at or found[0] < insert_at[cand]
                ):
                    insert_at[cand] = found[0]
                del lines[found[0] : found[1]]
                break

    # A supersede ref that matched no `from:` marker is ambiguous: a
    # never-promoted entry (idea/pattern/bug/closure — normal), a hand-edited
    # block that lost its marker (the correction would then sit as a silent
    # duplicate), or a typo. Classify by the referenced entry's tag and warn
    # only where a block should have existed — S1 is best-effort, never silent.
    warnings: list[str] = []
    for sid in sorted(supersede_ids - replaced_ids):
        tag = _memory_tag_for_id(d, sid)
        if not tag:
            warnings.append(f"superseded id {sid} not found in memories — typo?")
        elif _id_is_retired(d, sid, entry_id):
            warnings.append(
                f"superseded id {sid} is already retired (superseded or resolved) "
                f"— its block was replaced on purpose; supersede the successor id instead"
            )
        elif tag in PROMOTE_TAGS:
            warnings.append(
                f"superseded entry {sid} ([{tag}]) has no README block — its "
                f"marker was lost via a hand edit or promotion was skipped; "
                f"check for a stale duplicate"
            )

    body_text = _promoted_body(content)
    block_lines = f"{body_text}\n{_promoted_marker(entry_id, date)}".splitlines()

    # Growth guard on the NET delta. The save-side gates ran before this and
    # measured a README without the promoted block, so an append can push
    # full-body past the ceiling with nothing watching. Refusing here (rather
    # than writing and reporting) keeps the ceiling true; the note keeps it loud,
    # because a silently-disabled promotion on a fat KB is a defect (P5).
    growth = (len(body_text) if section else 0) - freed
    if growth > 0:
        projected = _full_body_total_chars(d, "") + growth
        if projected > KB_FULL_BODY_GROWTH_LIMIT_CHARS:
            return (
                f"⚠️ Promotion skipped: this would bring full-body to "
                f"~{projected:,} chars, over the ~{KB_FULL_BODY_GROWTH_LIMIT_CHARS:,} "
                f"ceiling. The memory entry was saved — run `/recall:tidy` to shrink "
                f"the section, then promote."
            )

    if section:
        lines = bodies[section]
        at = insert_at.get(section)
        if at is not None and at <= len(lines):
            lines[at:at] = [*block_lines, ""]
        else:
            if lines and lines[-1].strip():
                lines.append("")
            lines.extend(block_lines)

    if not section and not replaced:
        return " ".join(f"⚠️ {w}." for w in warnings) if warnings else ""

    # Splice from the end so the earlier spans stay valid.
    for cand in sorted(spans, key=lambda c: spans[c][0], reverse=True):
        cs = spans[cand]
        body = re.sub(r"\n{3,}", "\n\n", "\n".join(bodies[cand])).strip()
        text = text[: cs[0]] + "\n" + body + "\n" + text[cs[1] :]
    _atomic_write(readme, text)
    _ensure_git_repo(d.parent)

    parts = [f"id {entry_id}"]
    if replaced:
        parts.append(f"replaced {len(replaced)} block(s): {'; '.join(replaced)}")
    head = f"Promoted to <{section}>" if section else "README updated"
    detail = f" ({', '.join(parts)})"
    warn = (" " + " ".join(f"⚠️ {w}." for w in warnings)) if warnings else ""
    return f"{head}{detail}.{warn}"


def _promote_if_eligible(d: Path, content: str, entry_id: str, date: str) -> str:
    """S1: promote one just-saved entry into its README section. Never raises.

    Eligibility is the pure map lookup alone — no age or tenure condition, so
    W = 0. Reversibility replaces delay: `_apply_promotion` replaces a block when
    its source entry is superseded, so promoting immediately is safe rather than
    premature.

    The contract's third conjunct ("never superseded") is vacuous on this path:
    `entry_id` was just minted, so nothing can reference it yet. It belongs to
    any future backfill pass over existing memories, not to write-time promotion.

    A *pure closure* entry — `[supersedes:XXXX]` with no promotable tag of its
    own — still runs, because the reference and not the tag is what replaces a
    block. Its `section` is None: there is nothing to append, only a block to find
    by marker and replace.

    `[resolved:XXXX]` alone does NOT enter. The README block is the live copy the
    receipt points at, so a bare receipt is a no-op here; hiding the source
    memory is `_collect_memory_entries`' job, on the read side. See
    `_apply_promotion`'s docstring for why deleting on that ref is wrong.

    S4 dedup gate: before an *append* (a `section` with no supersede), a
    near-duplicate of an existing block skips promotion and reports which block
    — the memory is already saved, and the model/user decides to merge or
    supersede. The gate is promotion-only and never runs on a `[supersedes]`
    correction, which replaces a block (net-negative) rather than adding a copy.

    Best-effort by design: a failed README write must not fail the memory save,
    but it must never be silent either — the caller surfaces the returned note.
    """
    first_line = content.strip().splitlines()[0]
    supersede_ids = _closure_ids(first_line, "supersedes")
    m = re.match(r"^\*{0,2}\[([a-zA-Z]+)", content.strip())
    section = PROMOTE_SECTIONS.get(m.group(1).lower()) if m else None
    if not section and not supersede_ids:
        return ""

    if section and not supersede_ids:
        dup = _find_near_duplicate(d, section, content)
        if dup:
            opener, sim = dup
            return (
                f"⚠️ Promotion skipped: near-duplicate of an existing <{section}> "
                f"block (Jaccard {sim:.2f}): {opener}. The memory entry was saved "
                f"— merge into that block or supersede it by hand rather than "
                f"adding a second copy."
            )

    try:
        return _apply_promotion(d, section, entry_id, content, date, supersede_ids)
    except Exception as exc:  # noqa: BLE001 — best-effort, the save is primary
        where = f"<{section}>" if section else "README"
        return (
            f"⚠️ Promotion to {where} failed ({type(exc).__name__}: {exc}). "
            f"The memory entry itself was saved — update the section by hand if it matters."
        )


@mcp.tool()
def save_memory(slug: str, content: str, project: str = "") -> str:
    """Prepend an engineering insight to a feature's memories file.

    WHEN: Skip routine steps/summaries/anything derivable from code (file
    coords, "added retry", test outcomes — a diff/git-log already shows these).
    Never call twice for the same insight — scan the loaded KB first, skip if a
    semantically identical entry exists.

    Quality gate — apply before every call:
    - Bug/constraint: would a fresh session repeat this mistake without it? -> save
    - Decision/trade-off: would re-litigating waste real effort and reach the
      same conclusion anyway? -> save
    - Already covered with equal detail in a README section? -> skip
    Never save: file coordinates (use update_readme key_files instead), what the
    code does, test outcomes, finished-work summaries — anything a grep, diff or
    git-log already shows.

    FORMAT: content = What/Why/Apply, never raw notes.
        **[tag] title (<=15 words)**
        What: <the fact, bug, or decision>
        Why: <root cause, or why alternatives were rejected>
        Apply: <code path/operation that retriggers this (bugs/constraints), or
            the future signal that would reopen it (decisions) — not
            "when working on X">
    English only. Tags: [gotcha] [bug] [decision] [constraint] [rule] [idea]
        [pattern]. [rule] = non-negotiable invariant (e.g. "payment must be
        idempotent"). [idea] = proposed, undecided.
    Size: one insight, not a document — 4000+ chars rejected, 1500+ adds a trim note.
    [supersedes:XXXX] replaces a stale entry's README block; [resolved:XXXX]
        marks one as dealt with and only hides it, leaving README untouched —
        the response footer repeats the full protocol.

    Args:
        slug: Feature slug (e.g. 'payment-gateway'). Bare name — no path/prefix.
        content: What/Why/Apply formatted insight — never raw notes.
        project: Optional — only needed if this slug exists in >1 project.

    OUTPUT: Success returns filename + date, plus a footer with the Tags list
    and promotion mapping (this survives even if the rest of this docstring
    gets truncated by the caller). "Feature not found" -> call list_features
    for the correct slug, then retry.

    Examples:
        SAVE [gotcha] "Redis maxconn=10 is per-process, not global — pool
        exhausts silently under concurrent load." Not derivable from code,
        caused a production incident -> save.
        SAVE [decision] "Rejected event sourcing for the audit log (team
        lacks Kafka expertise); chose append-only Postgres." Would be
        re-proposed by a fresh session otherwise -> save.
        SKIP "Fixed null check in payment processor" -> visible in the diff.
    """
    t0 = time.monotonic()
    # Before `re.search` and `len` below, both of which raise a bare TypeError on
    # a non-string and both of which run OUTSIDE the try — so a model that emitted
    # a number for content got `TypeError: expected string or bytes-like object`
    # as the entire tool result, with no hint of what to send instead.
    if not isinstance(content, str):
        return _arg_type_directive("content", content, "a string")
    projects = _active_project(project)
    _tag_m = re.search(r"\[([a-z]+)", content)
    log_kwargs: dict = {
        "slug": slug,
        "project": _resolved_project(projects, project),
        "tag": _tag_m.group(1) if _tag_m else "",
        "char_count": len(content),
        "status": "rejected",
    }
    try:
        validation_error = _validate_memory_content(content)
        if validation_error:
            _log_reject(log_kwargs, "content validation failed")
            return f"Rejected: {validation_error}"

        # Per-entry size guard — the only limit keyed on a single write, not the
        # whole KB. Hard rejects (pathological blob); soft only sets a note that
        # is appended to a successful save below, never a reject.
        content_len = len(content.strip())
        if content_len > KB_MEMORY_ENTRY_HARD_LIMIT_CHARS:
            _log_reject(log_kwargs, "content over hard limit")
            return _memory_too_long_msg(content_len, hard=True)
        length_note = (
            _memory_too_long_msg(content_len, hard=False)
            if content_len > KB_MEMORY_ENTRY_SOFT_LIMIT_CHARS
            else ""
        )

        result = _resolve_slug(slug, projects)
        if isinstance(result, str):
            _log_reject(log_kwargs, "slug ambiguous")
            return result
        d, slug, _ = result
        if not d:
            _log_reject(log_kwargs, "slug not found")
            return (
                _not_found_msg(slug, projects) + "\nCreate it first with init_feature."
            )

        # Save-time dedup: enforce "Never call twice for the same insight" against
        # the live memory entries. A correction (`[supersedes:XXXX]`) or a closure
        # (`[resolved:XXXX]`) is BY DESIGN near-identical to the entry it retires,
        # so those always bypass — the gate must never block the escape hatch it
        # names.
        first_line = content.strip().splitlines()[0]
        has_closure = _closure_ids(first_line, "supersedes") | _closure_ids(
            first_line, "resolved"
        )
        if not has_closure:
            entries, _ = _collect_memory_entries(_memories_files(d))
            dup = _find_near_duplicate_memory(entries, content)
            if dup:
                eid, sim = dup
                _log_reject(log_kwargs, "near-duplicate save")
                return (
                    f"Rejected: near-duplicate of existing entry [id:{eid}] "
                    f"(Jaccard {sim:.2f}). Do not save the same insight twice — "
                    f"scan the loaded KB and skip if a semantically identical "
                    f"entry already exists. If this is a correction of that "
                    f"entry, save it with [supersedes:{eid}] instead."
                )

        username = _resolve_username()
        memories_file = d / f"memories-{username}.md"
        today = _today_iso()
        entry_id = secrets.token_hex(3)  # 6-char hex, unique per entry
        new_entry = f"- **{today}** [id:{entry_id}]: {content.strip()}"

        blocked = _growth_gate(d, slug, new_entry)
        if blocked:
            log_kwargs["status"] = "oversized"
            return blocked

        footer = (
            "\n---\n"
            "**Prepend-only:** this call never edits or merges an existing entry — "
            "to correct an old one, save a new entry instead: tag [supersedes:XXXX] "
            "when it's replaced by this new content, or [resolved:XXXX] when it is "
            "dealt with and has no README block (ID from loaded KB either way). "
            "[resolved:XXXX] only hides the memory from future loads — it never edits "
            "the README. You do NOT need it after a promotion: a promoted memory whose "
            "block is still verbatim is auto-hidden on load.\n"
            "**Closing an idea:** if this entry is a [decision] that resolves a prior "
            "[idea], tag it [decision][supersedes:XXXX] using the idea's ID.\n"
            "**Tags:** [gotcha] [bug] [decision] [constraint] [rule] [idea] [pattern] "
            "([rule]=non-negotiable invariant, [idea]=proposed/undecided).\n"
            "**Promotion:** automatic at save time — [gotcha]/[constraint]->critical_warnings, "
            "[decision]->architecture, [rule]->business_rules; [bug]/[idea]/[pattern] are skipped. "
            "Read the note above: `Promoted to <section> (id X).` means the block landed with its "
            "`<!-- from:X -->` marker — that id is how you supersede this block later. "
            "`⚠️ Promotion skipped`/`failed` means act on the stated reason (growth ceiling -> "
            "run /recall:tidy). Hand-writing the same content instead works too, but copy the "
            "memory's body verbatim — a verbatim block is recognised and gets its marker "
            "attached, while a paraphrase stays an orphan that no later supersede can replace. "
            "The promoted memory is "
            "auto-hidden from future loads once its block is captured (verbatim) — do not write "
            "[resolved:XXXX] after a promotion; [resolved:XXXX] is only for something dealt with "
            "that has no README block (e.g. a [bug] fixed in code). A [decision] promotion also "
            "re-checks open_items for rows it resolves — ask before marking any row resolved.\n"
            '**critical_warnings tip:** prefer "verify at file.py::SYMBOL" over quoting '
            "a value directly — quoted values go stale silently when code changes."
        )

        if not memories_file.exists():
            _atomic_write(
                memories_file,
                f"# Engineering Memory — {username}\n\n*Prepend-only.*\n\n---\n\n{new_entry}\n",
            )
            _ensure_git_repo(d.parent)
            log_kwargs["status"] = "ok"
            promo = _promote_if_eligible(d, content, entry_id, today)
            log_kwargs["promotion"] = promo[:80]
            return (
                f"Created memories-{username}.md and saved entry ({today})."
                f"{length_note}{footer}{promo}{_health_hint_suffix(d)}"
            )

        text = memories_file.read_text()
        lines = text.splitlines()

        inserted = False
        for i, line in enumerate(lines):
            if line.strip() == "---":
                lines.insert(i + 1, "")
                lines.insert(i + 2, new_entry)
                lines.insert(i + 3, "")
                inserted = True
                break

        if not inserted:
            lines.append(new_entry)

        _atomic_write(memories_file, "\n".join(lines) + "\n")
        _ensure_git_repo(d.parent)
        log_kwargs["status"] = "ok"
        promo = _promote_if_eligible(d, content, entry_id, today)
        log_kwargs["promotion"] = promo[:80]
        return (
            f"Saved to {slug}/memories-{username}.md ({today})."
            f"{length_note}{footer}{promo}{_health_hint_suffix(d)}"
        )
    except Exception:
        log_kwargs["status"] = "error"
        raise
    finally:
        log_kwargs["duration_ms"] = int((time.monotonic() - t0) * 1000)
        _log("save_memory", **log_kwargs)


@mcp.tool()
def init_feature(
    name: str,
    slug: str,
    summary: str,
    project: str = "",
    username: str = "",
    ticket: str = "",
    branch: str = "",
) -> str:
    """Create a new feature knowledge base with README.md and a per-person memories file.

    WHEN: Call when starting work on a new feature that doesn't have a KB yet.
    Check list_features first to confirm the slug doesn't already exist.
    Never create a KB without confirming the slug and username with the user first.

    FORMAT:
    - slug: lowercase, hyphens only (e.g. 'payment-gateway'). Becomes the directory
      name — cannot be renamed without also updating features.md. No 'feature-' prefix.
    - username: detect from git, show to user, let them override before calling.
    - branch: pass the current branch — used by the hook for auto-load matching.

    Args:
        name: Human-readable feature name (e.g. 'Payment Gateway').
        slug: URL-safe identifier — becomes the directory name. Choose carefully.
        summary: One-line description shown in the features.md index. Keep to ≤15 words —
            injected once per session (first message only). Include component names,
            actions, and key nouns Claude can match to user requests
            (e.g. "Stripe checkout + webhook reconciliation + invoice emails").
        project: Project name or path. Required if multiple projects are configured.
        username: Username for memories file. Detect from git, confirm with user before calling.
        ticket: Jira/Linear ticket ID (e.g. 'PROJ-1234'). Optional.
        branch: Git branch (e.g. 'feat/payment-gateway'). Used for hook auto-load. Optional.

    OUTPUT: On success, the README has empty section placeholders.
    If invoked via /recall:init, the command handles the fill flow — do NOT start
    asking for section content directly.
    If "⚠ Not configured" appears in the response, show the snippet to
    the user and ask them to add it to their project's CLAUDE.local.md (gitignored).
    """
    t0 = time.monotonic()
    projects = _active_project(project)
    log_kwargs: dict = {
        "slug": slug,
        "project": _resolved_project(projects, project),
        "status": "rejected",
    }
    try:
        if not projects:
            log_kwargs["status"] = "ok"
            return "No projects configured. Add paths to ~/.recall-mcp/config.json."
        if len(projects) > 1:
            _log_reject(log_kwargs, "multiple projects")
            names = ", ".join(p.name for p in projects)
            return f"Multiple projects found ({names}). Specify which one with the project argument."

        target = projects[0]
        feature_dir = _kb_root(target) / slug

        if feature_dir.exists():
            _log_reject(log_kwargs, "slug already exists")
            return f"Feature '{slug}' already exists at {feature_dir}."

        feature_dir.mkdir(parents=True)
        today = _today_iso()

        # Template uses $name/$slug/$summary (not {name}/.format()) -- templates
        # are hand-edited docs that may legitimately contain illustrative `{...}`
        # examples of their own (e.g. `{project-name}/{slug}/README.md` in a
        # key_files example, already used verbatim in this project's own KB).
        # A $-sigil placeholder can never collide with a curly-brace illustration,
        # and safe_substitute() leaves any unrecognized $-token untouched instead
        # of raising -- .format() would crash on the first stray `{...}` and
        # .replace() would silently corrupt a `{slug}`-shaped illustration.
        readme_tmpl = Template((TEMPLATES_DIR / "feature-README.md").read_text())
        _atomic_write(
            feature_dir / "README.md",
            readme_tmpl.safe_substitute(name=name, slug=slug, summary=summary),
        )

        username = _resolve_username(confirmed=username)
        memories_tmpl = Template((TEMPLATES_DIR / "feature-memories.md").read_text())
        _atomic_write(
            feature_dir / f"memories-{username}.md",
            memories_tmpl.safe_substitute(name=name),
        )

        # Update features.md index
        index_file = _kb_root(target) / "features.md"
        new_row = f"| {name} | {slug} | {ticket} | {branch} | {summary} | {today} |"
        if index_file.exists():
            text = index_file.read_text()
            lines = text.splitlines()
            insert_at = len(lines)
            for i, line in enumerate(lines):
                if line.strip().startswith("---"):
                    insert_at = i
                    break
            lines.insert(insert_at, new_row)
            _atomic_write(index_file, "\n".join(lines) + "\n")
        else:
            index_tmpl = Template((TEMPLATES_DIR / "features-index.md").read_text())
            _atomic_write(index_file, index_tmpl.safe_substitute(first_row=new_row))

        _ensure_git_repo(_kb_root(target))

        setup_hint = ""
        if not _has_recall_setup(target):
            snippet = (TEMPLATES_DIR / "claude-md-snippet.md").read_text().strip()
            setup_hint = (
                f"\n\n⚠  Not configured: {target / 'CLAUDE.local.md'} has no recall-mcp section.\n"
                f"Add this to your CLAUDE.local.md (gitignored — per-developer, not team-shared):\n\n{snippet}"
            )

        log_kwargs["status"] = "ok"
        return (
            f"Created feature KB '{slug}' in {target.name}:\n"
            f"  {feature_dir}/README.md\n"
            f"  {feature_dir}/memories-{username}.md\n"
            f"Updated features.md index."
            f"{setup_hint}"
        )
    except Exception:
        log_kwargs["status"] = "error"
        raise
    finally:
        log_kwargs["duration_ms"] = int((time.monotonic() - t0) * 1000)
        _log("init_feature", **log_kwargs)


@mcp.tool()
def report_miss(slug: str, description: str, project: str = "") -> str:
    """Record a context miss — a mistake Claude made that the KB should have prevented.

    WHEN: Call specifically when the user corrects Claude on something that (a) relates
    to a known feature AND (b) is the kind of constraint or pattern that should exist
    in the KB. The user may invoke this via /recall:miss.
    Do NOT use for general corrections or first-time mistakes with no prior KB context.
    Never call this proactively — only when a user explicitly flags a miss.

    FORMAT: description should cover both what went wrong AND what the correct behavior
    should be, so future sessions can act on it without reconstructing context.
    It is still one entry, so it is capped like one: over 4000 chars is rejected,
    over 1500 appends a "condense" note to the result.

    Args:
        slug: Feature slug where the miss occurred.
        description: What Claude did wrong + what the correct behavior should be.
        project: Optional project name or path to filter results.

    OUTPUT: On success, returns confirmation with filename and date.
    Immediately after, strengthen the KB — do not ask the user which path:
      - Rule/constraint that should always hold → call update_readme on the appropriate
        section (critical_warnings, business_rules, or architecture).
      - One-time edge case or incident → call save_memory with [constraint] or [gotcha]
        tag for richer What/Why/Apply context.
      - If both apply → do both.
    If this MISS was recorded with the wrong root cause: correct it by calling
      save_memory(slug=..., content="[gotcha][supersedes:XXXX] <corrected root cause>")
      where XXXX is the [id:XXXX] returned in this response. The incorrect MISS is
      hidden from future loads; the corrected entry stays visible.

    EXAMPLES: the user rarely types the exact words "report a miss" — recognize it from
    plain corrections, no special syntax required.
      SAVE (loaded KB already covered this, Claude still got it wrong): User: "that's
      wrong — the KB literally already says never use --directory for uv run." -> the
      rule was loaded and specific; Claude missed it -> report_miss.
      SKIP (nothing in the loaded KB about this topic): User: "no, that's wrong" on a
      first-time mistake -> not a KB miss -> save_memory with the correct insight instead.
    """
    t0 = time.monotonic()
    # Same non-string gap as save_memory.content, and again before the try: a
    # numeric description raised `TypeError: object of type 'int' has no len()`
    # from the log_kwargs line, so the caller never reached the write at all.
    if not isinstance(description, str):
        return _arg_type_directive("description", description, "a string")
    projects = _active_project(project)
    log_kwargs: dict = {
        "slug": slug,
        "project": _resolved_project(projects, project),
        "char_count": len(description),
        "status": "rejected",
    }
    try:
        # Per-entry size guard — same ceiling as save_memory, because a MISS is
        # also one entry in the same file. This writer had no gate at all: the
        # three largest entries in the live store (876-1100 chars) are all MISSes
        # written here, unvalidated and unchecked.
        content_len = len(description.strip())
        if content_len > KB_MEMORY_ENTRY_HARD_LIMIT_CHARS:
            _log_reject(log_kwargs, "content over hard limit")
            return _memory_too_long_msg(content_len, hard=True)
        length_note = (
            _memory_too_long_msg(content_len, hard=False)
            if content_len > KB_MEMORY_ENTRY_SOFT_LIMIT_CHARS
            else ""
        )

        result = _resolve_slug(slug, projects)
        if isinstance(result, str):
            _log_reject(log_kwargs, "slug ambiguous")
            return result
        d, slug, _ = result
        if not d:
            _log_reject(log_kwargs, "slug not found")
            return _not_found_msg(slug, projects)

        username = _resolve_username()
        memories_file = d / f"memories-{username}.md"
        today = _today_iso()
        entry_id = secrets.token_hex(3)  # 6-char hex, unique per entry
        new_entry = f"- **{today}** [id:{entry_id}] [MISS]: {description.strip()}"

        blocked = _growth_gate(d, slug, new_entry)
        if blocked:
            log_kwargs["status"] = "oversized"
            return blocked

        if not memories_file.exists():
            _atomic_write(
                memories_file,
                f"# Engineering Memory — {username}\n\n*Prepend-only.*\n\n---\n\n{new_entry}\n",
            )
        else:
            text = memories_file.read_text()
            lines = text.splitlines()
            inserted = False
            for i, line in enumerate(lines):
                if line.strip() == "---":
                    lines.insert(i + 1, "")
                    lines.insert(i + 2, new_entry)
                    lines.insert(i + 3, "")
                    inserted = True
                    break
            if not inserted:
                lines.append(new_entry)
            _atomic_write(memories_file, "\n".join(lines) + "\n")

        _ensure_git_repo(d.parent)
        log_kwargs["status"] = "ok"
        return (
            f"Recorded [MISS] [id:{entry_id}] in {slug}/memories-{username}.md "
            f"({today}).{length_note}"
        )
    except Exception:
        log_kwargs["status"] = "error"
        raise
    finally:
        log_kwargs["duration_ms"] = int((time.monotonic() - t0) * 1000)
        _log("report_miss", **log_kwargs)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

VALID_SECTIONS = (
    "overview",
    "key_files",
    "technical_stack",
    "business_rules",
    "architecture",
    "critical_warnings",
    "open_items",
    "checklist",
    "related_tickets",
)


@mcp.tool()
def update_readme(
    slug: str,
    section: str,
    content: str,
    project: str = "",
    mode: str = "replace",
    confirm: bool = False,
) -> str:
    """Replace or append content in a named XML section of a feature's README.md.

    WHEN: Call after init_feature to fill empty section placeholders, or when
    architecture or business rules change and a section is outdated.
    Do NOT use to append minor notes — use save_memory for dynamic insights; README
    sections are for stable, architectural knowledge.

    FORMAT:
    - mode 'append': writes immediately, adds after existing content — safe, nothing lost.
    - mode 'replace' (default): two-step. First call (confirm=False, default)
      writes nothing and returns a REAL diff (old vs proposed) — show it
      verbatim to the user, never your own paraphrase. Call again with
      confirm=True (same args) only after approval, to actually write.
    - Never pass the full tag (e.g. '<business_rules>') as section — use the tag name only.

    Language: MUST write all section content in English.

    Args:
        slug: Feature slug (e.g. 'payment-gateway').
        section: XML tag name only (e.g. 'business_rules') — not the full tag.
        content: New content to place inside the section tags.
        project: Optional project name or path to filter results.
        mode: 'replace' (default) diff-then-write; 'append' writes immediately.
        confirm: Only for mode='replace'. False (default) previews a diff,
            writes nothing. True actually writes — pass it only after showing
            the diff and getting approval (or when the caller already has its
            own no-approval convention, e.g. /recall:tidy).

    OUTPUT: mode='replace' + confirm=False -> a diff, nothing written; show it
    verbatim, then re-call with confirm=True to write. Otherwise returns
    "Updated <section> in slug/README.md (mode)." on success.
    If "Section not found" is returned, the README may have duplicate or missing tags —
    inspect the file manually before retrying.

    Content guidelines per section: business_rules/critical_warnings — **[tag] title**
    blocks (constraints/bugs/gotchas); architecture — prose or bullets (components, data
    flow, decisions); overview — 2-4 sentences (what/value/design choice); key_files —
    bullets w/ path+symbol names, never line:col (drifts); technical_stack — bullets
    (lib/framework + why); open_items — table (ID | Issue | Priority | Blocks); checklist
    — markdown checkboxes by phase; related_tickets — one line per entry (slug (TICKET): reason).
    """
    t0 = time.monotonic()
    projects = _active_project(project)
    log_kwargs: dict = {
        "slug": slug,
        "project": _resolved_project(projects, project),
        "section": section,
        "mode": mode,
        "confirm": confirm,
        "char_count": len(content),
        "status": "rejected",
    }
    try:
        if section not in VALID_SECTIONS:
            _log_reject(log_kwargs, "unknown section")
            return f"Unknown section '{section}'. Valid sections: {', '.join(VALID_SECTIONS)}."
        if mode not in ("replace", "append"):
            _log_reject(log_kwargs, "unknown mode")
            return f"Unknown mode '{mode}'. Valid modes: replace, append."
        if not content.strip():
            _log_reject(log_kwargs, "empty content")
            return "Rejected: content is empty — refusing to write a blank section."
        if not _strip_html_comments(content):
            _log_reject(log_kwargs, "content is only comments")
            return (
                "Rejected: content is only HTML comments (e.g. a copied template "
                "placeholder) — refusing to write a blank section."
            )
        result = _resolve_slug(slug, projects)
        if isinstance(result, str):
            _log_reject(log_kwargs, "slug ambiguous")
            return result
        d, slug, _ = result
        if not d:
            _log_reject(log_kwargs, "slug not found")
            return (
                _not_found_msg(slug, projects) + "\nCreate it first with init_feature."
            )

        readme = d / "README.md"
        text = readme.read_text()

        open_tag = f"<{section}>"
        close_tag = f"</{section}>"
        start = text.find(open_tag)
        end = text.find(close_tag)

        if start == -1 or end == -1:
            _log_reject(log_kwargs, "section not found")
            return f"Section <{section}> not found in {slug}/README.md."

        old_body = text[start + len(open_tag) : end]
        existing = _strip_readme_for_diff(old_body)
        new_content = content.strip()

        if mode == "append":
            # Blank-line join so appended entries stay visually separated for
            # a human scanning the file, regardless of tag/symbol convention.
            new_content = (existing + "\n\n" + new_content) if existing else new_content

        # One finalize call for both paths, so the preview diffs the text that
        # will actually be written. Before, only the write path ran marker
        # policy — the preview showed the model's raw content, and a provenance
        # change between approval and write was invisible.
        final, provenance = _finalize_readme_content(d, section, old_body, new_content)
        updated = text[: start + len(open_tag)] + "\n" + final + "\n" + text[end:]

        # README is injected whole on every load and no save gate covers this
        # tool, so a section write is the one path that can grow the KB past a
        # ceiling unopposed. Both paths check it: a preview that showed a diff
        # which the write then refused would be worse than either alone.
        blocked = _readme_write_blocked(d, updated)

        if mode == "replace" and not confirm:
            diff = "\n".join(
                difflib.unified_diff(
                    existing.splitlines(),
                    final.splitlines(),
                    fromfile="current",
                    tofile="proposed",
                    lineterm="",
                )
            )
            log_kwargs["status"] = "ok"
            if not diff:
                return (
                    f"No changes — proposed content for <{section}> is identical "
                    "to current. Nothing to write."
                )
            note = f"\n{provenance}\n" if provenance else ""
            if blocked:
                return (
                    f"Diff preview for <{section}> in {slug}/README.md — nothing written yet:\n\n"
                    f"{diff}\n{note}\n"
                    f"{_readme_blocked_msg(slug, section, *blocked)}\n\n"
                    "Show this diff verbatim to the user (not your own summary), the "
                    "refusal above included — re-running with confirm=True would not "
                    "write it. Shrink the section first, or write less."
                )
            return (
                f"Diff preview for <{section}> in {slug}/README.md — nothing written yet:\n\n"
                f"{diff}\n{note}\n"
                "Show this diff verbatim to the user (not your own summary). "
                "Call update_readme again with confirm=True and the same args to write it."
            )

        if blocked:
            log_kwargs["status"] = "oversized"
            return _readme_blocked_msg(slug, section, *blocked)

        _atomic_write(readme, updated)
        _ensure_git_repo(d.parent)
        log_kwargs["status"] = "ok"
        suffix = f"\n{provenance}" if provenance else ""
        suffix += _health_hint_suffix(d)
        if section == "key_files":
            suffix += _key_files_format_hint(final)
        if section in ("critical_warnings", "architecture", "business_rules"):
            suffix += (
                "\nIf this content came from a save_memory entry that has no live "
                "`<!-- from:XXXX -->` marker here, consider marking that memory "
                "[resolved:XXXX] — gotcha/decision/rule/constraint memories are always "
                "full-loaded by load_feature_context (never index-compressed), so an "
                "unresolved duplicate wastes context on every future load. Do NOT do "
                "this for a block whose marker is present: it is already hidden on "
                "load, and the marker is what lets a later correction replace it."
            )
        return f"Updated <{section}> in {slug}/README.md ({mode}).{suffix}"
    except Exception:
        log_kwargs["status"] = "error"
        raise
    finally:
        log_kwargs["duration_ms"] = int((time.monotonic() - t0) * 1000)
        _log("update_readme", **log_kwargs)


_FEATURE_INDEX_FIELDS = {"ticket": 3, "branch": 4, "summary": 5}


@mcp.tool()
def update_feature_index(
    slug: str,
    field: str,
    value: str,
    project: str = "",
    append: bool = False,
    confirm: bool = False,
) -> str:
    """Update one cell (ticket, branch, or summary) of a feature's row in features.md.

    WHEN: Call when a feature's index row has drifted from reality — summary no longer
    matches scope, branch was renamed, or a ticket ID was added/changed. Typically noticed
    at end-of-session (e.g. /recall:save) or when linking a renamed branch to its KB
    (/recall:link-feature). Never call this to edit README.md content — use update_readme
    for that; this only touches the features.md index row. Never edit features.md directly
    with Edit — always go through this tool.

    FORMAT: confirm=False (default) previews a diff, writes nothing. Call again with
    confirm=True and the same args only after showing the diff and getting approval.
    Never skip straight to confirm=True — the index row is visible to every session
    across the project, not just this one. append=True adds `, {value}` to the existing
    cell instead of replacing it — use for branch when a KB now tracks multiple branches;
    for summary/ticket, replacing (append=False) is almost always correct.

    Args:
        slug: Feature slug whose index row to update (e.g. 'payment-gateway').
        field: One of 'ticket', 'branch', 'summary'.
        value: New cell content. summary: ≤15 words, same guidance as init_feature's summary arg.
        project: Optional project name or path to filter results.
        append: False (default) replaces the cell. True appends ", {value}" to it.
        confirm: False (default) previews a diff. True writes it — only after approval.

    OUTPUT: confirm=False -> a diff of the index row (old vs proposed), nothing written;
    show it verbatim, then re-call with confirm=True to write. Otherwise returns
    "Updated features.md index row for <slug>." on success. If "Feature not found" is
    returned, check the slug against list_features() output.
    """
    t0 = time.monotonic()
    projects = _active_project(project)
    log_kwargs: dict = {
        "slug": slug,
        "project": _resolved_project(projects, project),
        "field": field,
        "append": append,
        "confirm": confirm,
        "status": "rejected",
    }
    try:
        if field not in _FEATURE_INDEX_FIELDS:
            _log_reject(log_kwargs, "unknown field")
            return f"Unknown field '{field}'. Valid fields: {', '.join(_FEATURE_INDEX_FIELDS)}."

        result = _resolve_slug(slug, projects)
        if isinstance(result, str):
            _log_reject(log_kwargs, "slug ambiguous")
            return result
        d, slug, _ = result
        if not d:
            _log_reject(log_kwargs, "slug not found")
            return _not_found_msg(slug, projects)

        target = d.parent
        index_file = target / "features.md"
        if not index_file.exists():
            _log_reject(log_kwargs, "no index file")
            return f"No features.md index found for project '{target.name}'."

        text = index_file.read_text()
        lines = text.splitlines()
        row_idx = None
        for i, line in enumerate(lines):
            cells = [c.strip() for c in line.split("|")]
            if len(cells) >= 3 and cells[2] == slug:
                row_idx = i
                break

        if row_idx is None:
            _log_reject(log_kwargs, "no index row")
            return f"No index row found for slug '{slug}' in {target.name}/features.md."

        cells = [c.strip() for c in lines[row_idx].split("|")]
        # cells[0] is empty (leading '|'), cells[1..6] = name, slug, ticket, branch, summary, date
        cell_idx = _FEATURE_INDEX_FIELDS[field]
        today = _today_iso()
        old_line = lines[row_idx]
        new_value = value.strip()
        if append and cells[cell_idx]:
            new_value = f"{cells[cell_idx]}, {new_value}"
        cells[cell_idx] = new_value
        cells[6] = today
        new_line = "| " + " | ".join(cells[1:7]) + " |"

        if old_line.strip() == new_line.strip():
            log_kwargs["status"] = "ok"
            return (
                f"No changes — proposed {field} for '{slug}' is identical to current."
            )

        if not confirm:
            diff = "\n".join(
                difflib.unified_diff(
                    [old_line],
                    [new_line],
                    fromfile="current",
                    tofile="proposed",
                    lineterm="",
                )
            )
            log_kwargs["status"] = "ok"
            return (
                f"Diff preview for '{slug}' row in {target.name}/features.md — nothing written yet:\n\n"
                f"{diff}\n\n"
                "Show this diff verbatim to the user (not your own summary). "
                "Call update_feature_index again with confirm=True and the same args to write it."
            )

        lines[row_idx] = new_line
        _atomic_write(index_file, "\n".join(lines) + "\n")
        _ensure_git_repo(target)
        log_kwargs["status"] = "ok"
        return f"Updated features.md index row for '{slug}'."
    except Exception:
        log_kwargs["status"] = "error"
        raise
    finally:
        log_kwargs["duration_ms"] = int((time.monotonic() - t0) * 1000)
        _log("update_feature_index", **log_kwargs)


def main() -> None:
    """Console-script entry point (`recall-server`) and `python -m` target.

    A named entry point rather than a script path: an installed console script
    does not depend on where the package landed, which is what made
    `uv run --project <SCRIPT_DIR> python <SCRIPT_DIR>/server.py` create a venv
    inside site-packages once the wheel shipped the repo root.
    """
    mcp.run()


if __name__ == "__main__":
    main()
