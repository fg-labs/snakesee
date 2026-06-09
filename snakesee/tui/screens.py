"""Modal screens: HelpScreen, EasterEggScreen, JobLogScreen."""

from __future__ import annotations

from pathlib import Path
from typing import ClassVar

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.binding import BindingType
from textual.screen import ModalScreen
from textual.widgets import RichLog
from textual.widgets import Static

from snakesee.tui.renderables import make_easter_egg
from snakesee.tui.renderables import make_help


class HelpScreen(ModalScreen[None]):
    """Modal help overlay; any of escape/space/enter/q/? dismisses it."""

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("escape,space,enter,q,question_mark", "app.pop_screen")
    ]

    def compose(self) -> ComposeResult:
        """Yield a Static containing the rendered help panel."""
        yield Static(make_help(), id="help-content")


class EasterEggScreen(ModalScreen[None]):
    """Modal easter egg overlay; any of escape/space/enter/q dismisses it.

    The Fulcrum logo is rendered to fill the terminal at mount time and
    re-rendered on resize so it tracks window changes.
    """

    BINDINGS: ClassVar[list[BindingType]] = [Binding("escape,space,enter,q", "app.pop_screen")]
    DEFAULT_CSS = """
    EasterEggScreen {
        align: center middle;
    }
    EasterEggScreen #easter-content {
        width: 100%;
        height: 100%;
    }
    """

    def compose(self) -> ComposeResult:
        """Yield a Static that will be populated with the resized logo on mount."""
        yield Static(id="easter-content")

    def on_mount(self) -> None:
        """Render the logo sized to the current terminal."""
        self._render_logo()

    def on_resize(self) -> None:
        """Re-render the logo when the terminal size changes."""
        self._render_logo()

    def _render_logo(self) -> None:
        size = self.app.size
        self.query_one("#easter-content", Static).update(
            make_easter_egg(console_width=size.width, console_height=size.height)
        )


class JobLogScreen(ModalScreen[None]):
    """Modal log viewer for a single job; escape or q dismisses it.

    The global toggle keys (pause/estimation/wildcard/accessibility/refresh) are
    re-bound here to the app's actions so they keep working while a log is open —
    app-level BINDINGS don't fire under a modal screen, so the keys would otherwise
    be inert in log-viewing mode.
    """

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("escape,q", "app.pop_screen"),
        Binding("p", "app.toggle_pause", show=False),
        Binding("e", "app.toggle_estimation", show=False),
        Binding("w", "app.toggle_wildcard", show=False),
        Binding("a", "app.toggle_accessibility", show=False),
        Binding("r", "app.force_refresh", show=False),
        Binding("ctrl+r", "app.hard_refresh", show=False),
    ]

    def __init__(
        self,
        log_path: Path | None,
        lines: list[str],
        header_lines: list[Text] | None = None,
    ) -> None:
        """Initialize with the log path (shown as the border title) and tail lines.

        Args:
            log_path: Path to the job's log file, or None if unknown.
            lines: Tail lines (most recent at end) to render in the RichLog.
            header_lines: Optional styled lines rendered above the log (e.g. a
                remote job's external id and console/CloudWatch links). Rich
                ``Text`` rather than ``str`` so styles survive the markup-less
                RichLog. For a remote job with no local log file, these may be
                the only content.
        """
        super().__init__()
        self._log_path = log_path
        self._lines = lines
        self._header_lines = header_lines or []

    def compose(self) -> ComposeResult:
        """Yield a single RichLog widget that will be populated on mount."""
        log = RichLog(
            id="job-log",
            highlight=True,
            markup=False,
            wrap=False,
            auto_scroll=False,
        )
        if self._log_path is not None:
            log.border_title = str(self._log_path)
        elif self._header_lines:
            log.border_title = "remote job"
        else:
            log.border_title = "job log"
        yield log

    def on_mount(self) -> None:
        """Write the optional header and captured tail lines into the RichLog widget."""
        log = self.query_one("#job-log", RichLog)
        for header_line in self._header_lines:
            log.write(header_line)
        if self._header_lines and self._lines:
            log.write("")  # blank separator between the remote header and the log tail
        for tail_line in self._lines:
            log.write(tail_line)
