"""Tests for the remote-job-state adapter and the handler's emit() hook."""

import logging
from typing import Any

from snakemake_logger_plugin_snakesee.events import EventType
from snakemake_logger_plugin_snakesee.remote_adapter import (
    NEUTRAL_WIRE_KEY,
    WIRE_KEY,
    event_from_payload,
    payload_from_record,
)


def _payload(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "schema_version": 1,
        "kind": "state",
        "jobid": 7,
        "executor": "aws-batch",
        "phase": "running",
    }
    base.update(overrides)
    return base


class TestEventFromPayload:
    """Translation of wire payloads into SnakeseeEvents."""

    def test_running_maps_to_job_started(self) -> None:
        event = event_from_payload(_payload(phase="running"), timestamp=142.0)
        assert event is not None
        assert event.event_type == EventType.JOB_STARTED
        assert event.job_id == 7
        assert event.executor == "aws-batch"
        assert event.timestamp == 142.0

    def test_queued_maps_to_job_queued(self) -> None:
        event = event_from_payload(_payload(phase="queued", queued_at=100.0), timestamp=100.0)
        assert event is not None
        assert event.event_type == EventType.JOB_QUEUED
        assert event.queued_at == 100.0

    def test_succeeded_maps_to_finished_with_duration(self) -> None:
        event = event_from_payload(
            _payload(phase="succeeded", started_at=142.0, stopped_at=200.0, exit_code=0),
            timestamp=200.0,
        )
        assert event is not None
        assert event.event_type == EventType.JOB_FINISHED
        assert event.duration == 58.0
        assert event.exit_code == 0

    def test_failed_maps_to_error_with_reason(self) -> None:
        event = event_from_payload(
            _payload(phase="failed", status_reason="OOM killed", exit_code=137),
            timestamp=200.0,
        )
        assert event is not None
        assert event.event_type == EventType.JOB_ERROR
        assert event.error_message == "OOM killed"
        assert event.status_reason == "OOM killed"
        assert event.exit_code == 137

    def test_external_id_and_region_carried(self) -> None:
        event = event_from_payload(
            _payload(external_jobid="arn:...:job/abc", region="us-east-1", queue="gv-spot"),
            timestamp=1.0,
        )
        assert event is not None
        assert event.external_jobid == "arn:...:job/abc"
        assert event.region == "us-east-1"
        assert event.queue == "gv-spot"

    def test_accepts_neutral_v2_schema(self) -> None:
        # The backend-neutral Tier-D shape (schema_version 2) has identical fields
        # and must translate to the same event as v1.
        event = event_from_payload(_payload(schema_version=2, phase="running"), timestamp=1.0)
        assert event is not None
        assert event.event_type == EventType.JOB_STARTED

    def test_v1_and_v2_produce_equal_events(self) -> None:
        # The core Tier-D guarantee: same fields under v1 vs v2 yield identical
        # events (only the schema_version gate differs). Locks out any future
        # version-keyed divergence in event_from_payload.
        fields = dict(
            phase="succeeded",
            external_jobid="arn:aws:batch:us-east-1:1:job/abc",
            started_at=142.0,
            stopped_at=200.0,
            exit_code=0,
            queue="gv-spot",
            region="us-east-1",
        )
        v1 = event_from_payload(_payload(schema_version=1, **fields), timestamp=5.0)
        v2 = event_from_payload(_payload(schema_version=2, **fields), timestamp=5.0)
        assert v1 == v2

    def test_unsupported_version_returns_none(self) -> None:
        assert event_from_payload(_payload(schema_version=3), timestamp=1.0) is None

    def test_unknown_phase_returns_none(self) -> None:
        assert event_from_payload(_payload(phase="bogus"), timestamp=1.0) is None

    def test_non_mapping_returns_none(self) -> None:
        assert event_from_payload(None, timestamp=1.0) is None
        assert event_from_payload("nope", timestamp=1.0) is None


class _FakeRecord:
    """Minimal stand-in for a logging record carrying a remote payload."""

    def __init__(self, **attrs: Any) -> None:
        for key, value in attrs.items():
            setattr(self, key, value)


class TestPayloadFromRecord:
    """payload_from_record finds the payload under either wire key."""

    def test_finds_legacy_key(self) -> None:
        rec = _FakeRecord(**{WIRE_KEY: _payload()})
        assert payload_from_record(rec) is not None

    def test_finds_neutral_key(self) -> None:
        rec = _FakeRecord(**{NEUTRAL_WIRE_KEY: _payload(schema_version=2)})
        assert payload_from_record(rec) is not None

    def test_prefers_neutral_over_legacy(self) -> None:
        neutral = _payload(schema_version=2, jobid=2)
        legacy = _payload(schema_version=1, jobid=1)
        rec = _FakeRecord(**{NEUTRAL_WIRE_KEY: neutral, WIRE_KEY: legacy})
        assert payload_from_record(rec) is neutral

    def test_none_when_absent(self) -> None:
        assert payload_from_record(_FakeRecord(event=None)) is None


class TestHandlerEmitHook:
    """The handler routes remote-state records through the adapter."""

    def _make_handler(self, tmp_path: Any) -> Any:
        from snakemake_logger_plugin_snakesee.handler import LogHandler

        handler = LogHandler.__new__(LogHandler)  # bypass plugin __init__ machinery

        class _CapturingWriter:
            def __init__(self) -> None:
                self.events: list[Any] = []

            def write(self, event: Any) -> None:
                self.events.append(event)

        handler._writer = _CapturingWriter()  # type: ignore[attr-defined]
        return handler

    def test_remote_record_emits_event(self, tmp_path: Any) -> None:
        handler = self._make_handler(tmp_path)
        record = _FakeRecord(**{WIRE_KEY: _payload(phase="running")})
        handler.emit(record)
        assert len(handler._writer.events) == 1
        assert handler._writer.events[0].event_type == EventType.JOB_STARTED

    def test_neutral_v2_record_emits_event(self, tmp_path: Any) -> None:
        # A record carrying the neutral Tier-D key is handled with no other change.
        handler = self._make_handler(tmp_path)
        record = _FakeRecord(**{NEUTRAL_WIRE_KEY: _payload(schema_version=2, phase="running")})
        handler.emit(record)
        assert len(handler._writer.events) == 1
        assert handler._writer.events[0].event_type == EventType.JOB_STARTED

    def test_malformed_remote_record_emits_nothing(self, tmp_path: Any) -> None:
        handler = self._make_handler(tmp_path)
        record = _FakeRecord(**{WIRE_KEY: _payload(schema_version=999)})
        handler.emit(record)
        assert handler._writer.events == []

    def test_record_without_marker_falls_through(self, tmp_path: Any) -> None:
        # A record with neither the remote marker nor an event should be ignored.
        handler = self._make_handler(tmp_path)
        record = _FakeRecord(event=None)
        handler.emit(record)
        assert handler._writer.events == []

    def test_remote_record_does_not_require_event_attr(self, tmp_path: Any, caplog: Any) -> None:
        handler = self._make_handler(tmp_path)
        # No `event` attribute at all — emit must not raise.
        record = _FakeRecord(**{WIRE_KEY: _payload(phase="queued")})
        with caplog.at_level(logging.ERROR):
            handler.emit(record)
        assert len(handler._writer.events) == 1
