Rewrite README sections of a feature KB to be compact, English-only, and high signal-to-token. Argument (optional): **$ARGUMENTS**

## When to use

When a KB README has grown verbose or cluttered — sections mix languages, entries have long backstory, or token cost of loading the KB has grown noticeably.
Never use to add new knowledge — use save_memory for that.
Never use for memories entries — use /recall:compact for that.
Never run automatically — only on explicit user request.

## Step 1 — Determine slug

**If $ARGUMENTS is provided:**
Call `list_features()`. Find the best match (exact, partial, substring). Proceed without asking.

**If $ARGUMENTS is empty — auto-detect:**
1. Run `git branch --show-current`. Match against available slugs.
2. If matched → proceed immediately, no confirmation.
3. If no match → show numbered menu, ask user to pick by number.

## Step 2 — Load and rewrite sections

Call `load_feature_context(slug="<slug>")` to ensure section content is in context before rewriting.
If it returns a "too large to load safely" error, that KB needs *this* command precisely because it's oversized — don't stop. Fall back to reading `<project>/<slug>/README.md` directly with the Read tool instead.

Review these sections in order: `critical_warnings`, `architecture`, `business_rules`.
Skip a section if no meaningful reduction is possible (all entries are already concise and distinct).

For each entry, apply these rules to produce a rewritten version:

**Compress:** line target 5 lines per entry, for every tag. For `[rule]`/`[constraint]`/`[gotcha]` entries only, it's fine to leave up to ~7-8 lines uncompressed rather than force-cut real WHY nuance. Do not use entry age as a compression signal (see KB `improve-compact-tidy` for the measured data behind this rule). Untagged prose blocks (e.g. architecture's structural descriptions) aren't covered by this — use judgment as before. Keep the actionable constraint or conclusion + WHY. Drop: backstory, how it was discovered, verbose implementation context.

**Translate:** normalize all content to English.

**Remove** entries that:
- Reference a file, tool, or feature that no longer exists in the codebase
- Are fully duplicate in intent with another entry in the same section

**Merge** entries with overlapping intent into one.

**Rejected options:** compress to 1-2 lines — keep the conclusion and the core reason. Drop elaboration.
> Example — Before: `**[decision] GAP-1 rejected — hook stdout injection is worse than MCP tool load** ... (8 lines)`
> After: `GAP-1 (direct KB injection via hook stdout): rejected — costs tokens on every message; MCP tool loads once into context.`

Do not remove entries solely because they are old. Do not drop the WHY entirely — a future session that sees only "X rejected" without reason may re-propose X.

## Step 3 — Verify, show, and execute

If no section needs changes — report "Nothing to tidy" and stop.

**Snapshot before executing** — ensures a rollback point exists.
`<project>` is the project name `list_features()` showed this slug grouped under in Step 1
(or the branch-matched feature's project, if auto-detected):
```
git -C ~/.recall-mcp/<project> rev-parse --is-inside-work-tree >/dev/null 2>&1 || git -C ~/.recall-mcp/<project> init -q
git -C ~/.recall-mcp/<project> add -- <slug>/
git -C ~/.recall-mcp/<project> commit -q --allow-empty -m "pre-tidy snapshot: <slug>"
```

For each changed section, call `update_readme(..., mode="replace", confirm=False)` first — this
writes nothing, just returns the REAL diff (`difflib.unified_diff`) between current and proposed
content. Check that diff against what you intended to change: does it touch only the entries you
meant to compress/translate/remove/merge, and nothing else? This step exists because a
self-authored summary can be wrong in ways the real diff catches — do not skip it, even though no
user approval follows.

If the diff looks wrong (touches unintended entries, drops something you didn't mean to remove) —
stop, re-derive the correct content, and re-check before proceeding. Never call confirm=True on a
diff you haven't actually looked at.

If the diff matches intent, show a compact summary to the user (one line per section):

```
{section}: N→N' entries, ~X→~Y chars
  ~ [tag] title — reason for change  (compressed or translated)
  - [tag] title — reason             (removed)
  ✦ title A + title B → new title    (merged)
  (N entries unchanged)
```

Then immediately call `update_readme(..., mode="replace", confirm=True)` with the same content —
no additional user approval needed (invoking `/recall:tidy` was already explicit consent for this
operation). Do this for each changed section **one at a time, sequentially — never in parallel**.

All changed sections live in the same README.md, so calls must be sequential: `update_readme` does a non-atomic read-modify-write with no lock — parallel calls to the same file silently lose one section's write (and can even return a spurious "Section not found" from a torn read of a file mid-write by another call).

## Step 4 — Report and snapshot

```
{section}: N→N' entries, ~X→~Y chars
{section}: skipped — already concise
Total: ~Z chars (~Z÷4 tokens) saved
```

Check the last `update_readme` call's response for a "KB maintenance" hint suffix — its absence
means README sections are now back under threshold; its presence means still over, note that
another pass (or `/recall:compact` for memories) may still be needed.

**Snapshot after executing and show the aggregate diff:**
```
git -C ~/.recall-mcp/<project> diff -- <slug>/
git -C ~/.recall-mcp/<project> add -- <slug>/ && git -C ~/.recall-mcp/<project> commit -q -m "post-tidy: <slug>"
```
Show this diff to the user alongside the report above — it's the real, whole-file change across
every section touched this run, not just the per-section diffs already checked in Step 3. If
something looks wrong: `git -C ~/.recall-mcp/<project> checkout <pre-commit-hash> -- <slug>/`
restores the pre-tidy state (get `<pre-commit-hash>` from `git -C ~/.recall-mcp/<project> log
--oneline -- <slug>/`).
