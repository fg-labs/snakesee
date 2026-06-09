"""Tests for the termination value-set and confidence-aware rendering."""

from __future__ import annotations

from rich.text import Text

from snakesee import remote_termination
from snakesee.remote_termination import CONFIDENCE_HIGH
from snakesee.remote_termination import CONFIDENCE_LOW
from snakesee.remote_termination import SOURCE_AWS_INSTANCE_STATE
from snakesee.remote_termination import SOURCE_EVENTBRIDGE
from snakesee.remote_termination import SOURCE_STATUS_REASON
from snakesee.remote_termination import TERM_OOM
from snakesee.remote_termination import TERM_SPOT
from snakesee.remote_termination import TERM_UNKNOWN
from snakesee.remote_termination import format_termination_marker
from snakesee.remote_termination import format_termination_source


class TestContractValues:
    """Lock the wire strings of the published value set (the contract surface)."""

    def test_category_wire_strings(self) -> None:
        assert remote_termination.TERM_SPOT == "spot"
        assert remote_termination.TERM_OOM == "oom"
        assert remote_termination.TERM_TIMEOUT == "timeout"
        assert remote_termination.TERM_NODE_FAILURE == "node_failure"
        assert remote_termination.TERM_CANCELLED == "cancelled"
        assert remote_termination.TERM_DEPENDENCY == "dependency"
        assert remote_termination.TERM_IMAGE_PULL == "image_pull"
        assert remote_termination.TERM_UNKNOWN == "unknown"

    def test_confidence_and_source_wire_strings(self) -> None:
        assert remote_termination.CONFIDENCE_HIGH == "high"
        assert remote_termination.CONFIDENCE_LOW == "low"
        assert remote_termination.SOURCE_EVENTBRIDGE == "eventbridge"
        assert remote_termination.SOURCE_AWS_INSTANCE_STATE == "aws_instance_state"
        assert remote_termination.SOURCE_EXECUTOR_HEURISTIC == "executor_heuristic"
        assert remote_termination.SOURCE_STATUS_REASON == "status_reason"


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
        assert any("⚠ spot interrupted" in line.plain for line in lines)
        assert not any("possibly" in line.plain for line in lines)

    def test_structured_suppresses_string_heuristic(self) -> None:
        # When a high-confidence classification AND a spot-matching status_reason
        # both exist, exactly one (firm) marker renders — the heuristic is suppressed.
        from snakesee.models import JobInfo
        from snakesee.tui.renderables import make_remote_job_info

        job = JobInfo(
            rule="align",
            job_id="7",
            external_jobid="abc",
            status_reason="Spot interruption: capacity reclaimed",  # would match heuristic
            termination_category=TERM_SPOT,
            termination_confidence=CONFIDENCE_HIGH,
        )
        lines = make_remote_job_info(job)
        markers = [line for line in lines if "spot interrupted" in line.plain]
        assert len(markers) == 1
        assert "⚠ spot interrupted" in markers[0].plain
        assert "possibly" not in markers[0].plain

    def test_explicit_unknown_suppresses_string_heuristic(self) -> None:
        # An explicit TERM_UNKNOWN is still a structured classification: the executor
        # looked and could not tell. The string heuristic must not override it.
        from snakesee.models import JobInfo
        from snakesee.tui.renderables import make_remote_job_info

        job = JobInfo(
            rule="align",
            job_id="7",
            external_jobid="abc",
            status_reason="Spot interruption: capacity reclaimed",  # would match heuristic
            termination_category=TERM_UNKNOWN,
            termination_confidence=CONFIDENCE_HIGH,
        )
        lines = make_remote_job_info(job)
        assert not any("spot interrupted" in line.plain for line in lines)

    def test_from_job_info_round_trip(self) -> None:
        from snakesee.models import JobInfo
        from snakesee.state.job_registry import Job

        info = JobInfo(
            rule="align",
            job_id="7",
            external_jobid="abc",
            termination_category=TERM_SPOT,
            termination_source="aws_instance_state",
            termination_confidence=CONFIDENCE_HIGH,
        )
        job = Job.from_job_info(info)
        assert job.termination_category == TERM_SPOT
        assert job.termination_source == "aws_instance_state"
        assert job.termination_confidence == CONFIDENCE_HIGH
        back = job.to_job_info()
        assert back.termination_category == TERM_SPOT
        assert back.termination_source == "aws_instance_state"
        assert back.termination_confidence == CONFIDENCE_HIGH

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
        assert any("possibly spot interrupted" in line.plain for line in lines)

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
        assert event.termination_source == "aws_instance_state"
        assert event.termination_confidence == "high"


class TestFormatTerminationSource:
    """format_termination_source renders provenance as a 'via ...' phrase."""

    def test_aws_instance_state_label(self) -> None:
        assert format_termination_source(SOURCE_AWS_INSTANCE_STATE) == "via EC2 instance state"

    def test_status_reason_label(self) -> None:
        assert format_termination_source(SOURCE_STATUS_REASON) == "via status-reason text"

    def test_unknown_source_falls_back_to_raw(self) -> None:
        # Contract-reserved but unemitted values render via the raw fallback
        # (forward-compat with executors that send new sources).
        assert format_termination_source("executor_heuristic") == "via executor heuristic"
        assert format_termination_source("eventbridge") == "via eventbridge"

    def test_none_source_yields_none(self) -> None:
        assert format_termination_source(None) is None

    def test_empty_source_yields_none(self) -> None:
        # An empty string must not render a dangling "via ".
        assert format_termination_source("") is None


class TestSourceDisplay:
    """The termination source renders as a dimmed parenthetical on the marker line."""

    @staticmethod
    def _marker_line(lines: list[Text]) -> Text:
        marker = next((line for line in lines if "spot interrupted" in line.plain), None)
        assert marker is not None, f"no marker line in {[line.plain for line in lines]}"
        return marker

    def test_structured_source_rendered_dimmed(self) -> None:
        from snakesee.models import JobInfo
        from snakesee.tui.renderables import make_remote_job_info

        job = JobInfo(
            rule="align",
            job_id="7",
            external_jobid="abc",
            termination_category=TERM_SPOT,
            termination_source=SOURCE_AWS_INSTANCE_STATE,
            termination_confidence=CONFIDENCE_HIGH,
        )
        marker = self._marker_line(make_remote_job_info(job))
        assert marker.plain == "  ⚠ spot interrupted (via EC2 instance state)"
        # Exactly the parenthetical (with its leading space) is dimmed.
        dim_spans = [span for span in marker.spans if span.style == "dim"]
        assert len(dim_spans) == 1
        assert marker.plain[dim_spans[0].start : dim_spans[0].end] == " (via EC2 instance state)"

    def test_unknown_source_renders_raw_fallback(self) -> None:
        from snakesee.models import JobInfo
        from snakesee.tui.renderables import make_remote_job_info

        job = JobInfo(
            rule="align",
            job_id="7",
            external_jobid="abc",
            termination_category=TERM_SPOT,
            termination_source=SOURCE_EVENTBRIDGE,  # contract-reserved, no friendly label
            termination_confidence=CONFIDENCE_HIGH,
        )
        marker = self._marker_line(make_remote_job_info(job))
        assert marker.plain == "  ⚠ spot interrupted (via eventbridge)"

    def test_no_source_no_parenthetical(self) -> None:
        from snakesee.models import JobInfo
        from snakesee.tui.renderables import make_remote_job_info

        job = JobInfo(
            rule="align",
            job_id="7",
            external_jobid="abc",
            termination_category=TERM_SPOT,
            termination_confidence=CONFIDENCE_HIGH,
        )
        marker = self._marker_line(make_remote_job_info(job))
        assert marker.plain == "  ⚠ spot interrupted"
        assert not marker.spans

    def test_orphaned_source_not_rendered(self) -> None:
        # A source with no usable category (no marker) is provenance with
        # nothing to attribute — deliberately dropped (spec §2).
        from snakesee.models import JobInfo
        from snakesee.tui.renderables import make_remote_job_info

        job = JobInfo(
            rule="align",
            job_id="7",
            external_jobid="abc",
            termination_source=SOURCE_AWS_INSTANCE_STATE,
        )
        lines = make_remote_job_info(job)
        assert not any("via" in line.plain for line in lines)

    def test_unknown_category_orphans_source(self) -> None:
        from snakesee.models import JobInfo
        from snakesee.tui.renderables import make_remote_job_info

        job = JobInfo(
            rule="align",
            job_id="7",
            external_jobid="abc",
            termination_category=TERM_UNKNOWN,
            termination_source=SOURCE_AWS_INSTANCE_STATE,
            termination_confidence=CONFIDENCE_HIGH,
        )
        lines = make_remote_job_info(job)
        assert not any("via" in line.plain for line in lines)

    def test_reader_fallback_labeled_status_reason_text(self) -> None:
        # snakesee's own string heuristic inspects status_reason, so it carries
        # the same label as the executor's status_reason source — by design.
        from snakesee.models import JobInfo
        from snakesee.tui.renderables import make_remote_job_info

        job = JobInfo(
            rule="align",
            job_id="7",
            external_jobid="abc",
            status_reason="Spot interruption: capacity reclaimed",
        )
        marker = self._marker_line(make_remote_job_info(job))
        assert marker.plain == "  possibly spot interrupted (via status-reason text)"
        assert any(span.style == "dim" for span in marker.spans)

    def test_structured_source_wins_over_fallback_eligible_reason(self) -> None:
        # Both a structured classification AND a spot-matching status_reason:
        # the structured source labels the marker; the heuristic never runs.
        from snakesee.models import JobInfo
        from snakesee.tui.renderables import make_remote_job_info

        job = JobInfo(
            rule="align",
            job_id="7",
            external_jobid="abc",
            status_reason="Spot interruption: capacity reclaimed",
            termination_category=TERM_SPOT,
            termination_source=SOURCE_AWS_INSTANCE_STATE,
            termination_confidence=CONFIDENCE_HIGH,
        )
        marker = self._marker_line(make_remote_job_info(job))
        assert marker.plain == "  ⚠ spot interrupted (via EC2 instance state)"
