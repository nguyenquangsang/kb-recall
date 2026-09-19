# Slash Command Template

Standard format for all `commands/*.md` files. Follow this structure exactly when creating a new command.

```markdown
<One-line description of what the command does>. Argument (optional): **$ARGUMENTS**

## When to use

<1–2 sentences: the specific situation this command is for.>
Never <the most common misuse or wrong trigger>.

## Step 1 — Determine slug

**If $ARGUMENTS is provided:**
Call `list_features()`. Find the best match (exact, partial, substring). Proceed without asking.

**If $ARGUMENTS is empty — auto-detect:**
1. Run `git branch --show-current`. Match against available slugs.
2. If matched → proceed immediately, no confirmation.
3. If no match → show numbered menu, ask user to pick by number.

## Step 2 — <Name of the main action>

<Describe the core logic of this command.>

## Step N — Report

Show a compact summary after completing:
- What was done (one line per item)
- What was skipped and why (one line)
```

## Rules

- **First line** must end with `Argument (optional): **$ARGUMENTS**` — Claude Code injects the user's argument here.
- **When to use** must include at least one `Never...` boundary.
- **Step 1 (slug detection)** is boilerplate — copy it verbatim for any command that targets a feature KB.
- **Report step** is required for commands that write data. Omit only for read-only commands (e.g. `list`).
- **Examples section** only if the logic is complex enough that prose alone won't prevent wrong usage. See `save.md` for reference.
- Do not add a "When to use" section to the tool docstring in `server.py` — the command file owns that gate.
