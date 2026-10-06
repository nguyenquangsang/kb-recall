# Copilot hooks — what kb-recall relies on

**Verified:** 2026-09-25 → 2026-09-26, VS Code **1.139.1** (Local harness) with `copilot-agent` **0.67.0**, on macOS.
**Re-verify before trusting:** the hook protocol changed between Copilot releases at least once during this work. Treat the dates above as the shelf life, not the spec.

This page records only what was **observed**, and distinguishes it from what is merely declared by the settings registry. Live documentation is linked at the bottom — deliberately not copied into this repo, because a vendored copy of an evolving reference silently goes stale while looking authoritative.

## The two channels kb-recall uses

| Channel           | Event          | Carries                                | kb-recall's use                              |
| ----------------- | -------------- | -------------------------------------- | -------------------------------------------- |
| Session start     | `sessionStart` | `hookSpecificOutput.additionalContext` | Auto-load the branch's KB before turn 1      |
| After a tool call | `postToolUse`  | `hookSpecificOutput.additionalContext` | Save / miss reminders on a tool-call cadence |

Both were observed firing on the VS Code Local harness. `additionalContext` from `sessionStart` is prepended to **every** later prompt in the session, not only the first — that is the mechanism that keeps the KB active across a long session. It is still a one-shot _injection_: nothing re-runs at prompt time, so switching branches mid-session does not reload a KB.

`postToolUse` fires once per matched tool call and its payload has **no turn identifier** — `tool_use_id` identifies a call, not a turn. Any cadence built on it is a tool-call cadence; reusing a turn-based interval inflates the counter several-fold and fires reminders continuously. Observed rate: **~2 state entries per tool call**, with entries occasionally arriving in pairs milliseconds apart.

## Payload shape differs by harness

| Harness              | Config event key           | Event name in payload                                                                             | Session id field | Output shape                                 |
| -------------------- | -------------------------- | ------------------------------------------------------------------------------------------------- | ---------------- | -------------------------------------------- |
| VS Code Local        | camelCase (`sessionStart`) | PascalCase + `snake_case` fields (`hook_event_name: "SessionStart"`, `session_id`, ISO timestamp) | `session_id`     | `hookSpecificOutput.additionalContext`       |
| Copilot CLI (native) | camelCase                  | camelCase, **no** `hook_event_name`                                                               | `sessionId`      | `additionalContext` as a plain top-level key |

Consequences, all of which bit during development:

- **Route the event via `argv`, not the payload.** With camelCase there is no `hook_event_name` to read, so dispatching on it silently misroutes every event to the first branch.
- **Read fields tolerantly** — `payload.get("session_id") or payload.get("sessionId")`.
- **A wrong output shape fails silently.** The JSON parses, the key is simply ignored. This harness distinction is the reason the generated config keeps camelCase keys while the reader stays case-tolerant.

## Unknown event keys are skipped per entry

A key the harness does not recognise is dropped at entry level — the rest of the config file still loads, and there is no warning. This cuts both ways:

- It makes a shared config file safe (VS Code Local ignores Copilot-only events rather than rejecting the file).
- It also means a **typo'd event name or an invalid `matcher` regex looks exactly like a working config**. Anything in this area needs an end-to-end observation, not a config review.

## Dead end: `userPromptTransformed`

**It never fires on VS Code Local** — zero invocations across four real sessions, matching a static read of the harness' event map (which has no such key). It is a Copilot **CLI** event.

VS Code's own prompt event is `UserPromptSubmit`, and its output is common-only (`continue`, `stopReason`, `systemMessage`) — there is no `additionalContext` field on it. So there is **no prompt-time context injection on VS Code at all**, and no way to block a turn from a prompt-time hook. Two implications:

- kb-recall's idle-gap cost guard (which blocks a prompt) is Claude-only and cannot be ported.
- "Per-turn injection on Copilot" means _per tool call_, because that is the only injection point that exists.

`userPromptTransformed` is also the one event with no PascalCase variant, so the harness-casing decision above does not apply to it.

## Size budgets

Three separate ceilings stack up, and only the first is documented here rather than in the harness docs:

| Limit                             | Value                                                                                 | Effect when exceeded                                                                                    |
| --------------------------------- | ------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------- |
| `additionalContext` per injection | ~10 KB (kb-recall uses `COPILOT_CONTEXT_CAP_CHARS`, truncating at a section boundary) | Context is cut; kb-recall appends a line telling the agent to call `load_feature_context`               |
| Tool-result size                  | ~20 KiB                                                                               | The harness writes the result to a temp file instead of returning it inline, costing an extra read turn |
| MCP server render ceiling         | `KB_CONTEXT_HARD_LIMIT_CHARS` = 40 KB                                                 | Server-side gate, not a Copilot limit                                                                   |

A KB larger than 10 KB is therefore **never** fully auto-loaded, and one approaching 20 KiB also spills on explicit loads. The only lever that fixes both is KB size — see the `/recall-compact` and `/recall-tidy` skills.

## Config requirements that fail silently

- **`.mcp.json` is ignored unless `chat.mcp.workspaceRootConfig.enabled` is true.** On VS Code Stable this defaults to `false`, so the file parses fine and no tools appear, with no error. Adding the server through the MCP UI sidesteps the flag.
- **Hook commands must use absolute paths.** The harness spawns them through a shell that does not source the user's rc, so a bare command name that only resolves via `.zshrc` matches nothing. kb-recall generates absolute paths and gitignores the resulting config, since it is machine-specific.
- **Hooks from every source are combined and all run.** Registering the same event at both user and project scope injects twice.

## `matcher` is parsed and then ignored on Local

Documented (not inferred): the Local harness **accepts Claude-compatible matcher syntax but ignores matcher values**, so every nested command for the event runs. To filter on Local, the hook script must inspect the event input itself.

This closes the question the observed data could not settle. The measured ~2 state entries per tool call is consistent with the matcher never filtering — and the failure direction is benign, which is why it was not chased: more calls counted means reminders fire _earlier_, never silently absent.

**Consequence for cadence.** A matcher that does not filter means the counter advances on _every_ tool call, not just `bash|edit|create` ones. Any interval tuned on the assumption of "matched calls only" is effectively shorter in wall-clock terms than intended. Measure before retuning.

## Namespacing: `/recall:load` is only available to plugins

A skill cannot name itself `recall:load` — and unlike most naming mistakes, this one is not merely unconventional, it **fails silently**. The spec allows `[a-z0-9-]` only within `name`, requires `name` to equal the parent directory name, and VS Code's docs call out colons and namespace prefixes explicitly as invalid.

The colon **is** available on Copilot, but it is _granted_ rather than declared: when a skill ships inside a plugin, the harness prefixes the plugin name — `/my-plugin:test-runner`. So kb-recall's `recall-load` / `recall-save` are a hand-rolled substitute for a prefix we cannot declare, and the same eight files would render as `/recall:load`, `/recall:save`, … with no edit at all if they were distributed as an agent plugin named `recall`.

kb-recall is not packaged as a plugin today; see `cli.py::cmd_setup` / `_write_copilot_skills` for the installed layout.

## Not verified

- **Copilot CLI and the cloud agent.** Everything above is VS Code Local. The CLI's payload/output shape is inferred from the SDK, not observed here.
- **Cloud agent hook support** for these two events.
- **Whether `UserPromptSubmit` can stop a turn on Local.** It is listed as a supported Local event, and its output includes `continue`/`stopReason` — which is what Claude Code's idle-gap guard uses. If those fields are honored, the guard rejected as "not portable" may be portable after all. Not observed either way.

## Live references

Fetch these at the time of use; do not vendor them.

- <https://code.visualstudio.com/docs/agents/reference/hooks-reference>
- <https://docs.github.com/en/copilot/reference/hooks-reference>
- <https://code.visualstudio.com/docs/agent-customization/hooks>
- <https://code.visualstudio.com/docs/agent-customization/agent-skills>
- <https://code.visualstudio.com/docs/agent-customization/mcp-servers>
- <https://code.visualstudio.com/docs/agent-customization/custom-instructions>

Agent Skills is an open standard with implementations beyond Copilot: <https://agentskills.io>

## Repo precedent

A snapshot of the Claude Code hooks reference was kept in this repo until 2026-09-16; within three months it had fallen **12 of 33 events behind** without any signal that it was incomplete. That is why this page states observations with dates and links out for everything normative.
