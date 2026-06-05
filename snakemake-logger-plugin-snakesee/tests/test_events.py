"""Tests for event types and serialization."""

import json

import pytest

from snakemake_logger_plugin_snakesee.events import EventType, SnakeseeEvent


class TestEventType:
    """Tests for EventType enum."""

    def test_event_type_values(self) -> None:
        """Test that event types have expected string values."""
        assert EventType.WORKFLOW_STARTED.value == "workflow_started"
        assert EventType.JOB_SUBMITTED.value == "job_submitted"
        assert EventType.JOB_STARTED.value == "job_started"
        assert EventType.JOB_FINISHED.value == "job_finished"
        assert EventType.JOB_ERROR.value == "job_error"
        assert EventType.PROGRESS.value == "progress"

    def test_event_type_is_string(self) -> None:
        """Test that EventType inherits from str."""
        assert isinstance(EventType.PROGRESS, str)
        assert EventType.PROGRESS == "progress"


class TestSnakeseeEvent:
    """Tests for SnakeseeEvent dataclass."""

    def test_minimal_event(self) -> None:
        """Test creating event with only required fields."""
        event = SnakeseeEvent(
            event_type=EventType.PROGRESS,
            timestamp=1234567890.123,
        )
        assert event.event_type == EventType.PROGRESS
        assert event.timestamp == 1234567890.123
        assert event.job_id is None

    def test_event_is_immutable(self) -> None:
        """Test that events are frozen — mutation after creation must raise."""
        event = SnakeseeEvent(
            event_type=EventType.PROGRESS,
            timestamp=1234567890.123,
        )
        with pytest.raises(AttributeError):
            event.timestamp = 0.0  # type: ignore[misc]

    def test_full_job_event(self) -> None:
        """Test creating event with all job-related fields."""
        event = SnakeseeEvent(
            event_type=EventType.JOB_SUBMITTED,
            timestamp=1234567890.123,
            job_id=42,
            rule_name="align",
            wildcards={"sample": "A", "lane": "1"},
            threads=4,
            resources={"mem_mb": 8000},
            input_files=["input.fastq"],
            output_files=["output.bam"],
        )
        assert event.job_id == 42
        assert event.rule_name == "align"
        assert event.wildcards == {"sample": "A", "lane": "1"}
        assert event.threads == 4

    def test_to_json_minimal(self) -> None:
        """Test JSON serialization with minimal fields."""
        event = SnakeseeEvent(
            event_type=EventType.PROGRESS,
            timestamp=1234567890.123,
            completed_jobs=5,
            total_jobs=10,
        )
        json_str = event.to_json()
        data = json.loads(json_str)

        assert data["event_type"] == "progress"
        assert data["timestamp"] == 1234567890.123
        assert data["completed_jobs"] == 5
        assert data["total_jobs"] == 10
        # None values should be omitted
        assert "job_id" not in data
        assert "rule_name" not in data

    def test_to_json_full(self) -> None:
        """Test JSON serialization with all fields."""
        event = SnakeseeEvent(
            event_type=EventType.JOB_FINISHED,
            timestamp=1234567890.123,
            job_id=42,
            rule_name="align",
            wildcards={"sample": "A"},
            duration=100.5,
        )
        json_str = event.to_json()
        data = json.loads(json_str)

        assert data["event_type"] == "job_finished"
        assert data["job_id"] == 42
        assert data["rule_name"] == "align"
        assert data["wildcards"] == {"sample": "A"}
        assert data["duration"] == 100.5

    def test_from_json_minimal(self) -> None:
        """Test JSON deserialization with minimal fields."""
        json_str = '{"event_type":"progress","timestamp":1234567890.123}'
        event = SnakeseeEvent.from_json(json_str)

        assert event.event_type == EventType.PROGRESS
        assert event.timestamp == 1234567890.123
        assert event.job_id is None

    def test_from_json_full(self) -> None:
        """Test JSON deserialization with all fields."""
        json_str = (
            '{"event_type":"job_submitted","timestamp":1234567890.123,'
            '"job_id":42,"rule_name":"align","wildcards":{"sample":"A"}}'
        )
        event = SnakeseeEvent.from_json(json_str)

        assert event.event_type == EventType.JOB_SUBMITTED
        assert event.job_id == 42
        assert event.rule_name == "align"
        assert event.wildcards == {"sample": "A"}

    def test_from_json_ignores_unknown_keys(self) -> None:
        """Forward compat: unknown keys from a newer build must not raise."""
        json_str = (
            '{"event_type":"job_finished","timestamp":1.0,"job_id":7,'
            '"some_future_field":{"x":1},"another":42}'
        )
        event = SnakeseeEvent.from_json(json_str)
        assert event.event_type == EventType.JOB_FINISHED
        assert event.job_id == 7

    def test_roundtrip(self) -> None:
        """Test that serialization round-trips correctly."""
        original = SnakeseeEvent(
            event_type=EventType.JOB_ERROR,
            timestamp=1234567890.123,
            job_id=42,
            rule_name="sort",
            wildcards={"sample": "A"},
            duration=50.0,
            error_message="Sort failed: out of memory",
        )
        json_str = original.to_json()
        restored = SnakeseeEvent.from_json(json_str)

        assert restored.event_type == original.event_type
        assert restored.timestamp == original.timestamp
        assert restored.job_id == original.job_id
        assert restored.rule_name == original.rule_name
        assert restored.wildcards == original.wildcards
        assert restored.duration == original.duration
        assert restored.error_message == original.error_message

    def test_from_json_invalid_event_type(self) -> None:
        """Test that invalid event type raises ValueError."""
        json_str = '{"event_type":"invalid","timestamp":1234567890.123}'
        with pytest.raises(ValueError):
            SnakeseeEvent.from_json(json_str)

    def test_from_json_missing_required(self) -> None:
        """Test that missing required fields raises error."""
        json_str = '{"event_type":"progress"}'
        with pytest.raises(TypeError):
            SnakeseeEvent.from_json(json_str)
