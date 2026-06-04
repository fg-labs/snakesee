"""Tests for the termination value-set and confidence-aware rendering."""

from __future__ import annotations

from snakesee.remote_termination import CONFIDENCE_HIGH
from snakesee.remote_termination import CONFIDENCE_LOW
from snakesee.remote_termination import TERM_OOM
from snakesee.remote_termination import TERM_SPOT
from snakesee.remote_termination import TERM_UNKNOWN
from snakesee.remote_termination import format_termination_marker


class TestFormatTerminationMarker:
    def test_high_confidence_spot(self) -> None:
        assert format_termination_marker(TERM_SPOT, CONFIDENCE_HIGH) == "⚠ spot interrupted"

    def test_low_confidence_spot_is_tentative(self) -> None:
        # A guessed classification must read as tentative, not asserted.
        assert format_termination_marker(TERM_SPOT, CONFIDENCE_LOW) == "possibly spot interrupted"

    def test_high_confidence_oom(self) -> None:
        assert format_termination_marker(TERM_OOM, CONFIDENCE_HIGH) == "⚠ out of memory"

    def test_unknown_category_has_no_marker(self) -> None:
        # Don't render a marker for an unclassified termination.
        assert format_termination_marker(TERM_UNKNOWN, CONFIDENCE_HIGH) is None

    def test_none_category_has_no_marker(self) -> None:
        assert format_termination_marker(None, CONFIDENCE_HIGH) is None

    def test_missing_confidence_defaults_to_tentative(self) -> None:
        # If confidence is unknown, be conservative and phrase it tentatively.
        assert format_termination_marker(TERM_SPOT, None) == "possibly spot interrupted"

    def test_unrecognized_category_passthrough_lowercased(self) -> None:
        # A category snakesee doesn't have a friendly label for still renders
        # (forward-compat with executors that send new categories).
        assert format_termination_marker("preempted", CONFIDENCE_HIGH) == "⚠ preempted"


class TestTerminationEndToEnd:
    """Structured termination flows event -> registry -> JobInfo -> render."""

    def test_apply_event_populates_termination(self) -> None:
        from snakesee.events import EventType
        from snakesee.events import SnakeseeEvent
        from snakesee.state.job_registry import JobRegistry

        reg = JobRegistry()
        reg.apply_event(
            SnakeseeEvent(
                event_type=EventType.JOB_ERROR,
                timestamp=200.0,
                job_id=7,
                rule_name="align",
                executor="aws-batch",
                status_reason="Host EC2 (instance i-abc) terminated.",
                termination_category=TERM_SPOT,
                termination_source="aws_instance_state",
                termination_confidence=CONFIDENCE_HIGH,
            )
        )
        job = reg.get_by_job_id("7")
        assert job is not None
        assert job.termination_category == TERM_SPOT
        assert job.termination_confidence == CONFIDENCE_HIGH
        assert job.to_job_info().termination_source == "aws_instance_state"

    def test_high_confidence_structured_renders_firm_marker(self) -> None:
        from snakesee.models import JobInfo
        from snakesee.tui.renderables import make_remote_job_info

        # statusReason has no "spot" token, so the string heuristic would miss it,
        # but the high-confidence structured classification renders firmly.
        job = JobInfo(
            rule="align",
            job_id="7",
            external_jobid="abc",
            status_reason="Host EC2 (instance i-abc) terminated.",
            termination_category=TERM_SPOT,
            termination_confidence=CONFIDENCE_HIGH,
        )
        lines = make_remote_job_info(job)
        assert any("⚠ spot interrupted" in line for line in lines)
        assert not any("possibly" in line for line in lines)

    def test_string_heuristic_fallback_is_tentative(self) -> None:
        from snakesee.models import JobInfo
        from snakesee.tui.renderables import make_remote_job_info

        # No structured category: fall back to the string heuristic, phrased tentatively.
        job = JobInfo(
            rule="align",
            job_id="7",
            external_jobid="abc",
            status_reason="Spot interruption: capacity reclaimed",
        )
        lines = make_remote_job_info(job)
        assert any("possibly spot interrupted" in line for line in lines)

    def test_plugin_adapter_passes_termination_through(self) -> None:
        # The plugin's reader-side event also carries the fields after JSON round trip.
        import orjson

        from snakesee.events import SnakeseeEvent

        payload = {
            "event_type": "job_error",
            "timestamp": 200.0,
            "job_id": 7,
            "termination_category": "spot",
            "termination_source": "aws_instance_state",
            "termination_confidence": "high",
        }
        event = SnakeseeEvent.from_json(orjson.dumps(payload))
        assert event.termination_category == "spot"
        assert event.termination_confidence == "high"
