"""Command-line entry point."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from snakesee import __version__
from snakesee.monitor import LOG_SUFFIX


def resolve_target(path: Path) -> tuple[Path, Path | None]:
    """Interpret the positional argument as a workflow dir or a specific log file.

    Returns:
        ``(workflow_dir, log_file)``; ``log_file`` is None to follow the latest log.
    """
    path = path.expanduser().resolve()
    if path.is_file():
        # .../<workflow>/.snakemake/log/<name>.snakemake.log
        parents = path.parents
        if path.name.endswith(LOG_SUFFIX) and parents[1].name == ".snakemake":
            return parents[2], path
        return Path.cwd(), path
    return path, None


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="snakesee",
        description="Monitor a running Snakemake workflow.",
    )
    p.add_argument(
        "path",
        nargs="?",
        type=Path,
        default=Path("."),
        help="workflow directory (containing .snakemake/) or a specific .snakemake.log file "
        "(default: current directory, following the newest log)",
    )
    p.add_argument(
        "-n",
        "--interval",
        type=float,
        default=2.0,
        help="seconds between polls of the Snakemake log (default: 2)",
    )
    p.add_argument("--version", action="version", version=f"snakesee {__version__}")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    workflow_dir, log_file = resolve_target(args.path)
    if log_file is None and not (workflow_dir / ".snakemake").is_dir():
        print(
            f"snakesee: no .snakemake directory found at {workflow_dir / '.snakemake'}",
            file=sys.stderr,
        )
        sys.exit(1)

    from snakesee.tui.app import SnakeseeApp  # deferred: keeps --help fast

    SnakeseeApp(workflow_dir, log_file=log_file, interval=max(0.2, args.interval)).run()


if __name__ == "__main__":
    main()
