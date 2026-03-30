"""Accessibility configuration for colorblind-friendly rendering.

Provides alternative visual encodings so that progress bar status
can be distinguished without relying on color perception alone.
"""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class BarStyle:
    """Character and label for a single progress bar segment.

    Attributes:
        char: The character used to fill the segment.
        label: Human-readable label for the legend.
    """

    char: str
    label: str


@dataclass(frozen=True, slots=True)
class AccessibilityConfig:
    """Visual encoding configuration for progress bar rendering.

    Controls which characters are used for each segment of the progress bar
    and whether the legend is always displayed.

    Attributes:
        succeeded: Style for completed/succeeded jobs.
        failed: Style for failed jobs.
        remaining: Style for remaining/pending jobs.
        incomplete: Style for incomplete jobs (workflow interrupted).
        show_legend: If True, always show the legend (not just on failure).
    """

    succeeded: BarStyle
    failed: BarStyle
    running: BarStyle
    remaining: BarStyle
    incomplete: BarStyle
    show_legend: bool


DEFAULT_CONFIG = AccessibilityConfig(
    succeeded=BarStyle(char="\u2588", label="succeeded"),
    failed=BarStyle(char="\u2588", label="failed"),
    running=BarStyle(char="\u2588", label="running"),
    remaining=BarStyle(char="\u2591", label="remaining"),
    incomplete=BarStyle(char="\u2591", label="incomplete"),
    show_legend=False,
)

ACCESSIBLE_CONFIG = AccessibilityConfig(
    succeeded=BarStyle(char="=", label="succeeded"),
    failed=BarStyle(char="X", label="failed"),
    running=BarStyle(char=">", label="running"),
    remaining=BarStyle(char="\u00b7", label="remaining"),
    incomplete=BarStyle(char="?", label="incomplete"),
    show_legend=True,
)
