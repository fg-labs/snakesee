# snakesee

**A terminal UI for monitoring Snakemake workflows.**

snakesee provides a rich TUI dashboard for passively monitoring Snakemake workflows. It reads directly from the `.snakemake/` directory, requiring no special flags or configuration when running Snakemake.

![snakesee monitoring a Snakemake workflow](assets/screenshot.svg)

## Features

- **Zero configuration** - Works on any existing workflow without modification
- **Historical browsing** - Navigate through past workflow executions
- **Time estimation** - Predicts remaining time from historical data
- **Rich TUI** - Vim-style keyboard controls, filtering, and sorting
- **Multiple layouts** - Full, compact, and minimal display modes
- **Optional logger plugin** - Real-time event streaming for enhanced accuracy

## Quick Start

```bash
# Install
pip install snakesee

# Watch a workflow
cd /path/to/snakemake/workflow
snakesee watch

# Or get a one-time status
snakesee status
```

## Documentation

- [Installation](installation.md) - How to install snakesee
- [Usage](usage.md) - CLI commands and TUI keyboard shortcuts

## Links

- [GitHub Repository](https://github.com/fg-labs/snakesee)
- [PyPI Package](https://pypi.org/project/snakesee/)
- [Issue Tracker](https://github.com/fg-labs/snakesee/issues)
