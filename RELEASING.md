# Releasing

Two parts: **one-time setup** (once per repo/PyPI pair) and **each release**
(repeated for every version). Publishing runs through GitHub Actions and PyPI
Trusted Publishing — there is no API token to store or rotate.

## One-time setup

### 1. Make the repository public

GitHub → Settings → General → Danger Zone → Change repository visibility → Public.

A private repo returns HTTP 404 to anonymous requests, which is also how you can
check the current state without logging in:

```bash
curl -s -o /dev/null -w "%{http_code}\n" https://github.com/nguyenquangsang/kb-recall
```

### 2. Create the `pypi` environment

GitHub → Settings → Environments → New environment → name it exactly `pypi`.

No secrets are needed — Trusted Publishing authenticates through OIDC. Optionally
enable **Required reviewers** to require a manual approval before any publish job
runs. The name must match the `environment:` value in `.github/workflows/publish.yml`
character for character.

### 3. Configure the PyPI trusted publisher

pypi.org → Account settings → Publishing → **Add a new pending publisher**:

| Field | Value |
|---|---|
| PyPI Project Name | `kb-recall` |
| Owner | `nguyenquangsang` |
| Repository name | `kb-recall` |
| Workflow name | `publish.yml` |
| Environment name | `pypi` |

It is a *pending* publisher because the project does not exist on PyPI yet — the
first successful publish creates it and converts the pending publisher into a
real one. **Workflow name is the filename `publish.yml`, not a path.** A
mismatch in either the workflow name or the environment name shows up only as an
OIDC failure in the `publish` job, on the one run that is hardest to redo.

Check the name is still free before relying on it:

```bash
curl -s -o /dev/null -w "%{http_code}\n" https://pypi.org/pypi/kb-recall/json   # 404 = free
```

## Each release

### 4. Land the change on `main`

Open a PR into `main` and let CI run, or merge locally and push. The PR path is
preferable for the first release: it exercises `.github/workflows/ci.yml` before
a tag ever depends on it.

### 5. Tag and push

The tag must point at a commit on `main`, and the tag must agree with `version`
in the `[project]` table of `pyproject.toml` — nothing checks this automatically.

```bash
# editor: bump version in pyproject.toml
git checkout main && git pull
git tag v0.1.0
git push origin v0.1.0
```

This triggers `.github/workflows/publish.yml`, which calls `ci.yml` (ruff, mypy
and pytest on Python 3.10–3.13) and only then builds and publishes to PyPI.

### 6. Verify

```bash
uv tool install kb-recall
recall --help
```

Then open <https://pypi.org/project/kb-recall/> — the full README is embedded in
the package metadata, so the project page should render completely.

## Notes

- **A published version number is permanent.** PyPI never allows re-uploading the
  same version, not even after a yank. A bug found post-publish means a new
  version, never a fix to the existing one.
- **A failed `publish` job has not consumed a version.** If the OIDC handshake
  fails because step 3 was filled in wrong, fix step 3, delete the tag, and push
  it again — nothing reached PyPI, so `0.1.0` is still available.
- **`permissions: id-token: write` without `contents: read`.** The `publish` job
  declares only the OIDC permission, matching PyPI's own documented example.
  For a public repository `actions/checkout` reads anonymously, so this is fine;
  if the checkout step ever fails, adding `contents: read` to that job's
  `permissions` block is the fix.
- **The distribution name is `kb-recall`, the command is `recall`.** They differ
  deliberately. `recall-mcp` on PyPI belongs to an unrelated project, so install
  lines must always say `kb-recall`.
