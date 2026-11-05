# snakesee

A fast, lightweight terminal UI for monitoring running [Snakemake](https://snakemake.readthedocs.io)
workflows, built with cluster (LSF) execution in mind.

This is a ground-up rewrite. The original [fg-labs/snakesee](https://github.com/fg-labs/snakesee)
code is in this repository's git history.

## Goals

- **Never block the UI.** All file reads and `bpeek` calls happen on background threads. Keys
  respond immediately even when the cluster filesystem is slow.
- **Cost scales with what's new, not with history.** The Snakemake log is read incrementally (only
  newly appended bytes). Nothing scans `.snakemake/metadata/` or stats input files.
- **Ctrl-Z works** at any time, including in the log viewer. `fg` brings snakesee back.
- **Job logs:** stdout (`.out`), stderr (`.err`), live `bpeek` output for running LSF jobs, and the
  rule's own `log:` files.

## Usage

```bash
snakesee                      # workflow in the current directory, following the newest log
snakesee path/to/workflow     # another workflow directory
snakesee path/to/workflow/.snakemake/log/2026-11-05T121544.990726.snakemake.log   # one specific run
snakesee -n 5                 # poll every 5 seconds (default 2)
```

### Keys

| Main screen | |
|---|---|
| `1` / `2` / `3` | Active / Failed / Done jobs |
| `enter` | Open logs for the selected job (bpeek for active jobs, stdout otherwise) |
| `o` / `e` / `b` | Open the selected job's stdout / stderr / bpeek directly |
| `r` | Poll now |
| `ctrl+z` | Suspend to the shell (`fg` to resume) |
| `q` | Quit |

| Log viewer | |
|---|---|
| `o` / `e` / `b` / `l` | Switch to stdout / stderr / bpeek / rule log (`l` cycles through multiple) |
| `f` | Toggle follow (auto-reload: files every 2s, bpeek every 5s). On by default for active jobs |
| `r` | Reload now |
| `g` / `G` | Top / bottom |
| `esc` / `q` | Back |

The Done and Failed tables show the newest 1000 jobs. The tab titles show the full counts.

## What it reads

Only the main Snakemake log in `.snakemake/log/`:

- `rule …:` blocks give the jobid, rule, wildcards, threads and `log:` paths.
- `Job N has been submitted with LSF jobid M (log: ….out)` (snakemake-executor-plugin-lsf), or
  `Submitted job N with external jobid '…'` (cluster-generic), gives the cluster job id and log.
  The stderr log is the same path with `.err` in place of `.out`.
- `Finished job N.` / `Finished jobid: N`, `Error in rule …:` blocks, `N of M steps`, `Job stats`,
  and the end-of-run messages.

"Active" means submitted but not yet finished. Snakemake's log does not say whether an LSF job is
pending or running.

## Development

```bash
uv sync
uv run pytest
uv run ruff format && uv run ruff check
uv run mypy
```
