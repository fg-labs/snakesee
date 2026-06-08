# Release Steps for snakesee

Releases are automated with [release-please](https://github.com/googleapis/release-please),
driven by [Conventional Commits](https://www.conventionalcommits.org/) on `main`.
Both packages are managed from one manifest: the main `snakesee` package (tags
`X.Y.Z`) and `snakemake-logger-plugin-snakesee` (tags `snakesee-logger-X.Y.Z`).

## How a release happens

1. Merge PRs to `main` using Conventional Commit messages (`feat:`, `fix:`,
   `perf:`, etc.). `feat:` bumps the minor version, `fix:`/`perf:` the patch
   version; `!`/`BREAKING CHANGE` bumps major.
2. The `release-please` workflow opens (or updates) a **release PR** per package
   that bumps the version in `pyproject.toml` and updates `CHANGELOG.md`.
3. Review and **merge the release PR**. release-please then creates the git tag
   and the GitHub Release.
4. The tag triggers `publish.yml` (or `publish-logger-plugin.yml`), which runs
   tests, builds the sdist, and publishes to PyPI via Trusted Publishing.

That's it — no manual version bumps, tags, or changelog edits.

## Configuration

- `release-please-config.json` — per-package release types and tag formats.
- `.release-please-manifest.json` — current released versions (release-please
  reads and writes this).
- `.github/workflows/release-please.yml` — runs release-please on push to `main`,
  authenticated as the FG_LABS_BOT GitHub App so the created tag triggers publish.

## Verifying a release

- PyPI (main): https://pypi.org/project/snakesee/
- PyPI (plugin): https://pypi.org/project/snakemake-logger-plugin-snakesee/
- GitHub Releases: https://github.com/fg-labs/snakesee/releases
- Install: `pip install snakesee==X.Y.Z`

## Manual release (emergency escape hatch)

If release-please is unavailable, you can publish by pushing a tag from `main`.
The publish workflows gate on the tag being on `main`.

```bash
# Main package: bump version in pyproject.toml, commit, then:
git tag X.Y.Z
git push origin X.Y.Z

# Logger plugin: bump version in snakemake-logger-plugin-snakesee/pyproject.toml, commit, then:
git tag snakesee-logger-X.Y.Z
git push origin snakesee-logger-X.Y.Z
```

After a manual tag, update `.release-please-manifest.json` to the released
version so release-please stays in sync.

**Tag format**: bare semver for snakesee (`0.8.1`), `snakesee-logger-` prefix for
the plugin (`snakesee-logger-0.1.1`). No `v` prefix.

---

## First-time Setup (already completed)

Kept for reference.

### PyPI Trusted Publishing

Both packages publish via OIDC Trusted Publishing — no API tokens. The PyPI
trusted-publisher registration is pinned to the workflow filenames
(`publish.yml`, `publish-logger-plugin.yml`) and the `pypi` environment; these are
unchanged by the release-please automation, so no PyPI-side edits are needed.

### FG_LABS_BOT GitHub App

`release-please.yml` mints an installation token from the org `FG_LABS_BOT` app
(`FG_LABS_BOT_APP_ID` / `FG_LABS_BOT_PRIVATE_KEY`, org secrets with visibility
ALL). The App token — not `GITHUB_TOKEN` — is required so the tag release-please
creates can trigger the tag-driven publish workflows.

### Bioconda

To update the bioconda recipe after a PyPI release:

1. Fork https://github.com/bioconda/bioconda-recipes
2. Update `recipes/snakesee/meta.yaml` with the new version and SHA256:
   ```bash
   curl -sL https://pypi.org/pypi/snakesee/json | \
     python -c "import sys, json; print(json.load(sys.stdin)['urls'][0]['digests']['sha256'])"
   ```
3. Submit a PR to bioconda-recipes.

### Read the Docs

Docs are hosted at https://snakesee.readthedocs.io/ and configured via
`.readthedocs.yml`.

### Codecov

Coverage reporting is configured via `codecov.yml` and the `CODECOV_TOKEN` secret.
