"""DataTable row builders and sort helpers."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any
from typing import NamedTuple
from typing import TypeVar

from snakesee.models import JobInfo
from snakesee.models import RuleTimingStats
from snakesee.models import ThreadTimingStats
from snakesee.models import WorkflowProgress
from snakesee.plugins.base import ToolProgress


class RunningRow(NamedTuple):
    """One row of the running-jobs table.

    Fields mirror what ``_build_running_job_data`` returns for each job so that
    callers can convert the existing data-source tuples with zero additional
    computation.
    """

    job: JobInfo
    elapsed_seconds: float | None
    remaining_seconds: float | None
    start_time: float | None
    tool_progress: ToolProgress | None


class CompletionRow(NamedTuple):
    """One row of the recent-completions table."""

    job: JobInfo
    is_failed: bool


class PendingRow(NamedTuple):
    """One row of the pending-jobs table."""

    rule: str
    job_count: int


class FailedRow(NamedTuple):
    """One row of the failed-jobs panel."""

    job: JobInfo


class IncompleteRow(NamedTuple):
    """One row of the incomplete-jobs panel."""

    job: JobInfo
    display_path: str


class StatsRow(NamedTuple):
    """One row of the rule-statistics panel.

    ``rule_display`` is the rule name for the first thread-count sub-row and
    an empty string for subsequent sub-rows (visual grouping).  ``threads``
    is the thread-count string shown in the Thr column (``"-"`` when unknown).
    ``stats`` holds the per-thread (or aggregate) timing data.
    """

    rule_display: str
    threads: str
    stats: RuleTimingStats


_R = TypeVar("_R", bound=tuple)


def sort_rows(rows: list[_R], column: int, ascending: bool) -> list[_R]:
    """Sort *rows* by the value at *column* index, in-place, and return them.

    Works on any :class:`NamedTuple` because named tuples support integer
    indexing.  ``None`` values sort last regardless of direction.

    Args:
        rows: List of row tuples to sort.
        column: Zero-based column index to use as the sort key.
        ascending: When ``True`` sort smallest-first; otherwise largest-first.

    Returns:
        The same list, sorted in-place and returned for convenience.
    """

    # The leading bucket flag pushes None values to the end regardless of sort
    # direction. With ascending=True we put non-None in bucket 0 and None in
    # bucket 1; with descending we invert (since the final reverse=True flips
    # bucket order too) so None still ends up after non-None.
    none_bucket = 1 if ascending else 0
    value_bucket = 1 - none_bucket

    def _key(row: _R) -> tuple[int, object]:
        val = row[column]
        if val is None:
            return (none_bucket, "")
        if isinstance(val, str):
            return (value_bucket, val.lower())
        return (value_bucket, val)

    rows.sort(key=_key, reverse=not ascending)
    return rows


# Maps a visible stats-table column index to a key extracting the comparable value
# from a StatsRow. The stats table renders columns Rule / Thr / Count / Avg / Std Dev,
# but a StatsRow is ``(rule_display, threads, stats)`` — so a positional sort would
# compare the wrong field for Count (the whole ``RuleTimingStats`` object) and raise
# IndexError for Avg (no 4th tuple element). Std Dev is intentionally absent: it is not
# a sortable column (the 1-4 sort bindings cover columns 0-3 only).
_STATS_SORT_KEYS: dict[int, Callable[[StatsRow], Any]] = {
    0: lambda row: row.rule_display.lower(),
    # threads is a display string, so parse it for a numeric sort ("10" after "8",
    # not before "2"); the "-" aggregate row maps to -1 so it sorts first ascending.
    1: lambda row: -1 if row.threads == "-" else int(row.threads),
    2: lambda row: row.stats.count,
    3: lambda row: row.stats.mean_duration,
}


def sort_stats_rows(rows: list[StatsRow], column: int, ascending: bool) -> list[StatsRow]:
    """Sort stats rows by a *visible* column index, mapped to the right StatsRow field.

    Unlike :func:`sort_rows`, this does not index the tuple positionally — the stats
    table's visible columns don't line up with the StatsRow shape (see
    ``_STATS_SORT_KEYS``). An unrecognized column leaves the rows untouched.

    Args:
        rows: List of :class:`StatsRow` to sort.
        column: Zero-based *visible* column index (0=Rule, 1=Thr, 2=Count, 3=Avg).
        ascending: When ``True`` sort smallest-first; otherwise largest-first.

    Returns:
        A new sorted list, or the input list unchanged for an unknown column.
    """
    key = _STATS_SORT_KEYS.get(column)
    if key is None:
        return rows
    return sorted(rows, key=key, reverse=not ascending)


def running_rows(
    job_data: list[tuple[JobInfo, float | None, float | None, float | None, ToolProgress | None]],
) -> list[RunningRow]:
    """Convert raw running-job data tuples to typed :class:`RunningRow` objects.

    The raw tuples are produced by
    :meth:`~snakesee.tui.data_source.WorkflowDataSource._build_running_job_data`,
    which already computes elapsed, remaining, start_time, and tool_progress.
    This function is a thin adapter so that consumers hold typed data rather
    than anonymous tuples.

    Args:
        job_data: List of ``(job, elapsed, remaining, start_time, tool_progress)``
            tuples as returned by ``WorkflowDataSource._build_running_job_data``.

    Returns:
        Equivalent list of :class:`RunningRow` named tuples.
    """
    return [
        RunningRow(job, elapsed, remaining, start, tp)
        for job, elapsed, remaining, start, tp in job_data
    ]


def completion_rows(
    jobs: list[JobInfo],
    failed_job_ids: set[int],
) -> list[CompletionRow]:
    """Build completion rows from a sorted list of completed/failed jobs.

    Args:
        jobs: Ordered list of completed (and failed) jobs as returned by
            :meth:`~snakesee.tui.data_source.WorkflowDataSource.get_completions_sorted`.
        failed_job_ids: Set of ``id(job)`` values for jobs that failed.

    Returns:
        List of :class:`CompletionRow` with one entry per job.
    """
    return [CompletionRow(job=job, is_failed=id(job) in failed_job_ids) for job in jobs]


def pending_rows(pending_rules: dict[str, int]) -> list[PendingRow]:
    """Build pending rows from the inferred pending-rule counts.

    The default ordering (count descending) is applied here; callers may
    re-sort via :func:`sort_rows` if custom sorting is active.

    Args:
        pending_rules: Mapping from rule name to estimated pending count, as
            returned by
            :meth:`~snakesee.tui.data_source.WorkflowDataSource.get_inferred_pending_rules`.

    Returns:
        List of :class:`PendingRow` sorted by count descending.
    """
    rows = [PendingRow(rule=rule, job_count=count) for rule, count in pending_rules.items()]
    rows.sort(key=lambda r: r.job_count, reverse=True)
    return rows


def failed_rows(progress: WorkflowProgress) -> list[FailedRow]:
    """Build failed-job rows from workflow progress.

    Args:
        progress: Current :class:`~snakesee.models.WorkflowProgress`.

    Returns:
        List of :class:`FailedRow` in the order they appear in
        ``progress.failed_jobs_list``.
    """
    return [FailedRow(job=job) for job in progress.failed_jobs_list]


def incomplete_rows(progress: WorkflowProgress) -> list[IncompleteRow]:
    """Build incomplete-job rows from workflow progress.

    Each row's ``display_path`` is the output file path relative to the
    workflow directory when possible, the absolute path otherwise, or the
    string ``"unknown"`` when no output file is recorded.

    Args:
        progress: Current :class:`~snakesee.models.WorkflowProgress`.

    Returns:
        List of :class:`IncompleteRow` in the order they appear in
        ``progress.incomplete_jobs_list``.
    """
    rows: list[IncompleteRow] = []
    for job in progress.incomplete_jobs_list:
        if job.output_file is not None:
            try:
                display_path = str(job.output_file.relative_to(progress.workflow_dir))
            except ValueError:
                display_path = str(job.output_file)
        else:
            display_path = "unknown"
        rows.append(IncompleteRow(job=job, display_path=display_path))
    return rows


def stats_rows(
    stats_list: list[RuleTimingStats],
    thread_stats_dict: dict[str, ThreadTimingStats],
) -> list[StatsRow]:
    """Build flattened stats rows for the rule-statistics panel.

    Rules with per-thread timing data are expanded into one sub-row per
    thread count, with the rule name shown only on the first sub-row.  Rules
    without thread data get a single row with ``"-"`` in the Thr column.

    The default ordering (count descending) should be applied by the caller
    *before* calling this function so that the thread expansion preserves the
    sorted order.

    Args:
        stats_list: Ordered list of :class:`~snakesee.models.RuleTimingStats`
            objects (already filtered and sorted by the caller).
        thread_stats_dict: Mapping from rule name to
            :class:`~snakesee.models.ThreadTimingStats` as returned by
            :meth:`~snakesee.state.rule_registry.RuleRegistry.to_thread_stats_dict`.

    Returns:
        Flat list of :class:`StatsRow` ready for rendering.
    """
    rows: list[StatsRow] = []
    for stats in stats_list:
        rule = stats.rule
        if rule in thread_stats_dict and thread_stats_dict[rule].stats_by_threads:
            rule_thread_stats = thread_stats_dict[rule]
            sorted_threads = sorted(rule_thread_stats.stats_by_threads.keys())
            for i, threads in enumerate(sorted_threads):
                ts = rule_thread_stats.stats_by_threads[threads]
                rule_display = rule if i == 0 else ""
                rows.append(StatsRow(rule_display=rule_display, threads=str(threads), stats=ts))
        else:
            rows.append(StatsRow(rule_display=rule, threads="-", stats=stats))
    return rows
