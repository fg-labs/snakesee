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
snakesee --bjobs-interval 120 # query LSF for running jobs' memory every 2 min (default 60; 0 = off)
```

### Keys

| Main screen | |
|---|---|
| `1` / `2` / `3` | Active / Failed / Done jobs |
| `4` | Rules: per-rule progress drawn as a dependency graph |
| `j` / `k`, arrows | Move down / up |
| `ctrl+d` / `ctrl+u` | Half page down / up |
| `ctrl+f` / `ctrl+b`, PgDn / PgUp | Page down / up |
| `g` / `G` | First / last job |
| `enter` | Open logs for the selected job (bpeek for active jobs, stdout otherwise) |
| `o` / `e` / `b` | Open the selected job's stdout / stderr / bpeek directly |
| `r` | Poll now |
| `ctrl+z` | Suspend to the shell (`fg` to resume) |
| `q` / `ctrl+q` | Quit |

| Log viewer | |
|---|---|
| `o` / `e` / `b` / `l` | Switch to stdout / stderr / bpeek / rule log (`l` cycles through multiple) |
| `f` | Toggle follow (auto-reload: files every 2s, bpeek every 5s). On by default for active jobs |
| `r` | Reload now |
| `j` / `k`, arrows | Scroll one line |
| `ctrl+d` / `ctrl+u` | Half page down / up |
| `ctrl+f` / `space` / `ctrl+b` | Page down / page down / page up |
| `g` / `G` | Top / bottom |
| mouse drag, then `ctrl+c` | Copy the selected text |
| `esc` / `q` | Back |

The Done and Failed tables show the newest 1000 jobs. The tab titles show the full counts.

Copying uses the OSC 52 terminal escape, which most modern terminals support (iTerm2, kitty,
WezTerm, Windows Terminal, recent xterm), including over SSH. Inside tmux, set
`set -g set-clipboard on`. If your terminal doesn't support it, hold `shift` (or `option` on macOS)
while dragging to use the terminal's own selection.

## What it reads

Only the main Snakemake log in `.snakemake/log/`:

- `rule …:` blocks give the jobid, rule, wildcards, threads and `log:` paths.
- `Job N has been submitted with LSF jobid M (log: ….out)` (snakemake-executor-plugin-lsf), or
  `Submitted job N with external jobid '…'` (cluster-generic), gives the cluster job id and log.
  The stderr log is the same path with `.err` in place of `.out`.
- `Finished job N.` / `Finished jobid: N`, `Error in rule …:` blocks, `N of M steps`, `Job stats`,
  and the end-of-run messages.

### Memory use (LSF)

- **Running jobs:** a single batched `bjobs -o "jobid stat max_mem avg_mem memlimit run_time" -json`
  call for all active jobs, every `--bjobs-interval` seconds, on a background thread. This also
  gives the LSF state, so Active shows `PEND` / `RUN`, and the header shows running vs queued.
- **Finished and failed jobs:** the "Resource usage summary" LSF writes at the top of each job's
  `.out`. Each file is read once, and only for jobs shown in the tables. Missing files are retried
  for a few minutes, since LSF writes them at job end.
- Tables show **Max mem** and **% req**: yellow at 75% or more of the requested memory, red at 90%
  or more. Failed jobs show LSF's termination reason (e.g. `TERM_MEMLIMIT`) when there is one.
- The log viewer header shows max/avg memory, run and CPU time, and threads.

### Pending jobs and the Rules view

Snakemake only names a job in the log when it submits it, so individual pending jobs can't be
listed. Per-rule pending counts come from the `Job stats` table: planned jobs minus those seen so
far. They appear in the header and in the Rules tab.

The Rules tab draws rules as a `git log --graph`-style DAG, coloured by state: red if any job
failed, yellow if any are running, green when all are done, dim if nothing has started.
Dependencies are inferred from the log: rule A → rule B when a B job's input is an output of an A
job in this run. Edges are therefore only known once a rule's first job has been submitted. Rules
with no jobs yet are listed separately below the graph. Dependencies on outputs from earlier runs
aren't shown. snakesee never runs Snakemake to build the DAG.

"Active" means submitted but not yet finished. Snakemake's log does not say whether an LSF job is
pending or running.

## Development

```bash
uv sync
uv run pytest
uv run ruff format && uv run ruff check
uv run mypy
```
