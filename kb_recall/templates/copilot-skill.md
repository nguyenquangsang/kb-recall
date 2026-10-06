# Agent Skill Template

Standard format for `kb_recall/skills/<name>/SKILL.md` files. Follow this structure exactly when creating a new skill.

```markdown
---
name: <lowercase-hyphen, must match the parent directory>
description: <what the skill does AND when to use it — ≤1024 chars, lead with "recall">
argument-hint: <optional, e.g. "[slug]">
---

# <Title>

## When to use

<1–2 sentences: the specific situation this skill is for.>
Never <the most common misuse or wrong trigger>.

## Step 1 — Determine slug

**If an argument follows the command (e.g. `/recall-load payment-gateway`):**
<call the target tool with that argument directly; do NOT call `list_features()` first.>

**If no argument is given — auto-detect from branch:**

Run `git branch --show-current`. Call `list_features()` and match the branch against available slugs.

## Step 2 — <Name of the main action>

<Describe the core logic of this skill.>

## Step N — Report

Show a compact summary after completing:
- What was done (one line per item)
- What was skipped and why (one line)
```

## Rules

- **`name` must match the parent directory name exactly.** Lowercase letters, digits, and
  hyphens only; no slashes, colons, dots, or namespace prefixes; ≤64 chars. An invalid or
  mismatched name makes the skill **silently fail to load** — the config parses, looks
  correct to a human, and the skill never appears. This is the trap the whole port exists
  to avoid.
- **`description` is the discovery surface.** The model decides to auto-invoke based on it
  alone, so it must state what the skill does AND when to use it, and lead with "recall" so
  the three skills group. ≤1024 chars.
- **No `$ARGUMENTS` variable.** The user types the argument after the slash command
  (`/recall-load payment-gateway`); there is no variable to substitute. Read the argument
  from the text after the command.
- **Keep `## When to use` with at least one `Never...` boundary** — the same gate the
  Claude command owns. The operational rules inside a skill are shared with its
  `commands/*.md` sibling, but the two are not the same file: the skill has frontmatter the
  command lacks, and the command has `$ARGUMENTS` the skill lacks, so do not mechanically
  generate one from the other.
- **Report step is required for skills that write data. Omit only for read-only skills**
  (e.g. `recall-list`).
- **Do not copy the two Copilot gaps into a skill** — branch-switch reload and self-save
  reminders belong in `.github/copilot-instructions.md`, which injects every session; a
  skill loads only on invocation and must stay focused on its own task.
