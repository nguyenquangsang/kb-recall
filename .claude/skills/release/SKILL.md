---
name: release
description: Take a release from a feature branch to a publishable main. Phase A prepares the branch — version, uv.lock, CHANGELOG, docs synced in both directions, the local suite, commit history, the PR, the release notes. Phase B verifies the commit that will be tagged — version agreement, a free tag and PyPI name, green CI, the README that becomes the PyPI page. Never creates the GitHub Release; it ends by walking you through the merge and the publish command. Use when asked whether a release is ready, or before creating one.
argument-hint: "[version, e.g. 0.3.0]"
---

# Release

Two phases, split by where the commits are:

| | Phase A — prepare | Phase B — verify |
| --- | --- | --- |
| Runs on | the branch | `origin/main` |
| Writes | version, `uv.lock`, CHANGELOG, docs, notes, history | nothing in the repo — read-only |
| A defect found here | fixed cheaply | costs another commit + PR + CI round |

Both phases are one skill because the boundary between them is **observable** — the skill
routes itself rather than making you remember which half to run. **Phase A is where defects
are cheap**: every FAIL Phase B reports is a PR-shaped bill for something Phase A could have
caught.

**This skill never publishes.** No GitHub Release, no tag push, no PyPI upload — permanent,
and the human's. Phase B ends by handing over the command.

**Re-running it must be safe.** Phase B is read-only, so run it as often as you like. Phase A
*writes*, and each write converges on a target state rather than appending to one: re-entering
after an interruption must reach the same place, not a doubled one. The steps below say where
that is load-bearing — a re-run that cannot tell "already done" from "not yet done" is a
re-run that corrupts.

**Scratch artifacts live in `dist/`.** The PR body (Step A6) and the release notes (Step A8) are
written to `dist/pr-body-vX.Y.Z.md` and `dist/release-notes-vX.Y.Z.md`. `dist/` is gitignored and
hatchling leaves VCS-ignored files out of the sdist — measured, not assumed: a file placed there
does not appear in `uv build`'s tarball. The names are version-stamped so one release cannot
overwrite another's notes. A note written anywhere else in the tree becomes a file that ships.

`RELEASING.md` is the process document; this skill is a check on it, not a replacement. One
place they differ deliberately — `RELEASING.md` step 5 lands the version bump and CHANGELOG
entry **directly on `main`**, while Phase A does that same work on the branch, so the commit
that gets tagged is one CI has already seen. Both work; this skill implements the second.

## When to use

Before creating a GitHub Release, or when asked whether a release is ready.
Never to publish: no tag, no release, no PyPI upload, and this must not be extended to.

## Step 1 — Route

```bash
git fetch origin
git fetch --tags origin
git rev-parse --abbrev-ref HEAD
git log --oneline "origin/main..HEAD"
```

- **Commits ahead of `origin/main`** → Phase A.
- **Nothing ahead, release work already on `origin/main`** → Phase B.
- **Both** — a branch ahead *and* `origin/main` carrying the un-released version: finish
  Phase A and re-fetch first. Phase B reads `origin/main`, so running it now verifies the
  commit *before* the release work — all green, all meaningless.

Carry `X.Y.Z` through every step: strip a leading `v` from the argument if one was given. With
no argument, Phase A proposes it in Step A1 and Phase B reads it from `pyproject.toml`. Every
`X.Y.Z` in a command below is that number substituted in, not a literal.

---

# Phase A — prepare (on the branch)

Writes. Everything here is still mutable, which is the point.

## Step A1 — Propose the version, then wait

**Re-entry guard — check whether the bump already happened**, so an interrupted run resumes
instead of starting over. Read all three: the newest tag, the branch tip, and the file you are
about to write.

```bash
LAST=$(git describe --tags --abbrev=0)
echo "at $LAST : $(git show "$LAST":pyproject.toml | grep -m1 '^version = ')"
echo "on HEAD : $(git show HEAD:pyproject.toml | grep -m1 '^version = ')"
echo "on disk : $(grep -m1 '^version = ' pyproject.toml)"
git log --oneline "$LAST"..HEAD
git diff --stat "$LAST"..HEAD
```

If `HEAD` **or** the working tree already carries a version newer than `$LAST`, the bump has
landed — confirm the number is the one you intend and skip to Step A3. The working tree is in
that check because Phase A edits files before it commits them, so `HEAD` alone would miss a
bump that is written but not yet committed.

Otherwise there is a version to choose. If an argument was given, strip its `v` and use it; if
not, propose one and **stop for confirmation before writing anything**.

State the rule the number came from:

- **major** — a breaking change to the CLI, the MCP tool signatures, or the on-disk KB layout
- **minor** — a new feature, flag or tool
- **patch** — fixes only

Below 1.0.0 this repo has treated a breaking change as **minor** (`0.2.0` added Copilot,
`0.3.0` removed `--platform`). Say what you inferred and why. This is the one step in the
skill that must not be automated: a diff cannot tell "new feature" from "breaking change" by
itself, and a number published to PyPI is permanent — never corrected, only superseded.

## Step A2 — Write the bookkeeping

```bash
# pyproject.toml → version = "X.Y.Z"
uv lock          # required: uv.lock pins the project's own version, and ci.yml runs
                 # `uv sync --locked`, which fails every job if the two disagree
```

Then the CHANGELOG entry under a new heading **dated today**:

```markdown
## [X.Y.Z] — YYYY-MM-DD
```

and its compare-link ref at the bottom of the file:

```markdown
[X.Y.Z]: https://github.com/nguyenquangsang/kb-recall/compare/vLAST...vX.Y.Z
```

Both are needed — the heading renders the entry, the ref resolves the version number. There
is no `Unreleased` section to move; this changelog is written at release time, composed from
the `git log` and diffstat above. The date is the **release** day, not the day the entry was
drafted: Phase B fails on a stale one, after merge, when fixing it costs a PR.

**Re-entry rule — this is the one write in Phase A that is not a no-op the second time.** If
`## [X.Y.Z]` already exists, edit that section; never append a second heading. Likewise
replace the existing `[X.Y.Z]:` ref rather than adding another line. Duplicate headings and
duplicate refs both render wrong and neither raises an error, so check before writing:

```bash
grep -n "^## \[X.Y.Z\]\|^\[X.Y.Z\]:" CHANGELOG.md
```

## Step A3 — Documentation, in both directions

Sync runs both ways, and only one of them is a grep.

**Docs that describe something that is gone** — the defects that are invisible locally and
permanent once published:

```bash
# Stated test counts must match the real one.
grep -rn '[0-9]\+ tests' README.md GUIDE.md
uv run pytest --collect-only -q | tail -1        # compare

# Retired flags and command forms, in prose and in examples.
grep -rn -- '--platform\|recall setup$' README.md GUIDE.md CLAUDE.md
```

**Behaviour that is new and undocumented.** No grep finds this one, because a missing sentence
has nothing to match. Read what the branch itself changed, and ask per user-facing change whether
a reader of `README.md` would learn it exists:

```bash
git diff --stat "$(git merge-base HEAD origin/main)"..HEAD
```

A new flag, command, MCP tool or setup step that no README or GUIDE line mentions is a FAIL. The
CHANGELOG entry is not a substitute: it is read after the fact, by people already interested,
while someone deciding whether to upgrade reads the docs. `recall doctor` got its README section
in 0.3.0 because a human remembered to write one — no step required it, which is exactly the gap
this half closes.

`README.md` matters most: it is embedded in the package metadata and **becomes the PyPI
project page**. A wrong number there ships, and a published version cannot be re-uploaded —
only superseded. Everything else can be corrected in the next release; that one is frozen.

## Step A4 — The local suite, before CI

```bash
uv run pytest -q
uv run ruff check kb_recall tests scripts
uv run ruff format --check .
uv run mypy kb_recall
uv run recall-server < /dev/null                 # expect exit 0
```

`ci.yml` runs `ruff check` only. The repo is otherwise format-clean, so formatter drift is
invisible in CI and shows up the first time anyone runs the formatter by hand — 0.3.0 landed
an unformatted block in `cli.py` exactly that way.

## Step A5 — Commit hygiene (the part that expires at merge)

```bash
LAST=$(git describe --tags --abbrev=0)
git log --oneline "$LAST"..HEAD
git rev-parse --abbrev-ref --symbolic-full-name '@{u}'   # does the branch have a remote?
```

**This is the last step that writes to a tracked file, and the ordering is deliberate.** Steps
A1–A4 all edit the tree, so squashing ahead of them would leave their output as fresh commits
*after* the squash — two commits to land where one was intended, and a second squash to clean up.
Squashing last folds the version, `uv.lock`, the CHANGELOG and the doc fixes into the single
commit that gets tagged.

The review half of "look at the commits first" is not deferred by that: Step A1's guard already
prints the full branch log and diffstat before anything is written, and the block above reprints
it. What waits until now is only the rewrite.

**Squash when the branch has no upstream** — nothing has seen the history yet, so rewriting
it costs nothing:

```bash
BASE=$(git merge-base HEAD origin/main)
BEFORE=$(git rev-parse 'HEAD^{tree}')
git reset --soft "$BASE" && git commit -m "<subject>"
test "$BEFORE" = "$(git rev-parse 'HEAD^{tree}')"    # must match: same tree, fewer commits
```

`-m` is not optional: a bare `git commit` opens an editor and hangs a non-interactive agent.

`git reset --soft` rather than `git rebase -i`, which not every harness can open. The tree
check is not decorative — a squash that changes the tree is a squash that dropped something.

Re-running the squash is tree-safe — same base, same tree, still one commit — but it rebuilds
the commit every time, so **the SHA changes**. Harmless while the branch is local, which is
exactly why the upstream check comes first.

**Ask before squashing an already-pushed branch**: that needs a force-push, which is
outward-facing and invalidates any review or CI already run on those commits. Once the work
has merged, Step B7 can only *report* — the subjects are permanent, and the CHANGELOG entry
is composed from exactly that log.

A subject carries what changed and why. No placeholders — `Commit`, `wip`, `fix`, `update`,
`test`, a bare version number.

## Step A6 — Push and open the PR

```bash
git push -u origin "$(git rev-parse --abbrev-ref HEAD)"
gh pr view "$(git rev-parse --abbrev-ref HEAD)" --json url -q .url 2>/dev/null \
  || gh pr create --base main --title "<subject>" --body-file dist/pr-body-vX.Y.Z.md
```

The `gh pr view` guard is what makes this step re-runnable: `git push` is already a no-op the
second time ("Everything up-to-date"), but `gh pr create` is not — it exits non-zero with
"a pull request for branch … already exists". Harmless, and still worth not doing: it is the
one error in Phase A that looks like a failure while nothing is wrong.

Write the body to a file rather than letting `--fill` copy the commit message in: a commit
message is not a PR description, and `gh` will not open an editor for you. A good body says
what changed, how it was verified, and what happens after merge — `v0.3.0`'s is the shape to
copy. The file goes in gitignored `dist/`, so the body never becomes a tracked file that ships.

Stop here. Merging `main` is the human's, and this skill does not do it — but the *button* is
load-bearing, so name it. This repo merges with **Create a merge commit**: `83001d3` carries two
parents, and the squashed `46b255a` is still an ancestor of `main`. Under that strategy the commit
Step A5 built is exactly what lands.

**"Squash and merge" throws it away and substitutes the PR title** — quietly undoing A5, and
leaving Step B7 to read a history that no longer says what was written. That is why `--title`
above is not decoration: keep it byte-identical to A5's subject, so either button lands the same
subject and only the wasted path differs.

From here the hand-off is four steps, and only the last is irreversible:

1. CI green on the PR head — Step A7.
2. You merge, with **Create a merge commit**.
3. Phase B runs against the merged `origin/main`.
4. Phase B hands you `gh release create`; you run it.

## Step A7 — CI on the PR head

```bash
gh pr checks --watch
```

Every job green on the branch tip, or Phase A is not finished. Step B6 checks CI again on
the merge commit — which is what actually gets tagged — but that check can only block; this
one still allows a fix.

## Step A8 — The release notes

The text that goes into the GitHub Release, written now while the change is still fresh — not
later, standing in the release form trying to remember what the branch was for.

```bash
$EDITOR dist/release-notes-vX.Y.Z.md
```

Shape, following `v0.2.0`: a short hand-written intro paragraph saying what this release is for
and who should care, then the CHANGELOG section for `X.Y.Z` **verbatim**. Copy it, do not
paraphrase it, so the two cannot drift apart.

This is the one Phase A step that produces nothing inside the repository: `dist/` is gitignored,
so there is nothing to commit and Step A5's squash never saw it. The CHANGELOG stays the
permanent record; these notes are the announcement, and `gh release create` is where they land.

Drafting early has one cost worth naming — review, a late fix, or the merge itself can change what
is actually being released, and these notes were written from the branch. Step B10 is the
reconciliation: it re-reads the CHANGELOG **on `SHA`** against this file before handing over the
command. Draft early, verify late.

---

# Phase B — verify (on `origin/main`)

Read-only. Everything here is frozen: a FAIL can only be fixed by another PR.

## Step B1 — Fix what is being released

The release is built from the commit its **Target** points at, not from your working tree.
Everything below therefore reads `origin/main`, and reading the working tree instead is the
one mistake that makes this whole phase report on the wrong commit.

```bash
git rev-parse origin/main                       # this is the commit that will be tagged
```

Set `SHA` to `origin/main` and read every file below with `git show "$SHA":<path>` (or check
out `main` first, which is what `RELEASING.md` step 5 tells you to do).

```bash
git show "$SHA":pyproject.toml | grep -m1 '^version = '
```

Carry that value through every step as `X.Y.Z`. If an argument was given and it disagrees,
that is a failure — do not silently prefer either one.

## Step B2 — Version agreement

```bash
git show "$SHA":pyproject.toml | grep -m1 '^version = '           # expect version = "X.Y.Z"
git show "$SHA":uv.lock | grep -A1 'name = "kb-recall"' | tail -1 # expect version = "X.Y.Z"
uv sync --locked
```

**Caveat on `uv sync --locked`:** it validates the *working tree*, so it is authoritative
only when `HEAD == SHA`. On a feature branch it proves nothing about the release commit —
Step B6's green CI on `SHA` covers that case, because `ci.yml` runs `uv sync --locked` itself.

## Step B3 — CHANGELOG entry

```bash
git show "$SHA":CHANGELOG.md | grep -n "^## \[X.Y.Z\]"      # heading must exist
git show "$SHA":CHANGELOG.md | grep -n "^\[X.Y.Z\]:"        # compare-link ref must exist
```

Check the heading's date is **today** and that the entry is substantive — an empty or
placeholder section passes a grep and should not pass this step.

## Step B4 — Tag is free

```bash
git tag -l "vX.Y.Z"                        # local — expect empty
git ls-remote --tags origin "vX.Y.Z"       # remote — expect empty
```

## Step B5 — Version is free on PyPI

```bash
curl -s https://pypi.org/pypi/kb-recall/json \
  | python3 -c "import sys,json; print(sorted(json.load(sys.stdin)['releases']))"
```

`X.Y.Z` must not appear in that list. Use the **JSON API, never the HTML page**: the project
page at `pypi.org/project/kb-recall/` sits behind a bot challenge that answers HTTP **200**
with a "Client Challenge" body, so scraping it for a 404 gives a false pass.

One of the two things `publish.yml` cannot check. PyPI never allows re-uploading a version,
even after a yank — a wrong number here costs the version permanently.

## Step B6 — CI on the commit being released

```bash
gh run list --branch main --limit 5 --json status,conclusion,headSha,displayTitle \
  -q '.[] | "\(.status)\t\(.conclusion // "-")\t\(.headSha[0:7])\t\(.displayTitle)"'
```

Expect a `completed` + `success` row whose `headSha` matches `SHA`. A blank conclusion means
still running — not passing. If no row matches `SHA` at all, CI has not run on it yet: wait,
do not proceed.

Belt and braces: `publish.yml` re-runs the whole suite on the tagged commit, so this catches
nothing that would ship. What it catches is a release attempted against a commit whose CI you
have not actually looked at.

## Step B7 — The history under the tag (WARN)

Every other step reads files; this one reads the commits. They are already on `main`, so it
can only **report** — rewriting `main`'s history is a different risk class from rewriting a
branch, and is not the remedy. Say so; do not offer it.

```bash
LAST=$(git describe --tags --abbrev=0 "$SHA")
git log --oneline "$LAST".."$SHA"
```

WARN when either holds:

- **A subject carries no information** — `Commit`, `wip`, `fix`, `update`, `test`, a bare
  version number. Permanent once on `main`, and the CHANGELOG entry was composed from
  exactly this log.
- **A fixup landed as its own commit** — one or two files, existing only to correct the commit
  immediately before it.

Report the count and the offending subjects, and name the remedy as something the *branch*
should have had — Step A5, before landing. Nothing about the release is blocked by this.

## Step B8 — The PyPI-facing README

`README.md` is embedded in the package metadata and becomes the PyPI project page. Anything
broken there ships to users, so check it here rather than after publishing.

```bash
# No repo-relative links: on PyPI they resolve against pypi.org and dead-end.
git show "$SHA":README.md | grep -noE '\]\([^)#][^)]*\)' | grep -v http   # expect empty

# The stated test count must match the real one.
uv run pytest --collect-only -q | tail -1
git show "$SHA":README.md | grep -o '# [0-9]* tests'
```

The test count is the one check that reads a different tree than the rest — it counts what is
on disk now. Still worth running, but treat a mismatch as *inconclusive* rather than a failure
unless `HEAD == SHA`; the count on `SHA` is whatever CI collected there.

Images are the exception already handled: they use absolute `raw.githubusercontent.com` URLs
for the same reason as the links.

## Step B9 — Local checkout state

```bash
git status --porcelain        # expect empty
git rev-parse HEAD            # compare to SHA
```

Not a failure — a warning, and a specific one. The release form's **Target** dropdown accepts
any branch and `publish.yml` does not validate it, so the state this step exists to catch is
filling in that form while your checkout still sits on a feature branch. That is exactly where
a wrong Target gets picked. Say so plainly; do not fail the run over it.

## Step B10 — Report

One table, every step, `PASS` / `FAIL` / `WARN` plus the observed value. Then:

**If anything failed** — list what is missing and the command that fixes it. Stop there. Do
not offer to fix it in the same breath; a release blocked for a reason is information, and the
fix may deserve its own commit and PR. If the FAIL is Phase A's kind — a version, a CHANGELOG
entry, a stale claim, documentation that never got written, notes that no longer match — say
plainly that it belonged in Phase A, where it cost nothing.

**If everything passed** — say so plainly, then hand over the publish step. It is the human's
to run:

```bash
gh release create vX.Y.Z --target main --title "vX.Y.Z" \
  --notes-file dist/release-notes-vX.Y.Z.md
```

**Reconcile the notes first — the one thing this phase is allowed to touch.** They are the file
Step A8 drafted from the *branch*, while the commit being tagged is `SHA`; and they live in
gitignored `dist/`, not in the released tree, so refreshing them does not break this phase's
contract of changing nothing that ships. Compare them against the frozen CHANGELOG:

```bash
git show "$SHA":CHANGELOG.md | sed -n '/^## \[X.Y.Z\]/,/^## \[/p'
cat dist/release-notes-vX.Y.Z.md
```

Absent, or quoting a CHANGELOG section that has moved since they were written, is a FAIL to
report: a release note describing a commit that is not the one being tagged is exactly the defect
this check exists for. Offer to rewrite them — but do not create the release.

Close by naming what remains unverifiable from here: the release form's **Target** must be
`main` when the human fills it in, and a published version can never be corrected, only
superseded.

## Known traps

| Trap | Why it bites |
| --- | --- |
| Reading the working tree instead of `origin/main` | Silently validates the wrong commit. Your checkout is usually still on the feature branch right after the release PR merges — the exact moment Phase B runs. |
| Running Phase B while the branch is still ahead | It reads `origin/main`, so it verifies the commit *before* the release work: green, and meaningless. |
| `git push origin vX.Y.Z` | Publishes nothing. `publish.yml` listens for a *published release*, not a tag push — deliberate, so a stray tag cannot ship a branch commit. |
| Version bump without `uv lock` | `ci.yml`'s `uv sync --locked` fails every job; looks like a code failure. |
| CHANGELOG dated the day the entry was drafted | Phase B fails on it after merge, and the date is part of what ships. |
| Squashing a branch that is already pushed | Needs a force-push, which invalidates any review or CI already run on those commits. Ask first. |
| `pypi.org` HTML returns 200 | Bot challenge. Use `https://pypi.org/pypi/kb-recall/json`. |
| A tag left over from a failed attempt | Deleting the tag and re-pushing re-triggers nothing. Re-run the failed job instead — nothing reached PyPI, so the number is still free. |
| Editing `README.md` after publishing | You cannot. A version cannot be re-uploaded, only superseded — whatever `README.md` said is what the project page says, permanently. |
| Placeholder commit subjects already on `main` | Invisible to every other step, permanent once landed, and the CHANGELOG entry is composed from exactly that log. Step B7 WARNs; the fix belonged in Step A5, before it landed. |
| Merging with "Squash and merge" | Discards the subject Step A5 composed and substitutes the PR title, at the last click. This repo uses **Create a merge commit** — `46b255a` is still an ancestor of `main`. |
| Squashing before the version and CHANGELOG are written | Those writes then land as fresh commits *after* the squash: two commits where one was intended, and a second squash to clean up. Step A5 is deliberately last. |
| Scratch files written into the working tree | A PR body or note left in the repo is untracked noise at best; tracked, it ships in the sdist. Both belong in gitignored `dist/`. |
| Release notes written at hand-over time | Composed from a faded memory, with nothing having checked them against the CHANGELOG on the commit being tagged. Step A8 drafts them early; Step B10 reconciles. |
