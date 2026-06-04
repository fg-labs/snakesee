"""Tests for remote-executor fields on SnakeseeEvent and forward-compat parsing."""

import orjson

from snakesee.events import EventType
from snakesee.events import SnakeseeEvent


class TestJobQueuedEventType:
    """The new JOB_QUEUED event type."""

    def test_job_queued_value(self) -> None:
        """JOB_QUEUED has the expected wire string."""
        assert EventType.JOB_QUEUED.value == "job_queued"


class TestRemoteFields:
    """Remote-executor enrichment fields on SnakeseeEvent."""

    def test_remote_fields_default_none(self) -> None:
        """Remote fields are optional and default to None."""
        event = SnakeseeEvent(event_type=EventType.JOB_STARTED, timestamp=1.0)
        assert event.external_jobid is None
        assert event.executor is None
        assert event.queued_at is None
        assert event.started_at is None
        assert event.stopped_at is None
        assert event.attempt is None
        assert event.exit_code is None
        assert event.status_reason is None
        assert event.queue is None
        assert event.region is None
        assert event.log_stream is None

    def test_remote_fields_round_trip_from_json(self) -> None:
        """Remote fields survive a JSON round trip."""
        payload = {
            "event_type": "job_started",
            "timestamp": 142.0,
            "job_id": 7,
            "rule_name": "align",
            "executor": "aws-batch",
            "external_jobid": "arn:aws:batch:us-east-1:1:job/abc",
            "remote_status": "RUNNING",
            "queued_at": 100.0,
            "started_at": 142.0,
            "queue": "graviton-spot",
            "region": "us-east-1",
        }
        event = SnakeseeEvent.from_json(orjson.dumps(payload))
        assert event.event_type == EventType.JOB_STARTED
        assert event.executor == "aws-batch"
        assert event.external_jobid == "arn:aws:batch:us-east-1:1:job/abc"
        assert event.remote_status == "RUNNING"
        assert event.queued_at == 100.0
        assert event.started_at == 142.0
        assert event.queue == "graviton-spot"
        assert event.region == "us-east-1"


class TestUnknownKeyTolerance:
    """from_json must ignore unknown keys for forward compatibility."""

    def test_unknown_keys_dropped(self) -> None:
        """A future field snakesee doesn't know about must not crash parsing."""
        payload = {
            "event_type": "job_finished",
            "timestamp": 200.0,
            "job_id": 7,
            "duration": 58.0,
            "some_future_field": {"nested": "value"},
            "another_unknown": 123,
        }
        event = SnakeseeEvent.from_json(orjson.dumps(payload))
        assert event.event_type == EventType.JOB_FINISHED
        assert event.job_id == 7
        assert event.duration == 58.0
