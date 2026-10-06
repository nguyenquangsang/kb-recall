# Smoke tests — does the instruction surface survive a non-Claude model?

These probe one question: **can a model other than Claude actually be driven by
kb-recall's docstrings, hook output, and CLAUDE.md rules?** They are not a
substitute for `uv run pytest`, and they must never be wired into CI.

## Why this is measured, not asserted

kb-recall's regression safety lives in the deterministic layers:

| Layer | What it covers | Where |
| --- | --- | --- |
| Server | tools registered, stdio round-trip | `tests/test_stdio_smoke.py` |
| Hook | index + branch suggestion injected | `tests/test_prompt_submit.py` |
| Contract | docstring structure, `Args:` above the truncation cut | `tests/test_docstring_contract.py` |
| Lifecycle | init → save → promote → load → search | `tests/test_end_to_end.py` |

Model behaviour is none of those. The same prompt can produce a tool call on one
run and a confident wrong answer on the next, so a single run proves nothing and
a pass/fail assertion would flake. What these scripts produce is a **rate over
trials**. Read it as evidence about the instruction surface, never as a gate.

## The harness

`claude` is a *harness*; DeepSeek is a *model* plugged into it. Both matter, and
only one is being varied here.

This project runs Claude Code with `ANTHROPIC_BASE_URL` pointed at
`https://api.deepseek.com/anthropic`. That means the hooks fire, the deferred-tool
list applies, and the ~2000–2150-char description truncation still cuts — all
harness behaviour, model-independent. **Only compare models within the same
harness.** A result from a different client says nothing about this one.

## Step 1 — capability gate

Run this first. If the model cannot make one well-formed tool call, every
scenario below is uninterpretable.

```bash
uv run python scripts/smoke/probe.py                    # 1 trial, deepseek-v4-flash
uv run python scripts/smoke/probe.py --runs 5 --keep     # 5 trials, keep the stores
```

It asks for `list_features` and nothing else. A failure means stop and check
tool-use support before reading anything into scenario results.

## Step 1b — the same gate on the Copilot CLI

`probe.py` asks the question of Claude Code. `probe_copilot.py` asks it of the
Copilot CLI — a second harness, and the only non-Claude one reachable headlessly.

```bash
uv run python scripts/smoke/probe_copilot.py --runs 5
```

Two differences from `probe.py`, both deliberate:

- **It scores from the log, not from stdout.** `usage.jsonl` is written by the
  MCP *server*, so it records a call whichever client made it; the CLI's own
  output is prose. A byte offset marks the log's end before the run and only
  what lands past it counts.
- **No isolated HOME.** `~/.copilot` holds the CLI's config and its credential
  store sits outside HOME, so moving HOME would break auth in exchange for a
  clean log — and the byte offset already provides one. The cost: nothing else
  may call recall tools during a run.

The invocation it drives is:

```bash
copilot -p <prompt> --additional-mcp-config @<file> --allow-all-tools -s
```

`--additional-mcp-config` is not optional. A bare `copilot mcp list` in this
repo lists only the builtin servers, so the workspace `.mcp.json` that
`recall setup --platform copilot` writes is invisible without it — a config
that works in VS Code is silently inert here. `--allow-all-tools` is likewise
required, since non-interactive mode refuses to start tool calls without it.

**What a pass does not cover.** The CLI is not VS Code Local: payload shape and
output differ, and hooks reach the CLI through `plugin` rather than
`.github/hooks/recall.json`. So a pass says the *tool-calling* path works on a
non-Claude harness — it says nothing about the VS Code hook path, which remains
covered only by `tests/test_copilot_adapter.py` and the recorded E2E spike in
`docs/copilot-hooks.md`.

## Step 2 — scenario conformance

`score.py` scores a recorded `usage.jsonl` against a scenario's expected
signature:

```bash
uv run python scripts/smoke/score.py \
  --scenario scripts/smoke/scenarios/load-on-branch.json \
  ~/.recall-mcp/usage.jsonl
```

A scenario is JSON (`scenarios/*.json`) — add one without touching code:

| Field | Meaning |
| --- | --- |
| `requires` | List of ordered subsequences, each of which must appear |
| `forbids` | Tools that must not appear anywhere in the run |
| `min_calls` | Lower bound on total tool calls |

Gaps are allowed inside a sequence, because an extra exploratory call
(`list_features` before the real one) is not a failure. Order is not — that is
the whole point of declaring a sequence.

### Why `usage.jsonl` is the right instrument

Every tool call is already logged to `~/.recall-mcp/usage.jsonl` with its slug,
tag, and status. No transcript scraping needed.

Its one blind spot is load-bearing: **it records calls that happened, never a
call that should have happened and did not.** That is precisely why scenarios
declare `requires` — otherwise a model that silently answers from memory without
loading the KB would score a clean run.

### The scenarios

| Scenario | Boundary it probes |
| --- | --- |
| `load-on-branch` | KB is loaded before answering, not after |
| `save-in-turn` | Root cause saved in the turn it is learned, not deferred |
| `search-on-existence-question` | "Was this already done?" triggers a search |
| `search-not-slugs` | `search_features` is never used for slug discovery |

Only the two probes are fully automated. Running a scenario needs its `setup`
field satisfied by hand — a branch matching a seeded KB, or (for
`search-on-existence-question`) a KB that deliberately does *not* hold the
answer. Seeding that automatically is deliberately out of scope for now: it is a
lot of machinery to build behind a gate that has not been shown to pass.

## Rules

- **Never in CI.** Non-deterministic, needs network, costs tokens.
- **Never fewer than 5 trials** for a rate worth quoting; the scorer warns below
  that.
- **Never tune the docstrings to one model.** A weak model failing is a signal
  about the instruction surface; rewriting the surface until DeepSeek passes is
  overfitting to a probe.
- **Keep scenarios under 8 turns** — the hook's turn-counter reminders (8 for
  `save_memory`, 16 for `report_miss`) otherwise perturb the measurement.

## Related

`scripts/analyze_effectiveness.py` is the descriptive counterpart: it reads the
same `usage.jsonl` plus session transcripts and reports what is there, with no
expectations committed upfront. Use it to discover what to assert, then encode
the finding as a scenario here.
