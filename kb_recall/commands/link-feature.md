Link current branch to an existing feature KB, or create a new one. Argument (optional): **$ARGUMENTS**

## When to use

- You renamed a git branch and the KB auto-load stopped working
- The hook said "Branch X has no feature KB" but a KB already exists for this feature

## Steps

### Step 1 — Get current branch

Run `git branch --show-current`.

### Step 2 — Ask user

> "Branch `{branch}` has no KB linked. What would you like to do?
> 1. Link to an existing KB (branch was renamed or reusing a KB)
> 2. Create a new KB for this branch"

### Step 3A — If linking to existing KB

Call `list_features()`. Show a numbered list of slugs with their summaries.

Ask: "Which KB should `{branch}` link to? (enter number)"

After user picks `{slug}`:

1. Ask: "Replace old branch name, or keep both?"
2. Call `update_feature_index(slug="{slug}", field="branch", value="{branch}", append=<True if "keep both" else False>)` — `confirm` defaults to False, so this writes nothing and returns a diff.
3. Show that diff verbatim, then call it again with `confirm=True` to write.
4. Confirm: "[recall-mcp] Branch `{branch}` linked to KB `{slug}`."

Never edit `features.md` directly with Edit for this — always go through `update_feature_index`.

### Step 3B — If creating new KB

Run `/recall:init` with name and slug derived from the branch name.
