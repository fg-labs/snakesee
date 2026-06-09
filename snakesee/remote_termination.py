"""Normalized job-termination classification: value set and rendering.

When a remote job dies, *why* it died (Spot reclamation, OOM, timeout, node
failure, ...) is a fact the backend knows with varying certainty. snakesee does
not classify terminations itself — that happens upstream (the executor, which
has the cloud context) and arrives on the event as a structured triple:

    termination_category   — what kind of death (this module's TERM_* values)
    termination_source     — where the classification came from (provenance)
    termination_confidence — how sure the producer was (high / low)

snakesee's only job is to *render* that classification honestly: a high-confidence
classification is stated firmly, a low-confidence (heuristic) one is phrased
tentatively, so the UI never asserts something a lower layer merely guessed.

This module is the reader-side half of the contract; the executor defines the
same value strings independently (the two packages can't share code).
"""

from __future__ import annotations

from typing import Final

# Termination categories (wire strings).
TERM_SPOT: Final[str] = "spot"
TERM_OOM: Final[str] = "oom"
TERM_TIMEOUT: Final[str] = "timeout"
TERM_NODE_FAILURE: Final[str] = "node_failure"
TERM_CANCELLED: Final[str] = "cancelled"
TERM_DEPENDENCY: Final[str] = "dependency"
TERM_IMAGE_PULL: Final[str] = "image_pull"
TERM_UNKNOWN: Final[str] = "unknown"

# Confidence levels (wire strings).
CONFIDENCE_HIGH: Final[str] = "high"
CONFIDENCE_LOW: Final[str] = "low"

# Classification provenance (wire strings) — most to least authoritative.
SOURCE_EVENTBRIDGE: Final[str] = "eventbridge"
SOURCE_AWS_INSTANCE_STATE: Final[str] = "aws_instance_state"
SOURCE_EXECUTOR_HEURISTIC: Final[str] = "executor_heuristic"
SOURCE_STATUS_REASON: Final[str] = "status_reason"

# Friendly labels for known categories. Unknown categories fall back to the raw
# string so a newer executor's category still renders (forward compatibility).
_CATEGORY_LABELS: Final[dict[str, str]] = {
    TERM_SPOT: "spot interrupted",
    TERM_OOM: "out of memory",
    TERM_TIMEOUT: "timed out",
    TERM_NODE_FAILURE: "node failure",
    TERM_CANCELLED: "cancelled",
    TERM_DEPENDENCY: "dependency failed",
    TERM_IMAGE_PULL: "image pull failed",
}

# Friendly labels for known sources — only the values the AWS Batch executor's
# classifier actually emits today. Anything else (including contract-reserved
# values like "eventbridge" and "executor_heuristic") falls back to the raw
# string, the same forward-compat pattern the category labels use.
_SOURCE_LABELS: Final[dict[str, str]] = {
    SOURCE_AWS_INSTANCE_STATE: "EC2 instance state",
    SOURCE_STATUS_REASON: "status-reason text",
}


def format_termination_source(source: str | None) -> str | None:
    """Render the provenance of a termination classification as a "via ..." phrase.

    Args:
        source: The classification provenance (a SOURCE_* value, or a
            forward-compat string). None or empty yields no phrase, so a
            missing source can't render a dangling "via ".

    Returns:
        A "via <source label>" string, or None when there is no source to attribute.
    """
    if not source:
        return None
    return f"via {_SOURCE_LABELS.get(source, source.replace('_', ' '))}"


def format_termination_marker(category: str | None, confidence: str | None) -> str | None:
    """Render a one-line termination marker, phrased by confidence.

    Args:
        category: The termination category (a TERM_* value, or a forward-compat
            string). None or TERM_UNKNOWN yields no marker.
        confidence: CONFIDENCE_HIGH renders a firm marker; anything else
            (including None) renders a tentative one.

    Returns:
        A marker string, or None when there is nothing meaningful to assert.
    """
    if not category or category == TERM_UNKNOWN:
        return None
    label = _CATEGORY_LABELS.get(category, category.replace("_", " "))
    if confidence == CONFIDENCE_HIGH:
        return f"⚠ {label}"
    return f"possibly {label}"
