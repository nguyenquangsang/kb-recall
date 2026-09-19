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
from typing import Optional

from mcp.server.fastmcp import FastMCP

CONFIG_PATH = Path.home() / ".recall-mcp" / "config.json"
KB_ROOT = Path.home() / ".recall-mcp"
LOG_FILE = KB_ROOT / "usage.log"
LOG_JSONL = KB_ROOT / "usage.jsonl"
TEMPLATES_DIR = Path(__file__).parent / "templates"

mcp = FastMCP("recall")

SECTION_PRIORITY = {
    "critical_warnings": 4,
    "business_rules": 3,
    "architecture": 3,
    "technical_stack": 2,
    "key_files": 2,
    "overview": 2,
    "open_items": 1,
    "checklist": 1,
    "related_tickets": 1,
}
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

# architecture/business_rules use char thresholds, not entry counts — architecture
# is prose by design (TODO #41: 18/22 real KBs), so counting "**[" tag markers
# there systematically undercounts. business_rules IS tag-based like
# critical_warnings, so its threshold is pegged to the same 10-entry budget
# (avg real entry size on this machine: business_rules 364 chars/entry, vs.
# critical_warnings 566 chars/entry x 10 = 5,660 chars) — not to a population
# quantile, since that produced a threshold that fired far earlier than
# critical_warnings' for the same kind of content. architecture has no
# entries to peg to, so it's set near the midpoint of the other two budgets.
KB_ARCHITECTURE_TIDY_CHAR_THRESHOLD = 4_600  # ~midpoint of the two budgets below
KB_BUSINESS_RULES_TIDY_THRESHOLD = 3_600  # ~10 entries x 364 chars/entry avg

# Empirically bisected on this harness: a load_feature_context response of
# 48,146 chars succeeded, 56,078 chars failed with "exceeds maximum allowed
# tokens" (KB `improve` hit this for real — 144,596 chars, hard failure, no
# recovery path since /recall:compact/tidy both depend on this same call).
# Kept well under the lowest observed failure point since real text may
# tokenize denser than the synthetic content used to bisect.
KB_CONTEXT_HARD_LIMIT_CHARS = 40_000


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
) -> tuple[Optional[Path], str, bool] | str:
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


def _current_session_id() -> str:
    try:
        return (KB_ROOT / "current-session").read_text().strip()
    except Exception:
        return ""


def _log(tool: str, **kwargs) -> None:
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    session_id = _current_session_id()
    fields = {k: v for k, v in kwargs.items()}
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
    except Exception:
        pass


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
            )
    except Exception:
        pass


def _read_config() -> dict:
    if not CONFIG_PATH.exists():
        return {}
    return json.loads(CONFIG_PATH.read_text())


def _write_config(data: dict) -> None:
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(data, indent=2) + "\n")


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
        )
        name = result.stdout.strip()
        if name:
            return name.lower().replace(" ", "-")
    except Exception:
        pass
    return "unknown"


def _memories_files(feature_dir: Path) -> list[Path]:
    """Return all memories files for a feature (memories-{username}.md pattern)."""
    return list(feature_dir.glob("memories-*.md"))


def _entry_id(block: str) -> str:
    """Extract [id:XXXX] from an entry's first line, or empty string if absent."""
    m = re.search(r"\[id:([a-f0-9]{4,6})\]", block.splitlines()[0], re.IGNORECASE)
    return m.group(1).lower() if m else ""


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

_ENTRY_TAG_RE = re.compile(r"\[id:[a-f0-9]{4,6}\]:\s*\*{0,2}\[([a-zA-Z]+)")


def _entry_tag(block: str) -> str:
    """Extract the leading category tag (e.g. 'idea') from an entry's first line.

    Returns "" for bare [resolved:XXXX] closures and malformed entries — both
    fall outside INDEX_ONLY_TAGS, so they default to the gating tier.
    """
    m = _ENTRY_TAG_RE.search(block.splitlines()[0])
    return m.group(1).lower() if m else ""


_ENTRY_DATE_RE = re.compile(r"^- \*\*(\d{4}-\d{2}-\d{2})\*\*")


def _collect_memory_entries(files: list[Path]) -> tuple[list[tuple[str, str]], int]:
    """Parse, sort (date descending), and filter memory entries from files.

    Entries are blank-line-delimited blocks. Each block starting with "- **"
    is treated as one entry — preserving multi-line What/Why/Apply content.

    Filtering rules (raw files are never modified):
    - [supersedes:XXXX]: the TARGET entry (id:XXXX) is hidden; the replacement
      entry containing [supersedes:XXXX] stays visible — it holds the new content.
    - [resolved:XXXX]: the TARGET entry (id:XXXX) is hidden; the closure record
      containing [resolved:XXXX] stays visible — Claude needs it to know what was
      resolved and why, so it doesn't re-investigate a fixed issue.

    Returns (entries, malformed_count) as (date, block) pairs, newest first.
    An entry whose first line doesn't match the "- **YYYY-MM-DD**" prefix
    save_memory always writes (e.g. hand-edited into the file without a date)
    sorts as "0000-00-00" — oldest — instead of erroring; malformed_count lets
    the caller surface this instead of it happening silently.
    """
    entries: list[tuple[str, str]] = []  # (date, block)
    malformed = 0

    for fpath in files:
        blocks = re.split(r"\n\s*\n", fpath.read_text())
        for block in blocks:
            block = block.strip()
            if not block.startswith("- **"):
                continue
            first_line = block.splitlines()[0]
            m = _ENTRY_DATE_RE.match(first_line)
            if m:
                entries.append((m.group(1), block))
            else:
                malformed += 1
                entries.append(("0000-00-00", block))

    entries.sort(key=lambda x: x[0], reverse=True)

    filtered = _superseded_ids(entries) | _resolved_ids(entries)
    if filtered:
        entries = [(d, b) for d, b in entries if _entry_id(b) not in filtered]

    return entries, malformed


def _merge_memories(files: list[Path]) -> tuple[str, int]:
    """Merge entries from multiple memories files into one text blob, sorted
    by date descending. See _collect_memory_entries for filtering rules.

    Returns (merged_text, malformed_count).
    """
    entries, malformed = _collect_memory_entries(files)
    merged = "\n\n".join(block for _, block in entries) if entries else ""
    return merged, malformed


def _strip_readme_for_context(text: str) -> str:
    """Strip HTML comments and remove empty XML sections before returning to Claude.

    The stored README keeps comments (useful for human editing). This function
    strips them from the context-injected copy to reduce token cost.
    """
    stripped = _strip_html_comments(text)

    def _drop_if_empty(m: re.Match) -> str:
        return "" if not m.group(2).strip() else m.group(0)

    stripped = re.sub(r"<(\w+)>(.*?)</\1>", _drop_if_empty, stripped, flags=re.DOTALL)
    stripped = re.sub(r"\n{3,}", "\n\n", stripped)
    return stripped.strip()


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


def _kb_health_hints(readme_text: str, combined_memories: str) -> list[str]:
    """Return maintenance hints if KB exceeds size thresholds."""
    hints = []

    mem_chars = len(combined_memories)
    if mem_chars > KB_MEMORIES_COMPACT_THRESHOLD:
        hints.append(
            f"**KB maintenance:** memories are large ({mem_chars:,} chars) — "
            f"run `/recall:compact` to reduce token cost."
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
    combined, _ = _merge_memories(_memories_files(d))
    hints = _kb_health_hints(readme_text, combined)
    return ("\n\n" + "\n".join(hints)) if hints else ""


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
        "status": "error",
    }
    try:
        if not projects:
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
        "status": "error",
    }
    try:
        if not projects:
            return "No projects configured. Add paths to ~/.recall-mcp/config.json."

        keywords = [w.lower() for w in query.split() if w.strip()]
        if not keywords:
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
                kw for kw, pat in zip(keywords, keyword_patterns) if pat.search(low)
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

                combined, _malformed = _merge_memories(_memories_files(feature_dir))
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
        # KB_CONTEXT_HARD_LIMIT_CHARS, rather than trust the per-hit cap alone.
        while (
            hit_lines and sum(len(line) + 1 for line in hit_lines) > SEARCH_RESULT_CHAR_LIMIT
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
            f"# Search: '{query}'{stopword_note} — {total_hits} hit(s) across "
            f"{len(hits_by_key)} feature(s), ranked by relevance"
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
    finally:
        log_kwargs["duration_ms"] = int((time.monotonic() - t0) * 1000)
        _log("search_features", **log_kwargs)


@mcp.tool()
def load_feature_context(
    slug: str, project: str = "", expand_ids: list[str] | None = None
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
    section from a prior call on the same slug; never guess an id.

    Args:
        slug: Feature slug (e.g. 'payment-gateway'). Exact match preferred.
        project: Optional — only needed if this slug exists in >1 project.
        expand_ids: Optional entry ids to force full body for — use when an
            index-only [idea]/[pattern] title looks relevant to the current
            task. Omit on the first call for a slug.

    OUTPUT:
    - "too large to load safely": don't retry or read memories-*.md directly
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
    - Related features (from the response, or a request touching a known feature
      KB) → call load_feature_context for it now, don't ask first; notify after:
      "[recall-mcp] Loaded KB `{slug}` — detected your request touches {reason}."
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
        "status": "error",
    }
    try:
        original_slug = slug
        result = _resolve_slug(slug, projects)
        if isinstance(result, str):
            return result
        d, slug, was_fuzzy = result
        if not d:
            return _not_found_msg(slug, projects)

        # Read content first so token estimate can be included in the header
        readme = d / "README.md"
        readme_text = ""
        readme_stripped = ""
        related_hint = ""
        if readme.exists():
            readme_text = readme.read_text()
            readme_stripped = _strip_readme_for_context(readme_text)
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
        memory_files = _memories_files(d)
        if memory_files:
            entries, log_kwargs["memories_date_parse_failures"] = (
                _collect_memory_entries(memory_files)
            )
            expand_id_set = {e.strip().lower() for e in (expand_ids or [])}
            gating_blocks = []
            index_lines = []
            for _entry_date, block in entries:
                tag = _entry_tag(block)
                if tag in INDEX_ONLY_TAGS and _entry_id(block) not in expand_id_set:
                    index_lines.append(block.splitlines()[0])
                else:
                    gating_blocks.append(block)
            combined = "\n\n".join(gating_blocks)
            index_text = "\n".join(f"  • {line}" for line in index_lines)

        readme_loaded = len(readme_stripped)
        memories_loaded = len(combined) + len(index_text)
        total_chars = readme_loaded + memories_loaded
        # Cosmetic estimate for the header only — NOT the safety gate below,
        # which deliberately compares total_chars directly (see
        # KB_CONTEXT_HARD_LIMIT_CHARS' comment: bisected on real char counts,
        # not tokens). Don't "fix" this by making the gate token-based.
        total_tokens = total_chars // 4

        if total_chars > KB_CONTEXT_HARD_LIMIT_CHARS:
            log_kwargs["status"] = "oversized"
            log_kwargs["readme_chars"] = readme_loaded
            log_kwargs["memories_chars"] = memories_loaded
            return (
                f"KB '{slug}' is too large to load safely: README ~{readme_loaded:,} chars + "
                f"memories ~{memories_loaded:,} chars = ~{total_chars:,} chars total "
                f"(limit ~{KB_CONTEXT_HARD_LIMIT_CHARS:,}). Returning this would exceed the "
                f"tool-result size cap and fail outright — do not retry this call, it will "
                f"fail the same way.\n"
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
        parts = [header]

        hints = _kb_health_hints(readme_text, combined + index_text)
        if hints:
            parts.append("\n".join(hints))

        if readme_stripped:
            parts.append(f"## README.md\n\n{readme_stripped}")
        if combined:
            parts.append(f"## memories\n\n{combined}")
        if index_text:
            parts.append(
                f"## memories index ([idea]/[pattern] only — title-only to save "
                f"context)\n\n{index_text}\n\n"
                f"If one of these titles looks relevant to the current task, call "
                f"load_feature_context('{slug}', expand_ids=['<id>']) now for its full "
                f"body — don't guess from the title alone, and don't wait to be asked."
            )

        footer = "---\n"
        if related_hint:
            footer += related_hint + "\n"
        if key_files_hint:
            footer += key_files_hint + "\n"
        footer += (
            f"**Apply immediately:** critical_warnings, business_rules, and architecture "
            f"above constrain every decision this session — apply them before acting, don't "
            f"just skim past them.\n"
            f"**Stale-check:** if a memory above carries [resolved:XXXX] or "
            f"[supersedes:XXXX] and the original claim still sits verbatim in "
            f"critical_warnings, business_rules, architecture, or open_items, that section "
            f"is stale — fix it now via update_readme, don't wait for /recall:save.\n"
            f"**Inline save rule for this session:** the moment you discover a bug root cause, "
            f"non-obvious constraint, gotcha, or rejected approach — call "
            f"`save_memory(slug='{slug}', ...)` immediately at that point. "
            f"Do not defer to end of session — deferred saves are forgotten.\n"
            f"**Cross-feature uncertainty:** when unsure whether a related/mapped feature "
            f"KB actually applies to the current task, load it anyway — missing "
            f"cross-feature context costs more than one extra `load_feature_context` call."
        )
        parts.append(footer)

        log_kwargs["slug"] = slug
        log_kwargs["readme_chars"] = readme_loaded
        log_kwargs["memories_chars"] = memories_loaded
        log_kwargs["memories_count"] = sum(
            1 for ln in combined.splitlines() if ln.startswith("- **")
        )
        log_kwargs["status"] = "ok"
        return "\n\n".join(parts)
    finally:
        log_kwargs["duration_ms"] = int((time.monotonic() - t0) * 1000)
        _log("load_feature_context", **log_kwargs)


MEMORY_TAGS = ("gotcha", "bug", "decision", "constraint", "rule", "idea", "pattern")
# "resolved" is a valid standalone leading tag too — the retire-only closure
# format is `[resolved:XXXX] <one-line reason>` with no category tag before it
# (see commands/compact.md Step 4). "supersedes" never leads alone — the
# replace format keeps the original category tag first: "[tag][supersedes:XXXX] ...".
FIRST_TAG_ALLOWED = MEMORY_TAGS + ("resolved",)


def _validate_memory_content(content: str) -> str:
    """Return an error message if content fails the hard quality gate, else "".

    Server-side enforcement, not just docstring guidance — catches the empty/
    near-empty save_memory call regardless of whether the caller read or
    followed the prompt instructions (observed twice in real usage: a "quiet
    round" Save-check line followed by a reflexive save_memory call anyway).
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
    code does (visible in the diff), test outcomes, summaries of finished work —
    anything a grep/git-log/diff would already show.

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
    [supersedes:XXXX]/[resolved:XXXX] (ID from loaded KB) supersede/retire a
        stale entry — the response footer repeats the full promotion protocol.

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
    projects = _active_project(project)
    _tag_m = re.search(r"\[([a-z]+)", content)
    log_kwargs: dict = {
        "slug": slug,
        "project": _resolved_project(projects, project),
        "tag": _tag_m.group(1) if _tag_m else "",
        "char_count": len(content),
        "status": "error",
    }
    try:
        validation_error = _validate_memory_content(content)
        if validation_error:
            return f"Rejected: {validation_error}"

        result = _resolve_slug(slug, projects)
        if isinstance(result, str):
            return result
        d, slug, _ = result
        if not d:
            return (
                _not_found_msg(slug, projects) + "\nCreate it first with init_feature."
            )

        username = _resolve_username()
        memories_file = d / f"memories-{username}.md"
        today = date.today().isoformat()
        entry_id = secrets.token_hex(3)  # 6-char hex, unique per entry
        new_entry = f"- **{today}** [id:{entry_id}]: {content.strip()}"

        footer = (
            "\n---\n"
            "**Prepend-only:** this call never edits or merges an existing entry — "
            "to correct an old one, save a new entry instead: tag [supersedes:XXXX] "
            "when it's replaced by this new content, or [resolved:XXXX] when it no "
            "longer applies and nothing replaces it (ID from loaded KB either way).\n"
            "**Closing an idea:** if this entry is a [decision] that resolves a prior "
            "[idea], tag it [decision][supersedes:XXXX] using the idea's ID.\n"
            "**Tags:** [gotcha] [bug] [decision] [constraint] [rule] [idea] [pattern] "
            "([rule]=non-negotiable invariant, [idea]=proposed/undecided).\n"
            "**Promotion:** [gotcha]/[constraint]->critical_warnings, "
            "[decision]->architecture, [rule]->business_rules. "
            "Skip [bug]/[idea]/[pattern] entirely.\n"
            "Pure append (nothing existing removed/changed) -> call "
            "update_readme(mode='append') now, announce with one line first. "
            "Anything removed/merged/marked stale -> show the synthesized section, "
            'ask "Apply to README {section}?", write only after approval — this human '
            'gate exists because misjudging "partially done" as "resolved" has caused '
            "real mistakes before. A [decision] promotion also re-checks open_items for "
            "rows it resolves — ask before marking any row resolved.\n"
            '**critical_warnings tip:** prefer "verify at file.py::SYMBOL" over quoting '
            "a value directly — quoted values go stale silently when code changes."
        )

        if not memories_file.exists():
            memories_file.write_text(
                f"# Engineering Memory — {username}\n\n*Prepend-only.*\n\n---\n\n{new_entry}\n"
            )
            _ensure_git_repo(d.parent)
            log_kwargs["status"] = "ok"
            return (
                f"Created memories-{username}.md and saved entry ({today})."
                f"{footer}{_health_hint_suffix(d)}"
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

        memories_file.write_text("\n".join(lines) + "\n")
        _ensure_git_repo(d.parent)
        log_kwargs["status"] = "ok"
        return f"Saved to {slug}/memories-{username}.md ({today}).{footer}{_health_hint_suffix(d)}"
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

    OUTPUT: On success, the README has empty section placeholders.
    If invoked via /recall:init, the command handles the fill flow — do NOT start
    asking for section content directly.
    If "⚠ Not configured" appears in the response, show the snippet to
    the user and ask them to add it to their project's CLAUDE.local.md (gitignored).

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
    """
    t0 = time.monotonic()
    projects = _active_project(project)
    log_kwargs: dict = {
        "slug": slug,
        "project": _resolved_project(projects, project),
        "status": "error",
    }
    try:
        if not projects:
            return "No projects configured. Add paths to ~/.recall-mcp/config.json."
        if len(projects) > 1:
            names = ", ".join(p.name for p in projects)
            return f"Multiple projects found ({names}). Specify which one with the project argument."

        target = projects[0]
        feature_dir = _kb_root(target) / slug

        if feature_dir.exists():
            return f"Feature '{slug}' already exists at {feature_dir}."

        feature_dir.mkdir(parents=True)
        today = date.today().isoformat()

        # Template uses $name/$slug/$summary (not {name}/.format()) -- templates
        # are hand-edited docs that may legitimately contain illustrative `{...}`
        # examples of their own (e.g. `{project-name}/{slug}/README.md` in a
        # key_files example, already used verbatim in this project's own KB).
        # A $-sigil placeholder can never collide with a curly-brace illustration,
        # and safe_substitute() leaves any unrecognized $-token untouched instead
        # of raising -- .format() would crash on the first stray `{...}` and
        # .replace() would silently corrupt a `{slug}`-shaped illustration.
        readme_tmpl = Template((TEMPLATES_DIR / "feature-README.md").read_text())
        (feature_dir / "README.md").write_text(
            readme_tmpl.safe_substitute(name=name, slug=slug, summary=summary)
        )

        username = _resolve_username(confirmed=username)
        memories_tmpl = Template((TEMPLATES_DIR / "feature-memories.md").read_text())
        (feature_dir / f"memories-{username}.md").write_text(
            memories_tmpl.safe_substitute(name=name)
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
            index_file.write_text("\n".join(lines) + "\n")
        else:
            index_tmpl = Template((TEMPLATES_DIR / "features-index.md").read_text())
            index_file.write_text(index_tmpl.safe_substitute(first_row=new_row))

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
    projects = _active_project(project)
    log_kwargs: dict = {
        "slug": slug,
        "project": _resolved_project(projects, project),
        "char_count": len(description),
        "status": "error",
    }
    try:
        result = _resolve_slug(slug, projects)
        if isinstance(result, str):
            return result
        d, slug, _ = result
        if not d:
            return _not_found_msg(slug, projects)

        username = _resolve_username()
        memories_file = d / f"memories-{username}.md"
        today = date.today().isoformat()
        entry_id = secrets.token_hex(3)  # 6-char hex, unique per entry
        new_entry = f"- **{today}** [id:{entry_id}] [MISS]: {description.strip()}"

        if not memories_file.exists():
            memories_file.write_text(
                f"# Engineering Memory — {username}\n\n*Prepend-only.*\n\n---\n\n{new_entry}\n"
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
            memories_file.write_text("\n".join(lines) + "\n")

        _ensure_git_repo(d.parent)
        log_kwargs["status"] = "ok"
        return f"Recorded [MISS] [id:{entry_id}] in {slug}/memories-{username}.md ({today})."
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
        "status": "error",
    }
    try:
        if section not in VALID_SECTIONS:
            return f"Unknown section '{section}'. Valid sections: {', '.join(VALID_SECTIONS)}."
        if mode not in ("replace", "append"):
            return f"Unknown mode '{mode}'. Valid modes: replace, append."
        if not content.strip():
            return "Rejected: content is empty — refusing to write a blank section."
        if not _strip_html_comments(content):
            return (
                "Rejected: content is only HTML comments (e.g. a copied template "
                "placeholder) — refusing to write a blank section."
            )
        result = _resolve_slug(slug, projects)
        if isinstance(result, str):
            return result
        d, slug, _ = result
        if not d:
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
            return f"Section <{section}> not found in {slug}/README.md."

        existing = _strip_html_comments(text[start + len(open_tag) : end])
        new_content = content.strip()

        if mode == "append":
            # Blank-line join so appended entries stay visually separated for
            # a human scanning the file, regardless of tag/symbol convention.
            new_content = (existing + "\n\n" + new_content) if existing else new_content
        elif not confirm:
            diff = "\n".join(
                difflib.unified_diff(
                    existing.splitlines(),
                    new_content.splitlines(),
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
            return (
                f"Diff preview for <{section}> in {slug}/README.md — nothing written yet:\n\n"
                f"{diff}\n\n"
                "Show this diff verbatim to the user (not your own summary). "
                "Call update_readme again with confirm=True and the same args to write it."
            )

        updated = text[: start + len(open_tag)] + "\n" + new_content + "\n" + text[end:]
        readme.write_text(updated)
        _ensure_git_repo(d.parent)
        log_kwargs["status"] = "ok"
        suffix = _health_hint_suffix(d)
        if section == "key_files":
            suffix += _key_files_format_hint(new_content)
        if section in ("critical_warnings", "architecture", "business_rules"):
            suffix += (
                "\nIf this content originated from a save_memory entry now fully "
                "captured here, consider marking that memory [resolved:XXXX] — "
                "gotcha/decision/rule/constraint memories are always full-loaded "
                "by load_feature_context (never index-compressed), so an "
                "unresolved duplicate of promoted content wastes context on "
                "every future load."
            )
        return f"Updated <{section}> in {slug}/README.md ({mode}).{suffix}"
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
        "status": "error",
    }
    try:
        if field not in _FEATURE_INDEX_FIELDS:
            return f"Unknown field '{field}'. Valid fields: {', '.join(_FEATURE_INDEX_FIELDS)}."

        result = _resolve_slug(slug, projects)
        if isinstance(result, str):
            return result
        d, slug, _ = result
        if not d:
            return _not_found_msg(slug, projects)

        target = d.parent
        index_file = target / "features.md"
        if not index_file.exists():
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
            return f"No index row found for slug '{slug}' in {target.name}/features.md."

        cells = [c.strip() for c in lines[row_idx].split("|")]
        # cells[0] is empty (leading '|'), cells[1..6] = name, slug, ticket, branch, summary, date
        cell_idx = _FEATURE_INDEX_FIELDS[field]
        today = date.today().isoformat()
        old_line = lines[row_idx]
        new_value = value.strip()
        if append and cells[cell_idx]:
            new_value = f"{cells[cell_idx]}, {new_value}"
        cells[cell_idx] = new_value
        cells[6] = today
        new_line = "| " + " | ".join(cells[1:7]) + " |"

        if old_line.strip() == new_line.strip():
            log_kwargs["status"] = "ok"
            return f"No changes — proposed {field} for '{slug}' is identical to current."

        if not confirm:
            diff = "\n".join(
                difflib.unified_diff(
                    [old_line], [new_line], fromfile="current", tofile="proposed", lineterm=""
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
        index_file.write_text("\n".join(lines) + "\n")
        _ensure_git_repo(target)
        log_kwargs["status"] = "ok"
        return f"Updated features.md index row for '{slug}'."
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
