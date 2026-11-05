"""Widgets with less/vim-style navigation keys."""

from __future__ import annotations

from typing import Any
from typing import ClassVar

from textual.binding import Binding
from textual.binding import BindingType
from textual.containers import VerticalScroll
from textual.widget import Widget
from textual.widgets import DataTable
from textual.widgets import Log


class JobTable(DataTable[Any]):
    """DataTable with j/k, ctrl+d/ctrl+u (half page), ctrl+f/ctrl+b (page), g/G."""

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("j", "cursor_down", show=False),
        Binding("k", "cursor_up", show=False),
        Binding("ctrl+d", "move(0.5)", show=False),
        Binding("ctrl+u", "move(-0.5)", show=False),
        Binding("ctrl+f", "move(1.0)", show=False),
        Binding("ctrl+b", "move(-1.0)", show=False),
        Binding("g", "first", show=False),
        Binding("G", "last", show=False),
    ]

    def _page_rows(self) -> int:
        header = 1 if self.show_header else 0
        return max(1, self.scrollable_content_region.height - header)

    def action_move(self, pages: float) -> None:
        if self.row_count == 0:
            return
        delta = int(self._page_rows() * pages) or (1 if pages > 0 else -1)
        row = min(max(self.cursor_row + delta, 0), self.row_count - 1)
        self.move_cursor(row=row, animate=False)

    def action_first(self) -> None:
        self.move_cursor(row=0, animate=False)

    def action_last(self) -> None:
        self.move_cursor(row=max(0, self.row_count - 1), animate=False)


LESS_SCROLL_BINDINGS: list[BindingType] = [
    Binding("j", "scroll_lines(1)", show=False),
    Binding("k", "scroll_lines(-1)", show=False),
    Binding("ctrl+d", "scroll_pages(0.5)", show=False),
    Binding("ctrl+u", "scroll_pages(-0.5)", show=False),
    Binding("ctrl+f,space", "scroll_pages(1.0)", show=False),
    Binding("ctrl+b", "scroll_pages(-1.0)", show=False),
    Binding("g", "top", show=False),
    Binding("G", "bottom", show=False),
]


def _scroll_lines(self: Widget, lines: int) -> None:
    self.scroll_relative(y=lines, animate=False)


def _scroll_pages(self: Widget, pages: float) -> None:
    height = max(1, self.scrollable_content_region.height)
    self.scroll_relative(y=int(height * pages) or (1 if pages > 0 else -1), animate=False)


def _top(self: Widget) -> None:
    self.scroll_home(animate=False)


def _bottom(self: Widget) -> None:
    self.scroll_end(animate=False)


# Shared as plain functions rather than a Widget mixin: a mixin in the bases would
# break Textual's type selectors (e.g. ``query_one(Log)``).
class PagerLog(Log):
    """Log viewer with less-style scrolling. Mouse-drag selects; ctrl+c copies."""

    BINDINGS: ClassVar[list[BindingType]] = LESS_SCROLL_BINDINGS
    action_scroll_lines = _scroll_lines
    action_scroll_pages = _scroll_pages
    action_top = _top
    action_bottom = _bottom


class PagerScroll(VerticalScroll):
    """Scrollable container with less-style keys."""

    BINDINGS: ClassVar[list[BindingType]] = LESS_SCROLL_BINDINGS
    action_scroll_lines = _scroll_lines
    action_scroll_pages = _scroll_pages
    action_top = _top
    action_bottom = _bottom
