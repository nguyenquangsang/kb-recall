# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project adheres
to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

The distribution name is `kb-recall`, the command is `recall`.

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
  The suite grew from 174 to 437 tests.
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

## [0.1.0] — 2026-09-19

### Added

- Initial release. `recall setup`, the `UserPromptSubmit` hook that loads a
  branch-matched KB, the 8 MCP tools (`list_features`, `load_feature_context`,
  `search_features`, `save_memory`, `init_feature`, `update_readme`,
  `update_feature_index`, `report_miss`), the slash commands, the per-contributor
  journal model, and the idle-gap cost guard.

[0.2.0]: https://github.com/nguyenquangsang/kb-recall/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/nguyenquangsang/kb-recall/releases/tag/v0.1.0
