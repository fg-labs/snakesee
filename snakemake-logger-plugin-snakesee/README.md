# snakemake-logger-plugin-snakesee

A Snakemake logger plugin that integrates with [snakesee](https://github.com/fg-labs/snakesee) for enhanced workflow monitoring.

## Overview

This plugin captures real-time workflow events directly from Snakemake's execution engine and writes them to a JSONL file that snakesee can read. This provides more accurate and timely job status information than log parsing alone.

## Installation

```bash
pip install snakemake-logger-plugin-snakesee
```

Or install alongside snakesee:

```bash
pip install snakesee snakemake-logger-plugin-snakesee
```

## Usage

Run Snakemake with the snakesee logger:

```bash
snakemake --logger snakesee --cores 4
```

In another terminal, run snakesee to monitor:

```bash
snakesee watch
```

The plugin writes workflow events to `.snakesee_events.jsonl` in the workflow directory. Snakesee automatically detects and reads these events for real-time updates.

## Benefits

Using the logger plugin provides:

- **Instant job detection**: Jobs are reported immediately when they start/finish, not when log lines are parsed
- **Accurate timing**: Job durations are calculated from actual execution timestamps
- **Direct wildcard access**: Wildcard values come directly from Snakemake, not regex parsing
- **Reliable error tracking**: Failed jobs are detected via Snakemake events, not log patterns

## Configuration

| Setting | Default | Description |
|---------|---------|-------------|
| `event_file` | `.snakesee_events.jsonl` | Path to event file (relative to workflow directory) |
| `buffer_size` | `1` | Events to buffer before flush (1 = immediate) |

## Event Types

The plugin emits the following event types:

| Event | Description |
|-------|-------------|
| `workflow_started` | Workflow initialization |
| `job_submitted` | Job queued for execution |
| `job_started` | Job execution began |
| `job_finished` | Job completed successfully |
| `job_error` | Job failed |
| `progress` | Overall progress update |

## Event File Format

Events are written as JSON Lines (one JSON object per line):

```jsonl
{"event_type":"workflow_started","timestamp":1703001234.123}
{"event_type":"job_submitted","timestamp":1703001235.0,"job_id":1,"rule_name":"align","wildcards":{"sample":"A"}}
{"event_type":"job_started","timestamp":1703001235.5,"job_id":1}
{"event_type":"job_finished","timestamp":1703001335.5,"job_id":1,"duration":100.0}
{"event_type":"progress","timestamp":1703001335.6,"completed_jobs":1,"total_jobs":10}
```

## Backward Compatibility

Snakesee works with or without this plugin:

- **Without plugin**: Uses log parsing and metadata files (existing behavior)
- **With plugin**: Uses real-time events for enhanced accuracy

The plugin is completely optional - snakesee will automatically use events when available and fall back to log parsing otherwise.

## Requirements

- Python 3.11+
- Snakemake 8.0+ (for logger plugin support)
- snakemake-interface-logger-plugins >= 1.0.0

## License

MIT
