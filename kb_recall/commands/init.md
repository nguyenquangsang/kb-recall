Create a new feature knowledge base.

## When to use

Starting work on a brand new feature with no KB yet.
Never run if a KB might already exist — check `/recall:list` first.

## Step 0 — Get current branch

Run `git branch --show-current`.

## Step 1 — Collect inputs (one message)

Ask the user in **one numbered message** (not one-by-one) — number each item 1→n so the user can reply by number:
1. Feature name — use $ARGUMENTS if provided; otherwise, if the branch name (stripped of a leading `feat/`/`feature/`/`fix/`-style prefix) reads as a descriptive slug — not just a ticket ID, not generic like `main`/`wip`/`fix` — suggest a title-cased version of it (e.g. branch `feat/enhance-init` → suggest "Enhance Init"); let the user confirm or override either way. Never guess or invent the branch name to make this suggestion — use only the exact string Step 0 actually returned; if Step 0 wasn't run yet, run it first, and if the branch still isn't descriptive enough to suggest from, just ask for the name plainly instead of fabricating one.
2. Slug — if the name above came from the branch, reuse that branch segment directly as the slug (it's already lowercase-hyphenated); otherwise suggest from the name (e.g. "Payment Gateway" → `payment-gateway`). Let user confirm either way.
3. One-line summary — optional; mention that a real one-liner makes the feature easier to look up later (not just a formality to fill in), but if the user still skips it, don't chase them for a better one — just use the feature name as `summary` (a rushed one-liner typed just to get past the question isn't meaningfully better than reusing the name — don't invent a fancier placeholder either)
4. Ticket ID — search the current branch name for a key-shaped pattern (uppercase letters + hyphen + digits, e.g. `PROJ-1430`) and pre-fill it if found; otherwise ask. Let the user confirm or override either way.
5. Username — detected from `git config user.name`, shown as the default; let the user override.

Then call `init_feature(name="...", slug="...", summary="...", project="...", branch="<current-branch>", username="...", ticket="...")` — `summary` is required by the tool, so pass the feature name when the user skipped it.

If `summary` ended up identical to `name` (the skip case above), tell the user in one line right after creation: "Summary currently matches the feature name — no keywords for other KBs to recognize this one by. You can update it anytime with `update_feature_index(slug=\"<slug>\", field=\"summary\", value=\"...\")`." Non-blocking — do not ask them to fix it now, just surface it once.

## Step 1.5 — Issue tracker check (only if a ticket ID is set)

`cli.py setup` normally asks and persists the tracker for this project once, up
front — the steps below only re-ask as a fallback (e.g. setup ran before this
project existed in config, or on a machine where setup was never run for this
project, since `~/.recall-mcp/config.json` is per-machine, not per-repo).

1. Read `~/.recall-mcp/config.json`. Look up `issue_trackers.<absolute-project-path>`.
2. **Not present yet (fallback):** ask the user once — "What issue tracker does this project use? A) Jira  B) Other / none" — then persist the answer by writing `issue_trackers.<absolute-project-path>` into config.json (merge, don't overwrite other keys). Never ask again for this project after this.
3. **Tracker is "Other / none":** skip straight to Step 2.
4. **Tracker is "Jira":**
   - Look up `issue_tracker_autofetch.<absolute-project-path>` (`"always"` or absent/`"ask"`).
   - First check whether the Atlassian Rovo tools are actually usable this session (e.g. `getAccessibleAtlassianResources` is listed as connected, not in an "requires authentication" state). If Rovo is unavailable/unauthorized — say so in one line (point to claude.ai connector settings, or `/mcp` in an interactive session) and go straight to Step 2. Do NOT ask the "fetch via Rovo?" confirmation when there's nothing to confirm, and do not touch the autofetch preference.
   - **Preference is `"always"`:** skip the confirmation and fetch directly (still report the outcome in one line below — success or failure — this isn't silent, it just isn't a question).
   - **Preference is absent/`"ask"`:** ask a one-line confirmation — "Detected Jira ticket `<TICKET>` — fetch it via Rovo?" If the user declines, fall through to Step 2 unchanged (leave the preference untouched — only a successful fetch upgrades it).
   - **Fetch** (on confirm, or directly when preference is `"always"`):
     - Call `getAccessibleAtlassianResources` to resolve `cloudId` (skip if already known this session). No accessible site returned → tell the user briefly ("no Atlassian site connected to Rovo"), fall through to Step 2.
     - Call `getJiraIssue(cloudId="...", issueIdOrKey="<TICKET>")`.
     - On success: feed the returned description/fields directly into the document-ingestion flow below (the Jira/Linear row in the table already covers the section mapping) and proceed straight to "After filling" at the end of that flow — no seeding question to skip, since Step 2 no longer blocks on one. If the preference was absent/`"ask"`, persist `issue_tracker_autofetch.<absolute-project-path> = "always"` now (silently — don't ask, just do it and let the next init skip the question).
     - On failure — name the actual cause in one line, don't use a generic message: ticket key not found/typo'd, no permission to view this issue (403), or a network/connector error. Then fall through to Step 2 either way — the ticket ID and other Step 1 inputs are already saved, nothing is lost, only the auto-ingest shortcut is skipped. Do not change the autofetch preference on failure — a transient failure shouldn't downgrade an "always" nor lock in "always" from an "ask" state.

## Step 2 — Seed the KB (non-blocking)

After creating the KB (and if Step 1.5 didn't already auto-ingest a ticket), do NOT ask a blocking "how would you like to seed it" question — most users have nothing ready yet and would just skip past it anyway, so the question itself is the overhead. Instead, fold one line into the same message as Step 3's summary: "You can paste a spec/ticket anytime and I'll parse it into sections — otherwise they'll fill in naturally as we work." Then go straight to Step 3 without waiting for a reply.

**Document ingestion still runs whenever a document shows up** — in the same message as Step 1's inputs, or in any later message while sections are still empty/placeholder: if it's > 200 words, has bullet/heading structure, or uses keywords like "ticket" / "spec" / "PRD" / "document", treat it as seeding input and follow the flow below.

Documents show up in many forms (Jira/Linear ticket, PRD, RFC/TDD, ADR, meeting notes...) — don't try to classify the type first. Extract content by recognizing what the text contains, regardless of document type:

| Content in document | Fill section |
|---|---|
| Feature description, goal, why it exists | `overview` |
| Acceptance criteria, constraints, must/never/always rules | `business_rules` |
| Technical approach, data flow, component design, decisions made | `architecture` |
| Frameworks, libraries, infra components mentioned | `technical_stack` |
| TODOs, open questions, deferred work, unknowns | `open_items` |
| Linked tickets, related features | `related_tickets` |
| Warnings, gotchas, edge cases, past incidents | `critical_warnings` |

Call `update_readme(..., confirm=True)` for each section that has extractable content. Do NOT ask section-by-section. Pass `confirm=True` directly (skip the diff-preview step) — sections are still empty placeholders here, nothing is at risk of being overwritten.
`key_files` is never extracted from documents — ask explicitly or let it fill through code reading.

If a section's content is ambiguous — make a best-effort fill and prefix the entry with `<!-- uncertain -->`. Do not block to clarify.

After filling:
1. Tell the user which sections were filled and which remain empty.
2. Ask about `key_files` explicitly — it can never be extracted from documents: "Which files or modules are the entry points for this feature? List by symbol name."
3. If other sections are still empty, list them in one message and offer to fill now or leave for later. Do NOT interview them one by one.
4. Revisit any `<!-- uncertain -->` entries — show each one and ask the user to confirm or correct.

**If the user volunteers a bit of context unprompted** (not a full document — just a sentence or two about the feature, or a hard constraint) in their Step 1 reply or shortly after: fold it into `overview`/`business_rules` via `update_readme(..., confirm=True)` — same reasoning, still empty placeholders. Don't turn this into an interview — take what's offered, don't ask follow-ups to expand it.

**Otherwise** — nothing was offered, nothing to do. Sections stay exactly as `init_feature` created them (only `overview`, pre-filled from the summary). They'll fill naturally through `save_memory`/`update_readme` during real work.

## Step 3 — Reload KB and suggest next steps

Always run this once, in the same message as Step 2's non-blocking note (or right after document ingestion / Jira auto-ingest finishes, if either ran). Never end the command silently on the last `update_readme` call, and never wait on a reply before showing this.

1. Call `load_feature_context(slug="<slug>")` — reload the freshly-written KB so the summary reflects what's actually on disk, not just what was intended.
2. In one compact message, show:
   - Which sections are filled vs. still empty/placeholder (one line each — not a re-dump of content)
   - Any `open_items` rows carried over from the seed
   - 2–4 concrete next-step suggestions, e.g.: fill `key_files` once the entry point is known, confirm any `<!-- uncertain -->` entries still unresolved, revisit empty sections during the first real work session, use `/recall:save` at natural stopping points
3. Do not re-ask about anything already covered in Step 2 — this is a summary + suggestions, not another interview.
