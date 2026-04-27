# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

snakesee is a terminal UI for monitoring Snakemake workflows. It passively reads from `.snakemake/` directories without requiring special flags when running Snakemake. Key features include real-time monitoring, historical browsing, time estimation from historical data, and vim-style keyboard controls.

## Build, Test, and Lint Commands

**Using pixi (recommended):**
```bash
pixi run install-dev    # Install development environment
pixi run check          # Run all checks (format, lint, type, tests)
pixi run fix            # Auto-fix linting issues
pixi run test           # Run unit tests only
pixi run docs           # Build documentation
```

**Using uv:**
```bash
uv sync --group dev --group docs   # Install dependencies
uv run poe check-all               # Run all checks
uv run poe fix-all                 # Auto-fix issues
uv run pytest                      # Run tests
uv run pytest tests/integration -v --no-cov  # Integration tests (requires Snakemake 9+)
```

**Running a single test:**
```bash
uv run pytest tests/test_models.py::TestJobInfo::test_specific_method -v
```

## Architecture

```text
snakesee/
├── cli.py              # CLI entry point (defopt-based)
├── models.py           # Core data models (JobInfo, WorkflowProgress)
├── estimator.py        # Time estimation orchestration
├── events.py           # Event file I/O and streaming
├── parser/             # Log parsing
│   ├── core.py         # Main parsing orchestration
│   ├── line_parser.py  # Individual log line parsing
│   └── patterns.py     # Regex patterns
├── estimation/         # Time estimation
│   ├── estimator.py    # TimeEstimator main class
│   ├── data_loader.py  # Load timing data from metadata/events
│   └── pending_inferrer.py  # Infer pending rule distribution
├── plugins/            # Tool-specific progress plugins
│   ├── base.py         # ToolProgressPlugin base class
│   └── registry.py     # Plugin discovery
├── state/              # State management
│   ├── workflow_state.py  # Top-level state container
│   ├── job_registry.py    # Job state tracking
│   └── clock.py           # Injectable clock for testing
└── tui/
    ├── app.py          # SnakeseeApp - main Textual App class
    ├── app.tcss        # CSS layout and theming
    ├── data_source.py  # WorkflowDataSource - pure-data layer
    ├── renderables.py  # Rich renderables (header, progress bar, footer)
    ├── tables.py       # DataTable row builders
    ├── screens.py      # Modal screens (help, easter egg, job log)
    └── accessibility.py # Colorblind-accessible rendering helpers
```

**Data flow:** `.snakemake/` directory → parser module → state module → estimation module → tui rendering

## Key Patterns

**Dependency injection for testing:** Use `Clock` protocol for deterministic time:
```python
from snakesee.state import FrozenClock, set_clock
clock = FrozenClock(1000.0)
set_clock(clock)
clock.advance(60.0)
```

**Deferred imports:** Many modules import inside functions to break circular dependencies. This is intentional—do not consolidate into top-level imports.

**Plugin discovery order:** Built-in → User plugins (`~/.snakesee/plugins/`) → Entry-point plugins

**Frozen dataclasses:** Core models use `@dataclass(frozen=True, slots=True)` for immutability.

## Code Style

- **Line length:** 100 characters
- **Type hints:** Required on all functions (mypy strict mode)
- **Imports:** Single-line (ruff isort)
- **Commit messages:** Conventional Commits (`feat:`, `fix:`, `docs:`, etc.)

## Logger Plugin

The `snakemake-logger-plugin-snakesee/` subdirectory contains an optional Snakemake 9+ logger plugin for real-time event streaming (vs log polling). Develop separately:
```bash
cd snakemake-logger-plugin-snakesee
pip install -e .
pytest tests/
```
