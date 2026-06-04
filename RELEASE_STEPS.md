# Release Steps for snakesee

## Quick Reference: Release Checklist

```bash
# 1. Bump version in pyproject.toml (single source of truth)
#    __init__.py reads it dynamically via importlib.metadata

# 2. Commit the version bump
git add pyproject.toml
git commit -m "chore: bump version to X.Y.Z"
git push origin main

# 3. Create and push a tag (bare semver, NO 'v' prefix)
git tag X.Y.Z
git push origin X.Y.Z

# 4. Verify
#    - PyPI: https://pypi.org/project/snakesee/
#    - GitHub: https://github.com/fg-labs/snakesee/releases
#    - Install: pip install snakesee==X.Y.Z
```

CI automatically:
1. Verifies the tag is on `main`
2. Runs all tests (Python 3.11, 3.12, 3.13)
3. Builds source distribution (`uv build --sdist`)
4. Publishes to PyPI (OIDC authentication)
5. Generates changelog via `git-cliff`
6. Creates GitHub Release with changelog

## Pre-release Checklist

Before releasing, ensure:
- [ ] All tests pass: `pixi run check` or `uv run poe check-all`
- [ ] Coverage meets 80% threshold
- [ ] Documentation builds: `pixi run docs`
- [ ] All PRs for this release are merged to `main`

## Important Notes

- **Tag format**: Use bare semver (`0.7.0`), NOT `v0.7.0`. The publish workflow
  triggers on tags matching `[0-9]+.[0-9]+.[0-9]+`.
- **Version location**: Only update `pyproject.toml`. The `__init__.py` reads
  the version dynamically via `importlib.metadata.version("snakesee")`.
- **Changelog**: Generated automatically by `git-cliff` from conventional commit
  messages. No manual CHANGELOG.md updates needed.
- **Commit messages**: Use [Conventional Commits](https://www.conventionalcommits.org/)
  (`feat:`, `fix:`, `docs:`, `perf:`, `refactor:`, `chore:`, etc.) so git-cliff
  categorizes them correctly.

---

## Releasing the Logger Plugin

The Snakemake logger plugin (`snakemake-logger-plugin-snakesee/`) has its own
independent release cycle.

```bash
# 1. Bump version in snakemake-logger-plugin-snakesee/pyproject.toml

# 2. Commit the version bump
git add snakemake-logger-plugin-snakesee/pyproject.toml
git commit -m "chore: bump logger plugin version to X.Y.Z"
git push origin main

# 3. Create and push a tag (prefixed with 'snakesee-logger-')
git tag snakesee-logger-X.Y.Z
git push origin snakesee-logger-X.Y.Z

# 4. Verify
#    - PyPI: https://pypi.org/project/snakemake-logger-plugin-snakesee/
#    - GitHub: https://github.com/fg-labs/snakesee/releases
#    - Install: pip install snakemake-logger-plugin-snakesee==X.Y.Z
```

CI automatically:
1. Verifies the tag is on `main`
2. Runs plugin tests (Python 3.11, 3.12, 3.13)
3. Builds source distribution from the subdirectory
4. Publishes to PyPI (OIDC authentication)
5. Creates GitHub Release

**Tag format**: Use `snakesee-logger-X.Y.Z` (e.g., `snakesee-logger-0.1.0`).
This is distinct from the main snakesee tags which use bare semver.

---

## First-time Setup (already completed)

These steps were done during initial project setup and are kept here for reference.

### PyPI Publishing

Publishing uses OIDC token authentication (configured in `.github/workflows/publish.yml`
and `.github/workflows/publish-logger-plugin.yml` with environment `pypi`). No API
tokens need to be stored as secrets. The logger plugin reuses the same `pypi` environment
— ensure `snakemake-logger-plugin-snakesee` is registered as a trusted publisher in
PyPI settings.

### Bioconda

To add or update the bioconda recipe:

1. Fork https://github.com/bioconda/bioconda-recipes
2. Create/update `recipes/snakesee/meta.yaml` with the new version and SHA256 hash:
   ```bash
   curl -sL https://pypi.org/pypi/snakesee/json | \
     python -c "import sys, json; print(json.load(sys.stdin)['urls'][0]['digests']['sha256'])"
   ```
3. Submit PR to bioconda-recipes

### Read the Docs

Documentation is hosted at https://snakesee.readthedocs.io/ and configured via
`.readthedocs.yml` in the repository root.

### Codecov

Coverage reporting is configured via `codecov.yml` and the `CODECOV_TOKEN` GitHub secret.
