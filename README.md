# snakesee

[![Language][language-badge]][language-link]
[![Python][python-badge]][python-link]
[![Code style][code-style-badge]][code-style-link]
[![Type checked][type-check-badge]][type-check-link]
[![License][license-badge]][license-link]

[![Tests][tests-badge]][tests-link]
[![codecov][codecov-badge]][codecov-link]
[![Documentation][docs-badge]][docs-link]
[![PyPI version][pypi-badge]][pypi-link]
[![PyPI downloads][pypi-downloads-badge]][pypi-link]
[![Bioconda][bioconda-badge]][bioconda-link]

**A terminal UI for monitoring Snakemake workflows.**

snakesee provides a Textual TUI dashboard for passively monitoring Snakemake workflows. It reads directly from the `.snakemake/` directory, requiring no special flags or configuration when running Snakemake.

<p align="center">
<img alt="snakesee monitoring a Snakemake workflow" src="docs/assets/screenshot.svg" width="800">
</p>

<p>
<a href="https://fulcrumgenomics.com">
<picture>
  <source media="(prefers-color-scheme: dark)" srcset=".github/logos/fulcrumgenomics-dark.svg">
  <source media="(prefers-color-scheme: light)" srcset=".github/logos/fulcrumgenomics-light.svg">
  <img alt="Fulcrum Genomics" src=".github/logos/fulcrumgenomics-light.svg" height="100">
</picture>
</a>
</p>

[Visit us at Fulcrum Genomics](https://www.fulcrumgenomics.com) to learn more about how we can power your Bioinformatics with snakesee and beyond.

<a href="mailto:contact@fulcrumgenomics.com?subject=[GitHub inquiry]"><img src="https://img.shields.io/badge/Email_us-%2338b44a.svg?&style=for-the-badge&logo=gmail&logoColor=white"/></a>
<a href="https://www.fulcrumgenomics.com"><img src="https://img.shields.io/badge/Visit_Us-%2326a8e0.svg?&style=for-the-badge&logo=wordpress&logoColor=white"/></a>

## Features

- **Zero configuration** - Works on any existing workflow without modification
- **Historical browsing** - Navigate through past workflow executions
- **Time estimation** - Predicts remaining time from historical data
- **Textual TUI** - Vim-style keyboard controls, filtering, and sorting
- **Multiple layouts** - Full, compact, and minimal display modes

## Why snakesee?

| Tool | Approach | Requirements | Status |
|------|----------|--------------|--------|
| **snakesee** | Passive (reads `.snakemake/`) | None | Active |
| [snkmt](https://github.com/cademirch/snkmt) | Active (logger plugin) | `--logger snkmt` + SQLite | Active |
| [Panoptes](https://github.com/panoptes-organization/panoptes) | Active (WMS monitor) | `--wms-monitor` + server | Early dev |
| [snakemake-terminal-monitor](https://github.com/nesi/snakemake-terminal-monitor) | Passive (reads logs) | Requires running workflow | Maintained |
| [snk](https://github.com/Wytamma/snk) | CLI wrapper | Workflow installation | Active |
| Built-in `--dag`/`--rulegraph` | Static visualization | Graphviz | Built-in |

## Installation

### pip (recommended)

```bash
pip install snakesee
```

### pip with logo support

```bash
pip install snakesee[logo]
```

### conda / mamba

```bash
conda install -c bioconda snakesee
```

## Usage

### Watch a workflow in real-time

```bash
# In a workflow directory
snakesee watch

# Or specify a path
snakesee watch /path/to/workflow
```

### Get a one-time status snapshot

```bash
snakesee status
snakesee status /path/to/workflow
```

### Options

```bash
snakesee watch --refresh 5.0      # Refresh every 5 seconds (default: 2.0)
snakesee watch --no-estimate      # Disable time estimation
snakesee status --no-estimate     # Status without ETA
```

## Time Estimation

snakesee predicts remaining workflow time using historical execution data from `.snakemake/metadata/`. The estimation uses multiple strategies depending on available data:

### Estimation Methods

| Method | When Used | Confidence |
|--------|-----------|------------|
| **Weighted** | Historical data available | High (0.5-0.9) |
| **Simple** | No historical data, some jobs completed | Medium (0.3-0.7) |
| **Bootstrap** | No jobs completed yet | Low (0.05) |

### How It Works

1. **Per-rule timing**: Historical execution times are tracked for each rule (e.g., `align`, `sort`, `index`)
2. **Recency weighting**: Recent runs are weighted more heavily using exponential decay
3. **Pending rule inference**: Assumes remaining jobs follow the same rule distribution as completed jobs
4. **Parallelism adjustment**: Estimates concurrent job execution from historical completion rates

### ETA Display Formats

| Format | Meaning |
|--------|---------|
| `~5m` | High confidence estimate |
| `3m - 8m` | Medium confidence, shows range |
| `~10m (rough)` | Low confidence estimate |
| `~15m (very rough)` | Very low confidence |
| `unknown` | Insufficient data |

### Weighting Strategies

snakesee supports two strategies for weighting historical timing data:

#### Index-Based Weighting (Default)

Weights runs by how many runs ago they occurred, regardless of actual time elapsed:
- **Most recent run** has the highest weight
- **Older runs** (by log index) progressively contribute less
- **Default half-life**: 10 logs (after 10 runs, weight is halved)

This is ideal for **active development** where each pipeline run may fix issues:

```bash
snakesee watch --weighting-strategy index --half-life-logs 10
```

#### Time-Based Weighting

Weights runs by wall-clock time since each run:
- **Recent runs** (within the last week) have the highest influence
- **Default half-life**: 7 days (after 7 days, a run's weight is halved)

This is better for **stable pipelines** where old data should naturally age out:

```bash
snakesee watch --weighting-strategy time --half-life-days 7
```

Both strategies help adapt to:
- Hardware changes (new machine, more cores)
- Software updates (faster tool versions)
- Pipeline improvements and bug fixes

### Wildcard Conditioning

When enabled, snakesee tracks timing separately for each wildcard value (e.g., `sample=A`, `sample=B`). This improves estimates when different inputs have significantly different runtimes.

```bash
# Enable via CLI flag
snakesee watch --wildcard-timing

# Or toggle in TUI with 'w' key
```

**When to use**: Enable when your workflow processes inputs of varying sizes (e.g., genome samples, dataset batches) and execution times vary significantly between them.

### Portable Timing Profiles

Export timing data to share across machines or bootstrap new runs:

```bash
# Export profile from current workflow
snakesee profile-export

# Export to a specific file
snakesee profile-export --output timing.json

# Merge with existing profile (combine data)
snakesee profile-export --merge

# View profile contents
snakesee profile-show .snakesee-profile.json

# Use a profile for estimation
snakesee watch --profile timing.json
```

Profiles are auto-discovered: snakesee searches for `.snakesee-profile.json` in the workflow directory and parent directories.

### Tool-Specific Progress Plugins

snakesee includes plugins that parse tool-specific log files to show real-time progress within running jobs. This is particularly useful for long-running bioinformatics tools.

**Built-in plugins:**
| Tool | Progress Detection |
|------|-------------------|
| **BWA** | Processed reads count |
| **STAR** | Finished reads count |
| **samtools sort** | Records processed |
| **samtools index** | Records indexed |
| **fastp** | Reads processed/passed |
| **fgbio** | Records processed |

**How it works:**
1. When a job is running, snakesee searches for its log file
2. Plugins detect the tool from rule name or log content
3. Progress is extracted and displayed in the TUI

**Creating custom plugins:**

Create a Python file in `~/.snakesee/plugins/` or `~/.config/snakesee/plugins/`:

```python
# ~/.snakesee/plugins/my_tool.py
import re
from snakesee.plugins.base import ToolProgress, ToolProgressPlugin

class MyToolPlugin(ToolProgressPlugin):
    @property
    def tool_name(self) -> str:
        return "mytool"

    def can_parse(self, rule_name: str, log_content: str) -> bool:
        return "mytool" in rule_name.lower()

    def parse_progress(self, log_content: str) -> ToolProgress | None:
        # Parse your tool's log format
        match = re.search(r"Processed (\d+) items", log_content)
        if match:
            return ToolProgress(
                items_processed=int(match.group(1)),
                unit="items"
            )
        return None
```

User plugins are automatically discovered and loaded when snakesee starts.

**Entry-point plugins (for package authors):**

Third-party packages can register plugins via setuptools entry points. Add to your `pyproject.toml`:

```toml
[project.entry-points."snakesee.plugins"]
my_tool = "my_package.plugins:MyToolPlugin"
```

Entry-point plugins are discovered automatically when the package is installed.

### Enhanced Monitoring with Real-Time Events

For real-time event streaming (instead of log polling), you can enable event-based monitoring:

#### Snakemake 9.0+ (Logger Plugin)

Install the optional Snakemake logger plugin:

```bash
pip install snakemake-logger-plugin-snakesee
```

Then run Snakemake with the logger:

```bash
snakemake --logger snakesee --cores 4
```

#### Snakemake 8.x (Log Handler Script)

Use the built-in log handler script:

```bash
snakemake --log-handler-script $(snakesee log-handler-path) --cores 4
```

> **Note:** The log handler script is optimized for local execution where jobs start
> immediately after submission. For cluster/cloud executors (SLURM, AWS Batch, etc.),
> jobs shown as "running" may still be queued. For accurate queue tracking on clusters,
> use Snakemake 9+ with the logger plugin.

#### Monitoring

In another terminal, monitor with snakesee:

```bash
snakesee watch
```

**Benefits of real-time events:**

| Feature | Log Parsing | Real-Time Events |
|---------|-------------|------------------|
| Job detection | Polling (delayed) | Immediate |
| Start times | Approximate (log mtime) | Exact timestamp |
| Durations | Calculated from logs | Precise from events |
| Failed jobs | Pattern matching | Direct notification |

Real-time events are optional - snakesee works without them using log parsing, and automatically uses events when available.

### Workflow Status Detection

snakesee determines if a workflow is actively running by checking:

1. **Lock files** exist in `.snakemake/locks/`
2. **Incomplete markers** exist in `.snakemake/incomplete/` (jobs in progress)
3. **Log file** was recently modified (within the stale threshold)

If lock files AND incomplete markers exist, the workflow is considered **RUNNING** regardless of log age. This handles very long-running jobs that don't update the log file.

If lock files exist but no incomplete markers, snakesee falls back to checking log freshness. The **stale threshold** defaults to **30 minutes** (1800 seconds). If the log hasn't been updated within this threshold, the workflow is considered interrupted (INCOMPLETE status).

## TUI Keyboard Shortcuts

### General

| Key | Action |
|-----|--------|
| `q` | Quit |
| `?` | Show help |
| `p` | Pause/resume auto-refresh |
| `e` | Toggle time estimation |
| `w` | Toggle wildcard conditioning |
| `r` | Force refresh |
| `Ctrl+r` | Hard refresh (reload historical data) |

### Refresh Rate

| Key | Action |
|-----|--------|
| `+` / `-` | Fine adjust (±0.5s) |
| `<` / `>` | Coarse adjust (±5s) |
| `0` | Reset to default (1s) |
| `G` | Set to minimum (0.5s, fastest) |

### Layout & Filtering

| Key | Action |
|-----|--------|
| `Tab` | Cycle layout (full/compact/minimal) |
| `/` | Filter rules by name |
| `n` / `N` | Next/previous filter match |
| `Esc` | Clear filter, return to latest log |

### Log History Navigation

| Key | Action |
|-----|--------|
| `[` / `]` | View older/newer log (1 step) |
| `{` / `}` | View older/newer log (5 steps) |

### Table Sorting

| Key | Action |
|-----|--------|
| `s` / `S` | Cycle sort table forward/backward |
| `1-4` | Sort by column (press again to reverse) |

### Modal Navigation (vim-style)

snakesee uses a two-mode navigation system for exploring jobs and logs:

**Enter Table Mode:** Press `Enter` from the main view

| Key | Action |
|-----|--------|
| `j` / `k` | Move down/up one row |
| `g` / `G` | Jump to first/last row |
| `Ctrl+d` / `Ctrl+u` | Half-page down/up |
| `Ctrl+f` / `Ctrl+b` | Full-page down/up |
| `h` / `l` | Switch to running/completions table |
| `Tab` | Cycle between tables |
| `Enter` | View selected job's log |
| `Esc` | Exit table mode |

**Log Viewing Mode:** Press `Enter` on a selected job

| Key | Action |
|-----|--------|
| `j` / `k` | Scroll down/up one line |
| `g` / `G` | Jump to start/end of log |
| `Ctrl+d` / `Ctrl+u` | Half-page down/up |
| `Ctrl+f` / `Ctrl+b` | Full-page down/up |
| `Esc` | Return to table mode |

## Development

See [CONTRIBUTING.md](CONTRIBUTING.md) for development setup and guidelines.

## Disclaimer

This codebase was written with the assistance of AI (Claude). All code has been reviewed and tested, but users should evaluate fitness for their use case.

## License

[MIT License](LICENSE) - Copyright (c) 2024 Fulcrum Genomics LLC

[language-badge]: https://img.shields.io/badge/language-Python-blue
[language-link]: https://www.python.org/
[python-badge]: https://img.shields.io/badge/python-3.11%20%7C%203.12%20%7C%203.13-blue
[python-link]: https://www.python.org/
[code-style-badge]: https://img.shields.io/badge/code%20style-ruff-000000
[code-style-link]: https://github.com/astral-sh/ruff
[type-check-badge]: https://img.shields.io/badge/type%20checked-mypy-blue
[type-check-link]: https://mypy.readthedocs.io/
[license-badge]: https://img.shields.io/badge/license-MIT-blue
[license-link]: https://github.com/nh13/snakesee/blob/main/LICENSE
[tests-badge]: https://github.com/nh13/snakesee/actions/workflows/tests.yml/badge.svg
[tests-link]: https://github.com/nh13/snakesee/actions/workflows/tests.yml
[codecov-badge]: https://codecov.io/gh/nh13/snakesee/graph/badge.svg
[codecov-link]: https://codecov.io/gh/nh13/snakesee
[docs-badge]: https://readthedocs.org/projects/snakesee/badge/?version=latest
[docs-link]: https://snakesee.readthedocs.io/en/latest/
[pypi-badge]: https://img.shields.io/pypi/v/snakesee
[pypi-link]: https://pypi.org/project/snakesee/
[pypi-downloads-badge]: https://img.shields.io/pypi/dm/snakesee
[bioconda-badge]: https://img.shields.io/conda/vn/bioconda/snakesee
[bioconda-link]: https://anaconda.org/bioconda/snakesee
