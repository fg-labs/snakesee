"""Adapter from the remote-job-state wire contract to snakesee events.

A remote executor (AWS Batch, SLURM, ...) attaches a structured payload to one
of its log records under the well-known key :data:`WIRE_KEY`. This adapter is
the *single* place in the plugin that knows the wire shape: it validates the
payload and translates it into a :class:`SnakeseeEvent` enriched with remote
fields. Keeping that knowledge here means the rest of the plugin — and all of
snakesee downstream — is insulated from the contract's exact wire format, which
makes evolving the contract (or swapping it for a future standard upstream
event) a localized change.

This mirrors :mod:`snakesee.remote` on the reader side. The two are deliberately
independent (the plugin must not depend on snakesee); the shared contract is the
spec, and each side implements it.
"""

from __future__ import annotations

from typing import Any, Final

from snakemake_logger_plugin_snakesee.events import EventType, SnakeseeEvent

# Log-record attribute the executor uses to carry the payload.
#
# Two keys are recognised so the consumer is forward-compatible without any
# change to the rest of the plugin or to snakesee:
#   - WIRE_KEY ("snakesee_remote", schema_version 1) is the snakesee-specific
#     contract the AWS Batch executor emits today.
#   - NEUTRAL_WIRE_KEY ("remote_job_state", schema_version 2) is the
#     backend-neutral shape proposed for upstream standardisation (Tier D). When
#     Snakemake's executor interface emits a standard remote-job-state record,
#     it will use this key; the payload shape is otherwise identical, so only
#     this adapter needs to know about it.
WIRE_KEY: Final[str] = "snakesee_remote"
NEUTRAL_WIRE_KEY: Final[str] = "remote_job_state"

# All record attributes that may carry a remote-state payload, newest first.
WIRE_KEYS: Final[tuple[str, ...]] = (NEUTRAL_WIRE_KEY, WIRE_KEY)

# Wire schema versions this plugin understands.
SUPPORTED_SCHEMA_VERSIONS: Final[frozenset[int]] = frozenset({1, 2})

# Normalized phase string -> snakesee event type.
_PHASE_TO_EVENT: Final[dict[str, EventType]] = {
    "queued": EventType.JOB_QUEUED,
    "running": EventType.JOB_STARTED,
    "succeeded": EventType.JOB_FINISHED,
    "failed": EventType.JOB_ERROR,
}


def payload_from_record(record: Any) -> Any | None:
    """Extract a remote-state payload from a log record, or None if absent.

    Checks the recognised wire keys (neutral first, then the snakesee-specific
    one) so a record carrying either contract is handled. This is the single
    place that knows *where* on the record the payload lives.
    """
    for key in WIRE_KEYS:
        payload = getattr(record, key, None)
        if payload is not None:
            return payload
    return None


def event_from_payload(payload: Any, timestamp: float) -> SnakeseeEvent | None:
    """Translate a remote-state wire payload into a SnakeseeEvent.

    Never raises on bad input: malformed or unsupported payloads return None so
    a faulty or future executor cannot break event emission.

    Args:
        payload: The decoded wire object (expected to be a mapping).
        timestamp: Event timestamp to stamp on the resulting event.

    Returns:
        An enriched SnakeseeEvent, or None if the payload is not a supported,
        well-formed contract instance.
    """
    if not isinstance(payload, dict):
        return None

    if payload.get("schema_version") not in SUPPORTED_SCHEMA_VERSIONS:
        return None

    jobid = payload.get("jobid")
    executor = payload.get("executor")
    phase = payload.get("phase")
    if jobid is None or executor is None or phase is None:
        return None
    if isinstance(jobid, bool) or not isinstance(jobid, int):
        return None

    event_type = _PHASE_TO_EVENT.get(str(phase))
    if event_type is None:
        return None

    started_at = _opt_float(payload.get("started_at"))
    stopped_at = _opt_float(payload.get("stopped_at"))

    # Prefer an explicit execution window for duration; only meaningful on a
    # terminal event where both ends are known.
    duration: float | None = None
    if event_type in (EventType.JOB_FINISHED, EventType.JOB_ERROR):
        if started_at is not None and stopped_at is not None:
            duration = max(0.0, stopped_at - started_at)

    status_reason = _opt_str(payload.get("status_reason"))

    return SnakeseeEvent(
        event_type=event_type,
        timestamp=timestamp,
        job_id=jobid,
        executor=str(executor),
        external_jobid=_opt_str(payload.get("external_jobid")),
        remote_status=_opt_str(payload.get("remote_status")),
        queued_at=_opt_float(payload.get("queued_at")),
        started_at=started_at,
        stopped_at=stopped_at,
        attempt=_opt_int(payload.get("attempt")),
        exit_code=_opt_int(payload.get("exit_code")),
        status_reason=status_reason,
        queue=_opt_str(payload.get("queue")),
        log_stream=_opt_str(payload.get("log_stream")),
        region=_opt_str(payload.get("region")),
        duration=duration,
        # Surface the failure reason as the error message for JOB_ERROR events.
        error_message=status_reason if event_type == EventType.JOB_ERROR else None,
    )


def _opt_str(value: Any) -> str | None:
    """Coerce an optional value to str, preserving None."""
    return None if value is None else str(value)


def _opt_int(value: Any) -> int | None:
    """Coerce an optional value to int, returning None on failure."""
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _opt_float(value: Any) -> float | None:
    """Coerce an optional value to float, returning None on failure."""
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
