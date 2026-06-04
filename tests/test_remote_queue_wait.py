"""Tests for the per-rule / per-queue queue-wait metric (Phase 2)."""

from __future__ import annotations

from pathlib import Path

from snakesee.events import EventType
from snakesee.events import SnakeseeEvent
from snakesee.state.rule_registry import RuleRegistry


def _event(event_type: EventType, **kwargs: object) -> SnakeseeEvent:
    return SnakeseeEvent(event_type=event_type, timestamp=kwargs.pop("timestamp", 0.0), **kwargs)  # type: ignore[arg-type]


class TestQueueWaitTracking:
    """RuleRegistry tracks queue wait separately from execution duration."""

    def test_record_and_estimate_per_rule(self) -> None:
        reg = RuleRegistry()
        reg.record_queue_wait("align", 30.0)
        reg.record_queue_wait("align", 50.0)
        reg.record_queue_wait("align", 40.0)
        # Median of [30, 40, 50] = 40.
        assert reg.queue_wait_for_rule("align") == 40.0

    def test_unknown_rule_returns_none(self) -> None:
        reg = RuleRegistry()
        assert reg.queue_wait_for_rule("nope") is None

    def test_per_queue_tracking(self) -> None:
        reg = RuleRegistry()
        reg.record_queue_wait("align", 10.0, queue="on-demand")
        reg.record_queue_wait("sort", 100.0, queue="spot")
        reg.record_queue_wait("sort", 200.0, queue="spot")
        assert reg.queue_wait_for_queue("on-demand") == 10.0
        assert reg.queue_wait_for_queue("spot") == 150.0
        assert reg.queue_wait_for_queue("missing") is None

    def test_overall_queue_wait(self) -> None:
        reg = RuleRegistry()
        assert reg.overall_queue_wait() is None
        reg.record_queue_wait("align", 20.0)
        reg.record_queue_wait("sort", 60.0)
        # Median across all recorded waits [20, 60] = 40.
        assert reg.overall_queue_wait() == 40.0

    def test_negative_wait_ignored(self) -> None:
        # A clock-skew negative wait must not pollute the metric.
        reg = RuleRegistry()
        reg.record_queue_wait("align", -5.0)
        assert reg.queue_wait_for_rule("align") is None
        assert reg.overall_queue_wait() is None

    def test_queue_wait_does_not_affect_duration_stats(self) -> None:
        # Recording a queue wait must not touch execution-duration statistics.
        reg = RuleRegistry()
        reg.record_completion(rule="align", duration=100.0, timestamp=1.0)
        reg.record_queue_wait("align", 30.0)
        stats = reg.get("align")
        assert stats is not None
        assert stats.aggregate.durations == [100.0]  # unchanged


class TestDataSourceRecordsQueueWait:
    """A completed remote job feeds queue wait into the rule registry."""

    def test_completed_remote_job_records_queue_wait_and_execution_duration(
        self, snakemake_dir: object, tmp_path: object
    ) -> None:
        from snakesee.models import WorkflowProgress
        from snakesee.models import WorkflowStatus
        from snakesee.tui.data_source import WorkflowDataSource

        assert isinstance(tmp_path, Path)
        ds = WorkflowDataSource(workflow_dir=tmp_path, use_estimation=True)
        base = WorkflowProgress(
            workflow_dir=tmp_path, status=WorkflowStatus.RUNNING, total_jobs=1, completed_jobs=0
        )
        # queued at 100, started at 142 (42s wait), stopped at 200 (58s execution).
        ds.apply_events_to_progress(
            base,
            [
                _event(
                    EventType.JOB_QUEUED,
                    timestamp=100.0,
                    job_id=7,
                    rule_name="align",
                    executor="aws-batch",
                    queue="gv-spot",
                    queued_at=100.0,
                )
            ],
        )
        ds.apply_events_to_progress(
            base, [_event(EventType.JOB_STARTED, timestamp=150.0, job_id=7, started_at=142.0)]
        )
        ds.apply_events_to_progress(
            base,
            [
                _event(
                    EventType.JOB_FINISHED,
                    timestamp=210.0,
                    job_id=7,
                    rule_name="align",
                    stopped_at=200.0,
                    duration=58.0,
                )
            ],
        )
        # _update_rule_stats_from_completions runs in poll_state; call it directly.
        ds._update_rule_stats_from_completions(base)

        rules = ds._workflow_state.rules
        # Execution duration learned is the 58s window, NOT 100s wall (dispatch->finish).
        stats = rules.get("align")
        assert stats is not None
        assert stats.aggregate.durations == [58.0]
        # Queue wait tracked separately, by rule and by queue.
        assert rules.queue_wait_for_rule("align") == 42.0
        assert rules.queue_wait_for_queue("gv-spot") == 42.0
        # Recorded exactly once despite both the event path and the registry-sweep
        # path running (stats_recorded de-dupes) — a double-record would still read
        # median 42, so assert the sample count directly.
        assert rules.queue_wait_count_for_rule("align") == 1


class TestQueueFieldPlumbing:
    """The `queue` field survives enrichment and JobInfo round trips."""

    def test_enrich_remote_fields_backfills_queue(
        self, snakemake_dir: object, tmp_path: object
    ) -> None:
        from snakesee.models import WorkflowProgress
        from snakesee.models import WorkflowStatus
        from snakesee.tui.data_source import WorkflowDataSource

        assert isinstance(tmp_path, Path)
        ds = WorkflowDataSource(workflow_dir=tmp_path, use_estimation=False)
        base = WorkflowProgress(
            workflow_dir=tmp_path, status=WorkflowStatus.RUNNING, total_jobs=1, completed_jobs=0
        )
        # A started remote job carries queue in the registry; the running JobInfo
        # is freshly built without it and must be backfilled by _enrich_remote_fields.
        result = ds.apply_events_to_progress(
            base,
            [
                _event(
                    EventType.JOB_STARTED,
                    timestamp=150.0,
                    job_id=7,
                    rule_name="align",
                    executor="aws-batch",
                    external_jobid="arn:aws:batch:us-east-1:1:job/abc",
                    queue="gv-spot",
                    started_at=142.0,
                )
            ],
        )
        running = [j for j in result.running_jobs if j.job_id == "7"]
        assert len(running) == 1
        assert running[0].queue == "gv-spot"

    def test_job_info_round_trip_preserves_queue(self) -> None:
        from snakesee.models import JobInfo
        from snakesee.state.job_registry import Job

        info = JobInfo(rule="align", job_id="7", queue="gv-spot", external_jobid="abc")
        job = Job.from_job_info(info)
        assert job.queue == "gv-spot"
        assert job.to_job_info().queue == "gv-spot"
