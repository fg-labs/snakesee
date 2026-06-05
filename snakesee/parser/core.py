"""Parsers for Snakemake log files and job tracking.

This module contains log file parsing functions for tracking running,
completed, and failed jobs. Metadata parsing has been moved to
snakesee.parser.metadata and statistics collection to snakesee.parser.stats.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from pathlib import Path
from typing import TYPE_CHECKING

from snakesee.constants import STALE_WORKFLOW_THRESHOLD_SECONDS
from snakesee.models import JobInfo
from snakesee.models import WorkflowProgress
from snakesee.models import WorkflowStatus
from snakesee.parser.log_reader import IncrementalLogReader

# Import from new focused modules for backward compatibility
from snakesee.parser.metadata import MetadataRecord
from snakesee.parser.metadata import collect_rule_code_hashes
from snakesee.parser.metadata import parse_metadata_files
from snakesee.parser.metadata import parse_metadata_files_full

# Import regex patterns from single source of truth
from snakesee.parser.patterns import ERROR_IN_RULE_PATTERN
from snakesee.parser.patterns import FINISHED_JOB_PATTERN
from snakesee.parser.patterns import JOBID_PATTERN
from snakesee.parser.patterns import LOG_PATTERN
from snakesee.parser.patterns import PROGRESS_PATTERN
from snakesee.parser.patterns import RULE_START_PATTERN
from snakesee.parser.patterns import THREADS_PATTERN
from snakesee.parser.patterns import TIMESTAMP_PATTERN
from snakesee.parser.patterns import WILDCARDS_PATTERN

if TYPE_CHECKING:
    from snakesee.persistence.backend import PersistenceBackend
from snakesee.parser.stats import collect_rule_timing_stats
from snakesee.parser.stats import collect_wildcard_timing_stats
from snakesee.parser.utils import _parse_non_negative_int
from snakesee.parser.utils import _parse_positive_int
from snakesee.parser.utils import _parse_timestamp
from snakesee.parser.utils import _parse_wildcards
from snakesee.parser.utils import calculate_input_size
from snakesee.parser.utils import estimate_input_size_from_output

logger = logging.getLogger(__name__)

# Re-export for backward compatibility
__all__ = [
    # From metadata module
    "MetadataRecord",
    "parse_metadata_files",
    "parse_metadata_files_full",
    "collect_rule_code_hashes",
    # From stats module
    "collect_rule_timing_stats",
    "collect_wildcard_timing_stats",
    # From utils module
    "_parse_wildcards",
    "_parse_positive_int",
    "_parse_non_negative_int",
    "_parse_timestamp",
    "calculate_input_size",
    "estimate_input_size_from_output",
    # Local functions
    "parse_cores_from_log",
    "parse_job_stats_from_log",
    "parse_job_stats_counts_from_log",
    "parse_progress_from_log",
    "parse_rules_from_log",
    "parse_running_jobs_from_log",
    "parse_failed_jobs_from_log",
    "parse_incomplete_jobs",
    "parse_completed_jobs_from_log",
    "parse_threads_from_log",
    "parse_all_jobs_from_log",
    "is_workflow_running",
    "parse_workflow_state",
]


# --- Job parsing functions (remain in this module) ---


def parse_job_stats_from_log(log_path: Path) -> set[str]:
    """
    Parse the 'Job stats' table from a Snakemake log to get the set of rules.

    The job stats table appears at the start of a Snakemake run and lists all
    rules that will be executed along with their job counts.

    Args:
        log_path: Path to the Snakemake log file.

    Returns:
        Set of rule names from the job stats table. Returns empty set if
        the table cannot be found or parsed.
    """
    rules: set[str] = set()

    try:
        content = log_path.read_text(errors="ignore")
    except OSError as e:
        logger.info("Could not read log file %s: %s", log_path, e)
        return rules

    lines = content.splitlines()
    in_job_stats = False
    past_header = False

    for line in lines:
        # Look for the start of job stats table
        if line.strip() == "Job stats:":
            in_job_stats = True
            continue

        if not in_job_stats:
            continue

        stripped = line.strip()

        # Skip empty lines
        if not stripped:
            # Empty line after data means end of table
            if past_header and rules:
                break
            continue

        # Skip header line (job count)
        if stripped.startswith("job") and "count" in stripped:
            continue

        # Skip separator line (dashes)
        if stripped.startswith("-"):
            past_header = True
            continue

        # Parse data row: "rule_name    count"
        if past_header:
            parts = stripped.split()
            if len(parts) >= 2:
                rule_name = parts[0]
                # Skip 'total' row
                if rule_name != "total":
                    rules.add(rule_name)

    return rules


def parse_job_stats_counts_from_log(log_path: Path) -> dict[str, int]:
    """
    Parse the 'Job stats' table from a Snakemake log to get rule -> job count mapping.

    The job stats table appears at the start of a Snakemake run and lists all
    rules that will be executed along with their job counts. This function
    captures both the rule names AND their expected counts.

    Args:
        log_path: Path to the Snakemake log file.

    Returns:
        Dictionary mapping rule names to their expected job counts.
        Returns empty dict if the table cannot be found or parsed.
    """
    counts: dict[str, int] = {}

    try:
        content = log_path.read_text(errors="ignore")
    except OSError as e:
        logger.info("Could not read log file %s: %s", log_path, e)
        return counts

    lines = content.splitlines()
    in_job_stats = False
    past_header = False

    for line in lines:
        # Look for the start of job stats table
        if line.strip() == "Job stats:":
            in_job_stats = True
            continue

        if not in_job_stats:
            continue

        stripped = line.strip()

        # Skip empty lines
        if not stripped:
            # Empty line after data means end of table
            if past_header and counts:
                break
            continue

        # Skip header line (job count)
        if stripped.startswith("job") and "count" in stripped:
            continue

        # Skip separator line (dashes)
        if stripped.startswith("-"):
            past_header = True
            continue

        # Parse data row: "rule_name    count"
        if past_header:
            parts = stripped.split()
            if len(parts) >= 2:
                rule_name = parts[0]
                # Skip 'total' row
                if rule_name != "total":
                    try:
                        count = int(parts[-1])
                        counts[rule_name] = count
                    except ValueError:
                        # Skip if count isn't a valid integer
                        pass

    return counts


def parse_cores_from_log(log_path: Path) -> int | None:
    """
    Parse the ``Provided cores: N`` line from a Snakemake log.

    This line appears near the start of a run and reflects the ``-j``/``--cores``
    value passed to Snakemake.  Only the first occurrence is used.

    Args:
        log_path: Path to the Snakemake log file.

    Returns:
        The core count, or ``None`` if the line was not found.
    """
    from snakesee.parser.patterns import CORES_PATTERN

    try:
        content = log_path.read_text(errors="ignore")
    except OSError as e:
        logger.info("Could not read log file %s: %s", log_path, e)
        return None

    match = CORES_PATTERN.search(content)
    if match is not None:
        return int(match.group(1))
    return None


def parse_progress_from_log(
    log_path: Path,
    *,
    _cached_lines: list[str] | None = None,
) -> tuple[int, int]:
    """
    Parse current progress from a snakemake log file.

    Reads the log file and finds the most recent progress line.

    Args:
        log_path: Path to the snakemake log file.
        _cached_lines: Pre-read log lines (internal optimization).

    Returns:
        Tuple of (completed_count, total_count). Returns (0, 0) if no progress found.
    """
    completed, total = 0, 0
    try:
        lines = _cached_lines if _cached_lines is not None else log_path.read_text().splitlines()
        for line in lines:
            if match := PROGRESS_PATTERN.search(line):
                completed = int(match.group(1))
                total = int(match.group(2))
    except OSError as e:
        logger.info("Could not read log file %s: %s", log_path, e)
    return completed, total


def parse_rules_from_log(log_path: Path) -> dict[str, int]:
    """
    Parse rule execution counts from a snakemake log file.

    Counts how many times each rule appears to have completed based on log entries.

    Args:
        log_path: Path to the snakemake log file.

    Returns:
        Dictionary mapping rule names to completion counts.
    """
    rule_counts: dict[str, int] = {}
    current_rule: str | None = None
    job_rules: dict[str, str] = {}

    try:
        for line in log_path.read_text().splitlines():
            # Track current rule being executed
            if match := RULE_START_PATTERN.match(line.lstrip()):
                current_rule = match.group(1)
            # Map jobid to current rule
            elif (match := JOBID_PATTERN.match(line)) and current_rule is not None:
                job_rules[match.group(1)] = current_rule
            # Count finished jobs using jobid-to-rule mapping
            elif match := FINISHED_JOB_PATTERN.search(line):
                rule = job_rules.get(match.group(1), current_rule)
                if rule is not None:
                    rule_counts[rule] = rule_counts.get(rule, 0) + 1
    except OSError as e:
        logger.info("Could not read log file %s: %s", log_path, e)

    return rule_counts


def parse_running_jobs_from_log(  # noqa: C901
    log_path: Path,
    *,
    _cached_lines: list[str] | None = None,
) -> list[JobInfo]:
    """
    Parse currently running jobs by analyzing the log file.

    Tracks jobs that have started (rule + jobid) but not yet finished or failed.

    Args:
        log_path: Path to the snakemake log file.
        _cached_lines: Pre-read log lines (internal optimization).

    Returns:
        List of JobInfo for jobs that appear to be running.
    """
    # Track started jobs: jobid -> (rule, start_line_num, wildcards, threads)
    started_jobs: dict[str, tuple[str, int, dict[str, str] | None, int | None]] = {}
    # Job logs: jobid -> log_path (separate lookup by unique jobid)
    job_logs: dict[str, str] = {}
    finished_jobids: set[str] = set()
    failed_jobids: set[str] = set()

    current_rule: str | None = None
    current_jobid: str | None = None
    current_wildcards: dict[str, str] | None = None
    current_threads: int | None = None
    current_log_path: str | None = None

    # Track pending error to capture jobid from error block
    pending_error_rule: str | None = None
    error_jobid: str | None = None

    def record_pending_error() -> None:
        """Record the failed jobid from a pending error block."""
        nonlocal pending_error_rule, error_jobid
        if pending_error_rule is not None and error_jobid is not None:
            failed_jobids.add(error_jobid)
        pending_error_rule = None
        error_jobid = None

    try:
        lines = _cached_lines if _cached_lines is not None else log_path.read_text().splitlines()
        for line_num, line in enumerate(lines):
            # Track current rule being executed
            if match := RULE_START_PATTERN.match(line.lstrip()):
                record_pending_error()
                current_rule = match.group(1)
                current_jobid = None  # Reset jobid for new rule block
                current_wildcards = None
                current_threads = None
                current_log_path = None

            # Timestamp lines end error blocks
            elif TIMESTAMP_PATTERN.match(line.lstrip()):
                record_pending_error()

            # Capture wildcards within rule block
            elif match := WILDCARDS_PATTERN.match(line):
                current_wildcards = _parse_wildcards(match.group(1))

            # Capture threads within rule block
            elif match := THREADS_PATTERN.match(line):
                current_threads = int(match.group(1))
                # Update already-stored job if threads comes after jobid
                if current_jobid and current_jobid in started_jobs:
                    rule, ln, wc, _ = started_jobs[current_jobid]
                    started_jobs[current_jobid] = (rule, ln, wc, current_threads)

            # Capture log path within rule block - store by jobid
            elif match := LOG_PATTERN.match(line):
                current_log_path = match.group(1).strip()
                if current_jobid:
                    job_logs[current_jobid] = current_log_path

            # Capture jobid within rule block or error block
            elif match := JOBID_PATTERN.match(line):
                jobid = match.group(1)
                current_jobid = jobid
                if current_rule is not None and jobid not in started_jobs:
                    started_jobs[jobid] = (
                        current_rule,
                        line_num,
                        current_wildcards,
                        current_threads,
                    )
                # Store log by jobid if we already captured it
                if current_log_path:
                    job_logs[jobid] = current_log_path
                # If we're in an error block, capture this jobid as failed
                if pending_error_rule is not None:
                    error_jobid = jobid

            # Track finished jobs
            elif match := FINISHED_JOB_PATTERN.search(line):
                finished_jobids.add(match.group(1))

            # Track error blocks to capture failed jobids
            elif match := ERROR_IN_RULE_PATTERN.search(line):
                record_pending_error()
                pending_error_rule = match.group(1)
                # Initialize error_jobid from context if rule matches
                if current_rule == pending_error_rule:
                    error_jobid = current_jobid
                else:
                    error_jobid = None

        # Record any final pending error
        record_pending_error()

    except OSError as e:
        logger.info("Could not read log file %s: %s", log_path, e)
        return []

    # Jobs that started but haven't finished or failed are running
    running: list[JobInfo] = []
    # Use try/except to avoid TOCTOU race between exists() and stat()
    log_mtime: float | None = None
    try:
        log_mtime = log_path.stat().st_mtime
    except OSError:
        pass

    excluded_jobids = finished_jobids | failed_jobids
    for jobid, (rule, _line_num, wildcards, threads) in started_jobs.items():
        if jobid not in excluded_jobids:
            # Look up log by jobid - unique within this run
            job_log = job_logs.get(jobid)
            running.append(
                JobInfo(
                    rule=rule,
                    job_id=jobid,
                    start_time=log_mtime,  # Approximate; could improve with timestamps
                    wildcards=wildcards,
                    threads=threads,
                    log_file=Path(job_log) if job_log else None,
                )
            )

    return running


def parse_failed_jobs_from_log(  # noqa: C901
    log_path: Path,
    *,
    _cached_lines: list[str] | None = None,
) -> list[JobInfo]:
    """
    Parse failed jobs from the snakemake log file.

    Looks for "Error in rule X:" patterns and extracts the rule name
    and associated job ID when available. Useful for --keep-going workflows.

    Args:
        log_path: Path to the snakemake log file.
        _cached_lines: Pre-read log lines (internal optimization).

    Returns:
        List of JobInfo for jobs that failed.
    """
    failed_jobs: list[JobInfo] = []
    seen_failures: set[tuple[str, str | None]] = set()  # (rule, jobid) pairs

    # Track context: rule, jobid, wildcards, threads, and log for each job block
    current_rule: str | None = None
    current_jobid: str | None = None
    current_wildcards: dict[str, str] | None = None
    current_threads: int | None = None
    current_log_path: str | None = None
    # Job logs: jobid -> log_path (separate lookup by unique jobid)
    job_logs: dict[str, str] = {}

    # Track pending error - we defer emission until the error block is fully parsed
    # because jobid and log come AFTER "Error in rule X:" in the error block
    pending_error_rule: str | None = None
    error_jobid: str | None = None
    error_wildcards: dict[str, str] | None = None
    error_threads: int | None = None
    error_log_path: str | None = None

    def emit_pending_error() -> None:
        """Emit the pending error if we have one."""
        nonlocal pending_error_rule, error_jobid, error_wildcards, error_threads, error_log_path
        if pending_error_rule is not None:
            # Look up log by jobid if not captured directly in error block
            job_log = error_log_path or (job_logs.get(error_jobid) if error_jobid else None)
            key = (pending_error_rule, error_jobid)
            if key not in seen_failures:
                seen_failures.add(key)
                failed_jobs.append(
                    JobInfo(
                        rule=pending_error_rule,
                        job_id=error_jobid,
                        wildcards=error_wildcards,
                        threads=error_threads,
                        log_file=Path(job_log) if job_log else None,
                    )
                )
            # Reset pending error state
            pending_error_rule = None
            error_jobid = None
            error_wildcards = None
            error_threads = None
            error_log_path = None

    try:
        lines = _cached_lines if _cached_lines is not None else log_path.read_text().splitlines()
        for line in lines:
            # Track current rule - this also ends any pending error block
            if match := RULE_START_PATTERN.match(line.lstrip()):
                emit_pending_error()
                current_rule = match.group(1)
                current_jobid = None
                current_wildcards = None
                current_threads = None
                current_log_path = None

            # Timestamp lines end error blocks
            elif TIMESTAMP_PATTERN.match(line.lstrip()):
                emit_pending_error()

            # Capture wildcards - applies to both rule blocks and error blocks
            elif match := WILDCARDS_PATTERN.match(line):
                wildcards = _parse_wildcards(match.group(1))
                current_wildcards = wildcards
                if pending_error_rule is not None:
                    error_wildcards = wildcards

            # Capture threads - applies to both rule blocks and error blocks
            elif match := THREADS_PATTERN.match(line):
                threads = int(match.group(1))
                current_threads = threads
                if pending_error_rule is not None:
                    error_threads = threads

            # Capture log path - applies to both rule blocks and error blocks
            elif match := LOG_PATTERN.match(line):
                # Strip "(check log file(s) for error details)" suffix from error blocks
                log_value = match.group(1).strip()
                if " (check log file" in log_value:
                    log_value = log_value.split(" (check log file")[0].strip()
                current_log_path = log_value
                if current_jobid:
                    job_logs[current_jobid] = log_value
                if pending_error_rule is not None:
                    error_log_path = log_value

            # Capture jobid - applies to both rule blocks and error blocks
            elif match := JOBID_PATTERN.match(line):
                jobid = match.group(1)
                current_jobid = jobid
                # Store log by jobid if we already captured it
                if current_log_path:
                    job_logs[jobid] = current_log_path
                if pending_error_rule is not None:
                    error_jobid = jobid

            # Detect errors - start a new pending error block
            elif match := ERROR_IN_RULE_PATTERN.search(line):
                # Emit any previous pending error first
                emit_pending_error()
                rule = match.group(1)
                # Start new pending error - we'll capture jobid/wildcards/threads/log
                # from subsequent lines in the error block
                pending_error_rule = rule
                # Initialize with context from the rule block if the rule matches
                if current_rule == rule:
                    error_jobid = current_jobid
                    error_wildcards = current_wildcards
                    error_threads = current_threads
                    error_log_path = current_log_path or (
                        job_logs.get(current_jobid) if current_jobid else None
                    )
                else:
                    error_jobid = None
                    error_wildcards = None
                    error_threads = None
                    error_log_path = None

        # Emit any final pending error at end of file
        emit_pending_error()

    except OSError as e:
        logger.info("Could not read log file %s: %s", log_path, e)

    return failed_jobs


def parse_incomplete_jobs(
    incomplete_dir: Path, min_start_time: float | None = None
) -> Iterator[JobInfo]:
    """
    Parse currently running jobs from incomplete markers.

    Snakemake creates marker files in .snakemake/incomplete/ for jobs that
    are in progress. The marker filename is base64-encoded output file path.
    The file modification time indicates when the job started.

    Note: This is a fallback method. Prefer parse_running_jobs_from_log()
    which provides rule names.

    Args:
        incomplete_dir: Path to .snakemake/incomplete/ directory.
        min_start_time: If provided, only yield markers with mtime >= this time.
            Used to filter out stale markers from previous workflow runs.

    Yields:
        JobInfo instances for each in-progress job.
    """
    import base64

    if not incomplete_dir.exists():
        return

    for marker in incomplete_dir.rglob("*"):
        if marker.is_file() and marker.name != "migration_underway":
            try:
                marker_mtime = marker.stat().st_mtime

                # Skip markers that are older than the current workflow run
                if min_start_time is not None and marker_mtime < min_start_time:
                    continue

                # Decode the base64 filename to get the output file path
                output_file: Path | None = None
                try:
                    decoded = base64.b64decode(marker.name).decode("utf-8")
                    output_file = Path(decoded)
                except (ValueError, UnicodeDecodeError) as e:
                    logger.warning("Failed to decode base64 marker filename %s: %s", marker.name, e)

                # The marker's mtime is approximately when the job started
                yield JobInfo(
                    rule="unknown",  # Cannot determine rule from marker filename
                    start_time=marker_mtime,
                    output_file=output_file,
                )
            except OSError:
                continue


def _get_first_log_timestamp(
    log_path: Path,
    *,
    _cached_lines: list[str] | None = None,
) -> float | None:
    """Extract the first timestamp from a snakemake log file.

    This provides a more accurate workflow start time than the file's ctime,
    since the log file may be created before jobs actually start.

    Args:
        log_path: Path to the snakemake log file.
        _cached_lines: Pre-read log lines (internal optimization).
    """
    try:
        if _cached_lines is not None:
            for line in _cached_lines:
                if match := TIMESTAMP_PATTERN.match(line.lstrip()):
                    return _parse_timestamp(match.group(1))
        else:
            with log_path.open() as f:
                for line in f:
                    if match := TIMESTAMP_PATTERN.match(line.lstrip()):
                        return _parse_timestamp(match.group(1))
    except OSError as e:
        logger.info("Could not read log file %s: %s", log_path, e)
    return None


def parse_completed_jobs_from_log(
    log_path: Path,
    *,
    _cached_lines: list[str] | None = None,
) -> list[JobInfo]:
    """
    Parse completed jobs with timing from a snakemake log file.

    Extracts job start/end times from log timestamps to reconstruct
    job durations. This is useful for historical logs where metadata
    may have been overwritten by later runs.

    Args:
        log_path: Path to the snakemake log file.
        _cached_lines: Pre-read log lines (internal optimization).

    Returns:
        List of JobInfo for completed jobs with timing information.
    """
    completed_jobs: list[JobInfo] = []

    # Track started jobs: jobid -> (rule, start_time, wildcards, threads)
    started_jobs: dict[str, tuple[str, float, dict[str, str] | None, int | None]] = {}
    # Job logs: jobid -> log_path (separate lookup by unique jobid)
    job_logs: dict[str, str] = {}
    current_rule: str | None = None
    current_timestamp: float | None = None
    current_wildcards: dict[str, str] | None = None
    current_threads: int | None = None
    current_jobid: str | None = None
    current_log_path: str | None = None

    try:
        lines = _cached_lines if _cached_lines is not None else log_path.read_text().splitlines()
        for line in lines:
            # Check for timestamp
            if match := TIMESTAMP_PATTERN.match(line.lstrip()):
                current_timestamp = _parse_timestamp(match.group(1))

            # Track current rule being executed
            elif match := RULE_START_PATTERN.match(line.lstrip()):
                current_rule = match.group(1)
                current_wildcards = None
                current_threads = None
                current_jobid = None
                current_log_path = None

            # Capture wildcards within rule block
            elif match := WILDCARDS_PATTERN.match(line):
                current_wildcards = _parse_wildcards(match.group(1))

            # Capture threads within rule block
            elif match := THREADS_PATTERN.match(line):
                current_threads = int(match.group(1))
                # Update already-stored job if threads comes after jobid
                if current_jobid and current_jobid in started_jobs:
                    rule, ts, wc, _ = started_jobs[current_jobid]
                    started_jobs[current_jobid] = (rule, ts, wc, current_threads)

            # Capture log path within rule block - store by jobid
            elif match := LOG_PATTERN.match(line):
                current_log_path = match.group(1).strip()
                if current_jobid:
                    job_logs[current_jobid] = current_log_path

            # Capture jobid within rule block
            elif match := JOBID_PATTERN.match(line):
                current_jobid = match.group(1)
                if current_rule is not None and current_timestamp is not None:
                    started_jobs[current_jobid] = (
                        current_rule,
                        current_timestamp,
                        current_wildcards,
                        current_threads,
                    )
                # Store log by jobid if we already captured it
                if current_log_path:
                    job_logs[current_jobid] = current_log_path

            # Track finished jobs
            elif match := FINISHED_JOB_PATTERN.search(line):
                jobid = match.group(1)
                if jobid in started_jobs and current_timestamp is not None:
                    rule, start_time, wildcards, threads = started_jobs[jobid]
                    # Look up log by jobid - unique within this run
                    job_log = job_logs.get(jobid)
                    completed_jobs.append(
                        JobInfo(
                            rule=rule,
                            job_id=jobid,
                            start_time=start_time,
                            end_time=current_timestamp,
                            wildcards=wildcards,
                            threads=threads,
                            log_file=Path(job_log) if job_log else None,
                        )
                    )

    except OSError as e:
        logger.info("Could not read log file %s: %s", log_path, e)

    return completed_jobs


def parse_threads_from_log(log_path: Path) -> dict[str, int]:
    """
    Parse a jobid -> threads mapping from a snakemake log file.

    This is used to augment metadata completions with thread info,
    since metadata files don't store the threads directive.

    Args:
        log_path: Path to the snakemake log file.

    Returns:
        Dictionary mapping job_id to thread count.
    """
    threads_map: dict[str, int] = {}
    current_jobid: str | None = None
    current_threads: int | None = None

    try:
        for line in log_path.read_text().splitlines():
            # Track current rule (resets context)
            if RULE_START_PATTERN.match(line.lstrip()):
                current_jobid = None
                current_threads = None

            # Capture threads
            elif match := THREADS_PATTERN.match(line):
                current_threads = int(match.group(1))
                # Update already-stored job if threads comes after jobid
                if current_jobid and current_jobid not in threads_map:
                    threads_map[current_jobid] = current_threads

            # Capture jobid
            elif match := JOBID_PATTERN.match(line):
                current_jobid = match.group(1)
                if current_threads is not None and current_jobid not in threads_map:
                    threads_map[current_jobid] = current_threads

    except OSError as e:
        logger.info("Could not read log file %s: %s", log_path, e)

    return threads_map


def parse_all_jobs_from_log(
    log_path: Path,
    *,
    _cached_lines: list[str] | None = None,
) -> list[JobInfo]:
    """
    Parse all scheduled jobs from a snakemake log file.

    Extracts job information (rule, wildcards, threads, jobid) from the log
    by parsing job blocks. This captures ALL jobs that snakemake scheduled,
    including those not yet started.

    This is useful for getting pending jobs with their wildcards when the
    snakesee logger plugin is not available.

    Args:
        log_path: Path to the snakemake log file.
        _cached_lines: Pre-read log lines (internal optimization).

    Returns:
        List of JobInfo for all scheduled jobs.
    """
    all_jobs: list[JobInfo] = []
    seen_jobids: set[str] = set()

    current_rule: str | None = None
    current_jobid: str | None = None
    current_wildcards: dict[str, str] | None = None
    current_threads: int | None = None

    try:
        lines = _cached_lines if _cached_lines is not None else log_path.read_text().splitlines()
        for line in lines:
            # Track current rule being scheduled
            if match := RULE_START_PATTERN.match(line.lstrip()):
                # Save previous job if complete
                if current_rule is not None and current_jobid is not None:
                    if current_jobid not in seen_jobids:
                        seen_jobids.add(current_jobid)
                        all_jobs.append(
                            JobInfo(
                                rule=current_rule,
                                job_id=current_jobid,
                                wildcards=current_wildcards,
                                threads=current_threads,
                            )
                        )

                # Start new job block
                current_rule = match.group(1)
                current_jobid = None
                current_wildcards = None
                current_threads = None

            # Capture wildcards within rule block
            elif match := WILDCARDS_PATTERN.match(line):
                current_wildcards = _parse_wildcards(match.group(1))

            # Capture threads within rule block
            elif match := THREADS_PATTERN.match(line):
                current_threads = int(match.group(1))

            # Capture jobid within rule block
            elif match := JOBID_PATTERN.match(line):
                current_jobid = match.group(1)

        # Don't forget the last job
        if current_rule is not None and current_jobid is not None:
            if current_jobid not in seen_jobids:
                all_jobs.append(
                    JobInfo(
                        rule=current_rule,
                        job_id=current_jobid,
                        wildcards=current_wildcards,
                        threads=current_threads,
                    )
                )

    except OSError as e:
        logger.info("Could not read log file %s: %s", log_path, e)

    return all_jobs


def is_workflow_running(
    snakemake_dir: Path,
    stale_threshold: float = STALE_WORKFLOW_THRESHOLD_SECONDS,
    backend: PersistenceBackend | None = None,
) -> bool:
    """
    Check if a workflow is currently running.

    Uses multiple signals:
    1. Lock files exist in .snakemake/locks/
    2. Incomplete markers exist in .snakemake/incomplete/ (jobs in progress)
    3. Log file was recently modified (within stale_threshold seconds)

    If locks AND incomplete markers both exist, the workflow is definitely running
    (incomplete markers are created when jobs start and removed when they finish).
    Log freshness is only used as a fallback when there are no incomplete markers.

    Args:
        snakemake_dir: Path to the .snakemake directory.
        stale_threshold: Seconds since last log modification before considering
            the workflow stale/dead. Default 1800 seconds (30 minutes).
        backend: Optional persistence backend. If provided, uses it for
            lock and incomplete checks instead of filesystem.

    Returns:
        True if workflow appears to be actively running, False otherwise.
    """
    from snakesee.state.clock import get_clock
    from snakesee.state.paths import WorkflowPaths

    # Use backend for lock/incomplete checks if provided
    if backend is not None:
        has_locks = backend.has_locks()
        if not has_locks:
            return False

        if backend.has_incomplete_jobs():
            return True

        # Fall back to log freshness check
        paths = WorkflowPaths(snakemake_dir.parent)
        log_file = paths.find_latest_log()
        if log_file is None:
            return True

        try:
            log_mtime = log_file.stat().st_mtime
            age = get_clock().now() - log_mtime
            return age < stale_threshold
        except OSError:
            return True

    # Original filesystem-based logic (unchanged below)
    locks_dir = snakemake_dir / "locks"
    if not locks_dir.exists():
        return False

    try:
        has_locks = any(locks_dir.iterdir())
    except OSError:
        return False

    if not has_locks:
        return False

    # If locks exist AND incomplete markers exist, workflow is definitely running
    # (incomplete markers are created when jobs start, removed when they finish)
    incomplete_dir = snakemake_dir / "incomplete"
    if incomplete_dir.exists():
        try:
            has_incomplete = any(incomplete_dir.iterdir())
            if has_incomplete:
                return True
        except OSError:
            pass

    # Fall back to log freshness check when no incomplete markers
    # snakemake_dir is .snakemake, so parent is workflow_dir
    paths = WorkflowPaths(snakemake_dir.parent)
    log_file = paths.find_latest_log()
    if log_file is None:
        # No log file but locks exist - assume running (early startup)
        return True

    try:
        log_mtime = log_file.stat().st_mtime
        age = get_clock().now() - log_mtime
        # If log hasn't been modified recently, workflow is likely dead
        return age < stale_threshold
    except OSError:
        # Can't stat log file - assume running to be safe
        return True


# --- Workflow state helper functions ---


def _filter_completions_by_timeframe(
    completions: list[JobInfo],
    log_path: Path,
    cutoff_time: float | None = None,
) -> list[JobInfo]:
    """
    Filter completions to jobs that completed during a specific workflow run.

    Args:
        completions: List of all completed jobs from metadata.
        log_path: Path to the log file.
        cutoff_time: Optional upper bound (e.g., when next log started).
                     If None, no upper bound is applied.

    Returns:
        Filtered list of completions that occurred during this workflow run.
    """
    try:
        log_start = log_path.stat().st_ctime
        if cutoff_time is not None:
            return [
                j
                for j in completions
                if j.end_time is not None and log_start <= j.end_time < cutoff_time
            ]
        else:
            return [j for j in completions if j.end_time is not None and j.end_time >= log_start]
    except OSError:
        return []  # Can't determine timeframe - return empty to avoid stale data


def _augment_completions_with_threads(
    completions: list[JobInfo],
    log_path: Path,
    *,
    _cached_lines: list[str] | None = None,
) -> list[JobInfo]:
    """Augment completions with threads from log parsing.

    Metadata completions don't have threads - match by rule + end_time.

    Args:
        completions: List of completed jobs to augment.
        log_path: Path to the snakemake log file.
        _cached_lines: Pre-read log lines (internal optimization).
    """
    log_completions = parse_completed_jobs_from_log(log_path, _cached_lines=_cached_lines)
    # Build lookup: (rule, end_time_rounded) -> threads
    threads_lookup: dict[tuple[str, int], int] = {}
    for lc in log_completions:
        if lc.threads is not None and lc.end_time is not None:
            key = (lc.rule, int(lc.end_time))
            threads_lookup[key] = lc.threads

    if not threads_lookup:
        return completions

    augmented: list[JobInfo] = []
    for job in completions:
        threads = job.threads
        if threads is None and job.end_time is not None:
            key = (job.rule, int(job.end_time))
            threads = threads_lookup.get(key)
        if threads is not None and job.threads is None:
            job = JobInfo(
                rule=job.rule,
                job_id=job.job_id,
                start_time=job.start_time,
                end_time=job.end_time,
                output_file=job.output_file,
                wildcards=job.wildcards,
                input_size=job.input_size,
                threads=threads,
            )
        augmented.append(job)
    return augmented


def _determine_final_workflow_status(
    *,
    running: list[JobInfo],
    failed_list: list[JobInfo],
    incomplete_list: list[JobInfo],
    completed: int,
    total: int,
    is_latest_log: bool,
    workflow_is_running: bool,
) -> WorkflowStatus:
    """Determine final workflow status from job state.

    Args:
        running: List of currently running jobs.
        failed_list: List of failed jobs.
        incomplete_list: List of jobs with incomplete markers.
        completed: Number of completed jobs.
        total: Total number of jobs.
        is_latest_log: Whether we're looking at the latest log.
        workflow_is_running: Whether workflow lock is held.

    Returns:
        The determined WorkflowStatus.
    """
    if running and is_latest_log and workflow_is_running:
        return WorkflowStatus.RUNNING
    if len(failed_list) > 0:
        return WorkflowStatus.FAILED
    if incomplete_list:
        return WorkflowStatus.INCOMPLETE
    if completed < total and not workflow_is_running:
        return WorkflowStatus.INCOMPLETE
    return WorkflowStatus.RUNNING if workflow_is_running else WorkflowStatus.COMPLETED


def _collect_filtered_completions(
    all_completions: list[JobInfo],
    log_path: Path | None,
    cutoff_time: float | None,
    log_reader: IncrementalLogReader | None,
) -> list[JobInfo]:
    """Collect and filter completions for the relevant timeframe.

    Args:
        all_completions: All parsed completions from metadata.
        log_path: Path to the log file.
        cutoff_time: Optional upper bound for filtering.
        log_reader: Optional incremental log reader.

    Returns:
        Filtered list of completed jobs.
    """
    if log_path is None:
        return []

    filtered = _filter_completions_by_timeframe(all_completions, log_path, cutoff_time)
    if filtered:
        return filtered

    if log_reader is not None:
        return log_reader.completed_jobs

    # No matching metadata - parse completions from the log file
    return parse_completed_jobs_from_log(log_path)


def _reconcile_job_lists(
    running: list[JobInfo],
    failed_list: list[JobInfo],
    incomplete_list: list[JobInfo],
    workflow_is_running: bool,
) -> tuple[list[JobInfo], list[JobInfo]]:
    """Reconcile running, failed, and incomplete job lists.

    Removes failed jobs from running list and handles the running/incomplete
    overlap based on whether the workflow is actually running.

    Args:
        running: List of jobs that appear to be running.
        failed_list: List of failed jobs.
        incomplete_list: List of incomplete markers.
        workflow_is_running: Whether workflow lock is held.

    Returns:
        Tuple of (reconciled_running, reconciled_incomplete).
    """
    # Remove failed jobs from running list
    failed_job_ids = {job.job_id for job in failed_list if job.job_id is not None}
    if failed_job_ids:
        running = [job for job in running if job.job_id not in failed_job_ids]

    if not workflow_is_running:
        # Not running = orphaned jobs, not truly running
        return [], incomplete_list

    # Workflow IS running = incomplete markers are running jobs, not interrupted
    return running, []


def parse_workflow_state(
    workflow_dir: Path,
    log_file: Path | None = None,
    cutoff_time: float | None = None,
    log_reader: IncrementalLogReader | None = None,
) -> WorkflowProgress:
    """
    Parse complete workflow state from .snakemake directory.

    Combines information from log files, metadata, incomplete markers,
    and lock files to build a complete picture of workflow state.

    Args:
        workflow_dir: Root directory containing .snakemake/.
        log_file: Optional specific log file to parse. If None, uses the latest.
        cutoff_time: Optional upper bound for filtering completions (e.g., when
                     the next log started). Used for "time machine" view of
                     historical logs.
        log_reader: Optional incremental log reader for efficient polling.
                    When provided, uses cached state instead of re-parsing
                    the entire log file on each call.

    Returns:
        Current workflow state as a WorkflowProgress instance.
    """
    from snakesee.state.paths import WorkflowPaths

    paths = WorkflowPaths(workflow_dir)

    # Auto-detect persistence backend (SQLite DB or filesystem)
    from snakesee.persistence import detect_backend

    backend = detect_backend(workflow_dir)

    snakemake_dir = paths.snakemake_dir

    # Use specified log file or find latest
    latest_log = paths.find_latest_log()
    log_path = log_file if log_file is not None else latest_log
    is_latest_log = log_file is None or log_file == latest_log

    # Determine status from lock files (only relevant for latest log)
    workflow_is_running = is_latest_log and is_workflow_running(snakemake_dir, backend=backend)
    if workflow_is_running:
        status = WorkflowStatus.RUNNING
    else:
        status = WorkflowStatus.COMPLETED

    # Update incremental reader if provided and log path matches/changes
    if log_reader is not None and log_path is not None:
        log_reader.set_log_path(log_path)
        log_reader.read_new_lines()

    # Cache log file lines to avoid repeated reads when not using log_reader
    # This is a significant performance optimization for large log files
    cached_lines: list[str] | None = None
    if log_reader is None and log_path is not None:
        try:
            cached_lines = log_path.read_text().splitlines()
        except OSError:
            cached_lines = []

    # Parse progress from log file (or use reader state)
    if log_reader is not None and log_path is not None:
        completed, total = log_reader.progress
    else:
        completed, total = (
            (0, 0)
            if log_path is None
            else parse_progress_from_log(log_path, _cached_lines=cached_lines)
        )

    # Get workflow start time for filtering incomplete markers
    # Prefer the first timestamp in the log (when jobs actually started) over file ctime
    start_time: float | None = None
    if log_path is not None:
        start_time = _get_first_log_timestamp(log_path, _cached_lines=cached_lines)
        if start_time is None:
            # Fall back to file ctime if no timestamps in log yet
            try:
                start_time = log_path.stat().st_ctime
            except OSError:
                pass

    # Parse completed jobs for recent completions
    metadata_dir = snakemake_dir / "metadata"
    all_completions = list(parse_metadata_files(metadata_dir))

    # Filter completions to the relevant timeframe
    completions = _collect_filtered_completions(all_completions, log_path, cutoff_time, log_reader)

    # Augment completions with threads from log (metadata doesn't have threads)
    if log_path is not None and completions:
        completions = _augment_completions_with_threads(
            completions, log_path, _cached_lines=cached_lines
        )

    completions.sort(key=lambda j: j.end_time or 0, reverse=True)

    # Parse running jobs from log file (provides rule names)
    running: list[JobInfo] = []
    failed_list: list[JobInfo] = []
    if log_path is not None:
        if log_reader is not None:
            running = log_reader.running_jobs
            failed_list = log_reader.failed_jobs
        else:
            running = parse_running_jobs_from_log(log_path, _cached_lines=cached_lines)
            failed_list = parse_failed_jobs_from_log(log_path, _cached_lines=cached_lines)

    # Check for incomplete jobs (from DB or filesystem markers)
    if is_latest_log:
        incomplete_list = [
            JobInfo(
                rule=ij.rule or "unknown",
                start_time=ij.start_time,
                output_file=ij.output_file,
                external_jobid=ij.external_jobid,
            )
            for ij in backend.iterate_incomplete_jobs(min_start_time=start_time)
        ]
    else:
        incomplete_list = []

    # Reconcile running, failed, and incomplete job lists
    running, incomplete_list = _reconcile_job_lists(
        running, failed_list, incomplete_list, workflow_is_running
    )

    # Determine final status based on running jobs, failures, and incomplete markers
    status = _determine_final_workflow_status(
        running=running,
        failed_list=failed_list,
        incomplete_list=incomplete_list,
        completed=completed,
        total=total,
        is_latest_log=is_latest_log,
        workflow_is_running=workflow_is_running,
    )

    return WorkflowProgress(
        workflow_dir=workflow_dir,
        status=status,
        total_jobs=total,
        completed_jobs=completed,
        failed_jobs=len(failed_list),
        failed_jobs_list=failed_list,
        incomplete_jobs_list=incomplete_list,
        running_jobs=running,
        recent_completions=completions,
        start_time=start_time,
        log_file=log_path,
    )
