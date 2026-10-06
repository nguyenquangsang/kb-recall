---
name: recall-list
description: List the recall-mcp feature knowledge bases configured for this project. Use when you need to see what KBs exist before loading one, or can't recall the exact slug.
argument-hint: [project]
---

# List feature knowledge bases

## When to use

To see what feature KBs exist before loading one, or when you don't remember the exact slug.
Never run this as routine session-start orientation — the hook already injects the feature index automatically.

## Steps

**If an argument follows the command:** Call `list_features(project="<argument>")`.

**If no argument is given — auto-detect project:**
1. Get the current working directory and extract the last path component as the project name.
2. Pass `project=<name>` to `list_features()`.
3. If the result is empty or the project was not found: read `~/.recall-mcp/config.json`, show the user a numbered list of configured projects, and ask them to pick one. Then retry `list_features(project=<chosen>)`.
4. Only omit `project=` if config.json is also unreadable.

Display results as a numbered list:

```
1. Payment Gateway (payment-gateway) — Handles Stripe checkout and webhook reconciliation
2. Fraud Detection (fraud-detection) — Flags suspicious transactions before settlement
...
```

After the list, add one line: "Use `/recall-load <number or name>` to load a feature."

If no features exist for the detected project, say so (mentioning the project name) and suggest calling `init_feature` to create the first one.
