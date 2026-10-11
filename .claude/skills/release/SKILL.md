---
name: release
description: Pre-flight check before releasing kb-recall to PyPI. Verifies that version, uv.lock, CHANGELOG, git tag, PyPI name, branch, CI and the README that becomes the PyPI page all agree, then hands over the publish command. Read-only — it never creates the release. Use when asked whether a release is ready, or before creating a GitHub Release.
argument-hint: "[version, e.g. 0.2.1]"
---

# Release pre-flight

Answers one question: **can `vX.Y.Z` be published right now — and if not, exactly what is
missing?** This is the automated half of `RELEASING.md` step 5: the half `publish.yml`
cannot cover, because that workflow only runs *after* the release exists.

## When to use

Before creating a GitHub Release, or when asked whether `main` is ready to ship.
Never to publish: this skill creates no tag, no release, no PyPI upload, and must not be
extended to. Publishing is permanent and belongs to the human.

Read `RELEASING.md` before changing anything here — it is the source of truth for the process,
and this skill is a check on it, not a replacement.

## Step 1 — Fix what is being released, before checking anything

The release is built from the commit its **Target** points at, not from your working tree.
Everything below therefore reads `origin/main`, and reading the working tree instead is the
one mistake that makes this whole skill report on the wrong commit.

```bash
git fetch origin
git rev-parse origin/main                       # this is the commit that will be tagged
git rev-parse --abbrev-ref HEAD                 # your local branch — informational
```

Set `SHA` to `origin/main` and read every file below with
`git show "$SHA":<path>` (or check out `main` first, which is what `RELEASING.md` step 5
tells you to do).

**Determine the target version:** strip a leading `v` from the argument if one was given
(`/release 0.2.1`); otherwise take it from the commit being released:

```bash
git show "$SHA":pyproject.toml | grep -m1 '^version = '
```

Carry that value through every step as `X.Y.Z`. If an argument was given and it disagrees with
this, that is a failure — do not silently prefer either one.

## Step 2 — Version agreement

```bash
git show "$SHA":pyproject.toml | grep -m1 '^version = '          # expect: version = "X.Y.Z"
git show "$SHA":uv.lock | grep -A1 'name = "kb-recall"' | tail -1 # expect: version = "X.Y.Z"
uv sync --locked                                                  # see caveat below
```

`uv.lock` pins the project's own version, so a bump without `uv lock` leaves it stale — and
`uv sync --locked` in `ci.yml` then fails every job, which reads as a code failure rather than
a forgotten bookkeeping step.

**Caveat on `uv sync --locked`:** it validates the *working tree*, so it is only authoritative
here when `HEAD == SHA`. If you are on a feature branch, it proves nothing about the release
commit — Step 6's green CI on `SHA` is what covers that case, because `ci.yml` runs
`uv sync --locked` itself.

## Step 3 — CHANGELOG entry

```bash
git show "$SHA":CHANGELOG.md | grep -n "^## \[X.Y.Z\]"      # heading must exist
git show "$SHA":CHANGELOG.md | grep -n "^\[X.Y.Z\]:"        # compare-link ref must exist
```

Both are needed: the heading renders the entry, the link ref resolves the version number.
Check the heading's date is today and that the entry is substantive — an empty or placeholder
section passes a grep and should not pass this step.

There is no `Unreleased` section to move; this changelog is written at release time.

## Step 4 — Tag is free

```bash
git tag -l "vX.Y.Z"                        # local — expect empty
git ls-remote --tags origin "vX.Y.Z"       # remote — expect empty
```

## Step 5 — Version is free on PyPI

```bash
curl -s https://pypi.org/pypi/kb-recall/json \
  | python3 -c "import sys,json; print(sorted(json.load(sys.stdin)['releases']))"
```

`X.Y.Z` must not appear in that list. Use the **JSON API, never the HTML page**: the project
page at `pypi.org/project/kb-recall/` sits behind a bot challenge that answers HTTP **200**
with a "Client Challenge" body, so scraping it for a 404 gives a false pass.

This is one of the two things `publish.yml` cannot check. PyPI never allows re-uploading a
version, even after a yank — a wrong number here costs the version permanently.

## Step 6 — CI on the commit being released

```bash
gh run list --branch main --limit 5 --json conclusion,headSha,displayTitle \
  -q '.[] | "\(.conclusion)\t\(.headSha[0:7])\t\(.displayTitle)"'
```

Expect a `success` row whose `headSha` matches `SHA`. A blank conclusion means still running,
not passing. If no row matches `SHA` at all, CI has not run on it yet — wait, do not proceed.

Belt and braces: `publish.yml` re-runs the whole suite on the tagged commit, so this catches
nothing that would ship. What it catches is a release attempted against a commit whose CI you
have not actually looked at.

## Step 7 — The history under the tag (WARN)

Every other step reads files; this one reads the commits. By the time this skill runs they are
already on `main`, so it can only **report** — rewriting `main`'s history is a different risk
class from rewriting a branch, and is not the remedy. Say so; do not offer it.

```bash
LAST=$(git describe --tags --abbrev=0 "$SHA")
git log --oneline "$LAST".."$SHA"
```

WARN when either holds:

- **A subject carries no information** — `Commit`, `wip`, `fix`, `update`, `test`, a bare
  version number. It is permanent once on `main`, and the CHANGELOG entry is composed from
  exactly this log.
- **A fixup landed as its own commit** — one or two files, existing only to correct the commit
  immediately before it.

Report the count and the offending subjects, and name the remedy as something the *branch*
should have had: squash there before landing, with `git reset --soft <base> && git commit`
when an interactive rebase is unavailable. Nothing about the release is blocked by this.

## Step 8 — The PyPI-facing README

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
on disk now. It is still worth running, but treat a mismatch as *inconclusive* rather than a
failure unless `HEAD == SHA`; the count on `SHA` is whatever CI collected there.

Images are the exception already handled: they use absolute `raw.githubusercontent.com` URLs
for the same reason as the links.

## Step 9 — Local checkout state

```bash
git status --porcelain        # expect empty
git rev-parse HEAD            # compare to SHA
```

Not a failure — a warning, and a specific one. The release form's **Target** dropdown accepts
any branch and `publish.yml` does not validate it, so the state this step exists to catch is
you filling in that form while your checkout still sits on a feature branch. That is exactly
where a wrong Target gets picked. Say so plainly; do not fail the run over it.

## Step 10 — Report

One table, every step, `PASS` / `FAIL` / `WARN` plus the observed value. Then:

**If anything failed** — list what is missing and the command that fixes it. Stop there. Do not
offer to fix it in the same breath; a release blocked for a reason is information, and the fix
may deserve its own commit and PR.

**If everything passed** — say so plainly, then hand over the publish step. It is the human's to
run:

```bash
gh release create vX.Y.Z --target main --title "vX.Y.Z" --notes-file <notes>
```

For `<notes>`, follow `v0.2.0`'s shape: a short hand-written intro paragraph, then the
CHANGELOG section verbatim. Offer to write that file — but do not create the release.

Close by naming what remains unverifiable from here: the release form's **Target** must be
`main` when the human fills it in, and a published version can never be corrected, only
superseded.

## Known traps

| Trap | Why it bites |
| --- | --- |
| Reading the working tree instead of `origin/main` | Silently validates the wrong commit. Your checkout is usually still on the feature branch right after the release PR merges — the exact moment this skill runs. |
| `git push origin vX.Y.Z` | Publishes nothing. `publish.yml` listens for a *published release*, not a tag push — deliberate, so a stray tag cannot ship a branch commit. |
| Version bump without `uv lock` | `ci.yml`'s `uv sync --locked` fails every job; looks like a code failure. |
| `pypi.org` HTML returns 200 | Bot challenge. Use `https://pypi.org/pypi/kb-recall/json`. |
| A tag left over from a failed attempt | Deleting the tag and re-pushing re-triggers nothing. Re-run the failed job instead — nothing reached PyPI, so the number is still free. |
| Placeholder commit subjects already on `main` | Invisible to every other step, permanent once landed, and the CHANGELOG entry is composed from exactly that log. Step 7 WARNs; the fix belonged on the branch, before it landed. |
