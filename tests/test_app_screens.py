"""Tests for modal screens (help, easter egg, job log) in SnakeseeApp."""

from pathlib import Path

from rich.text import Text

from snakesee.tui.app import SnakeseeApp
from snakesee.tui.screens import EasterEggScreen
from snakesee.tui.screens import HelpScreen
from snakesee.tui.screens import JobLogScreen


async def test_question_mark_opens_help(snakemake_dir: Path, tmp_path: Path) -> None:
    """Pressing ? pushes a HelpScreen, escape pops it."""
    app = SnakeseeApp(workflow_dir=tmp_path)
    async with app.run_test() as pilot:
        await pilot.press("question_mark")
        await pilot.pause()
        assert isinstance(app.screen, HelpScreen)
        await pilot.press("escape")
        await pilot.pause()
        assert type(app.screen).__name__ != "HelpScreen"
        await pilot.press("q")


async def test_easter_egg_f_then_g(snakemake_dir: Path, tmp_path: Path) -> None:
    """Pressing f then g (within the 2s window) pushes the EasterEggScreen."""
    app = SnakeseeApp(workflow_dir=tmp_path)
    async with app.run_test() as pilot:
        await pilot.press("f")
        await pilot.press("g")
        await pilot.pause()
        assert isinstance(app.screen, EasterEggScreen)
        await pilot.press("escape")
        await pilot.press("q")


async def test_easter_egg_g_alone_does_nothing(snakemake_dir: Path, tmp_path: Path) -> None:
    """Pressing g without a preceding f does not trigger the easter egg."""
    app = SnakeseeApp(workflow_dir=tmp_path)
    async with app.run_test() as pilot:
        await pilot.press("g")
        await pilot.pause()
        assert not isinstance(app.screen, EasterEggScreen)
        await pilot.press("q")


async def test_help_dismissed_by_q(snakemake_dir: Path, tmp_path: Path) -> None:
    """Pressing q while HelpScreen is open dismisses it (does not quit the app)."""
    app = SnakeseeApp(workflow_dir=tmp_path)
    async with app.run_test() as pilot:
        await pilot.press("question_mark")
        await pilot.pause()
        assert isinstance(app.screen, HelpScreen)
        await pilot.press("q")
        await pilot.pause()
        assert type(app.screen).__name__ != "HelpScreen"
        await pilot.press("q")


async def test_job_log_screen_renders_lines(tmp_path: Path) -> None:
    """JobLogScreen mounts and writes the supplied tail lines into its RichLog."""
    from textual.widgets import RichLog

    log_path = tmp_path / "job.log"
    lines = ["line one", "line two", "line three"]

    app = SnakeseeApp(workflow_dir=tmp_path)
    async with app.run_test() as pilot:
        screen = JobLogScreen(log_path, lines)
        app.push_screen(screen)
        await pilot.pause()
        assert app.screen is screen
        rich_log = screen.query_one("#job-log", RichLog)
        # Border title shows the log path so the user knows which job is rendered.
        assert rich_log.border_title == str(log_path)
        # Three writes happened on mount; line count reflects that.
        assert len(rich_log.lines) == len(lines)
        await pilot.press("escape")
        await pilot.pause()
        assert app.screen is not screen
        await pilot.press("q")


async def test_job_log_screen_default_title_when_no_path(tmp_path: Path) -> None:
    """When log_path is None, the border title falls back to a generic label."""
    from textual.widgets import RichLog

    app = SnakeseeApp(workflow_dir=tmp_path)
    async with app.run_test() as pilot:
        screen = JobLogScreen(None, [])
        app.push_screen(screen)
        await pilot.pause()
        rich_log = screen.query_one("#job-log", RichLog)
        assert rich_log.border_title == "job log"
        await pilot.press("escape")
        await pilot.press("q")


async def test_job_log_screen_renders_remote_header(tmp_path: Path) -> None:
    """A remote job with no local log shows only its header lines, titled 'remote job'."""
    from textual.widgets import RichLog

    header = [Text("aws-batch job: abc123"), Text("  console: https://example")]

    app = SnakeseeApp(workflow_dir=tmp_path)
    async with app.run_test() as pilot:
        screen = JobLogScreen(None, [], header_lines=header)
        app.push_screen(screen)
        await pilot.pause()
        rich_log = screen.query_one("#job-log", RichLog)
        assert rich_log.border_title == "remote job"
        assert len(rich_log.lines) == len(header)
        await pilot.press("escape")
        await pilot.press("q")


async def test_job_log_screen_header_plus_log_has_separator(tmp_path: Path) -> None:
    """Header lines and log tail are separated by a blank line when both present."""
    from textual.widgets import RichLog

    header = [Text("aws-batch job: abc123")]
    lines = ["log line one", "log line two"]

    app = SnakeseeApp(workflow_dir=tmp_path)
    async with app.run_test() as pilot:
        screen = JobLogScreen(tmp_path / "job.log", lines, header_lines=header)
        app.push_screen(screen)
        await pilot.pause()
        rich_log = screen.query_one("#job-log", RichLog)
        # header (1) + blank separator (1) + log lines (2) = 4
        assert len(rich_log.lines) == len(header) + 1 + len(lines)
        await pilot.press("escape")
        await pilot.press("q")


async def test_toggle_pause_works_in_log_viewing_mode(tmp_path: Path) -> None:
    """The global pause toggle (p) still fires while a JobLogScreen is open.

    App-level BINDINGS don't fire under a modal screen, so JobLogScreen re-binds the
    toggle keys to the app's actions (#60 parity).
    """
    app = SnakeseeApp(workflow_dir=tmp_path)
    async with app.run_test() as pilot:
        app.push_screen(JobLogScreen(tmp_path / "job.log", ["only line"]))
        await pilot.pause()
        assert isinstance(app.screen, JobLogScreen)
        assert app.paused == False  # noqa: E712
        await pilot.press("p")
        await pilot.pause()
        assert app.paused == True  # noqa: E712
        # The toggle must not dismiss the log screen.
        assert isinstance(app.screen, JobLogScreen)
        await pilot.press("escape")
        await pilot.press("q")


async def test_toggle_accessibility_works_in_log_viewing_mode(tmp_path: Path) -> None:
    """Pressing 'a' toggles accessibility mode without leaving the log screen."""
    from snakesee.tui.accessibility import ACCESSIBLE_CONFIG
    from snakesee.tui.accessibility import DEFAULT_CONFIG

    app = SnakeseeApp(workflow_dir=tmp_path)
    async with app.run_test() as pilot:
        app.push_screen(JobLogScreen(tmp_path / "job.log", ["only line"]))
        await pilot.pause()
        assert app._accessibility_config is DEFAULT_CONFIG
        await pilot.press("a")
        await pilot.pause()
        assert app._accessibility_config is ACCESSIBLE_CONFIG
        assert isinstance(app.screen, JobLogScreen)
        await pilot.press("escape")
        await pilot.press("q")
