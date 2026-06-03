"""Unit tests for the row-builder and sort helpers in `snakesee.tui.tables`."""

from __future__ import annotations

from snakesee.models import JobInfo
from snakesee.models import RuleTimingStats
from snakesee.models import ThreadTimingStats
from snakesee.tui.tables import CompletionRow
from snakesee.tui.tables import RunningRow
from snakesee.tui.tables import StatsRow
from snakesee.tui.tables import completion_rows
from snakesee.tui.tables import pending_rows
from snakesee.tui.tables import running_rows
from snakesee.tui.tables import sort_rows
from snakesee.tui.tables import sort_stats_rows
from snakesee.tui.tables import stats_rows


class TestSortRows:
    def test_sorts_ascending_by_column(self) -> None:
        rows = [(3, "c"), (1, "a"), (2, "b")]
        sorted_rows = sort_rows(rows, column=0, ascending=True)
        assert [r[0] for r in sorted_rows] == [1, 2, 3]

    def test_sorts_descending_by_column(self) -> None:
        rows = [(1, "a"), (3, "c"), (2, "b")]
        sorted_rows = sort_rows(rows, column=0, ascending=False)
        assert [r[0] for r in sorted_rows] == [3, 2, 1]

    def test_string_sort_is_case_insensitive(self) -> None:
        rows = [(0, "Beta"), (1, "alpha"), (2, "Gamma")]
        sorted_rows = sort_rows(rows, column=1, ascending=True)
        assert [r[1] for r in sorted_rows] == ["alpha", "Beta", "Gamma"]

    def test_none_values_sort_last_ascending(self) -> None:
        rows = [(1,), (None,), (2,)]
        sorted_rows = sort_rows(rows, column=0, ascending=True)
        assert [r[0] for r in sorted_rows] == [1, 2, None]

    def test_none_values_sort_last_descending(self) -> None:
        """None still sorts last even when direction is descending (regression guard)."""
        rows = [(1,), (None,), (2,)]
        sorted_rows = sort_rows(rows, column=0, ascending=False)
        assert [r[0] for r in sorted_rows] == [2, 1, None]


class TestRunningRows:
    def test_converts_tuples_to_named_rows(self) -> None:
        from snakesee.plugins.base import ToolProgress

        job = JobInfo(rule="map", job_id="1")
        elapsed: float | None = 10.0
        remaining: float | None = 20.0
        start: float | None = 100.0
        tool: ToolProgress | None = None
        data = [(job, elapsed, remaining, start, tool)]
        rows = running_rows(data)
        assert len(rows) == 1
        assert isinstance(rows[0], RunningRow)
        assert rows[0].job is job
        assert rows[0].elapsed_seconds == 10.0
        assert rows[0].remaining_seconds == 20.0
        assert rows[0].start_time == 100.0
        assert rows[0].tool_progress is None

    def test_empty_input_returns_empty_list(self) -> None:
        assert running_rows([]) == []


class TestCompletionRows:
    def test_marks_failed_ids_correctly(self) -> None:
        ok_job = JobInfo(rule="ok", job_id="1")
        bad_job = JobInfo(rule="oops", job_id="2")
        # Failed-id set is keyed on Python id() of the job object.
        rows = completion_rows([ok_job, bad_job], failed_job_ids={id(bad_job)})
        assert len(rows) == 2
        assert isinstance(rows[0], CompletionRow)
        assert rows[0].is_failed is False
        assert rows[1].is_failed is True


class TestPendingRows:
    def test_returns_one_row_per_rule(self) -> None:
        rows = pending_rows({"align": 5, "qc": 3})
        assert len(rows) == 2
        rule_to_count = {r.rule: r.job_count for r in rows}
        assert rule_to_count == {"align": 5, "qc": 3}

    def test_empty_input_returns_empty_list(self) -> None:
        assert pending_rows({}) == []


class TestStatsRows:
    def test_aggregate_only_when_no_thread_breakdown(self) -> None:
        agg = RuleTimingStats(rule="map", durations=[10.0, 12.0, 15.0])
        rows = stats_rows([agg], thread_stats_dict={})
        assert len(rows) == 1
        assert isinstance(rows[0], StatsRow)
        assert rows[0].rule_display == "map"
        assert rows[0].threads == "-"
        assert rows[0].stats is agg

    def test_thread_breakdown_expands_to_subrows(self) -> None:
        agg = RuleTimingStats(rule="map", durations=[10.0] * 10)
        per_thread = ThreadTimingStats(
            rule="map",
            stats_by_threads={
                1: RuleTimingStats(rule="map", durations=[20.0, 21.0, 19.0, 20.0]),
                4: RuleTimingStats(rule="map", durations=[8.0, 9.0, 7.0, 8.0, 8.5, 8.5]),
            },
        )
        rows = stats_rows([agg], thread_stats_dict={"map": per_thread})
        assert len(rows) == 2
        # First subrow shows the rule name; subsequent subrows show empty
        # string in rule_display so the grouping reads visually.
        assert rows[0].rule_display == "map"
        assert rows[1].rule_display == ""
        # Thread-count strings reflect the per-thread keys.
        thread_counts = sorted(r.threads for r in rows)
        assert thread_counts == ["1", "4"]


class TestSortStatsRows:
    """Stats-table sorting maps visible columns to the right StatsRow fields.

    The stats table's visible columns (Rule / Thr / Count / Avg) do not line up
    with the StatsRow tuple shape (rule_display, threads, stats), so a positional
    sort would compare the wrong field (Count → the whole stats object) or raise
    IndexError (Avg → no 4th tuple element). See the #65 review.
    """

    def _rows(self) -> list[StatsRow]:
        # counts: align=3, sort=1, dedup=5, merge=4 ; means: align=10, sort=20, dedup=2, merge=5
        return [
            StatsRow("align", "2", RuleTimingStats(rule="align", durations=[10.0, 10.0, 10.0])),
            StatsRow("sort", "4", RuleTimingStats(rule="sort", durations=[20.0])),
            StatsRow(
                "dedup", "8", RuleTimingStats(rule="dedup", durations=[2.0, 2.0, 2.0, 2.0, 2.0])
            ),
            StatsRow("merge", "10", RuleTimingStats(rule="merge", durations=[5.0] * 4)),
        ]

    def test_sort_by_rule_name(self) -> None:
        rows = sort_stats_rows(self._rows(), column=0, ascending=True)
        assert [r.rule_display for r in rows] == ["align", "dedup", "merge", "sort"]

    def test_sort_by_count_uses_count_not_stats_object(self) -> None:
        """Column 2 (Count) must sort by stats.count, ascending 1 → 3 → 4 → 5."""
        rows = sort_stats_rows(self._rows(), column=2, ascending=True)
        assert [r.stats.count for r in rows] == [1, 3, 4, 5]

    def test_sort_by_count_descending(self) -> None:
        rows = sort_stats_rows(self._rows(), column=2, ascending=False)
        assert [r.stats.count for r in rows] == [5, 4, 3, 1]

    def test_sort_by_avg_does_not_raise_indexerror(self) -> None:
        """Column 3 (Avg) previously raised IndexError; now sorts by mean_duration."""
        rows = sort_stats_rows(self._rows(), column=3, ascending=True)
        assert [r.stats.mean_duration for r in rows] == [2.0, 5.0, 10.0, 20.0]

    def test_sort_by_threads(self) -> None:
        """Column 1 (Thr) sorts numerically: "10" comes after "8", not before "2"."""
        rows = sort_stats_rows(self._rows(), column=1, ascending=True)
        assert [r.threads for r in rows] == ["2", "4", "8", "10"]

    def test_sort_by_threads_aggregate_sentinel_first(self) -> None:
        """The "-" aggregate row sorts before numeric thread counts when ascending."""
        rows = self._rows()
        rows.append(StatsRow("total", "-", RuleTimingStats(rule="total", durations=[1.0])))
        sorted_rows = sort_stats_rows(rows, column=1, ascending=True)
        assert [r.threads for r in sorted_rows] == ["-", "2", "4", "8", "10"]

    def test_unknown_column_returns_rows_unchanged(self) -> None:
        original = self._rows()
        rows = sort_stats_rows(list(original), column=99, ascending=True)
        assert [r.rule_display for r in rows] == [r.rule_display for r in original]
