# Contributing to snakesee

Thank you for your interest in contributing to snakesee!

## Development Setup

### Prerequisites

- Python 3.11 or later
- [pixi](https://pixi.sh/) (recommended) or uv

### Setup with pixi

```bash
# Clone the repository
git clone https://github.com/fg-labs/snakesee.git
cd snakesee

# Install development environment
pixi run install-dev

# Run all checks (lint, type check, tests)
pixi run check

# Run tests only
pixi run test

# Auto-fix linting issues
pixi run fix

# Build documentation
pixi run docs
```

### Setup with uv

```bash
# Clone the repository
git clone https://github.com/fg-labs/snakesee.git
cd snakesee

# Create virtual environment and install
uv sync --group dev --group docs
uv pip install -e '.[logo]'

# Run checks
uv run poe check-all
```

## Code Style

This project uses:

- **ruff** for linting and formatting
- **mypy** for type checking
- **pytest** for testing

All code should:

- Pass `ruff format` (formatting)
- Pass `ruff check` (linting)
- Pass `mypy` (type checking)
- Have tests with >65% coverage

## Running Checks

```bash
# Run all checks
pixi run check
# or
uv run poe check-all

# Auto-fix linting issues
pixi run fix
# or
uv run poe fix-all

# Run just tests
pixi run test
# or
uv run pytest
```

## Pull Request Process

1. Fork the repository and create a feature branch
2. Make your changes
3. Ensure all checks pass: `pixi run check`
4. Update documentation if needed
5. Submit a pull request

## Release Process

Releases are automated via GitHub Actions when a SemVer tag is pushed:

```bash
# Ensure everything is committed and pushed
git push origin main

# Create and push a tag
git tag 0.2.0
git push origin 0.2.0
```

The CI will automatically:
1. Run all tests
2. Build the source distribution
3. Publish to PyPI
4. Generate changelog with git-cliff
5. Create a GitHub release

## Commit Messages

We use [Conventional Commits](https://www.conventionalcommits.org/) for generating changelogs:

- `feat:` New features
- `fix:` Bug fixes
- `docs:` Documentation changes
- `test:` Test changes
- `refactor:` Code refactoring
- `style:` Code style changes
- `chore:` Maintenance tasks

Examples:
```
feat: add support for filtering by rule name
fix: handle missing log files gracefully
docs: update keyboard shortcuts table
```

## Logger Plugin Development

The repository includes an optional Snakemake logger plugin in `snakemake-logger-plugin-snakesee/`.

### Plugin Setup

```bash
# Install the plugin in development mode
cd snakemake-logger-plugin-snakesee
pip install -e .

# Run plugin tests
pytest tests/
```

### Plugin Structure

- `src/snakemake_logger_plugin_snakesee/handler.py` - Main LogHandler implementation
- `src/snakemake_logger_plugin_snakesee/events.py` - Event types and serialization
- `src/snakemake_logger_plugin_snakesee/writer.py` - JSONL event file writer

The plugin writes events to `.snakesee_events.jsonl` which snakesee reads via `snakesee/events.py`.

## Questions?

Open an issue at https://github.com/fg-labs/snakesee/issues
