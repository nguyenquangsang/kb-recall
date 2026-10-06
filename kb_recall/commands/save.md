Review this conversation and save significant insights to the feature KB. Argument (optional): **$ARGUMENTS**

## When to use

End-of-session batch retrospective — scans conversation since the last inline save (or the full session if no prior save exists).
Never use for in-moment saves during active work — call `save_memory` directly instead (cheaper, no full-conversation scan).

## Step 1 — Determine slug

**If $ARGUMENTS is provided:**
Call `list_features()`. Find the best match (exact, partial, substring). Proceed without asking.

**If $ARGUMENTS is empty — auto-detect:**

1. Run `git branch --show-current`. Match against available slugs.
2. If matched → proceed immediately, no confirmation.
3. If no match → show numbered menu, ask user to pick by number.

## Step 2 — Find scan window

Before scanning, identify the starting point:

1. Look at the conversation for the most recent `save_memory` **tool call** (one that returned a tool result — not a "[recall-mcp] Save check: nothing this round" text line, which is not a tool call).
2. If found → **only scan turns after that call**. Everything before was already captured.
3. If not found → scan the full conversation.

Note the window start point, then proceed to Step 3.

## Step 3 — Scan for signal

Scan only the window identified in Step 2 — not the full conversation.
Do not read linearly looking for "important things." Instead, scan for two types of signal:

**Implementation signal** (bugs, code, corrections):
- User corrected Claude ("that's wrong", "no, actually...", "wait that's not right") — what was wrong and what's correct
- A bug whose root cause was non-obvious — not the symptom, the WHY
- A constraint discovered through failure, not through reading
- Something that had to be explained and isn't readable from code or docs

**Discussion signal** (design, decisions, trade-offs):
- A conclusion reached after comparing options — what was chosen AND why the alternative was rejected
- A principle or rule that emerged ("thì ra nên ưu tiên X khi Y")
- A constraint that surfaced mid-discussion ("ah thì ra không thể làm Z vì...")
- An option explicitly ruled out with reasoning — future sessions will re-litigate without this
- A promising idea or direction raised but not yet decided — tag as `[idea]`, note where the discussion left off

**Skip always:**
- Steps taken to implement — belongs in git history
- What the code does — derivable by reading it
- Temporary state ("we'll fix this later", "for now we...")
- Things already in README.md or CLAUDE.md

## Step 4 — Quality filter

Apply the right question based on type:

**Implementation:** *"Would a fresh Claude session make the same mistake without this?"* Yes → save.

**Discussion:** *"Would re-litigating this waste significant effort AND likely reach the same conclusion?"* Yes → save.

A passing memory has a clear WHY, a specific Apply condition, and would have prevented at least one wrong turn.

**Second-order test:** Is this already captured in a README section with equivalent coverage? If yes, skip.

**Examples:**

*Implementation — SKIP (fails second-order test):*
> "Fixed null pointer in the payment processor when cart is empty."
> WHY is obvious from the diff, no non-obvious constraint → skip.

*Implementation — SAVE (passes both tests):*
> "Redis default maxconn=10 is per-process, not global — under concurrent load the pool exhausts silently without throwing an exception."
> Not derivable from reading code, caused a production incident, applies any time Redis is configured → save as [gotcha].

*Discussion — SAVE (rejected option with reason):*
> "Considered event sourcing for the audit log. Rejected due to team's lack of Kafka expertise and high operational overhead. Chose append-only Postgres table instead."
> A fresh session would re-propose event sourcing without knowing it was already weighed and rejected → save as [decision].

## Step 5 — Save

Call all `save_memory` calls **sequentially, never in parallel** — each save does a non-atomic read-modify-write on the memories file AND, since auto-promotion, on README.md; parallel calls to the same file silently lose entries (each reports success even when its write was clobbered).
Format as What/Why/Apply — do not pass raw notes. Be decisive — no approval needed per entry.

**Entry is wrong or outdated** → `[supersedes:XXXX]` on the new tag line (XXXX = hex ID from `[id:XXXX]` on the stale entry).
Example: `**[gotcha][supersedes:b2e1] Corrected insight**`

**`[idea]` resolved this session** → `[decision][supersedes:XXXX]` with conclusion (adopted) or reason (rejected).

**Memory is dealt with, nothing replaces it** → closure record. `[resolved:XXXX]` hides the memory from future loads and never edits the README — write it once the conclusion lives in a README section, or the change landed in code:
`**[resolved:XXXX] <title>** — <one sentence: what settled it>`
Bulk: multiple tags on one line → `**[resolved:aaa] [resolved:bbb] title** — reason`

*Decision rule:* `[supersedes]` = replaced by better info — the new entry's block takes the old one's place in README. `[resolved]` = dealt with, body needs no further loading — hides the memory, leaves the README block alone.

## Step 6 — Contradiction check

Scan the loaded KB (already in context) against what you actually observed this session:

- Did you grep a value and find it different from what KB says? → `save_memory` with `[supersedes:XXXX]`.
- Did you use a function, flag, or constant the KB describes, and it behaved differently? → `save_memory` with `[gotcha][supersedes:XXXX]` or `report_miss`.
- Did you notice a `critical_warnings` entry that no longer applies (the bug was fixed, the constraint removed)? → `save_memory` with `[supersedes:XXXX]` carrying the correction — `[resolved:XXXX]` would only hide the memory and leave the stale README entry in place.

Only flag real contradictions — things you actually verified this session, not hypothetical drift. Skip entirely if nothing in the session touched KB-tracked values.

## Step 7 — Check open_items

After saving (Step 5), check the `open_items` table from the loaded KB (already in context).
For each open row, ask: did this session resolve, implement, or reject it?

- **Yes** → call `update_readme(slug="<slug>", section="open_items", content="<updated table>")` (confirm defaults to False — writes nothing, returns a real diff). Show that diff verbatim, ask "Apply to README `open_items`?", then only after approval call it again with `confirm=True` to write.
- **No** → skip. Do not touch the table.

Run this step even if Step 5 saved nothing — a session can close an item without generating a new memory.

## Step 8 — Check summary drift

Compare the loaded KB's features.md summary (already in context) against what this session actually worked on.
Ask: does the one-line summary still describe the feature's current scope, or has it drifted (new sub-area added, direction changed)?

- **No drift** → skip, no call.
- **Drifted** → call `update_feature_index(slug="<slug>", field="summary", value="<updated one-liner>")` with `confirm=False` (default) — writes nothing, returns a diff. Show it verbatim, ask "Apply to features.md index?", then only after approval call it again with `confirm=True`.

Do this check here (end-of-session), not per `update_readme` call — the summary rarely drifts within a single README edit, and checking now is free since the KB is already fully loaded in context.

## Step 9 — Promotion is automatic

S1 promotes each saved entry to its README section at save time (with a `<!-- from:XXXX -->`
marker) — you do NOT call `update_readme` to promote. After Step 5, read each save's
promotion note:
- `Promoted to <section>.` → done; the block landed with its marker and the source memory is
  auto-hidden from future loads — do NOT write `[resolved:XXXX]` after a promotion.
- `⚠️ Promotion skipped: …` → act on the stated reason (missing section → add it via
  `update_readme`; growth ceiling → run `/recall:tidy`). Hand-writing the same content
  instead is fine, but copy the memory's body verbatim: a verbatim block is recognised and
  gets its `from:` marker attached, while a paraphrase stays an orphan that no later
  supersede can replace.
- `⚠️ Promotion to <section> failed (…)` → memory saved, promotion failed; fix by hand if it matters.

Tags S1 does not promote (`[bug]`/`[idea]`/`[pattern]`/`[resolved]`) stay in memories only.

## Step 10 — Miss check

Before reporting, ask yourself: did a mistake happen this session that a loaded KB entry should have prevented?
- If yes → call `report_miss(slug="<slug>", description="<what went wrong and what the KB should have said>")` now.
- If no → proceed.

## Step 11 — Report

Show a compact final summary of what actually happened:
- Saved: bullet list of what was saved (one line each)
- Promoted: entries S1 wrote to README (section → entry title), per the promotion notes
- Skipped/failed promotions: any `⚠️` notes from Step 9, with the reason
- Skipped: one line explaining what category was left out — not a full list
