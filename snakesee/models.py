"""Data models for workflow monitoring."""

import logging
from dataclasses import dataclass
from dataclasses import field
from enum import Enum
from pathlib import Path
from statistics import mean
from statistics import stdev
from typing import ClassVar
from typing import Literal

from snakesee.constants import MIN_SAMPLES_FOR_CONDITIONING
from snakesee.state.clock import ClockUtils
from snakesee.state.clock import get_clock

logger = logging.getLogger(__name__)

# Weighting strategy type for timing estimates
WeightingStrategy = Literal["time", "index"]


class WorkflowStatus(Enum):
    """Current status of a workflow."""

    UNKNOWN = "unknown"
    NOT_STARTED = "not_started"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    INCOMPLETE = "incomplete"
    STALE = "stale"  # No activity for extended period


@dataclass(frozen=True, slots=True)
class JobInfo:
    """
    Information about a single job execution.

    Attributes:
        rule: The name of the rule that was executed.
        job_id: Optional job identifier.
        start_time: Unix timestamp when the job started.
        end_time: Unix timestamp when the job completed (None if still running).
        output_file: The output file path this job produces.
        wildcards: Dictionary of wildcard names to values (e.g., {"sample": "A", "batch": "1"}).
        input_size: Total size of input files in bytes (None if unknown).
        threads: Number of threads allocated to this job (None if unknown).
        log_file: Path to the job's log file (parsed from snakemake log directive).
        external_jobid: External executor job id/ARN (e.g. AWS Batch), if remote.
        executor: Remote executor identifier (e.g. "aws-batch"), if applicable.
        region: Cloud region, used to build console deep links (if known).
        log_stream: Backend log stream identifier (e.g. CloudWatch stream), if known.
        queued_at: Epoch seconds the job entered the remote queue, if known. For a
            remote job, start_time is the true execution start, so the difference
            is the queue wait.
        queue: The remote queue / compute environment the job was routed to, if known.
        attempt: Attempt number for a retried/preempted remote job, if known.
        exit_code: Container/process exit code for a finished remote job, if known.
        status_reason: Backend-provided reason string (e.g. failure cause), if any.
        termination_category: Why the job died (e.g. "spot", "oom"), if classified.
        termination_source: Provenance of the classification (e.g. "aws_instance_state").
        termination_confidence: How sure the producer was ("high" / "low").
    """

    rule: str
    job_id: str | None = None
    start_time: float | None = None
    end_time: float | None = None
    output_file: Path | None = None
    wildcards: dict[str, str] | None = None
    input_size: int | None = None
    threads: int | None = None
    log_file: Path | None = None
    external_jobid: str | None = None
    executor: str | None = None
    region: str | None = None
    log_stream: str | None = None
    queued_at: float | None = None
    queue: str | None = None
    attempt: int | None = None
    exit_code: int | None = None
    status_reason: str | None = None
    termination_category: str | None = None
    termination_source: str | None = None
    termination_confidence: str | None = None

    @property
    def queue_wait(self) -> float | None:
        """Seconds spent queued before execution began (remote jobs only).

        Returns:
            start_time - queued_at when both are known and non-negative, else None.
        """
        if self.queued_at is None or self.start_time is None:
            return None
        wait = self.start_time - self.queued_at
        return wait if wait >= 0 else None

    @property
    def elapsed(self) -> float | None:
        """
        Elapsed time in seconds.

        Returns:
            Seconds elapsed since start, or None if start_time not set.
        """
        if self.start_time is None:
            return None
        end = self.end_time or get_clock().now()
        result = ClockUtils.calculate_duration(self.start_time, end, f"job {self.rule}")
        if not result.is_valid:
            logger.warning(
                "Negative elapsed time detected for job %s: %.2fs (clock skew?)",
                self.rule,
                result.raw_value,
            )
        return result.value

    @property
    def duration(self) -> float | None:
        """
        Total duration in seconds (only for completed jobs).

        Returns:
            Duration in seconds (always >= 0), or None if job not completed.
        """
        if self.start_time is not None and self.end_time is not None:
            result = ClockUtils.calculate_duration(
                self.start_time, self.end_time, f"job {self.rule}"
            )
            if not result.is_valid:
                logger.warning(
                    "Negative duration detected for job %s: %.2fs (clock skew?)",
                    self.rule,
                    result.raw_value,
                )
            return result.value
        return None


@dataclass
class RuleTimingStats:
    """
    Aggregated timing statistics for a rule.

    Attributes:
        rule: The name of the rule.
        durations: List of observed durations in seconds.
        timestamps: List of Unix timestamps when each run completed (parallel to durations).
        input_sizes: List of input file sizes in bytes (parallel to durations, None for unknown).
    """

    rule: str
    durations: list[float] = field(default_factory=list)
    timestamps: list[float] = field(default_factory=list)
    input_sizes: list[int | None] = field(default_factory=list)

    HIGH_VARIANCE_CV: ClassVar[float] = 0.5  # Coefficient of variation threshold
    DEFAULT_HALF_LIFE_DAYS: ClassVar[float] = 7.0  # Default half-life for time-based weighting
    DEFAULT_HALF_LIFE_LOGS: ClassVar[int] = 10  # Default half-life for index-based weighting
    DEFAULT_WEIGHTING_STRATEGY: ClassVar[WeightingStrategy] = "index"  # Default strategy

    @property
    def count(self) -> int:
        """Number of executions observed."""
        return len(self.durations)

    @property
    def mean_duration(self) -> float:
        """
        Mean duration in seconds.

        Returns:
            Mean of observed durations, or 0.0 if no data.
        """
        if not self.durations:
            return 0.0
        return mean(self.durations)

    @property
    def std_dev(self) -> float:
        """
        Standard deviation of durations.

        Returns:
            Standard deviation, or 0.0 if fewer than 2 observations.
        """
        if len(self.durations) < 2:
            return 0.0
        return stdev(self.durations)

    @property
    def min_duration(self) -> float:
        """Minimum observed duration."""
        return min(self.durations) if self.durations else 0.0

    @property
    def max_duration(self) -> float:
        """Maximum observed duration."""
        return max(self.durations) if self.durations else 0.0

    @property
    def coefficient_of_variation(self) -> float:
        """
        CV = stddev / mean, normalized measure of dispersion.

        Returns:
            Coefficient of variation, or 0.0 if mean is zero.
        """
        if self.mean_duration <= 0:
            return 0.0
        return self.std_dev / self.mean_duration

    @property
    def is_high_variance(self) -> bool:
        """True if this rule has highly variable runtimes."""
        return self.coefficient_of_variation > self.HIGH_VARIANCE_CV

    def weighted_mean(
        self,
        half_life_days: float | None = None,
        strategy: WeightingStrategy | None = None,
        half_life_logs: int | None = None,
    ) -> float:
        """
        Weighted mean favoring recent executions.

        Supports two weighting strategies:
        - "time": Exponential decay based on wall-clock time since each run.
                  Good for stable pipelines where data naturally ages out.
        - "index": Exponential decay based on log index (number of runs ago).
                   Good for active development where each run may fix issues.

        Args:
            half_life_days: After this many days, a run's weight is halved.
                           Only used when strategy="time".
                           Defaults to DEFAULT_HALF_LIFE_DAYS (7.0).
            strategy: Weighting strategy to use ("time" or "index").
                     Defaults to DEFAULT_WEIGHTING_STRATEGY ("index").
            half_life_logs: After this many runs, a run's weight is halved.
                           Only used when strategy="index".
                           Defaults to DEFAULT_HALF_LIFE_LOGS (10).

        Returns:
            Weighted mean duration, or 0.0 if no data.
        """
        if not self.durations:
            return 0.0

        if strategy is None:
            strategy = self.DEFAULT_WEIGHTING_STRATEGY

        if strategy == "index":
            if half_life_logs is None:
                half_life_logs = self.DEFAULT_HALF_LIFE_LOGS
            return self._index_weighted_mean(half_life_logs)

        # strategy == "time"
        if half_life_days is None:
            half_life_days = self.DEFAULT_HALF_LIFE_DAYS

        # Use time-based weighting if timestamps are available
        if self.timestamps and len(self.timestamps) == len(self.durations):
            return self._time_weighted_mean(half_life_days)

        # Fall back to index-based weighting if no timestamps
        if half_life_logs is None:
            half_life_logs = self.DEFAULT_HALF_LIFE_LOGS
        return self._index_weighted_mean(half_life_logs)

    def _time_weighted_mean(self, half_life_days: float) -> float:
        """
        Calculate weighted mean using actual timestamps.

        Args:
            half_life_days: Half-life for exponential decay in days.

        Returns:
            Time-weighted mean duration.
        """
        half_life_seconds = half_life_days * 86400  # Convert days to seconds

        weights: list[float] = []
        for ts in self.timestamps:
            age = ClockUtils.age_seconds(ts)
            weight = 0.5 ** (age / half_life_seconds)
            weights.append(weight)

        weighted_sum = sum(d * w for d, w in zip(self.durations, weights, strict=True))
        weight_total = sum(weights)

        if weight_total <= 0:
            return self.mean_duration  # Fallback to simple mean

        return weighted_sum / weight_total

    def _index_weighted_mean(self, half_life_logs: int) -> float:
        """
        Calculate weighted mean using index-based weights.

        Applies exponential decay based on how many runs ago each entry was,
        not based on wall-clock time. This is useful when each pipeline run
        potentially fixes issues, making older runs less valid regardless of
        actual time elapsed.

        Assumes durations list is ordered oldest-to-newest.

        Args:
            half_life_logs: After this many runs, weight is halved.
                           E.g., half_life_logs=10 means a run from 10 runs ago
                           has 50% weight, 20 runs ago has 25% weight, etc.

        Returns:
            Index-weighted mean duration.
        """
        n = len(self.durations)
        weights: list[float] = []

        for i in range(n):
            # i=0 is oldest, i=n-1 is most recent
            # runs_ago = n - 1 - i (0 for most recent, n-1 for oldest)
            runs_ago = n - 1 - i
            weight = 0.5 ** (runs_ago / half_life_logs)
            weights.append(weight)

        weighted_sum = sum(d * w for d, w in zip(self.durations, weights, strict=True))
        weight_total = sum(weights)

        if weight_total <= 0:
            return self.mean_duration  # Fallback to simple mean

        return weighted_sum / weight_total

    @property
    def median_input_size(self) -> int | None:
        """
        Median input size in bytes for jobs with known input sizes.

        Returns:
            Median size in bytes, or None if no size data available.
        """
        known_sizes = [s for s in self.input_sizes if s is not None]
        if not known_sizes:
            return None
        sorted_sizes = sorted(known_sizes)
        n = len(sorted_sizes)
        if n % 2 == 1:
            return sorted_sizes[n // 2]
        return (sorted_sizes[n // 2 - 1] + sorted_sizes[n // 2]) // 2

    def size_scaled_estimate(
        self,
        input_size: int,
        half_life_days: float | None = None,
        strategy: WeightingStrategy | None = None,
        half_life_logs: int | None = None,
    ) -> tuple[float, float]:
        """
        Estimate duration scaled by input file size.

        Uses the ratio of the given input size to the median historical input size
        to scale the expected duration. This helps when jobs with larger inputs
        take proportionally longer.

        Args:
            input_size: Size of input files for the job in bytes.
            half_life_days: Half-life for time-based weighting.
            strategy: Weighting strategy ("time" or "index").
            half_life_logs: Half-life for index-based weighting.

        Returns:
            Tuple of (scaled_estimate, scaling_confidence).
            Confidence is higher when we have good size data correlation.
        """
        base_estimate = self.weighted_mean(half_life_days, strategy, half_life_logs)
        median_size = self.median_input_size

        if median_size is None or median_size == 0:
            return base_estimate, 0.0  # No size data available

        # Count how many jobs have both duration and size data
        pairs_with_size = sum(
            1 for i, s in enumerate(self.input_sizes) if s is not None and i < len(self.durations)
        )

        if pairs_with_size < 3:
            return base_estimate, 0.0  # Not enough data for size correlation

        # Calculate scaling factor
        size_ratio = input_size / median_size

        # Apply scaling with dampening to avoid extreme extrapolation
        # sqrt dampening: 2x size -> 1.4x time (assuming sublinear scaling)
        dampened_ratio = size_ratio**0.5

        # Clamp to avoid extreme values
        dampened_ratio = max(0.5, min(2.0, dampened_ratio))

        scaled_estimate = base_estimate * dampened_ratio

        # Confidence based on sample size and variability
        confidence = min(0.8, pairs_with_size / 10)

        return scaled_estimate, confidence

    def recency_factor(
        self,
        half_life_days: float | None = None,
        strategy: WeightingStrategy | None = None,
        half_life_logs: int | None = None,
    ) -> float:
        """
        Calculate a recency factor (0.0-1.0) based on data freshness.

        This measures how recent the timing data is. A value of 1.0 means
        most data is very recent; lower values indicate stale data.

        For strategy="time": Based on wall-clock time since runs.
        For strategy="index": Based on number of runs (always 1.0 for index strategy
                              since recency is inherent in the weighting).

        Args:
            half_life_days: Half-life for time-based decay calculation.
            strategy: Weighting strategy ("time" or "index").
            half_life_logs: Half-life for index-based weighting (unused for recency).

        Returns:
            Recency factor between 0.0 and 1.0.
        """
        if strategy is None:
            strategy = self.DEFAULT_WEIGHTING_STRATEGY

        # For index-based strategy, recency is always "fresh" since we weight by index
        # The recency concept doesn't apply in the same way - data is never "stale"
        # just less weighted based on position
        if strategy == "index":
            # Return high recency if we have data, neutral otherwise
            return 0.9 if self.durations else 0.5

        # Time-based recency calculation
        if not self.timestamps:
            return 0.5  # Unknown recency, return neutral value

        if half_life_days is None:
            half_life_days = self.DEFAULT_HALF_LIFE_DAYS

        half_life_seconds = half_life_days * 86400

        # Calculate average weight of data points
        weights = [
            0.5 ** (ClockUtils.age_seconds(ts) / half_life_seconds) for ts in self.timestamps
        ]
        avg_weight = sum(weights) / len(weights) if weights else 0.5

        return min(1.0, avg_weight)

    def recent_consistency(self, days: int = 7) -> float:
        """
        Calculate consistency of recent runs (low CV = high consistency).

        Args:
            days: Number of days to look back for "recent" runs.

        Returns:
            Consistency score between 0.3 and 1.0.
            1.0 = very consistent recent runs, lower = more variable.
        """
        if not self.timestamps or not self.durations:
            return 0.5  # Not enough data

        now = get_clock().now()
        cutoff = now - (days * 86400)

        # Get recent durations
        recent = [d for d, ts in zip(self.durations, self.timestamps, strict=False) if ts >= cutoff]

        if len(recent) < 2:
            return 0.5  # Not enough recent data

        mean_recent = sum(recent) / len(recent)
        if mean_recent <= 0:
            return 0.5

        # Calculate coefficient of variation
        variance = sum((d - mean_recent) ** 2 for d in recent) / len(recent)
        std_recent = variance**0.5
        cv = std_recent / mean_recent

        # CV of 0 = consistency 1.0, CV of 0.5+ = consistency ~0.5
        return float(max(0.3, 1.0 - cv))


@dataclass
class WildcardTimingStats:
    """
    Timing statistics for a rule conditioned on a specific wildcard value.

    Tracks timing per (rule, wildcard_key, wildcard_value) tuple.
    Example: align rule with sample=A takes 5min, sample=B takes 20min.

    Attributes:
        rule: The rule name.
        wildcard_key: The wildcard dimension (e.g., "sample", "batch").
        stats_by_value: Dictionary mapping wildcard values to their timing stats.
    """

    rule: str
    wildcard_key: str
    stats_by_value: dict[str, RuleTimingStats] = field(default_factory=dict)

    # Use the authoritative constant from snakesee.constants
    MIN_SAMPLES_FOR_CONDITIONING: ClassVar[int] = MIN_SAMPLES_FOR_CONDITIONING

    def get_stats_for_value(self, value: str) -> RuleTimingStats | None:
        """
        Get timing stats for a specific wildcard value.

        Args:
            value: The wildcard value to look up.

        Returns:
            RuleTimingStats if available and has sufficient samples, None otherwise.
        """
        stats = self.stats_by_value.get(value)
        if stats is not None and stats.count >= self.MIN_SAMPLES_FOR_CONDITIONING:
            return stats
        return None

    @staticmethod
    def get_most_predictive_key(
        wildcard_stats: dict[str, "WildcardTimingStats"],
    ) -> str | None:
        """
        Find the wildcard key that explains the most variance in timing.

        Uses coefficient of variation to identify which wildcard dimension
        is most predictive of execution time.

        Args:
            wildcard_stats: Dictionary of wildcard timing stats by key.

        Returns:
            The most predictive wildcard key, or None if no good predictor found.
        """
        if not wildcard_stats:
            return None

        best_key: str | None = None
        best_variance_ratio = 0.0

        for key, wts in wildcard_stats.items():
            if len(wts.stats_by_value) < 2:
                continue  # Need at least 2 different values to compare

            # Calculate between-group variance (variance of means)
            # Only include values with enough samples for conditioning
            means = [
                s.mean_duration
                for s in wts.stats_by_value.values()
                if s.count >= WildcardTimingStats.MIN_SAMPLES_FOR_CONDITIONING
            ]
            if len(means) < 2:
                continue

            overall_mean = sum(means) / len(means)
            between_variance = sum((m - overall_mean) ** 2 for m in means) / len(means)

            # Higher between-variance relative to mean = more predictive
            if overall_mean > 0:
                variance_ratio = between_variance / (overall_mean**2)
                if variance_ratio > best_variance_ratio:
                    best_variance_ratio = variance_ratio
                    best_key = key

        # Only return if variance ratio is meaningful (> 0.05)
        # This corresponds to ~22% coefficient of variation between groups
        return best_key if best_variance_ratio > 0.05 else None


@dataclass
class ThreadTimingStats:
    """
    Timing statistics for a rule conditioned on thread count.

    Tracks timing per (rule, threads) tuple.
    Example: align rule with 1 thread takes 10min, with 8 threads takes 2min.

    Attributes:
        rule: The rule name.
        stats_by_threads: Dictionary mapping thread count to their timing stats.
    """

    rule: str
    stats_by_threads: dict[int, RuleTimingStats] = field(default_factory=dict)

    def get_stats_for_threads(self, threads: int) -> RuleTimingStats | None:
        """Get timing stats for a specific thread count."""
        return self.stats_by_threads.get(threads)

    def get_best_match(self, threads: int) -> tuple[RuleTimingStats | None, int | None]:
        """
        Get best matching stats with fallback strategy.

        Returns:
            Tuple of (stats, matched_threads) where:
            - If exact match exists: (exact_stats, threads)
            - If no exact match but other thread data exists: (aggregate_stats, None)
            - If no data at all: (None, None)
        """
        # Exact match first
        if threads in self.stats_by_threads:
            return self.stats_by_threads[threads], threads

        # Fallback: return aggregate across all thread counts
        if self.stats_by_threads:
            return self._aggregate_all_threads(), None

        return None, None

    def _aggregate_all_threads(self) -> RuleTimingStats:
        """Aggregate stats across all thread counts."""
        all_durations: list[float] = []
        all_timestamps: list[float] = []
        all_input_sizes: list[int | None] = []
        for stats in self.stats_by_threads.values():
            all_durations.extend(stats.durations)
            all_timestamps.extend(stats.timestamps)
            all_input_sizes.extend(stats.input_sizes)

        # Sort by timestamp to maintain chronological order for weighted_mean
        if all_timestamps and len(all_timestamps) == len(all_durations) == len(all_input_sizes):
            sorted_data = sorted(
                zip(all_timestamps, all_durations, all_input_sizes, strict=True),
                key=lambda x: x[0],
            )
            all_timestamps = [x[0] for x in sorted_data]
            all_durations = [x[1] for x in sorted_data]
            all_input_sizes = [x[2] for x in sorted_data]

        return RuleTimingStats(
            rule=self.rule,
            durations=all_durations,
            timestamps=all_timestamps,
            input_sizes=all_input_sizes,
        )


@dataclass(frozen=True, slots=True)
class TimeEstimate:
    """
    Time remaining estimate with uncertainty bounds.

    Attributes:
        seconds_remaining: Expected seconds remaining.
        lower_bound: Optimistic estimate (95% CI lower).
        upper_bound: Pessimistic estimate (95% CI upper).
        confidence: Confidence level (0.0 to 1.0).
        method: Estimation method used ("simple", "weighted", "bootstrap").
        inferred_cores: Estimated core count used for thread-aware estimation.
    """

    seconds_remaining: float
    lower_bound: float
    upper_bound: float
    confidence: float
    method: str
    inferred_cores: float | None = None

    def format_eta(self) -> str:
        """
        Format as human-readable ETA string.

        Delegates to snakesee.formatting.format_eta for centralized formatting.

        Returns:
            Formatted ETA with confidence indication.
            Examples: "~5m", "5-10m", "~15m (rough)", "unknown"
        """
        from snakesee.formatting import format_eta as _format_eta

        return _format_eta(
            seconds_remaining=self.seconds_remaining,
            lower_bound=self.lower_bound,
            upper_bound=self.upper_bound,
            confidence=self.confidence,
        )


@dataclass
class WorkflowProgress:
    """
    Current state of workflow progress.

    Attributes:
        workflow_dir: Path to the workflow directory.
        status: Current workflow status.
        total_jobs: Total number of jobs in the workflow.
        completed_jobs: Number of jobs completed.
        failed_jobs: Number of jobs that failed.
        failed_jobs_list: List of failed job details (for --keep-going).
        incomplete_jobs_list: List of jobs that were in progress when workflow was interrupted.
        running_jobs: List of currently running jobs.
        queued_jobs_list: List of jobs queued on a remote executor (awaiting a node).
        recent_completions: List of recently completed jobs.
        start_time: Unix timestamp when workflow started.
        log_file: Path to the current snakemake log file.
    """

    workflow_dir: Path
    status: WorkflowStatus
    total_jobs: int
    completed_jobs: int
    failed_jobs: int = 0
    failed_jobs_list: list[JobInfo] = field(default_factory=list)
    incomplete_jobs_list: list[JobInfo] = field(default_factory=list)
    running_jobs: list[JobInfo] = field(default_factory=list)
    recent_completions: list[JobInfo] = field(default_factory=list)
    pending_jobs_list: list[JobInfo] = field(default_factory=list)
    queued_jobs_list: list[JobInfo] = field(default_factory=list)
    start_time: float | None = None
    log_file: Path | None = None

    @property
    def percent_complete(self) -> float:
        """
        Progress as a percentage.

        Returns:
            Percentage of jobs completed (0.0 to 100.0).
        """
        if self.total_jobs == 0:
            return 0.0
        return min(100.0, (self.completed_jobs / self.total_jobs) * 100)

    @property
    def elapsed_seconds(self) -> float | None:
        """
        Seconds elapsed since workflow start.

        Returns:
            Elapsed time in seconds, or None if start_time not set.
        """
        if self.start_time is None:
            return None
        return get_clock().now() - self.start_time

    @property
    def pending_jobs(self) -> int:
        """Number of jobs not yet started (excludes failed, running, queued, incomplete)."""
        return max(
            0,
            self.total_jobs
            - self.completed_jobs
            - self.failed_jobs
            - len(self.running_jobs)
            - len(self.queued_jobs_list)
            - len(self.incomplete_jobs_list),
        )


def format_duration(seconds: float) -> str:
    """
    Format seconds as human-readable duration.

    This is a public API function preserved for backward compatibility.
    New code should use snakesee.formatting.format_duration directly.

    Args:
        seconds: Duration in seconds.

    Returns:
        Formatted duration string (e.g., "5s", "2m 30s", "1h 15m").
    """
    from snakesee.formatting import format_duration as _fmt_duration

    return _fmt_duration(seconds)
