Record a context miss — a mistake that the feature KB should have prevented. Argument (optional): **$ARGUMENTS**

## When to use

When Claude made a mistake that the KB should have prevented — a constraint was in the KB but Claude didn't apply it.
Never use for general corrections or first-time mistakes where no KB context existed.

## Step 1 — Determine slug

**If $ARGUMENTS is provided:**
Call `list_features()`. Find the best match (exact, partial, substring). Proceed without asking.

**If $ARGUMENTS is empty — auto-detect:**

1. Run `git branch --show-current`. Match against available slugs.
2. If matched → use it, no confirmation.
3. If no match → show numbered menu, ask user to pick by number.

## Step 2 — Record the miss

You MUST check conversation context first:

- If the mistake is identifiable from recent turns → self-assess: draft what went wrong and what the correct behavior should be. The result MUST follow 4 questions:
  1. What went wrong?
  2. What's the root cause?
  3. What should the KB have said?
  4. What's the fix?
State the draft to the user in one short paragraph.

- If the mistake is unclear → ask these four questions in one message:
  1. What went wrong?
  2. What's the root cause?
  3. What should the KB have said?
  4. What's the fix?
State the draft to the user in one short paragraph.

For both cases, you MUST show the answer to the user and ask their review.
If they say YES, Then you call `report_miss(slug="<slug>", description="<answer>")` and go to Step 3.

## Step 3 — Strengthen the KB

Based on the description, decide immediately:
- If it's a rule/constraint that should always hold → call `update_readme` on the appropriate section (`business_rules`, `critical_warnings`, or `architecture`). This is mode='replace' — the first call (confirm defaults to False) writes nothing and returns a real diff; show it verbatim, then call again with `confirm=True` to write. No extra question needed — the diff itself is the confirmation surface.
- If it's a one-time edge case or incident → call `save_memory`
- If both → do both

Do not ask which section to use — make the call and explain the choice in one sentence.

If the MISS was recorded with the wrong root cause: call
`save_memory(slug="<slug>", content="[gotcha][supersedes:XXXX] <corrected root cause>")`
using the ID returned by `report_miss`. The incorrect MISS is hidden from future loads;
the corrected entry stays visible.
