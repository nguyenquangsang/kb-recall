---
name: recall-load
description: Load a recall-mcp feature knowledge base — by slug argument, or auto-detected from the current git branch. Use when the branch-matching KB wasn't auto-loaded, you're on main or an unrelated branch, or you need a KB different from the branch match.
argument-hint: [slug]
---

# Load the feature KB

## When to use

The hook auto-loads the KB on feature branches — use this skill when:
- On `main` or an unrelated branch and need a specific KB
- Want to load a KB different from the branch match

Never call `list_features` first if the slug is already known.

## Step 1 — Determine slug

**If an argument follows the command (e.g. `/recall-load payment-gateway`):**
Call `load_feature_context(slug="payment-gateway")` directly. DO NOT call `list_features()` first.
If the tool returns a slug-not-found error, call `list_features()`, find the best match — exact, partial, or substring (e.g. "pay" → "payment-gateway", "fraud" → "fraud-detection"), then retry with the matched slug. If the retry also fails, stop and tell the user the slug could not be found.

**If no argument is given — auto-detect from branch:**

Run `git branch --show-current`. Call `list_features()` and match the branch against available slugs. The matched result will be one of the following cases:

- **One match**: load immediately. DO NOT ask for confirmation.
- **Multiple matches**: load all matched slugs. DO NOT ask the user which one to load. Notify the user after loading (e.g. "Loaded 2 KBs: `payment-gateway`, `payment-webhook`").
- **No match**: if the feature list is empty, tell the user no KBs exist yet. Otherwise, show the feature list as a numbered menu and ask the user to pick one by number — DO NOT ask them to type the slug.

## Step 2 — Display

Display the content returned by the tool. State which slug(s) were loaded and how they were detected (branch, exact argument, fuzzy-matched argument, or user pick).
