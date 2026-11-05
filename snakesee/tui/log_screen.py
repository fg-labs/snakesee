"""Full-screen viewer for a job's stdout / stderr / bpeek / rule logs."""

from __future__ import annotations

from pathlib import Path
from typing import ClassVar

from rich.text import Text
from textual import work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.binding import BindingType
from textual.screen import Screen
from textual.timer import Timer
from textual.widgets import Footer
from textual.widgets import Log
from textual.widgets import Static

from snakesee.joblogs import LogSource
from snakesee.joblogs import SourceKind
from snakesee.joblogs import fetch
from snakesee.joblogs import sources_for
from snakesee.model import Job
from snakesee.model import JobStatus

FILE_FOLLOW_INTERVAL = 2.0
BPEEK_FOLLOW_INTERVAL = 5.0  # bpeek queries the LSF master; don't hammer it


def default_source(job: Job, sources: list[LogSource]) -> LogSource | None:
    """bpeek for active jobs (cluster logs don't exist yet); otherwise stdout."""
    preferred = SourceKind.BPEEK if job.status is JobStatus.ACTIVE else SourceKind.STDOUT
    for src in sources:
        if src.kind is preferred:
            return src
    return sources[0] if sources else None


class LogScreen(Screen[None]):
    """Shows one log source of one job; switch sources with o/e/b/l."""

    CSS = """
    #log-header { height: auto; padding: 0 1; background: $panel; }
    #log-body { height: 1fr; }
    """

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("escape,q", "close", "Back"),
        Binding("o", "show('stdout')", "stdout"),
        Binding("e", "show('stderr')", "stderr"),
        Binding("b", "show('bpeek')", "bpeek"),
        Binding("l", "show('rule log')", "rule log"),
        Binding("r", "reload", "Reload"),
        Binding("f", "toggle_follow", "Follow"),
        Binding("g", "top", "Top", show=False),
        Binding("G", "bottom", "Bottom", show=False),
    ]

    def __init__(self, job: Job, workflow_dir: Path, kind: SourceKind | None = None) -> None:
        super().__init__()
        self.job = job
        self.workflow_dir = workflow_dir
        self.sources = sources_for(job, workflow_dir)
        self.source: LogSource | None = None
        if kind is not None:
            self.source = next((s for s in self.sources if s.kind is kind), None)
        if self.source is None:
            self.source = default_source(job, self.sources)
        self.follow = job.status is JobStatus.ACTIVE
        self.loading = False
        self.note = ""
        self._shown: tuple[LogSource | None, list[str]] = (None, [])
        self._timer: Timer | None = None

    def compose(self) -> ComposeResult:
        yield Static(id="log-header")
        yield Log(id="log-body", highlight=False)
        yield Footer()

    def on_mount(self) -> None:
        self.query_one(Log).focus()
        self._schedule_follow()
        self.action_reload()

    # --------------------------------------------------------------- loading
    def action_reload(self) -> None:
        if self.source is None:
            self._render_header()
            self.query_one(Log).write_line("[no logs known for this job]")
            return
        self.loading = True
        self._render_header()
        self._load(self.source)

    @work(thread=True, exclusive=True, group="log-load", exit_on_error=False)
    def _load(self, source: LogSource) -> None:
        lines = fetch(source)
        try:
            self.app.call_from_thread(self._show, source, lines)
        except RuntimeError:
            pass

    def _show(self, source: LogSource, lines: list[str]) -> None:
        self.loading = False
        if source != self.source:  # user switched source while loading
            self._render_header()
            return
        log = self.query_one(Log)
        prev_source, prev_lines = self._shown
        if prev_source == source and prev_lines == lines:
            self._render_header()
            return
        same = prev_source == source
        at_end = log.is_vertical_scroll_end
        scroll_y = log.scroll_y
        log.auto_scroll = not same or at_end
        log.clear()
        log.write_lines(lines)
        if same and not at_end:
            log.scroll_to(y=scroll_y, animate=False)
        self._shown = (source, lines)
        self._render_header()

    # ---------------------------------------------------------------- follow
    def _schedule_follow(self) -> None:
        if self._timer is not None:
            self._timer.stop()
            self._timer = None
        if self.follow and self.source is not None:
            interval = (
                BPEEK_FOLLOW_INTERVAL
                if self.source.kind is SourceKind.BPEEK
                else FILE_FOLLOW_INTERVAL
            )
            self._timer = self.set_interval(interval, self._follow_tick)

    def _follow_tick(self) -> None:
        if not self.loading:
            self.action_reload()

    def action_toggle_follow(self) -> None:
        self.follow = not self.follow
        self._schedule_follow()
        self._render_header()

    # --------------------------------------------------------------- actions
    def action_show(self, kind: str) -> None:
        k = SourceKind(kind)
        matching = [s for s in self.sources if s.kind is k]
        if not matching:
            self.note = f"no {kind} available for this job"
            self._render_header()
            return
        self.note = ""
        # Repeated presses cycle through multiple rule logs.
        if self.source in matching:
            idx = (matching.index(self.source) + 1) % len(matching)
            self.source = matching[idx]
        else:
            self.source = matching[0]
        self._schedule_follow()
        self.action_reload()

    def action_close(self) -> None:
        self.app.pop_screen()

    def action_top(self) -> None:
        self.query_one(Log).scroll_home(animate=False)

    def action_bottom(self) -> None:
        self.query_one(Log).scroll_end(animate=False)

    def update_job(self, job: Job) -> None:
        """Called by the app when this job's state changes (e.g. it finished)."""
        self.job = job
        self.sources = sources_for(job, self.workflow_dir)
        if self.source not in self.sources:
            self.source = default_source(job, self.sources)
            self._schedule_follow()
            self.action_reload()
        else:
            self._render_header()

    # ---------------------------------------------------------------- header
    def _render_header(self) -> None:
        job = self.job
        t = Text()
        t.append(f"job {job.jobid} ", style="bold")
        t.append(job.rule, style="bold cyan")
        if job.wildcards:
            t.append(f"  {job.wildcards}")
        t.append(f"  [{job.status.value}]", style="red" if job.status is JobStatus.FAILED else "")
        if job.external_id:
            t.append(f"  cluster id {job.external_id}", style="dim")
        if job.attempts > 1:
            t.append(f"  attempt {job.attempts}", style="yellow")
        if job.error_message:
            t.append(f"\n{job.error_message}", style="red")
        t.append("\n")
        for src in self.sources:
            style = "reverse bold" if src == self.source else "dim"
            t.append(f" {src.label} ", style=style)
            t.append(" ")
        if self.source is not None and self.source.path is not None:
            t.append(f" {self.source.path}", style="dim")
        t.append("\n")
        t.append(
            "loading…" if self.loading else "loaded", style="yellow" if self.loading else "dim"
        )
        t.append(f"   follow {'on' if self.follow else 'off'}", style="dim")
        if self.note:
            t.append(f"   {self.note}", style="yellow")
        self.query_one("#log-header", Static).update(t)
