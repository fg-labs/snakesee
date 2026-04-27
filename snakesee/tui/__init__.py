"""Terminal User Interface for Snakemake workflow monitoring (Textual).

This package provides a Textual-based TUI for monitoring Snakemake workflows.
The main entry point is `SnakeseeApp`, which is also exported as
`WorkflowMonitorTUI` for backward compatibility with existing callers.

Module structure:
- app.py: SnakeseeApp (the main Textual application) and bindings/reactives
- data_source.py: WorkflowDataSource (pure-data layer: polling, estimator,
  event/log readers, filter/sort helpers, log tail, tool-progress cache)
- renderables.py: Rich renderables (header, progress, summary, help, easter egg)
- tables.py: DataTable row builders and sort helpers
- screens.py: Modal screens (HelpScreen, EasterEggScreen, JobLogScreen)
- accessibility.py: Visual encoding configs for colorblind users
"""

from snakesee.constants import DEFAULT_REFRESH_RATE
from snakesee.constants import MAX_REFRESH_RATE
from snakesee.constants import MIN_REFRESH_RATE
from snakesee.tui.accessibility import ACCESSIBLE_CONFIG
from snakesee.tui.accessibility import DEFAULT_CONFIG
from snakesee.tui.accessibility import AccessibilityConfig
from snakesee.tui.app import LayoutMode
from snakesee.tui.app import SnakeseeApp
from snakesee.tui.renderables import FG_BLUE
from snakesee.tui.renderables import FG_GREEN

# Backward-compat alias: callers (CLI, tests) construct WorkflowMonitorTUI(...)
WorkflowMonitorTUI = SnakeseeApp

__all__ = [
    "ACCESSIBLE_CONFIG",
    "AccessibilityConfig",
    "DEFAULT_CONFIG",
    "DEFAULT_REFRESH_RATE",
    "FG_BLUE",
    "FG_GREEN",
    "MAX_REFRESH_RATE",
    "MIN_REFRESH_RATE",
    "LayoutMode",
    "SnakeseeApp",
    "WorkflowMonitorTUI",
]
