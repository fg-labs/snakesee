# CLAUDE.md

Guidance for Claude Code when working in this repository.

## Project

snakesee is a terminal UI (Textual) for monitoring running Snakemake workflows, especially on LSF.
It is a deliberate ground-up rewrite of fg-labs/snakesee (old code is in git history). The previous
codebase was abandoned because it blocked the UI thread with full rescans on every refresh.

## Non-negotiable design rules

1. **The UI thread never does I/O.** No file reads, `stat`, `scandir` or subprocesses in Textual
   event handlers, timers or actions. Do I/O in `@work(thread=True)` workers and hand plain data
   back with `call_from_thread`. A test (`test_ui_stays_responsive_when_filesystem_is_slow`) guards
   this.
2. **Polling cost must scale with new data, not history.** The log is tailed by byte offset
   (`tail.FileTailer`) and parsed incrementally (`parser.LogParser`). Never re-read the whole log,
   scan `.snakemake/metadata`, or stat workflow input files on a poll.
3. **Ctrl-Z must keep working with a single press**, including under `uv run`/`uvx`. The app
   overrides `action_suspend_process`: it restores the terminal first, then sends SIGTSTP to the
   whole process group. `tests/test_suspend.py` checks this in a real pty. Don't add bindings
   that swallow ctrl+z.
4. **Keep DataTables small and diff-updated.** Update changed cells only. Finished and failed tables
   are capped (`ROW_LIMIT`). `remove_row` is O(rows), so rebuild instead of removing many rows.
5. **No backwards compatibility constraints.** Change CLI, keys or behaviour freely.

## Layout

```text
snakesee/
├── cli.py          # argparse entry point; imports Textual lazily
├── model.py        # Job, JobStatus, RunStatus, RunSummary
├── parser.py       # LogParser: incremental, pure (no I/O), line-at-a-time
├── tail.py         # FileTailer (incremental byte-offset reads), read_tail
├── monitor.py      # WorkflowMonitor: finds newest log, tails it, feeds parser (worker thread)
├── joblogs.py      # job log sources: .out / .err / bpeek / rule logs, fetching (worker thread)
└── tui/
    ├── app.py        # SnakeseeApp: polling worker, tables, header
    ├── log_screen.py # LogScreen: per-job log viewer with follow mode
    ├── widgets.py    # JobTable / PagerLog: DataTable / Log with less-style keys
    └── format.py     # duration/time formatting
```

## Commands

```bash
uv sync
uv run pytest
uv run ruff format && uv run ruff check
uv run mypy          # strict
```

## Style

- Line length 100, single-line imports (ruff isort), full type hints (mypy strict).
- Conventional Commits (`feat:`, `fix:`, `perf:`, `docs:`, ...).
- Parser test fixtures live in `tests/data/`. Base new fixtures on real Snakemake logs, anonymized.
