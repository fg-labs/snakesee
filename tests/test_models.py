"""Tests for monitor data models."""

import math
import time
from pathlib import Path

from snakesee.models import JobInfo
from snakesee.models import RuleTimingStats
from snakesee.models import TimeEstimate
from snakesee.models import WorkflowProgress
from snakesee.models import WorkflowStatus
from snakesee.models import format_duration


class TestFormatDuration:
    """Tests for the format_duration function."""

    def test_seconds(self) -> None:
        """Test formatting seconds."""
        assert format_duration(5) == "5s"
        assert format_duration(59) == "59s"

    def test_minutes(self) -> None:
        """Test formatting minutes."""
        assert format_duration(60) == "1m"
        assert format_duration(90) == "1m 30s"
        assert format_duration(3599) == "59m 59s"

    def test_hours(self) -> None:
        """Test formatting hours."""
        assert format_duration(3600) == "1h"
        assert format_duration(3660) == "1h 1m"
        assert format_duration(7200) == "2h"

    def test_edge_cases(self) -> None:
        """Test edge cases."""
        assert format_duration(0) == "0s"
        assert format_duration(-1) == "0s"
        assert format_duration(float("inf")) == "unknown"


class TestJobInfo:
    """Tests for the JobInfo dataclass."""

    def test_elapsed_no_start(self) -> None:
        """Test elapsed time with no start time."""
        job = JobInfo(rule="test")
        assert job.elapsed is None

    def test_elapsed_running(self) -> None:
        """Test elapsed time for running job."""
        start = time.time() - 10
        job = JobInfo(rule="test", start_time=start)
        elapsed = job.elapsed
        assert elapsed is not None
        assert 9 < elapsed < 12  # Allow some tolerance

    def test_duration_complete(self) -> None:
        """Test duration for completed job."""
        job = JobInfo(rule="test", start_time=100.0, end_time=200.0)
        assert job.duration == 100.0

    def test_duration_incomplete(self) -> None:
        """Test duration for incomplete job."""
        job = JobInfo(rule="test", start_time=100.0)
        assert job.duration is None

    def test_duration_no_start(self) -> None:
        """Test duration when no start time."""
        job = JobInfo(rule="test", end_time=200.0)
        assert job.duration is None

    def test_threads_field(self) -> None:
        """Test threads field on JobInfo."""
        job = JobInfo(rule="test", threads=4)
        assert job.threads == 4

        job_no_threads = JobInfo(rule="test")
        assert job_no_threads.threads is None

    def test_duration_negative_returns_zero(self) -> None:
        """Test that negative durations (clock skew) return 0.0 instead of negative."""
        # This can happen with clock skew on distributed systems
        job = JobInfo(rule="test", start_time=200.0, end_time=100.0)  # end before start
        assert job.duration == 0.0

    def test_elapsed_negative_returns_zero(self) -> None:
        """Test that negative elapsed time (clock skew) returns 0.0."""
        # Set start_time in the future
        future_start = time.time() + 1000
        job = JobInfo(rule="test", start_time=future_start)
        assert job.elapsed == 0.0

    def test_finite_cost_estimate_preserved(self) -> None:
        """A finite cost_estimate (including 0.0) is stored unchanged."""
        assert JobInfo(rule="test", cost_estimate=0.0).cost_estimate == 0.0
        assert JobInfo(rule="test", cost_estimate=1.25).cost_estimate == 1.25
        assert JobInfo(rule="test").cost_estimate is None

    def test_non_finite_cost_estimate_dropped(self) -> None:
        """nan/inf/-inf cost values are dropped to None so they cannot poison aggregates."""
        for bad in (math.nan, math.inf, -math.inf):
            assert JobInfo(rule="test", cost_estimate=bad).cost_estimate is None


class TestRuleTimingStats:
    """Tests for the RuleTimingStats dataclass."""

    def test_empty_stats(self) -> None:
        """Test stats with no data."""
        stats = RuleTimingStats(rule="test")
        assert stats.count == 0
        assert stats.mean_duration == 0.0
        assert stats.std_dev == 0.0

    def test_single_observation(self) -> None:
        """Test stats with single observation."""
        stats = RuleTimingStats(rule="test", durations=[10.0])
        assert stats.count == 1
        assert stats.mean_duration == 10.0
        assert stats.std_dev == 0.0  # Need at least 2 for std_dev

    def test_multiple_observations(self) -> None:
        """Test stats with multiple observations."""
        stats = RuleTimingStats(rule="test", durations=[10.0, 20.0, 30.0])
        assert stats.count == 3
        assert stats.mean_duration == 20.0
        assert stats.std_dev > 0

    def test_weighted_mean_index_based(self) -> None:
        """Test weighted mean with index-based weighting (default strategy)."""
        stats = RuleTimingStats(rule="test", durations=[10.0, 20.0, 30.0])
        # Default half_life_logs=10
        # runs_ago: 2, 1, 0 for positions 0, 1, 2
        # weights: 0.5^(2/10), 0.5^(1/10), 0.5^0 ≈ 0.87, 0.93, 1.0
        # Most recent value (30) should have highest weight
        weighted = stats.weighted_mean(strategy="index")
        assert weighted > 20.0  # More than simple mean (20)
        assert weighted < 30.0  # Less than max

    def test_weighted_mean_index_based_custom_half_life(self) -> None:
        """Test index-based weighted mean with custom half_life_logs."""
        stats = RuleTimingStats(rule="test", durations=[10.0, 30.0])
        # With half_life_logs=1, oldest entry (1 run ago) has 50% weight
        weighted = stats.weighted_mean(strategy="index", half_life_logs=1)
        # Expected: (10 * 0.5 + 30 * 1.0) / 1.5 ≈ 23.33
        assert 23 < weighted < 24

    def test_weighted_mean_time_based(self) -> None:
        """Test weighted mean with time-based weighting using timestamps."""
        now = time.time()
        day = 86400
        # Recent run (0 days ago) should have highest weight
        # Old run (14 days ago) should have lowest weight
        stats = RuleTimingStats(
            rule="test",
            durations=[10.0, 30.0],  # old: 10s, recent: 30s
            timestamps=[now - 14 * day, now],  # 14 days ago, now
        )
        # With 7-day half-life, 14-day old data has weight ~0.25, today's has weight ~1.0
        # Should be closer to 30 than to 20 (the simple mean)
        weighted = stats.weighted_mean(strategy="time", half_life_days=7.0)
        assert weighted > 20.0  # More than simple mean
        assert weighted > 25.0  # Significantly weighted toward recent

    def test_weighted_mean_time_based_custom_half_life(self) -> None:
        """Test time-based weighted mean respects custom half-life parameter."""
        now = time.time()
        day = 86400
        stats = RuleTimingStats(
            rule="test",
            durations=[10.0, 30.0],  # old: 10s, recent: 30s
            timestamps=[now - 7 * day, now],  # 7 days ago, now
        )
        # With 7-day half-life, 7-day old data has weight 0.5, today's has weight 1.0
        # Weighted mean = (10 * 0.5 + 30 * 1.0) / 1.5 = 35/1.5 ≈ 23.33
        weighted_7d = stats.weighted_mean(strategy="time", half_life_days=7.0)
        assert 23 < weighted_7d < 24

        # With 1-day half-life, old data is heavily discounted
        weighted_1d = stats.weighted_mean(strategy="time", half_life_days=1.0)
        assert weighted_1d > weighted_7d  # Even more weighted toward recent

        # With 30-day half-life, old data retains more weight
        weighted_30d = stats.weighted_mean(strategy="time", half_life_days=30.0)
        assert weighted_30d < weighted_7d  # Closer to simple mean

    def test_weighted_mean_time_fallback_to_index(self) -> None:
        """Test time strategy falls back to index-based when no timestamps."""
        stats = RuleTimingStats(
            rule="test",
            durations=[10.0, 20.0, 30.0],
            timestamps=[],  # No timestamps
        )
        # With time strategy but no timestamps, should fall back to index-based
        weighted = stats.weighted_mean(strategy="time")
        # Should weight recent values higher
        assert weighted > 20.0  # More than simple mean

    def test_high_variance(self) -> None:
        """Test high variance detection."""
        low_var = RuleTimingStats(rule="test", durations=[10.0, 10.5, 10.2])
        high_var = RuleTimingStats(rule="test", durations=[10.0, 50.0, 100.0])

        assert not low_var.is_high_variance
        assert high_var.is_high_variance

    def test_coefficient_of_variation_zero_mean(self) -> None:
        """Test coefficient_of_variation returns 0 when mean is zero."""
        stats = RuleTimingStats(rule="test", durations=[])
        assert stats.coefficient_of_variation == 0.0

    def test_weighted_mean_empty(self) -> None:
        """Test weighted_mean returns 0.0 for empty durations."""
        stats = RuleTimingStats(rule="test", durations=[])
        assert stats.weighted_mean() == 0.0

    def test_weighted_mean_uses_default_strategy(self) -> None:
        """Test weighted_mean uses default strategy when none specified."""
        stats = RuleTimingStats(rule="test", durations=[10.0, 20.0, 30.0])
        # Should use index strategy by default
        result = stats.weighted_mean()
        assert result > 0

    def test_median_input_size_empty(self) -> None:
        """Test median_input_size returns None when no data."""
        stats = RuleTimingStats(rule="test", durations=[10.0], input_sizes=[])
        assert stats.median_input_size is None

    def test_median_input_size_all_none(self) -> None:
        """Test median_input_size returns None when all sizes are None."""
        stats = RuleTimingStats(rule="test", durations=[10.0, 20.0], input_sizes=[None, None])
        assert stats.median_input_size is None

    def test_median_input_size_odd_count(self) -> None:
        """Test median_input_size with odd number of values."""
        stats = RuleTimingStats(
            rule="test",
            durations=[10.0, 20.0, 30.0],
            input_sizes=[100, 200, 300],
        )
        assert stats.median_input_size == 200

    def test_median_input_size_even_count(self) -> None:
        """Test median_input_size with even number of values."""
        stats = RuleTimingStats(
            rule="test",
            durations=[10.0, 20.0, 30.0, 40.0],
            input_sizes=[100, 200, 300, 400],
        )
        # (200 + 300) // 2 = 250
        assert stats.median_input_size == 250

    def test_median_input_size_with_some_none(self) -> None:
        """Test median_input_size ignores None values."""
        stats = RuleTimingStats(
            rule="test",
            durations=[10.0, 20.0, 30.0],
            input_sizes=[100, None, 300],
        )
        # Only [100, 300] -> (100 + 300) // 2 = 200
        assert stats.median_input_size == 200

    def test_recency_factor_no_timestamps_time_strategy(self) -> None:
        """Test recency factor returns 0.5 when no timestamps with time strategy."""
        stats = RuleTimingStats(rule="test", durations=[10.0, 20.0])
        assert stats.recency_factor(strategy="time") == 0.5

    def test_recency_factor_no_data_index_strategy(self) -> None:
        """Test recency factor returns 0.5 when no data with index strategy."""
        stats = RuleTimingStats(rule="test", durations=[])
        assert stats.recency_factor(strategy="index") == 0.5

    def test_recency_factor_with_data_index_strategy(self) -> None:
        """Test recency factor returns 0.9 when data exists with index strategy."""
        stats = RuleTimingStats(rule="test", durations=[10.0, 20.0])
        assert stats.recency_factor(strategy="index") == 0.9

    def test_recency_factor_recent_data(self) -> None:
        """Test recency factor is high for recent data with time strategy."""
        now = time.time()
        stats = RuleTimingStats(
            rule="test",
            durations=[10.0, 20.0],
            timestamps=[now - 3600, now],  # 1 hour ago, now
        )
        assert stats.recency_factor(strategy="time") > 0.9  # Very recent

    def test_recency_factor_old_data(self) -> None:
        """Test recency factor is low for old data with time strategy."""
        now = time.time()
        day = 86400
        stats = RuleTimingStats(
            rule="test",
            durations=[10.0, 20.0],
            timestamps=[now - 30 * day, now - 28 * day],  # 30 and 28 days ago
        )
        assert stats.recency_factor(strategy="time") < 0.3  # Old data

    def test_recent_consistency_no_data(self) -> None:
        """Test recent consistency returns 0.5 when no data."""
        stats = RuleTimingStats(rule="test", durations=[])
        assert stats.recent_consistency() == 0.5

    def test_recent_consistency_consistent(self) -> None:
        """Test recent consistency is high for consistent runs."""
        now = time.time()
        stats = RuleTimingStats(
            rule="test",
            durations=[10.0, 10.1, 9.9, 10.0],
            timestamps=[now - 3600, now - 2400, now - 1200, now],
        )
        assert stats.recent_consistency() > 0.9  # Very consistent

    def test_recent_consistency_variable(self) -> None:
        """Test recent consistency is lower for variable runs."""
        now = time.time()
        stats = RuleTimingStats(
            rule="test",
            durations=[5.0, 20.0, 10.0, 30.0],  # High variance
            timestamps=[now - 3600, now - 2400, now - 1200, now],
        )
        assert stats.recent_consistency() < 0.7  # Variable data


class TestWildcardTimingStats:
    """Tests for the WildcardTimingStats class."""

    def test_init(self) -> None:
        """Test basic initialization."""
        from snakesee.models import WildcardTimingStats

        wts = WildcardTimingStats(rule="align", wildcard_key="sample")
        assert wts.rule == "align"
        assert wts.wildcard_key == "sample"
        assert wts.stats_by_value == {}

    def test_get_stats_for_value_not_found(self) -> None:
        """Test get_stats_for_value returns None for unknown value."""
        from snakesee.models import WildcardTimingStats

        wts = WildcardTimingStats(rule="align", wildcard_key="sample")
        assert wts.get_stats_for_value("unknown") is None

    def test_get_stats_for_value_insufficient_samples(self) -> None:
        """Test get_stats_for_value returns None for insufficient samples."""
        from snakesee.models import WildcardTimingStats

        wts = WildcardTimingStats(
            rule="align",
            wildcard_key="sample",
            stats_by_value={
                "A": RuleTimingStats(rule="align:sample=A", durations=[100.0, 110.0]),
            },
        )
        # Only 2 samples, need at least 3
        assert wts.get_stats_for_value("A") is None

    def test_get_stats_for_value_sufficient_samples(self) -> None:
        """Test get_stats_for_value returns stats when sufficient samples."""
        from snakesee.models import WildcardTimingStats

        wts = WildcardTimingStats(
            rule="align",
            wildcard_key="sample",
            stats_by_value={
                "A": RuleTimingStats(rule="align:sample=A", durations=[100.0, 110.0, 105.0]),
            },
        )
        stats = wts.get_stats_for_value("A")
        assert stats is not None
        assert stats.count == 3

    def test_get_most_predictive_key_empty(self) -> None:
        """Test get_most_predictive_key with no stats."""
        from snakesee.models import WildcardTimingStats

        result = WildcardTimingStats.get_most_predictive_key({})
        assert result is None

    def test_get_most_predictive_key_single_value(self) -> None:
        """Test get_most_predictive_key with only one value per key."""
        from snakesee.models import WildcardTimingStats

        wts = WildcardTimingStats(
            rule="align",
            wildcard_key="sample",
            stats_by_value={
                "A": RuleTimingStats(rule="align:sample=A", durations=[100.0]),
            },
        )
        # Need at least 2 values to compare
        result = WildcardTimingStats.get_most_predictive_key({"sample": wts})
        assert result is None

    def test_get_most_predictive_key_high_variance(self) -> None:
        """Test get_most_predictive_key identifies key with high variance."""
        from snakesee.models import WildcardTimingStats

        # sample key: A=100, B=500 (high between-group variance)
        # Need at least 3 samples per value for MIN_SAMPLES_FOR_CONDITIONING
        sample_wts = WildcardTimingStats(
            rule="align",
            wildcard_key="sample",
            stats_by_value={
                "A": RuleTimingStats(rule="align:sample=A", durations=[100.0, 102.0, 98.0]),
                "B": RuleTimingStats(rule="align:sample=B", durations=[500.0, 510.0, 490.0]),
            },
        )

        # batch key: 1=100, 2=110 (low between-group variance)
        batch_wts = WildcardTimingStats(
            rule="align",
            wildcard_key="batch",
            stats_by_value={
                "1": RuleTimingStats(rule="align:batch=1", durations=[100.0, 102.0, 98.0]),
                "2": RuleTimingStats(rule="align:batch=2", durations=[110.0, 112.0, 108.0]),
            },
        )

        result = WildcardTimingStats.get_most_predictive_key(
            {
                "sample": sample_wts,
                "batch": batch_wts,
            }
        )
        assert result == "sample"  # Higher variance between A and B


class TestTimeEstimate:
    """Tests for the TimeEstimate dataclass."""

    def test_format_eta_high_confidence(self) -> None:
        """Test ETA formatting with high confidence."""
        estimate = TimeEstimate(
            seconds_remaining=300,
            lower_bound=280,
            upper_bound=320,
            confidence=0.8,
            method="weighted",
        )
        assert estimate.format_eta() == "~5m"

    def test_format_eta_medium_confidence(self) -> None:
        """Test ETA formatting with medium confidence."""
        estimate = TimeEstimate(
            seconds_remaining=300,
            lower_bound=200,
            upper_bound=400,
            confidence=0.5,
            method="weighted",
        )
        eta = estimate.format_eta()
        assert "-" in eta  # Shows range

    def test_format_eta_low_confidence(self) -> None:
        """Test ETA formatting with low confidence."""
        estimate = TimeEstimate(
            seconds_remaining=300,
            lower_bound=100,
            upper_bound=600,
            confidence=0.2,
            method="simple",
        )
        assert "rough" in estimate.format_eta()

    def test_format_eta_unknown(self) -> None:
        """Test ETA formatting when unknown."""
        estimate = TimeEstimate(
            seconds_remaining=float("inf"),
            lower_bound=0,
            upper_bound=float("inf"),
            confidence=0.0,
            method="bootstrap",
        )
        assert estimate.format_eta() == "unknown"


class TestWorkflowProgress:
    """Tests for the WorkflowProgress dataclass."""

    def test_job_sequences_are_immutable_snapshots(self) -> None:
        """List inputs are frozen into tuples so the snapshot cannot be mutated in place."""
        source = [JobInfo(rule="a"), JobInfo(rule="b")]
        progress = WorkflowProgress(
            workflow_dir=Path("."),
            status=WorkflowStatus.RUNNING,
            total_jobs=10,
            completed_jobs=0,
            running_jobs=source,
        )
        # Stored as a tuple, decoupled from the source list...
        assert isinstance(progress.running_jobs, tuple)
        source.append(JobInfo(rule="c"))
        assert len(progress.running_jobs) == 2
        # ...and unset list fields default to an empty tuple, not a shared mutable list.
        assert progress.failed_jobs_list == ()
        assert isinstance(progress.recent_completions, tuple)

    def test_non_finite_total_cost_estimate_dropped(self) -> None:
        """nan/inf/-inf workflow totals are dropped to None so they cannot poison the header."""
        for bad in (math.nan, math.inf, -math.inf):
            progress = WorkflowProgress(
                workflow_dir=Path("."),
                status=WorkflowStatus.RUNNING,
                total_jobs=10,
                completed_jobs=0,
                total_cost_estimate=bad,
            )
            assert progress.total_cost_estimate is None
        finite = WorkflowProgress(
            workflow_dir=Path("."),
            status=WorkflowStatus.RUNNING,
            total_jobs=10,
            completed_jobs=0,
            total_cost_estimate=2.5,
        )
        assert finite.total_cost_estimate == 2.5

    def test_percent_complete(self) -> None:
        """Test percent complete calculation."""
        progress = WorkflowProgress(
            workflow_dir=Path("."),
            status=WorkflowStatus.RUNNING,
            total_jobs=100,
            completed_jobs=25,
        )
        assert progress.percent_complete == 25.0

    def test_percent_complete_zero_total(self) -> None:
        """Test percent complete with zero total."""
        progress = WorkflowProgress(
            workflow_dir=Path("."),
            status=WorkflowStatus.UNKNOWN,
            total_jobs=0,
            completed_jobs=0,
        )
        assert progress.percent_complete == 0.0

    def test_percent_complete_capped_at_100(self) -> None:
        """Test percent complete is capped at 100 when completed exceeds total."""
        progress = WorkflowProgress(
            workflow_dir=Path("."),
            status=WorkflowStatus.RUNNING,
            total_jobs=1718,
            completed_jobs=1864,  # More completed than total (can happen with parsing issues)
        )
        assert progress.percent_complete == 100.0

    def test_pending_jobs(self) -> None:
        """Test pending jobs calculation."""
        progress = WorkflowProgress(
            workflow_dir=Path("."),
            status=WorkflowStatus.RUNNING,
            total_jobs=100,
            completed_jobs=25,
            running_jobs=[JobInfo(rule="test")] * 5,
        )
        assert progress.pending_jobs == 70  # 100 - 25 - 5

    def test_pending_jobs_with_incomplete(self) -> None:
        """Test pending jobs calculation excludes incomplete jobs."""
        progress = WorkflowProgress(
            workflow_dir=Path("."),
            status=WorkflowStatus.INCOMPLETE,
            total_jobs=100,
            completed_jobs=25,
            failed_jobs=2,
            running_jobs=[],
            incomplete_jobs_list=[JobInfo(rule="incomplete")] * 3,
        )
        # 100 - 25 - 2 - 0 - 3 = 70
        assert progress.pending_jobs == 70

    def test_elapsed_seconds(self) -> None:
        """Test elapsed seconds calculation."""
        start = time.time() - 60
        progress = WorkflowProgress(
            workflow_dir=Path("."),
            status=WorkflowStatus.RUNNING,
            total_jobs=100,
            completed_jobs=25,
            start_time=start,
        )
        elapsed = progress.elapsed_seconds
        assert elapsed is not None
        assert 59 < elapsed < 62


class TestThreadTimingStats:
    """Tests for the ThreadTimingStats dataclass."""

    def test_get_stats_for_threads_exact_match(self) -> None:
        """Test get_stats_for_threads returns exact match."""
        from snakesee.models import ThreadTimingStats

        thread_stats = ThreadTimingStats(rule="align")
        thread_stats.stats_by_threads[4] = RuleTimingStats(rule="align", durations=[50.0])

        result = thread_stats.get_stats_for_threads(4)
        assert result is not None
        assert result.mean_duration == 50.0

    def test_get_stats_for_threads_no_match(self) -> None:
        """Test get_stats_for_threads returns None for no match."""
        from snakesee.models import ThreadTimingStats

        thread_stats = ThreadTimingStats(rule="align")
        thread_stats.stats_by_threads[4] = RuleTimingStats(rule="align", durations=[50.0])

        result = thread_stats.get_stats_for_threads(8)
        assert result is None

    def test_get_best_match_exact(self) -> None:
        """Test get_best_match returns exact match with thread count."""
        from snakesee.models import ThreadTimingStats

        thread_stats = ThreadTimingStats(rule="align")
        thread_stats.stats_by_threads[4] = RuleTimingStats(rule="align", durations=[50.0])

        stats, matched = thread_stats.get_best_match(4)
        assert stats is not None
        assert matched == 4
        assert stats.mean_duration == 50.0

    def test_get_best_match_fallback_aggregate(self) -> None:
        """Test get_best_match falls back to aggregate."""
        from snakesee.models import ThreadTimingStats

        thread_stats = ThreadTimingStats(rule="align")
        thread_stats.stats_by_threads[1] = RuleTimingStats(rule="align", durations=[100.0])
        thread_stats.stats_by_threads[8] = RuleTimingStats(rule="align", durations=[20.0])

        stats, matched = thread_stats.get_best_match(4)
        assert stats is not None
        assert matched is None  # No exact match
        assert stats.mean_duration == 60.0  # Average of 100 and 20

    def test_get_best_match_no_data(self) -> None:
        """Test get_best_match returns None when no data."""
        from snakesee.models import ThreadTimingStats

        thread_stats = ThreadTimingStats(rule="align")

        stats, matched = thread_stats.get_best_match(4)
        assert stats is None
        assert matched is None

    def test_aggregate_all_threads_sorts_chronologically(self) -> None:
        """Test _aggregate_all_threads sorts by timestamp."""
        from snakesee.models import ThreadTimingStats

        thread_stats = ThreadTimingStats(rule="align")
        # Add data out of order
        thread_stats.stats_by_threads[8] = RuleTimingStats(
            rule="align",
            durations=[20.0],
            timestamps=[200.0],
            input_sizes=[1000],
        )
        thread_stats.stats_by_threads[1] = RuleTimingStats(
            rule="align",
            durations=[100.0],
            timestamps=[100.0],
            input_sizes=[500],
        )

        # Get aggregate (via get_best_match with non-matching thread)
        stats, matched = thread_stats.get_best_match(4)
        assert stats is not None
        assert matched is None

        # Verify data is sorted by timestamp
        assert stats.timestamps == [100.0, 200.0]
        assert stats.durations == [100.0, 20.0]
        assert stats.input_sizes == [500, 1000]

    def test_aggregate_handles_mismatched_lengths(self) -> None:
        """Test _aggregate_all_threads handles mismatched array lengths."""
        from snakesee.models import ThreadTimingStats

        thread_stats = ThreadTimingStats(rule="align")
        # Add data with mismatched lengths (no timestamps)
        thread_stats.stats_by_threads[4] = RuleTimingStats(
            rule="align",
            durations=[50.0, 60.0],
            timestamps=[],  # No timestamps
            input_sizes=[1000, 2000],
        )

        # Should still aggregate, just not sorted
        stats, matched = thread_stats.get_best_match(8)
        assert stats is not None
        assert stats.count == 2


class TestConstantsSynchronization:
    """Tests to verify constants stay synchronized across modules."""

    def test_min_samples_for_conditioning_sync(self) -> None:
        """Verify MIN_SAMPLES_FOR_CONDITIONING is synchronized between modules.

        This test ensures the constant defined in models.py stays in sync with
        the authoritative value in constants.py to prevent subtle bugs from
        value drift.
        """
        from snakesee.constants import MIN_SAMPLES_FOR_CONDITIONING
        from snakesee.models import WildcardTimingStats

        assert WildcardTimingStats.MIN_SAMPLES_FOR_CONDITIONING == MIN_SAMPLES_FOR_CONDITIONING, (
            f"MIN_SAMPLES_FOR_CONDITIONING mismatch: "
            f"models.py has {WildcardTimingStats.MIN_SAMPLES_FOR_CONDITIONING}, "
            f"constants.py has {MIN_SAMPLES_FOR_CONDITIONING}"
        )
