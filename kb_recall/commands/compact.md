Retire noise from a feature's memories — translate, compress verbose entries, and hide ones already captured in README. Argument (optional): **$ARGUMENTS**

## When to use

When memories have accumulated noise: non-English entries, verbose What/Why/Apply blocks, or entries whose knowledge is already in a README section.
Never use to add new knowledge — use save_memory for that.
Never use for README sections — use /recall:tidy for that.
Never run automatically — only on explicit user request.

## Step 1 — Determine slug

**If $ARGUMENTS is provided:**
Call `list_features()`. Find the best match (exact, partial, substring). Proceed without asking.

**If $ARGUMENTS is empty — auto-detect:**
1. Run `git branch --show-current`. Match against available slugs.
2. If matched → proceed immediately, no confirmation.
3. If no match → show numbered menu, ask user to pick by number.

## Step 2 — Load KB

Call `load_feature_context(slug="<slug>")` to ensure memories and README sections are in
context before categorizing — always reload, even if the KB was loaded earlier this
session, since intervening `save_memory`/`update_readme` calls could have changed its state.
If it returns a "too large to load safely" error, that KB needs *this* command precisely because it's oversized — don't stop. Fall back to reading `<project>/<slug>/memories-*.md` directly with the Read tool instead.

## Step 3 — Categorize entries

Review each memory entry and assign exactly one category:

**Retire** — add `[resolved:XXXX]` to hide, no replacement:
- Entry's primary tag (first tag) is `[decision]`, `[gotcha]`, `[constraint]`, `[rule]`, or `[pattern]` AND the same conclusion already appears in a README section (`critical_warnings`, `architecture`, or `business_rules`) with equivalent coverage. Do not retire on vague similarity — retire only when the README entry captures the same actionable constraint or decision. `[idea]` is never eligible here even if it looks duplicate — an idea that matches README content was likely adopted, not merely repeated; correct that with `[decision][supersedes:XXXX]` instead (see the "Closing an idea" convention), not a silent retire.
- Entry is pure implementation history (file renames, feature deletions, debug steps) clearly derivable from `git log`. When in doubt, keep.

**Replace** — add `[supersedes:XXXX]` + rewritten English version:
- Entry is not in English — translate first, preserving the full What/Why/Apply structure. Then check: if the translated result still exceeds the line target below, compress it too. Both steps produce one `save_memory` call with the final result.
- Entry is already English but exceeds the line target below — compress WHY + Apply to fit, without losing the constraint.

**Line target:** 5 lines, for every tag. For `[rule]`/`[constraint]`/`[gotcha]` (safety/invariant-level knowledge) only, it's fine to leave up to ~7-8 lines uncompressed rather than force-cut real Why/Apply nuance. Do not use entry age as a compression signal — verified not to correlate with importance (see KB `improve-compact-tidy` for the measured data behind this rule).

**Keep** — no action:
- Already within its line target (above), English, and not fully covered by README.

Entries with no ID (old MISS entries without `[id:XXXX]`) cannot be hidden — both `[resolved:XXXX]` and `[supersedes:XXXX]` work by referencing a target ID, so there is nothing to reference. Skip them; leave as-is.

## Step 4 — Show and execute

Show the proposed changes before executing:

```
Retire — already in README (N):
  - [id:XXXX] title — which README section covers it

Retire — git history (N):
  - [id:XXXX] title

Replace — translate (N):
  ~ [id:XXXX] old title → new English title
  ~ [id:XXXX] old title → new English title (+ compressed)

Replace — compress, already English (N):
  ~ [id:XXXX] old title — reason

(N entries unchanged)
```

If no changes found — report "Nothing to compact" and stop.

**Snapshot before executing** — ensures a rollback point exists (`save_memory` has no dry-run/undo).
`<project>` is the project name `list_features()` showed this slug grouped under in Step 1
(or the branch-matched feature's project, if auto-detected):
```
git -C ~/.recall-mcp/<project> rev-parse --is-inside-work-tree >/dev/null 2>&1 || git -C ~/.recall-mcp/<project> init -q
git -C ~/.recall-mcp/<project> add -- <slug>/
git -C ~/.recall-mcp/<project> commit -q --allow-empty -m "pre-compact snapshot: <slug>"
```

Then immediately execute — no approval needed. Call `save_memory` for each change **one at a time, sequentially — never in parallel**:
- Format retire: `save_memory(slug=..., content="[resolved:XXXX] <one-line reason>")`
- Format replace: `save_memory(slug=..., content="[original-primary-tag][supersedes:XXXX] <rewritten English entry>")`

All calls target the same slug's memories file, so they must be sequential: `save_memory` does a non-atomic read-modify-write with no lock — parallel calls to the same file silently lose entries (each call reports success even when its write got clobbered).

## Step 5 — Report and snapshot

```
Retired:   N entries (M in README, K history)  ~X chars removed
Replaced:  N entries (translated: M, compressed: K)  ~Y chars saved
Unchanged: N entries
Total:     ~Z chars (~Z÷4 tokens) lighter per load
```

Estimate savings: each retired entry ≈ original_chars − 50 (closure record ~50 chars); each replaced entry ≈ original_chars − compressed_chars.

Check the last `save_memory` call's response for a "KB maintenance" hint suffix — its absence
means memories are now back under threshold; its presence means still over, note that another
pass (or `/recall:tidy` for README) may still be needed.

**Snapshot after executing and show the aggregate diff:**
```
git -C ~/.recall-mcp/<project> diff -- <slug>/
git -C ~/.recall-mcp/<project> add -- <slug>/ && git -C ~/.recall-mcp/<project> commit -q -m "post-compact: <slug>"
```
Show this diff to the user alongside the report above — it's the real, whole-file change across
everything touched this run, not just the per-call summary. If something looks wrong: `git -C
~/.recall-mcp/<project> checkout <pre-commit-hash> -- <slug>/` restores the pre-compact state
(get `<pre-commit-hash>` from `git -C ~/.recall-mcp/<project> log --oneline -- <slug>/`).
