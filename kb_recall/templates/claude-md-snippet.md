## Feature Knowledge Bases (recall-mcp)

Before anything else this session: check whether tools like `save_memory`,
`load_feature_context`, `list_features` are available to you.

- **Not available** → tell the user this project uses recall-mcp for persistent
  feature knowledge across sessions, and offer to set it up for them right now
  instead of asking them to open a terminal:
  - If `recall` is on PATH, offer to run it yourself:
    `cd <this project's directory> && recall setup`.
    Only run it after the user agrees. Afterward, tell them to reload Claude Code —
    you cannot trigger that yourself.
  - If `recall` isn't found, tell the user kb-recall isn't installed here and give
    them the one-line install: `uv tool install kb-recall`, then `recall setup` from
    their project directory. Do not attempt to install it yourself.
  Then continue with the user's original request normally.
- **Available** → follow the rules below.

Feature-specific context is stored in `~/.recall-mcp/` and accessed via MCP tools.
The hook handles loading automatically — KB index is injected at session start and the
active KB is loaded based on the current branch. You do not need to call `list_features`
or `load_feature_context` manually.

### Cross-feature search exists — narrow trigger

`search_features(query="...")` verifies a fact that might live in another feature —
see its own docstring for exact WHEN/FORMAT. Never use it for slug discovery; the
hook-injected feature index already covers that. Default scope is always the
current project — never pass `project="all"` or a project subset on your own
initiative.

**The moment a user asks whether something already exists, was done, was hit as a
bug, or has a rule/constraint elsewhere — or whether the current change affects
another feature — call `search_features` before answering.** This is a detectable
trigger tied to the user's own words, not a judgment call on your part; don't wait
for the save-before-write gate below to be the only path that reaches this tool.

**When the user asks to search other projects too — don't decide the scope
yourself.** Present the actual configured project names (e.g. via
`list_features()`) as choices and let them pick the relevant subset — never
assume the user already knows or will type exact names. In practice a bounded
subset is almost always the right scope (users rarely work across more than a
handful of projects at once); reserve `"all"` for when the user explicitly
wants literally everything — don't front it as an equally-weighted default,
since it tends to pull in noise from unrelated projects.

**Before saving a `[decision]`/`[rule]`/`[constraint]`/`[gotcha]` memory, or before
`report_miss`** — run this search first. If a match turns up:
- **Relevant here too, even if it lives elsewhere**: duplicate into the current KB
  anyway — yes, even though it looks redundant — with a source note ("originally
  documented in KB `<slug>`, dated `<date>`"); don't just add a `related_tickets`
  pointer instead.
- **Current task appears to belong entirely to another KB**: don't conclude from a
  snippet alone — confirm via an explicit self-declared signal (e.g. "Wrong-KB
  duplicate, authoritative KB is X") or `load_feature_context(candidate_slug)`. Even
  then, ask the user before skipping the current KB.
- **Searched only to answer a question, not to save**: just answer, no forced write.

### Save — in the same turn, not at the end

Call `save_memory(slug="<slug>", content="<insight>")` the moment you observe any of:
- A root cause or "turns out the real issue is..."
- A constraint, invariant, or rule not obvious from the code
- An approach you tried and rejected (and why)
- An architectural decision with non-obvious reasoning
- A promising direction or improvement idea raised but not yet decided — tag `[idea]`, note where discussion left off

Entries can be multi-sentence. Skip routine implementation details.
MUST write in English — KB content (memories and README sections) must be in English regardless of conversation language.
Any entry claiming something "doesn't exist / hasn't been built / isn't implemented" MUST include a `Verify: <grep/rg command you actually ran>` line — absence claims are the easiest to get wrong and the hardest to self-correct once trusted as source of truth.

After saving, if the tag qualifies for promotion, classify it by risk BEFORE answering the user:
- `[gotcha]` / `[constraint]` → section `critical_warnings`
- `[decision]` → section `architecture`
- `[rule]` → section `business_rules`
- `[bug]`, `[idea]`, `[pattern]`, `[resolved]` → skip by default — unless the content clearly matches another section's shape (e.g. a priority/triage synthesis matches `open_items`'s table, a step sequence matches `checklist`); route it to that section directly instead of skipping

**Pure append** (nothing existing needs removing, rewriting, or marking stale/superseded/resolved):
1. Output one line: `[recall-mcp] Promoted '{tag}' → {section} (auto): {title}.`
2. Call `update_readme(section="...", content="<new entry block only>", mode="append")` immediately — no approval needed.

**Removal or consolidation involved** (any existing entry needs removing, merging, or marking superseded/resolved):
1. Output one line: `[recall-mcp] Promoting '{tag}' → {section}.`
2. Read the current section content from the loaded KB (already in context).
3. Synthesize: remove stale/superseded entries, integrate the new memory alongside still-valid entries.
4. Call `update_readme(section="...", content="<synthesized>", mode="replace")` — `confirm` defaults to False, so this writes nothing and returns a real diff instead.
5. Show that diff verbatim — wrapped in a ```diff fenced code block for red/green coloring, never your own paraphrase — then ask "Apply to README `{section}`?"
6. Only after approval, call `update_readme(...)` again with `confirm=True` (same args) to actually write.

After promoting a `[decision]` — also scan `open_items`: if any row is resolved or rejected by this decision, show the updated table and ask "Apply to README `open_items`?" before calling `update_readme` — marking a row resolved always needs approval, never auto-write it.

After either promotion path succeeds, check whether the README content now fully captures the source memory's What/Why/Apply:
- **Fully captured**: ask "Mark the source memory (`id:XXXX`) as resolved now that it's promoted to README `{section}`?" — show the one-line closure note you'd write. Only on approval, call `save_memory(slug="<slug>", content="[resolved:XXXX] Promoted to README {section}: <one-line>")`. This matters because `[gotcha]`/`[decision]`/`[rule]` memories are always full-loaded (never index-only, see `load_feature_context`'s tag-tier pagination) — an unresolved duplicate of already-promoted content is pure wasted context on every future load.
- **README trimmed or paraphrased real reasoning out** (Why/Apply detail didn't make it in): don't ask to resolve — say so explicitly, since resolving would lose that detail (only the short closure line survives in the merged view; the raw memories-*.md file still has it, but recall-mcp's own guidance is not to read those directly).

Then answer the user's question normally.

### Update README when knowledge changes

Call `update_readme(slug="<slug>", section="<section>", content="<updated content>")` when:
- The moment you make or learn an architectural/business-rule decision — that's already covered by the Save flow above (save_memory → classify → promote); don't call update_readme directly for this
- You had to open files to answer a question about business logic or constraints (the KB is missing that knowledge — add it now)
- After `init_feature`, to fill in sections from scratch

### When you make a mistake the KB should have prevented

Only when the user explicitly flags it — never call this proactively for a mistake you caught yourself before it reached the user (that's a `save_memory` entry instead, e.g. tag `[gotcha]`).
- Call `report_miss(slug="<slug>", description="<what went wrong and what the KB should have said>")`.
- Immediately after, call `update_readme` on the section the miss revealed as missing.
