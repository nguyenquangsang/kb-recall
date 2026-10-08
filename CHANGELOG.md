# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project adheres
to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

The distribution name is `kb-recall`, the command is `recall`.

## [0.2.1] — 2026-10-08

### Added

- **A `windows` CI job** (windows-latest, Python 3.12, pytest only). A Windows runner
  pipes cp1252 for real, which is the exact condition that crashed `recall setup` — the
  bug below had never had a platform able to reproduce it. Lint and type checking are
  deliberately absent from it: ruff is platform-independent, so the ubuntu job already
  covers it, and mypy resolves its stubs *per platform*, so on Windows it would report
  on typeshed's Windows views of stdlib calls this package only ever makes on POSIX.
- **A non-UTF-8 locale lane** (`PYTHONUTF8=0 LC_ALL=C`) on the existing ubuntu matrix.
  US-ASCII is stricter than cp1252 — which leaves only five bytes undefined — so it
  reproduces the same class deterministically on a runner already being paid for.
  Measured across the sweep: 123 failed + 16 errors before, 458 passed after.
- `tests/test_encoding.py` — AST-walks `kb_recall/`, `tests/` and `scripts/` so a newly
  added bare `read_text()` / `write_text()` / `open()` fails the suite, and exercises the
  stdio guard in a child process forced to a non-UTF-8 locale.

### Fixed

- **`recall setup` crashed on Windows.** It read its own packaged template with a bare
  `Path.read_text()`, which does not mean UTF-8 — it means "whatever this machine uses".
  On Windows that is cp1252, and `⚠️` contains byte `0x8F`, one of the five bytes cp1252
  leaves undefined, so setup died with `UnicodeDecodeError` before writing anything. The
  same assumption stood at 247 text-stream sites across 20 files, in both directions:
  hooks print KB text carrying `—` / `→` / `✓` / `⚠`, and because hooks swallow every
  exception by design, an encode error there surfaced as an *empty* injection rather than
  an error. `kb_recall/stdio.py::force_utf8_stdio()` now reconfigures stdin/stdout/stderr
  to UTF-8 and is called first thing by all three entry points (`cli.main`,
  `hooks/prompt_submit.main`, `adapters/copilot/hook.main`), and `encoding="utf-8"` is
  named at every stream site.
- **`search_features` could fill all 20 result slots from one feature.** Hits were
  flattened contiguously per slug, so a single high-matching feature could crowd out the
  KB that actually held the answer. Hits are now emitted round-robin across ranked slugs;
  each slug's own relevance order still decides its first hit.
- **Subprocess fixtures isolated `HOME` but not `USERPROFILE`,** so on Windows a test
  that believed it was sandboxed read — and could write — the runner's real profile.
  `Path.home()` goes through `ntpath.expanduser()`, which reads `USERPROFILE` and never
  consults `HOME`. Invisible on macOS and Linux, which is why it survived every local run;
  fixed with `tests/conftest.py::fake_home()` at seven call sites.

### Changed

- **Links in `README.md` are absolute GitHub URLs.** PyPI renders that file as the project
  description at `https://pypi.org/project/kb-recall/`, where a repo-relative path such as
  `CHANGELOG.md` resolves against pypi.org and dead-ends — including the Changelog link in
  the table of contents, which was the one people actually clicked.

## [0.2.0] — 2026-10-06

### Added

- **GitHub Copilot support.** `recall setup --platform copilot` writes `.mcp.json`,
  `.github/hooks/recall.json`, and `.github/copilot-instructions.md`, and installs 8
  Agent Skills mirroring the Claude Code slash commands. `sessionStart` auto-loads the
  branch-matched KB, and `postToolUse` carries the per-turn `save_memory`/`report_miss`
  reminders. A new `recall-copilot-hook` console script backs the per-call hook — a
  console script rather than `python -m` because the harness spawns hook commands through
  a shell that never sources the user's rc.
- `docs/copilot-hooks.md` — the hook mapping for both harnesses, and the three gaps that
  are Claude-only by design: the idle-gap guard, mid-session branch reload, and
  prompt-time injection.
- **Deterministic promotion to a KB's `README.md`,** replacing the five-step lifecycle
  that asked the model to call `update_readme` with the right section after every
  `save_memory`. The tag-to-section mapping is now a pure function, and a promoted block
  carries a `<!-- from:XXXX -->` marker so a later `[supersedes:XXXX]` can find it. The
  marker is deliberately dropped when a block's opener is reworded, because a reworded
  block is a hand edit.
- **KB size management.** `index-all` per-platform thresholds, plus a full-body growth
  ceiling that is independent of them.
- **A near-duplicate gate on promotion,** using distinctive-token Jaccard similarity. It
  skips a redundant promotion and reports that it did; it never blocks the save itself,
  and it never blocks a `[supersedes]` correction.
- `scripts/smoke/` — a measurement harness (`harness.py`, `score.py`, two probes, four
  scenarios) that scores whether an agent actually calls the recall tools, for both
  Claude Code and the Copilot CLI.
- `.github/agents/kb-audit.agent.md` — a VS Code workspace agent that audits a KB for
  stale or unverifiable entries. Its tool list is an allowlist of read tools plus the
  three read-only MCP tools, so it cannot write to a KB even by mistake.
- Test suites for the command/skill contract (`test_commands.py`, `test_skills.py`),
  the tool docstring contract (`test_docstring_contract.py`), end-to-end lifecycle
  (`test_end_to_end.py`), stdio smoke (`test_stdio_smoke.py`), the smoke scorer
  (`test_smoke_scorer.py`), and the Copilot adapter (`test_copilot_adapter.py`).
  The suite grew from 174 to 451 tests.
- Ruff's rule set is pinned explicitly in `pyproject.toml` rather than left to each
  installed version's defaults.

### Changed

- **The `mcp` dependency moved from 1.x to `>=2.2.0,<2.3.0`.** The server migrated from
  `FastMCP` to `MCPServer`, the name it was given in mcp 2.0. An environment that pins
  `mcp` 1.x needs to move to 2.2.x before installing this version. The upper bound is
  load-bearing, not cosmetic: a future 2.3+/3.x could rename the API again, and only
  `uv.lock` would hide that on a development machine.
- **Promotion is no longer a follow-up call.** `save_memory` promotes on its own at save
  time: `[gotcha]`/`[constraint]` route to `critical_warnings`, `[decision]` to
  `architecture`, and `[rule]` to `business_rules`, while `[bug]`, `[idea]`, and
  `[pattern]` stay in the journal.
- Documentation now addresses "your agent" rather than "Claude" where the behavior is
  shared with Copilot.

### Fixed

- **`recall setup --help` ran a real setup** instead of printing usage. Only the first
  argument was checked for `-h`/`--help`, so the flag reached `cmd_setup`, which ignored
  what it did not recognize — registering the MCP server, installing hooks, and
  symlinking the commands for someone who asked for help.
- **A mistyped `recall setup` flag no longer falls back to the default platform.** An
  unrecognized argument is now rejected with usage rather than skipped, so
  `--platfrom copilot` aborts instead of silently running a Claude setup and writing
  to `~/.claude/`.

## [0.1.0] — 2026-09-19

### Added

- Initial release. `recall setup`, the `UserPromptSubmit` hook that loads a
  branch-matched KB, the 8 MCP tools (`list_features`, `load_feature_context`,
  `search_features`, `save_memory`, `init_feature`, `update_readme`,
  `update_feature_index`, `report_miss`), the slash commands, the per-contributor
  journal model, and the idle-gap cost guard.

[0.2.1]: https://github.com/nguyenquangsang/kb-recall/compare/v0.2.0...v0.2.1
[0.2.0]: https://github.com/nguyenquangsang/kb-recall/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/nguyenquangsang/kb-recall/releases/tag/v0.1.0
