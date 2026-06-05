"""Rich renderables (header, progress bar, progress panel, summary, help, easter egg)."""

from datetime import datetime
from datetime import timedelta
from pathlib import Path
from typing import TYPE_CHECKING

from rich.console import Group
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

if TYPE_CHECKING:
    from rich.console import RenderableType

    from snakesee.models import JobInfo

from snakesee.constants import DEFAULT_REFRESH_RATE
from snakesee.constants import MIN_REFRESH_RATE
from snakesee.events import EventReader
from snakesee.formatting import format_duration
from snakesee.models import TimeEstimate
from snakesee.models import WorkflowProgress
from snakesee.models import WorkflowStatus
from snakesee.state.clock import get_clock
from snakesee.tui.accessibility import AccessibilityConfig
from snakesee.tui.accessibility import BarStyle

# Fulcrum Genomics brand colors
FG_BLUE = "#26a8e0"
FG_GREEN = "#38b44a"

# Fulcrum Genomics logo path (easter egg)
FG_LOGO_PATH = Path(__file__).parent.parent / "assets" / "logo.png"


def format_cost(usd: float) -> str:
    """Format a USD cost: 4 decimals under $1 (per-job costs are tiny), else 2."""
    return f"${usd:.4f}" if usd < 1 else f"${usd:,.2f}"


def make_remote_job_info(job: "JobInfo") -> list[Text]:
    """Build display lines describing a remote job's external identifier and links.

    For a job that ran on a remote executor (e.g. AWS Batch), this surfaces the
    external job id and, when enough information is available, deep links to the
    AWS console and CloudWatch logs. It degrades gracefully: a bare job id with
    no region yields just the id line; a local job yields no lines at all.

    Lines are Rich ``Text`` rather than ``str`` so styling (e.g. the dimmed
    termination-source parenthetical) survives the job-detail ``RichLog``,
    which deliberately disables markup to avoid misrendering log content.

    Args:
        job: The job to describe.

    Returns:
        A list of text lines (empty if the job has no external identifier).
    """
    if not job.external_jobid:
        return []

    from snakesee.remote_links import batch_console_url
    from snakesee.remote_links import cloudwatch_url

    label = job.executor or "remote"
    lines = [Text(f"{label} job: {job.external_jobid}")]

    if job.queue is not None:
        lines.append(Text(f"  queue:   {job.queue}"))

    # Queue wait is distinct from run time: it's how long the job waited for a node.
    queue_wait = job.queue_wait
    if queue_wait is not None:
        lines.append(Text(f"  queued for: {format_duration(queue_wait)}"))

    # Attempt > 1 means the job was retried/preempted; worth surfacing.
    if job.attempt is not None and job.attempt > 1:
        lines.append(Text(f"  attempt: {job.attempt}"))

    if job.exit_code is not None:
        lines.append(Text(f"  exit code: {job.exit_code}"))

    # Prefer the executor's structured termination classification (rendered with
    # confidence). Fall back to snakesee's own low-confidence string heuristic only
    # when no structured category arrived (e.g. an older executor).
    from snakesee.remote_termination import format_termination_marker

    marker = format_termination_marker(job.termination_category, job.termination_confidence)
    if job.termination_category is None and job.status_reason:
        from snakesee.remote_links import is_spot_interruption

        if is_spot_interruption(job.status_reason):
            marker = "possibly spot interrupted"
    if marker is not None:
        lines.append(Text(f"  {marker}"))

    if job.status_reason:
        lines.append(Text(f"  reason: {job.status_reason}"))

    if job.cost_estimate is not None:
        lines.append(Text(f"  est. cost: {format_cost(job.cost_estimate)}"))

    console = batch_console_url(job.external_jobid, region=job.region)
    if console is not None:
        lines.append(Text(f"  console: {console}"))

    logs = cloudwatch_url(job.log_stream, region=job.region)
    if logs is not None:
        lines.append(Text(f"  logs:    {logs}"))

    return lines


def _truncate_path(path: str, max_len: int) -> str:
    """Middle-truncate a path string so it fits within ``max_len`` characters.

    Keeps the leading and trailing path components (the most informative parts) and
    elides the middle with an ellipsis when the path is too long.

    Args:
        path: The (already resolved) path string to truncate.
        max_len: Maximum number of characters the result may occupy.

    Returns:
        The original path if it fits, else a middle-elided version no longer than ``max_len``.
    """
    if len(path) <= max_len:
        return path
    head = max_len // 2 - 1
    tail = max_len // 2
    return path[:head] + "…" + path[-tail:]


def make_header(
    progress: WorkflowProgress,
    workflow_path: str,
    paused: bool,
    event_reader: EventReader | None,
    max_path_len: int = 60,
) -> Panel:
    """Create the header panel with workflow path and status.

    Args:
        progress: Current workflow progress snapshot.
        workflow_path: Resolved absolute path to the monitored workflow directory.
            Resolving once at the call site keeps the per-frame cost to a string
            truncation rather than a filesystem ``resolve()``.
        paused: Whether auto-refresh is currently paused.
        event_reader: Active event reader if using event-based monitoring, else None.
        max_path_len: Maximum characters to spend on the path before middle-truncating,
            so a long path can't crowd out the status fields.

    Returns:
        A Rich Panel containing the header text.
    """
    status_styles = {
        WorkflowStatus.RUNNING: "bold green",
        WorkflowStatus.COMPLETED: "bold blue",
        WorkflowStatus.FAILED: "bold red",
        WorkflowStatus.INCOMPLETE: "bold yellow",
        WorkflowStatus.UNKNOWN: "bold yellow",
    }
    style = status_styles.get(progress.status, "bold white")

    header_text = Text()
    header_text.append("FULCRUM GENOMICS", style=f"bold {FG_BLUE}")
    header_text.append(" │ ", style="dim")
    header_text.append("Snakemake Monitor", style="bold white")
    header_text.append("  │  ", style="dim")
    header_text.append(_truncate_path(workflow_path, max_path_len), style="dim")
    header_text.append("  │  Status: ")
    header_text.append(progress.status.value.upper(), style=style)

    if progress.elapsed_seconds is not None:
        header_text.append("  │  Elapsed: ")
        header_text.append(format_duration(progress.elapsed_seconds), style=FG_BLUE)

    # Remote executors can have jobs queued (awaiting a node) but not yet running;
    # surface that count so a "running" workflow waiting on the queue is honest.
    queued_count = len(progress.queued_jobs_list)
    if queued_count > 0:
        header_text.append("  │  Queued: ")
        header_text.append(str(queued_count), style="bold yellow")

    # Estimated workflow cost so far (remote executors with cost estimation on).
    if progress.total_cost_estimate is not None:
        header_text.append("  │  Cost: ")
        header_text.append(f"~{format_cost(progress.total_cost_estimate)}", style=FG_GREEN)
        header_text.append(" (est)", style="dim")

    if paused:
        header_text.append("  │  ")
        header_text.append("PAUSED", style="bold yellow")

    # Monitoring method indicator
    header_text.append("  │  ")
    if event_reader is not None:
        header_text.append("⚡ Events", style="bold green")
    else:
        header_text.append("📄 Parsing", style="bold blue")

    return Panel(header_text, style="white on grey23", border_style=FG_BLUE, height=3)


def _in_flight_segment(
    progress: WorkflowProgress,
    accessibility: AccessibilityConfig,
) -> tuple[int, BarStyle]:
    """Return the count and style for the in-flight (yellow) progress segment.

    The yellow segment shows jobs that are mid-flight. Which jobs those are depends on
    whether the workflow is live or stopped:

    - A live workflow reports its currently-executing jobs via ``running_jobs``.
    - A stopped (``INCOMPLETE``) workflow has no live jobs, so we surface the jobs that
      were in progress when it was interrupted (``incomplete_jobs_list``). This preserves
      the post-mortem "which jobs were interrupted" distinction when browsing a dead run.

    Args:
        progress: Current workflow progress snapshot.
        accessibility: Visual encoding config controlling the segment style.

    Returns:
        A tuple of (number of in-flight jobs, the BarStyle to render them with).
    """
    if progress.status == WorkflowStatus.INCOMPLETE:
        return len(progress.incomplete_jobs_list), accessibility.incomplete
    return len(progress.running_jobs), accessibility.running


def make_progress_bar(
    progress: WorkflowProgress,
    width: int,
    accessibility: AccessibilityConfig,
) -> Text:
    """Create a colored progress bar showing succeeded/failed/in-flight/pending portions.

    Args:
        progress: Current workflow progress snapshot.
        width: Total character width of the bar.
        accessibility: Visual encoding config controlling bar characters.

    Returns:
        A Rich Text object representing the progress bar.
    """
    total = max(1, progress.total_jobs)
    succeeded = progress.completed_jobs
    failed = progress.failed_jobs
    in_flight, in_flight_style = _in_flight_segment(progress, accessibility)
    config = accessibility

    # Calculate widths for each segment. Clamp each to non-negative bounds within the
    # remaining width so a transient counter skew (counts briefly exceeding total) can
    # never produce a negative segment and under-render the bar.
    succeeded_width = min(width, max(0, int((succeeded / total) * width)))
    failed_width = min(width - succeeded_width, max(0, int((failed / total) * width)))
    in_flight_width = min(
        width - succeeded_width - failed_width, max(0, int((in_flight / total) * width))
    )
    pending_width = max(0, width - succeeded_width - failed_width - in_flight_width)

    # Build the bar with colored segments
    bar = Text()
    bar.append(config.succeeded.char * succeeded_width, style="green")
    bar.append(config.failed.char * failed_width, style="red")
    bar.append(in_flight_style.char * in_flight_width, style="yellow")
    bar.append(config.remaining.char * pending_width, style="dim")

    return bar


def make_progress_panel(
    progress: WorkflowProgress,
    estimate: TimeEstimate | None,
    use_estimation: bool,
    accessibility: AccessibilityConfig,
    console_width: int = 80,
) -> Panel:
    """Create the progress bar panel.

    Args:
        progress: Current workflow progress snapshot.
        estimate: Time estimate from the estimator, or None if unavailable.
        use_estimation: Whether time estimation is enabled.
        accessibility: Visual encoding config for the progress bar.
        console_width: Width of the terminal console in characters.

    Returns:
        A Rich Panel containing the progress bar, ETA, and legend.
    """
    total = max(1, progress.total_jobs)
    completed = progress.completed_jobs + progress.failed_jobs
    percent = (completed / total) * 100

    # Calculate bar width based on console width
    # Reserve space for: "Progress " (9) + " XX.X% " (7) + "(XXX/XXX jobs)" (~15) + borders (~4)
    bar_width = max(20, console_width - 40)

    # Create colored progress bar
    progress_bar = make_progress_bar(progress, bar_width, accessibility)

    # Progress text line
    progress_line = Text()
    progress_line.append("Progress ", style=f"bold {FG_BLUE}")
    progress_line.append(progress_bar)
    progress_line.append(f" {percent:5.1f}% ", style="bold")
    progress_line.append(f"({completed}/{total} jobs)", style="dim")

    # ETA text - handle different workflow states
    eta_parts = []
    if progress.status == WorkflowStatus.FAILED:
        eta_parts.append("[bold red]FAILED[/bold red]")
        if progress.failed_jobs > 0:
            eta_parts.append(f"[dim]({progress.failed_jobs} job(s) failed)[/dim]")
    elif progress.status == WorkflowStatus.INCOMPLETE:
        eta_parts.append("[bold yellow]INCOMPLETE[/bold yellow]")
        if progress.incomplete_jobs_list:
            eta_parts.append(
                f"[dim]({len(progress.incomplete_jobs_list)} job(s) were in progress)[/dim]"
            )
    elif progress.status == WorkflowStatus.COMPLETED:
        eta_parts.append("[bold blue]Complete[/bold blue]")
    elif estimate is not None:
        eta_parts.append(f"ETA: {estimate.format_eta()}")

        if estimate.seconds_remaining < float("inf") and estimate.seconds_remaining > 0:
            # Use the injectable clock so tests can pin completion-time formatting.
            now = datetime.fromtimestamp(get_clock().now()).astimezone()
            completion_dt = now + timedelta(seconds=estimate.seconds_remaining)
            tz_name = completion_dt.strftime("%Z") or "local"
            # Include the date (and always the timezone) when the ETA crosses midnight,
            # so an overnight estimate isn't mistaken for one later today.
            if completion_dt.date() == now.date():
                completion_str = completion_dt.strftime("%H:%M:%S")
            else:
                completion_str = completion_dt.strftime("%Y-%m-%d %H:%M:%S")
            eta_parts.append(f"({completion_str} {tz_name})")

        # Show estimation method and inferred cores for transparency
        method_info = estimate.method
        if estimate.inferred_cores is not None and estimate.inferred_cores > 1:
            method_info += f" cores≈{estimate.inferred_cores:.0f}"
        eta_parts.append(f"[dim][{method_info}][/dim]")
    elif not use_estimation:
        eta_parts.append("[dim]ETA: disabled[/dim]")

    eta_text = Text.from_markup("  ".join(eta_parts)) if eta_parts else Text("")

    # Legend for the progress bar, showing every non-zero segment so the bar is
    # informative even before any job completes (and regardless of accessibility mode).
    config = accessibility
    legend = Text()
    legend_parts: list[tuple[str, str, str]] = []
    if progress.completed_jobs > 0:
        legend_parts.append(
            (config.succeeded.char, "green", f"{progress.completed_jobs} {config.succeeded.label}")
        )
    if progress.failed_jobs > 0:
        legend_parts.append(
            (config.failed.char, "red", f"{progress.failed_jobs} {config.failed.label}")
        )
    in_flight, in_flight_style = _in_flight_segment(progress, config)
    if in_flight > 0:
        legend_parts.append(
            (in_flight_style.char, "yellow", f"{in_flight} {in_flight_style.label}")
        )
    pending = progress.pending_jobs
    if pending > 0:
        legend_parts.append((config.remaining.char, "dim", f"{pending} {config.remaining.label}"))
    show_legend = bool(legend_parts)
    if show_legend:
        legend.append("  (", style="dim")
        for i, (symbol, style, label) in enumerate(legend_parts):
            if i > 0:
                legend.append("  ", style="dim")
            legend.append(symbol, style=style)
            legend.append(f"={label}", style="dim")
        legend.append(")", style="dim")

    # Border color based on status (use FG colors for normal states)
    border_colors = {
        WorkflowStatus.RUNNING: FG_BLUE,
        WorkflowStatus.COMPLETED: FG_GREEN,
        WorkflowStatus.FAILED: "red",
        WorkflowStatus.INCOMPLETE: "yellow",
        WorkflowStatus.UNKNOWN: "yellow",
    }
    border_style = border_colors.get(progress.status, FG_BLUE)

    # Combine progress line with legend if present
    if show_legend:
        full_progress = Text()
        full_progress.append(progress_line)
        full_progress.append(legend)
        return Panel(
            Group(full_progress, eta_text),
            title="Progress",
            border_style=border_style,
        )

    return Panel(
        Group(progress_line, eta_text),
        title="Progress",
        border_style=border_style,
    )


def make_summary_footer(progress: WorkflowProgress) -> Panel:
    """Create the job status summary as a one-line footer panel.

    Args:
        progress: Current workflow progress snapshot.

    Returns:
        A Rich Panel containing the job status summary.
    """
    succeeded = progress.completed_jobs
    failed = progress.failed_jobs
    running = len(progress.running_jobs)
    incomplete = len(progress.incomplete_jobs_list)
    pending = progress.pending_jobs

    summary = Text()
    summary.append("Jobs: ", style="dim")
    summary.append(f"{succeeded}", style="green")
    summary.append(" succeeded", style="dim")
    summary.append("  │  ", style="dim")
    summary.append(f"{failed}", style="red" if failed > 0 else "dim")
    summary.append(" failed", style="dim")
    summary.append("  │  ", style="dim")
    summary.append(f"{running}", style="cyan" if running > 0 else "dim")
    summary.append(" running", style="dim")
    # Show incomplete count if there are incomplete jobs
    if incomplete > 0:
        summary.append("  │  ", style="dim")
        summary.append(f"{incomplete}", style="yellow")
        summary.append(" incomplete", style="dim")
    summary.append("  │  ", style="dim")
    summary.append(f"{pending}", style="yellow" if pending > 0 else "dim")
    summary.append(" pending", style="dim")

    border_style = "red" if failed > 0 else FG_BLUE
    return Panel(summary, border_style=border_style, padding=(0, 1))


def make_help() -> Panel:
    """Create the help overlay panel.

    Returns:
        A Rich Panel containing the keyboard shortcut reference.
    """
    help_text = Table(show_header=False, box=None, padding=(0, 2))
    help_text.add_column("Key", style="bold cyan")
    help_text.add_column("Action")

    help_text.add_row("", "[bold]General[/bold]")
    help_text.add_row("q", "Quit")
    help_text.add_row("?", "Toggle this help")
    help_text.add_row("p", "Pause/resume auto-refresh")
    help_text.add_row("e", "Toggle time estimation")
    help_text.add_row("w", "Toggle wildcard conditioning")
    help_text.add_row("a", "Toggle colorblind-accessible mode")
    help_text.add_row("r", "Force refresh")
    help_text.add_row("Ctrl+r", "Hard refresh (reload historical data)")
    help_text.add_row("", "")
    help_text.add_row("", "[bold]Refresh Rate[/bold]")
    help_text.add_row("- / +", "Decrease/increase by 0.5s")
    help_text.add_row("< / >", "Decrease/increase by 5s")
    help_text.add_row("0", f"Reset to default ({DEFAULT_REFRESH_RATE}s)")
    help_text.add_row("G", f"Set to minimum ({MIN_REFRESH_RATE}s, fastest)")
    help_text.add_row("", "")
    help_text.add_row("", "[bold]Layout & Filter[/bold]")
    help_text.add_row("Tab", "Cycle layout (full/compact/minimal)")
    help_text.add_row("/", "Filter rules by name")
    help_text.add_row("n / N", "Next/previous filter match")
    help_text.add_row("Esc", "Clear filter, return to latest log")
    help_text.add_row("", "")
    help_text.add_row("", "[bold]Log Navigation[/bold]")
    help_text.add_row("[ / ]", "View older/newer log (1 step)")
    help_text.add_row("{ / }", "View older/newer log (5 steps)")
    help_text.add_row("", "")
    help_text.add_row("", "[bold]Table Sorting[/bold]")
    help_text.add_row("s / S", "Cycle sort table (forward/backward)")
    help_text.add_row("1-4", "Sort by column (press again to reverse)")
    help_text.add_row("", "")
    help_text.add_row("", "[bold]Table Navigation (Enter to start)[/bold]")
    help_text.add_row("j / k", "Move down/up one row")
    help_text.add_row("g / G", "Jump to first/last row")
    help_text.add_row("Ctrl+d/u", "Move down/up half page")
    help_text.add_row("Ctrl+f/b", "Move down/up full page")
    help_text.add_row("Tab / S-Tab", "Cycle all tables")
    help_text.add_row("h / l", "Switch to left/right column table")
    help_text.add_row("Enter", "View job log (running/completions only)")
    help_text.add_row("Esc", "Exit table navigation")
    help_text.add_row("", "")
    help_text.add_row("", "[bold]Log Viewing (Enter on job)[/bold]")
    help_text.add_row("j / k", "Scroll down/up one line")
    help_text.add_row("g / G", "Jump to start/end of log")
    help_text.add_row("Ctrl+d/u", "Scroll down/up half page")
    help_text.add_row("Ctrl+f/b", "Scroll down/up full page")
    help_text.add_row("Esc", "Return to table navigation")

    from snakesee import __version__

    return Panel(
        help_text,
        title="[bold]Keyboard Shortcuts[/bold]",
        subtitle=f"Press any key to close [dim]│ snakesee v{__version__}[/dim]",
        border_style="cyan",
    )


def make_easter_egg(console_width: int = 80, console_height: int = 24) -> "RenderableType":
    """Create the Fulcrum Genomics easter egg renderable.

    Renders the bundled logo (``snakesee/assets/logo.png``, rasterized from the
    upstream SVG with a flat dark background) into the terminal at the given
    size using rich-pixels' half-block characters. Falls back to a text logo if
    the image is missing or unreadable.

    The image is downscaled directly to the target rich-pixels render size with
    bilinear resampling — bilinear blurs slightly more than LANCZOS, but the
    softer edges read better through 1px-wide half-block cells.

    Args:
        console_width: Width of the terminal console in characters.
        console_height: Height of the terminal console in lines.

    Returns:
        A Rich renderable (Group of centered pixels + dismiss hint) displaying
        the Fulcrum Genomics logo, or a text fallback.
    """
    from PIL import Image
    from rich.align import Align
    from rich.console import Group
    from rich_pixels import Pixels

    # Reserve one line at the bottom for the dismiss hint.
    image_height_chars = max(3, console_height - 2)
    # rich-pixels: 1 char = 1 px wide, 2 px tall (half-block).
    target_pixel_width = max(8, console_width)
    target_pixel_height = max(8, image_height_chars * 2)

    hint = Text("\n[ press any key to return ]", style=f"dim {FG_BLUE}", justify="center")

    if FG_LOGO_PATH.exists():
        try:
            source = Image.open(FG_LOGO_PATH).convert("RGB")

            # Pick the larger axis we can fully fit while preserving aspect ratio.
            img_ratio = source.width / source.height
            terminal_ratio = target_pixel_width / target_pixel_height
            if terminal_ratio > img_ratio:
                # Terminal is wider than image — height-bound.
                new_height = target_pixel_height
                new_width = int(new_height * img_ratio)
            else:
                # Terminal is taller than image — width-bound.
                new_width = target_pixel_width
                new_height = int(new_width / img_ratio)

            resized = source.resize((new_width, new_height), Image.Resampling.BILINEAR)
            pixels = Pixels.from_image(resized)
            return Group(Align.center(pixels, vertical="middle"), hint)
        except (OSError, ValueError, TypeError):
            pass  # Image missing or unreadable; fall through to text fallback.

    fallback = Text()
    fallback.append("\n")
    fallback.append("FULCRUM GENOMICS", style=f"bold {FG_BLUE}")
    fallback.append("\n")
    return Group(Align.center(fallback, vertical="middle"), hint)
