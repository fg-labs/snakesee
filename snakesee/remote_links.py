"""Builders for AWS console / CloudWatch deep links from remote job identifiers.

These are pure string helpers: given an external AWS Batch job id (or ARN) and,
where available, a region and log stream, they build the URLs a user can open to
inspect the job in the AWS console or its logs in CloudWatch. Everything is
``None``-tolerant — when there isn't enough information to build a meaningful
link (most importantly, no region), the builder returns ``None`` and the caller
simply shows the raw identifier instead.
"""

from __future__ import annotations

import re
from urllib.parse import quote

# arn:aws:batch:<region>:<account>:job/<job-id>
_ARN_RE = re.compile(r"^arn:aws[\w-]*:batch:(?P<region>[^:]+):[^:]*:job/(?P<job_id>.+)$")

# CloudWatch's URL fragment double-encodes path separators: "/" becomes "$252F".
_DEFAULT_BATCH_LOG_GROUP = "/aws/batch/job"


def batch_job_id_from(external_jobid: str | None) -> str | None:
    """Return the bare AWS Batch job id, extracting it from an ARN if needed.

    Args:
        external_jobid: A Batch job ARN, a bare job id, or None.

    Returns:
        The bare job id, or None if the input is empty.
    """
    if not external_jobid:
        return None
    candidate = external_jobid.strip()
    if not candidate:
        return None
    match = _ARN_RE.match(candidate)
    if match:
        return match.group("job_id").strip()
    return candidate


def region_from_arn(external_jobid: str | None) -> str | None:
    """Return the region embedded in a Batch ARN, or None if not an ARN.

    Args:
        external_jobid: A Batch job ARN, a bare job id, or None.

    Returns:
        The region string, or None if the input is not an ARN.
    """
    if not external_jobid:
        return None
    match = _ARN_RE.match(external_jobid)
    return match.group("region") if match else None


def batch_console_url(external_jobid: str | None, region: str | None = None) -> str | None:
    """Build the AWS Batch console job-detail URL.

    The region is taken from the explicit argument, falling back to a region
    embedded in the ARN. Without a region (e.g. a bare job id from the metadata
    DB), no console URL can be built.

    Args:
        external_jobid: A Batch job ARN or bare job id.
        region: Explicit region, or None to derive it from an ARN.

    Returns:
        A console URL, or None if there is not enough information.
    """
    job_id = batch_job_id_from(external_jobid)
    if job_id is None:
        return None
    resolved_region = region or region_from_arn(external_jobid)
    if not resolved_region:
        return None
    return (
        f"https://{resolved_region}.console.aws.amazon.com/batch/home"
        f"?region={resolved_region}#jobs/detail/{job_id}"
    )


def cloudwatch_url(
    log_stream: str | None,
    region: str | None,
    log_group: str = _DEFAULT_BATCH_LOG_GROUP,
) -> str | None:
    """Build the CloudWatch Logs URL for a Batch job's log stream.

    Args:
        log_stream: The CloudWatch log stream name (e.g. from describe_jobs).
        region: The AWS region; required to build the URL.
        log_group: The log group; defaults to the Batch default ``/aws/batch/job``.

    Returns:
        A CloudWatch Logs URL, or None if region or stream are missing.
    """
    if not log_stream or not region:
        return None
    # The CloudWatch console fragment encodes the group/stream twice: each path
    # separator and reserved character is percent-encoded, then the percent signs
    # themselves are encoded ("%" -> "$25"), so "/" ends up as "$252F".
    group_enc = _cw_encode(log_group)
    stream_enc = _cw_encode(log_stream)
    return (
        f"https://{region}.console.aws.amazon.com/cloudwatch/home"
        f"?region={region}#logsV2:log-groups/log-group/{group_enc}"
        f"/log-events/{stream_enc}"
    )


def _cw_encode(value: str) -> str:
    """Apply CloudWatch's double percent-encoding to a path component."""
    once = quote(value, safe="")
    return once.replace("%", "$25")


# Substrings AWS Batch / ECS use in statusReason when a Spot instance is reclaimed.
_SPOT_MARKERS = ("spot interruption", "spot instance", "ec2 spot")
# A reclaimed Spot host shows up as the EC2 instance being terminated out from under
# the job; pair the host-terminated phrasing with a spot hint to avoid false positives.
_HOST_TERMINATED = "host ec2"


def is_spot_interruption(status_reason: str | None) -> bool:
    """Heuristically detect whether a failure was a Spot-instance interruption.

    Args:
        status_reason: The backend-provided status reason string, if any.

    Returns:
        True if the reason looks like a Spot reclamation / interruption.
    """
    if not status_reason:
        return False
    lowered = status_reason.lower()
    if any(marker in lowered for marker in _SPOT_MARKERS):
        return True
    # "Host EC2 (instance i-...) terminated." with a spot hint nearby. Note this
    # conservatively misses real Spot reclamations where Batch phrases the host
    # termination without the word "spot" — we accept that false negative to avoid
    # misclassifying ordinary host failures. A structured signal from the executor
    # would supersede this heuristic.
    return _HOST_TERMINATED in lowered and "spot" in lowered
