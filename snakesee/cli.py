"""Command-line interface for Snakemake workflow monitoring."""

import logging
import sys
from pathlib import Path
from typing import Literal
from typing import NoReturn

import defopt  # type: ignore[import-untyped]
from rich.console import Console

from snakesee.estimator import TimeEstimator
from snakesee.exceptions import InvalidProfileError
from snakesee.exceptions import ProfileNotFoundError
from snakesee.exceptions import WorkflowNotFoundError
from snakesee.models import WorkflowStatus
from snakesee.models import format_duration
from snakesee.parser import parse_workflow_state
from snakesee.profile import DEFAULT_PROFILE_NAME
from snakesee.profile import export_profile_from_metadata
from snakesee.profile import find_profile
from snakesee.profile import load_profile

logger = logging.getLogger(__name__)


def _validate_workflow_dir(workflow_dir: Path) -> Path:
    """
    Validate that the workflow directory contains a .snakemake directory.

    Args:
        workflow_dir: Path to the workflow directory.

    Returns:
        The validated path.

    Raises:
        WorkflowNotFoundError: If .snakemake directory doesn't exist.
    """
    snakemake_dir = workflow_dir / ".snakemake"
    if not snakemake_dir.exists():
        raise WorkflowNotFoundError(
            workflow_dir,
            f"No .snakemake directory found in {workflow_dir}",
        )
    return workflow_dir


def _handle_workflow_error(e: WorkflowNotFoundError) -> NoReturn:
    """Print a workflow error and exit."""
    console = Console(stderr=True)
    console.print(f"[red]Error:[/red] {e.message}")
    sys.exit(1)


def watch(
    workflow_dir: Path = Path("."),
    *,
    refresh: float = 2.0,
    no_estimate: bool = False,
    profile: Path | None = None,
    wildcard_timing: bool = True,
    weighting_strategy: Literal["index", "time"] = "index",
    half_life_logs: int = 10,
    half_life_days: float = 7.0,
    colorblind: bool = False,
) -> None:
    """
    Watch a Snakemake workflow in real-time with a TUI dashboard.

    Passively monitors the .snakemake/ directory without requiring
    special flags when running snakemake. Press 'q' to quit the TUI.

    Args:
        workflow_dir: Path to workflow directory containing .snakemake/.
        refresh: Refresh interval in seconds (0.5 to 60.0).
        no_estimate: Disable time estimation from historical data.
        profile: Optional path to a timing profile (.snakesee-profile.json)
                 for bootstrapping estimates. If not specified, will auto-discover.
        wildcard_timing: Use wildcard conditioning for estimates (estimate per
                         sample/batch). Enabled by default. Toggle with 'w' key in TUI.
        weighting_strategy: Strategy for weighting historical timing data.
                           "index" (default) - weight by run index, ideal for active
                           development where each run may fix issues.
                           "time" - weight by wall-clock time, better for stable pipelines.
        half_life_logs: Half-life in number of runs for index-based weighting.
                       After this many runs, a run's weight is halved. Default: 10.
                       Only used when weighting_strategy="index".
        half_life_days: Half-life in days for time-based weighting.
                       After this many days, a run's weight is halved. Default: 7.0.
                       Only used when weighting_strategy="time".
        colorblind: Use colorblind-accessible mode with distinct characters in
                    progress bars. Can also be toggled with 'a' key in TUI.
    """
    try:
        workflow_dir = _validate_workflow_dir(workflow_dir)
    except WorkflowNotFoundError as e:
        _handle_workflow_error(e)

    # Validate refresh rate
    if refresh < 0.5 or refresh > 60.0:
        console = Console(stderr=True)
        console.print("[red]Error:[/red] Refresh rate must be between 0.5 and 60.0 seconds")
        sys.exit(1)

    # Validate half-life parameters
    if half_life_logs <= 0:
        console = Console(stderr=True)
        console.print("[red]Error:[/red] half-life-logs must be positive")
        sys.exit(1)

    if half_life_days <= 0:
        console = Console(stderr=True)
        console.print("[red]Error:[/red] half-life-days must be positive")
        sys.exit(1)

    # Load profile if specified or auto-discover
    profile_path = profile or find_profile(workflow_dir)

    from snakesee.tui import WorkflowMonitorTUI
    from snakesee.tui.accessibility import ACCESSIBLE_CONFIG

    accessibility_config = ACCESSIBLE_CONFIG if colorblind else None

    tui = WorkflowMonitorTUI(
        workflow_dir=workflow_dir,
        refresh_rate=refresh,
        use_estimation=not no_estimate,
        profile_path=profile_path,
        use_wildcard_conditioning=wildcard_timing,
        weighting_strategy=weighting_strategy,
        half_life_logs=half_life_logs,
        half_life_days=half_life_days,
        accessibility_config=accessibility_config,
    )
    tui.run()


def status(
    workflow_dir: Path = Path("."),
    *,
    no_estimate: bool = False,
    profile: Path | None = None,
) -> None:
    """
    Show a one-time status snapshot (non-interactive).

    Useful for scripting or quick checks.

    Args:
        workflow_dir: Path to workflow directory containing .snakemake/.
        no_estimate: Disable time estimation from historical data.
        profile: Optional path to a timing profile for bootstrapping estimates.
    """
    try:
        workflow_dir = _validate_workflow_dir(workflow_dir)
    except WorkflowNotFoundError as e:
        _handle_workflow_error(e)
    console = Console()

    # Parse workflow state
    progress = parse_workflow_state(workflow_dir)

    # Status indicator
    status_colors = {
        WorkflowStatus.RUNNING: "green",
        WorkflowStatus.COMPLETED: "blue",
        WorkflowStatus.FAILED: "red",
        WorkflowStatus.INCOMPLETE: "yellow",
        WorkflowStatus.UNKNOWN: "yellow",
    }
    status_color = status_colors.get(progress.status, "white")
    console.print(f"Status: [{status_color}]{progress.status.value.upper()}[/{status_color}]")

    # Progress
    console.print(
        f"Progress: {progress.completed_jobs}/{progress.total_jobs} "
        f"({progress.percent_complete:.1f}%)"
    )

    # Elapsed time
    if progress.elapsed_seconds is not None:
        console.print(f"Elapsed: {format_duration(progress.elapsed_seconds)}")

    # Running jobs
    if progress.running_jobs:
        console.print(f"Running: {len(progress.running_jobs)} jobs")

    # Incomplete jobs (jobs that were in progress when workflow was interrupted)
    if progress.incomplete_jobs_list:
        count = len(progress.incomplete_jobs_list)
        console.print(f"[yellow]Incomplete: {count} job(s) were in progress[/yellow]")
        for job in progress.incomplete_jobs_list[:5]:  # Show up to 5
            if job.output_file:
                try:
                    rel_path = job.output_file.relative_to(workflow_dir)
                    console.print(f"  [dim]- {rel_path}[/dim]")
                except ValueError:
                    console.print(f"  [dim]- {job.output_file}[/dim]")
        if len(progress.incomplete_jobs_list) > 5:
            console.print(f"  [dim]... and {len(progress.incomplete_jobs_list) - 5} more[/dim]")

    # Time estimation
    if not no_estimate:
        estimator = TimeEstimator()
        snakemake_dir = workflow_dir / ".snakemake"
        metadata_dir = snakemake_dir / "metadata"

        # Load from profile if specified or auto-discover
        profile_path = profile or find_profile(workflow_dir)
        if profile_path is not None and profile_path.exists():
            try:
                loaded_profile = load_profile(profile_path)
                estimator.rule_stats = loaded_profile.to_rule_stats()
            except (ProfileNotFoundError, InvalidProfileError) as e:
                # Log the error for debugging, but fall back to metadata silently
                logger.debug("Failed to load profile %s: %s", profile_path, e)

        # Merge with live metadata (live data takes precedence for recent runs)
        if metadata_dir.exists():
            estimator.load_from_metadata(metadata_dir)

        estimate = estimator.estimate_remaining(progress)
        console.print(f"ETA: {estimate.format_eta()}")

    # Log file
    if progress.log_file is not None:
        console.print(f"Log: {progress.log_file}")


def profile_export(
    workflow_dir: Path = Path("."),
    *,
    output: Path | None = None,
    merge: bool = False,
) -> None:
    """
    Export timing profile from workflow metadata.

    Creates a portable JSON file containing historical timing data that can
    be shared across machines or used to bootstrap estimates for new runs.

    Args:
        workflow_dir: Path to workflow directory containing .snakemake/.
        output: Output file path. Defaults to .snakesee-profile.json in workflow_dir.
        merge: If output file exists, merge with existing data instead of replacing.
    """
    try:
        workflow_dir = _validate_workflow_dir(workflow_dir)
    except WorkflowNotFoundError as e:
        _handle_workflow_error(e)
    console = Console()

    metadata_dir = workflow_dir / ".snakemake" / "metadata"
    if not metadata_dir.exists():
        console.print("[red]Error:[/red] No metadata directory found")
        sys.exit(1)

    output_path = output or (workflow_dir / DEFAULT_PROFILE_NAME)

    try:
        exported = export_profile_from_metadata(
            metadata_dir=metadata_dir,
            output_path=output_path,
            merge_existing=merge,
        )

        rule_count = len(exported.rules)
        total_samples = sum(rp.sample_count for rp in exported.rules.values())

        console.print(f"[green]Profile exported:[/green] {output_path}")
        console.print(f"  Rules: {rule_count}")
        console.print(f"  Total samples: {total_samples}")

        if merge and output_path.exists():
            console.print("  [dim](merged with existing profile)[/dim]")

    except InvalidProfileError as e:
        console.print(f"[red]Error:[/red] {e.message}")
        sys.exit(1)
    except OSError as e:
        console.print(f"[red]Error:[/red] Failed to export profile: {e}")
        sys.exit(1)


def profile_show(
    profile_path: Path,
) -> None:
    """
    Display contents of a timing profile.

    Args:
        profile_path: Path to the profile file.
    """
    console = Console()

    try:
        loaded = load_profile(profile_path)

        console.print(f"[bold]Profile:[/bold] {profile_path}")
        console.print(f"  Version: {loaded.version}")
        console.print(f"  Created: {loaded.created}")
        console.print(f"  Updated: {loaded.updated}")
        if loaded.machine:
            console.print(f"  Machine: {loaded.machine}")
        console.print()

        console.print("[bold]Rules:[/bold]")
        for name, rp in sorted(loaded.rules.items()):
            console.print(
                f"  {name}: "
                f"n={rp.sample_count}, "
                f"mean={format_duration(rp.mean_duration)}, "
                f"std={format_duration(rp.std_dev)}, "
                f"range={format_duration(rp.min_duration)}-{format_duration(rp.max_duration)}"
            )

    except ProfileNotFoundError:
        console.print(f"[red]Error:[/red] Profile not found: {profile_path}")
        sys.exit(1)
    except InvalidProfileError as e:
        console.print(f"[red]Error:[/red] {e.message}")
        sys.exit(1)


def log_handler_path() -> None:
    """
    Print the path to the log handler script for Snakemake 8.x.

    Use with: snakemake --log-handler-script $(snakesee log-handler-path) --cores 4

    This enables real-time job tracking without requiring Snakemake 9+.
    """
    from snakesee import LOG_HANDLER_SCRIPT

    print(LOG_HANDLER_SCRIPT)


def demo(
    *,
    cores: int = 4,
    duration: Literal["short", "medium", "long"] = "short",
    keep_runs: int = 5,
    no_tui: bool = False,
    clean: bool = False,
) -> None:
    """
    Run a bundled demo workflow under Snakemake and watch it with the TUI.

    Useful for trying snakesee without setting up a real workflow. The demo
    is a small fake-bioinformatics DAG over 5 samples (~26 jobs) with one
    intentional failure on sample E to exercise the failed-jobs panel.

    Args:
        cores: Number of cores forwarded to `snakemake --cores`.
        duration: Per-job sleep preset. `short` ≈ 1.5min total wall time,
                  `medium` ≈ 5min, `long` ≈ 15min.
        keep_runs: Maximum demo dirs to keep in the cache (~/.cache/snakesee/demo/).
                   Older are pruned at startup.
        no_tui: Run Snakemake to completion without launching the TUI. Used for
                CI and scripting.
        clean: Wipe all demo dirs in the cache and exit without launching anything.
    """
    from snakesee.demo import run_demo

    rc = run_demo(
        cores=cores,
        duration=duration,
        keep_runs=keep_runs,
        no_tui=no_tui,
        clean=clean,
    )
    if rc != 0:
        sys.exit(rc)


def main() -> None:
    """Entry point for the snakesee CLI."""
    defopt.run(
        {
            "watch": watch,
            "status": status,
            "profile-export": profile_export,
            "profile-show": profile_show,
            "log-handler-path": log_handler_path,
            "demo": demo,
        },
        no_negated_flags=True,
    )


if __name__ == "__main__":
    main()
