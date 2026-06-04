"""Rule registry for centralized rule timing statistics.

This module provides a single source of truth for all rule timing data,
consolidating aggregate, thread-level, and wildcard-level statistics.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from dataclasses import field
from statistics import median
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from snakesee.models import RuleTimingStats
    from snakesee.models import ThreadTimingStats
    from snakesee.models import WildcardTimingStats
    from snakesee.state.config import EstimationConfig
    from snakesee.state.job_registry import Job


def _make_empty_rule_stats(rule: str = "") -> RuleTimingStats:
    """Factory function to create empty RuleTimingStats."""
    from snakesee.models import RuleTimingStats

    return RuleTimingStats(rule=rule)


def _median(values: list[float] | None) -> float | None:
    """Median of a list of values, or None if empty/None."""
    if not values:
        return None
    return float(median(values))


def _make_combination_key(wildcards: dict[str, str] | None, threads: int | None) -> str | None:
    """Create a composite key from wildcards and threads.

    Args:
        wildcards: Wildcard values dict.
        threads: Thread count.

    Returns:
        Composite key like "batch=1:sample=A:threads=4" or None if no data.
    """
    parts: list[str] = []

    # Add sorted wildcard key=value pairs
    if wildcards:
        for key in sorted(wildcards.keys()):
            parts.append(f"{key}={wildcards[key]}")

    # Add threads if provided
    if threads is not None:
        parts.append(f"threads={threads}")

    return ":".join(parts) if parts else None


@dataclass
class RuleStatistics:
    """Complete timing statistics for a single rule.

    Combines aggregate, thread-level, and wildcard-level statistics
    in a single container.

    Attributes:
        rule: The rule name.
        aggregate: Overall timing statistics for the rule.
        by_threads: Thread-specific timing statistics.
        by_wildcard: Wildcard-specific timing statistics.
        by_combination: Full wildcard+threads combination statistics.
        expected_count: Expected number of jobs for this rule (if known).
    """

    rule: str
    aggregate: RuleTimingStats = field(default_factory=_make_empty_rule_stats)
    by_threads: ThreadTimingStats | None = None
    by_wildcard: dict[str, WildcardTimingStats] = field(default_factory=dict)
    by_combination: dict[str, RuleTimingStats] = field(default_factory=dict)
    expected_count: int | None = None

    def __post_init__(self) -> None:
        """Ensure aggregate has correct rule name."""
        from snakesee.models import RuleTimingStats

        if self.aggregate.rule != self.rule:
            # Create new RuleTimingStats with correct rule name
            self.aggregate = RuleTimingStats(
                rule=self.rule,
                durations=list(self.aggregate.durations),
                timestamps=list(self.aggregate.timestamps),
                input_sizes=list(self.aggregate.input_sizes),
            )

    @property
    def typical_threads(self) -> float:
        """Weighted average of thread counts from historical data.

        Uses the number of observations at each thread count as weights.
        Defaults to 1.0 if no thread data is available.
        """
        if self.by_threads is None or not self.by_threads.stats_by_threads:
            return 1.0

        total_weight = 0
        weighted_sum = 0.0
        for thread_count, stats in self.by_threads.stats_by_threads.items():
            n = stats.count
            weighted_sum += thread_count * n
            total_weight += n

        return weighted_sum / total_weight if total_weight > 0 else 1.0

    def record_completion(
        self,
        duration: float,
        timestamp: float,
        threads: int | None = None,
        wildcards: dict[str, str] | None = None,
        input_size: int | None = None,
    ) -> None:
        """Record a job completion.

        Args:
            duration: Job duration in seconds.
            timestamp: Completion timestamp.
            threads: Number of threads used.
            wildcards: Wildcard values for the job.
            input_size: Input file size in bytes.
        """
        from snakesee.models import RuleTimingStats
        from snakesee.models import ThreadTimingStats
        from snakesee.models import WildcardTimingStats

        # Update aggregate stats
        self.aggregate.durations.append(duration)
        self.aggregate.timestamps.append(timestamp)
        self.aggregate.input_sizes.append(input_size)

        # Update thread-specific stats
        if threads is not None:
            if self.by_threads is None:
                self.by_threads = ThreadTimingStats(rule=self.rule, stats_by_threads={})
            # Get or create stats for this thread count
            if threads not in self.by_threads.stats_by_threads:
                self.by_threads.stats_by_threads[threads] = RuleTimingStats(
                    rule=f"{self.rule}@{threads}t"
                )
            thread_stats = self.by_threads.stats_by_threads[threads]
            thread_stats.durations.append(duration)
            thread_stats.timestamps.append(timestamp)

        # Update wildcard-specific stats (for each wildcard key)
        if wildcards:
            for wc_key, wc_value in wildcards.items():
                if wc_key not in self.by_wildcard:
                    self.by_wildcard[wc_key] = WildcardTimingStats(
                        rule=self.rule,
                        wildcard_key=wc_key,
                        stats_by_value={},
                    )
                wts = self.by_wildcard[wc_key]
                # Access stats_by_value directly (get_stats_for_value has min sample check)
                if wc_value not in wts.stats_by_value:
                    wts.stats_by_value[wc_value] = RuleTimingStats(
                        rule=f"{self.rule}:{wc_key}={wc_value}"
                    )
                value_stats = wts.stats_by_value[wc_value]
                value_stats.durations.append(duration)
                value_stats.timestamps.append(timestamp)

        # Update combination stats (full wildcard+threads key)
        combo_key = _make_combination_key(wildcards, threads)
        if combo_key:
            if combo_key not in self.by_combination:
                self.by_combination[combo_key] = RuleTimingStats(rule=f"{self.rule}@{combo_key}")
            combo_stats = self.by_combination[combo_key]
            combo_stats.durations.append(duration)
            combo_stats.timestamps.append(timestamp)


class RuleRegistry:
    """Central registry for all rule timing statistics.

    Consolidates what was previously scattered across TimeEstimator's
    rule_stats, thread_stats, and wildcard_stats dictionaries.

    Example:
        >>> registry = RuleRegistry()
        >>> stats = registry.get_or_create("align")
        >>> stats.record_completion(duration=100.0, timestamp=time.time(), threads=4)
        >>> mean, var = registry.get_estimate("align", threads=4)
    """

    def __init__(self, config: EstimationConfig | None = None) -> None:
        """Initialize empty registry.

        Args:
            config: Optional estimation configuration.
        """
        from snakesee.state.config import DEFAULT_CONFIG

        self.config = config or DEFAULT_CONFIG
        self._lock = threading.RLock()
        self._rules: dict[str, RuleStatistics] = {}
        self._global_mean_cache: float | None = None
        self._cache_valid: bool = False
        # Queue wait (remote executors only) is tracked separately from execution
        # duration: a job's time spent queued before a node picks it up is a
        # property of the queue/cluster, not the rule's compute cost.
        self._queue_waits_by_rule: dict[str, list[float]] = {}
        self._queue_waits_by_queue: dict[str, list[float]] = {}

    def __len__(self) -> int:
        """Return number of rules in registry."""
        with self._lock:
            return len(self._rules)

    def __contains__(self, rule: str) -> bool:
        """Check if rule exists in registry."""
        with self._lock:
            return rule in self._rules

    def get(self, rule: str) -> RuleStatistics | None:
        """Get statistics for a rule."""
        with self._lock:
            return self._rules.get(rule)

    def get_or_create(self, rule: str) -> RuleStatistics:
        """Get existing statistics or create new ones.

        Args:
            rule: Rule name.

        Returns:
            RuleStatistics for the rule.
        """
        with self._lock:
            if rule not in self._rules:
                self._rules[rule] = RuleStatistics(rule=rule)
                self._cache_valid = False
            return self._rules[rule]

    def record_completion(
        self,
        rule: str,
        duration: float,
        timestamp: float,
        threads: int | None = None,
        wildcards: dict[str, str] | None = None,
        input_size: int | None = None,
    ) -> None:
        """Record a job completion.

        Args:
            rule: Rule name.
            duration: Job duration in seconds.
            timestamp: Completion timestamp.
            threads: Number of threads used.
            wildcards: Wildcard values.
            input_size: Input file size.
        """
        with self._lock:
            if rule not in self._rules:
                self._rules[rule] = RuleStatistics(rule=rule)
            stats = self._rules[rule]
            stats.record_completion(duration, timestamp, threads, wildcards, input_size)
            self._cache_valid = False

    def record_job_completion(self, job: Job) -> None:
        """Record completion from a Job object.

        Args:
            job: Completed job with timing data.
        """
        if job.duration is None:
            return

        # record_completion already acquires lock
        self.record_completion(
            rule=job.rule,
            duration=job.duration,
            timestamp=job.end_time or 0.0,
            threads=job.threads,
            wildcards=job.wildcards if job.wildcards else None,
            input_size=job.input_size,
        )

    def record_queue_wait(self, rule: str, wait: float, queue: str | None = None) -> None:
        """Record the queue wait for a completed remote job.

        Queue wait is the time a job spent queued on a remote executor before a
        node began executing it. It is kept separate from execution duration so
        time estimates aren't inflated by queue contention.

        Args:
            rule: Rule name.
            wait: Seconds spent queued. Non-positive values (e.g. clock skew) are
                ignored.
            queue: Optional queue/compute-environment name the job was routed to.
        """
        if wait <= 0:
            return
        with self._lock:
            self._queue_waits_by_rule.setdefault(rule, []).append(wait)
            if queue is not None:
                self._queue_waits_by_queue.setdefault(queue, []).append(wait)

    def queue_wait_for_rule(self, rule: str) -> float | None:
        """Median observed queue wait for a rule, or None if no data."""
        with self._lock:
            return _median(self._queue_waits_by_rule.get(rule))

    def queue_wait_count_for_rule(self, rule: str) -> int:
        """Number of queue-wait samples recorded for a rule (0 if none)."""
        with self._lock:
            return len(self._queue_waits_by_rule.get(rule, []))

    def queue_wait_for_queue(self, queue: str) -> float | None:
        """Median observed queue wait for a named queue, or None if no data."""
        with self._lock:
            return _median(self._queue_waits_by_queue.get(queue))

    def overall_queue_wait(self) -> float | None:
        """Median queue wait across all rules, or None if no data."""
        with self._lock:
            all_waits = [w for waits in self._queue_waits_by_rule.values() for w in waits]
            return _median(all_waits)

    def global_mean_duration(self) -> float:
        """Get global mean duration across all rules.

        Returns:
            Mean duration in seconds, or config default if no data.
        """
        with self._lock:
            if self._cache_valid and self._global_mean_cache is not None:
                return self._global_mean_cache

            all_durations: list[float] = []
            for stats in self._rules.values():
                all_durations.extend(stats.aggregate.durations)

            if all_durations:
                self._global_mean_cache = sum(all_durations) / len(all_durations)
            else:
                self._global_mean_cache = self.config.default_global_mean

            self._cache_valid = True
            return self._global_mean_cache

    def set_expected_counts(self, counts: dict[str, int]) -> None:
        """Set expected job counts for rules.

        Args:
            counts: Dictionary of rule name to expected count.
        """
        with self._lock:
            for rule, count in counts.items():
                if rule not in self._rules:
                    self._rules[rule] = RuleStatistics(rule=rule)
                self._rules[rule].expected_count = count

    def clear(self) -> None:
        """Clear all statistics."""
        with self._lock:
            self._rules.clear()
            self._cache_valid = False
            self._global_mean_cache = None

    # Backward compatibility methods

    def to_rule_stats_dict(self) -> dict[str, RuleTimingStats]:
        """Convert to dict for backward compatibility with TimeEstimator."""
        with self._lock:
            return {name: stats.aggregate for name, stats in self._rules.items()}

    def to_thread_stats_dict(self) -> dict[str, ThreadTimingStats]:
        """Convert to thread stats dict for backward compatibility."""
        with self._lock:
            result: dict[str, ThreadTimingStats] = {}
            for name, stats in self._rules.items():
                if stats.by_threads is not None:
                    result[name] = stats.by_threads
            return result

    def to_wildcard_stats_dict(self) -> dict[str, dict[str, WildcardTimingStats]]:
        """Convert to wildcard stats dict for backward compatibility."""
        with self._lock:
            result: dict[str, dict[str, WildcardTimingStats]] = {}
            for name, stats in self._rules.items():
                if stats.by_wildcard:
                    result[name] = stats.by_wildcard
            return result

    def load_from_rule_stats(
        self,
        rule_stats: dict[str, RuleTimingStats],
        thread_stats: dict[str, ThreadTimingStats] | None = None,
        wildcard_stats: dict[str, dict[str, WildcardTimingStats]] | None = None,
    ) -> None:
        """Load from existing stats dictionaries.

        Args:
            rule_stats: Aggregate rule timing stats.
            thread_stats: Thread-specific stats.
            wildcard_stats: Wildcard-specific stats.
        """
        with self._lock:
            for rule, stats in rule_stats.items():
                if rule not in self._rules:
                    self._rules[rule] = RuleStatistics(rule=rule)
                self._rules[rule].aggregate = stats

            if thread_stats:
                for rule, tstats in thread_stats.items():
                    if rule not in self._rules:
                        self._rules[rule] = RuleStatistics(rule=rule)
                    self._rules[rule].by_threads = tstats

            if wildcard_stats:
                for rule, wstats in wildcard_stats.items():
                    if rule not in self._rules:
                        self._rules[rule] = RuleStatistics(rule=rule)
                    self._rules[rule].by_wildcard = wstats

            self._cache_valid = False

    def all_rules(self) -> list[str]:
        """Get all rule names."""
        with self._lock:
            return list(self._rules.keys())

    def get_combination_stats(
        self,
        rule: str,
        wildcards: dict[str, str] | None,
        threads: int | None,
        min_samples: int | None = None,
    ) -> RuleTimingStats | None:
        """Get timing stats for a specific wildcard+threads combination.

        Args:
            rule: Rule name.
            wildcards: Wildcard values dict.
            threads: Thread count.
            min_samples: Minimum samples required. If None, uses MIN_SAMPLES_FOR_CONDITIONING.
                        Set to 1 to get stats even with a single historical run.

        Returns:
            RuleTimingStats for the combination, or None if not found or insufficient samples.
        """
        from snakesee.constants import MIN_SAMPLES_FOR_CONDITIONING

        if min_samples is None:
            min_samples = MIN_SAMPLES_FOR_CONDITIONING

        combo_key = _make_combination_key(wildcards, threads)
        if combo_key is None:
            return None

        with self._lock:
            stats = self._rules.get(rule)
            if stats is None:
                return None

            combo_stats = stats.by_combination.get(combo_key)
            if combo_stats is None:
                return None

            # Require minimum samples for conditioning
            if combo_stats.count < min_samples:
                return None

            return combo_stats

    def typical_threads(self, rule: str) -> float:
        """Get the typical thread count for a rule.

        Args:
            rule: Rule name.

        Returns:
            Weighted average thread count, or 1.0 if no thread data.
        """
        with self._lock:
            stats = self._rules.get(rule)
            if stats is None:
                return 1.0
            return stats.typical_threads

    def total_sample_count(self) -> int:
        """Get total number of samples across all rules."""
        with self._lock:
            return sum(stats.aggregate.count for stats in self._rules.values())

    def rule_count(self) -> int:
        """Get number of rules in the registry."""
        with self._lock:
            return len(self._rules)
