"""Tests for the CLI module."""

import json
import sys
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from snakesee.cli import _exit_missing_tui_dependency
from snakesee.cli import _is_missing_tui_dependency
from snakesee.cli import _validate_workflow_dir
from snakesee.cli import demo
from snakesee.cli import profile_export
from snakesee.cli import profile_show
from snakesee.cli import status
from snakesee.cli import watch
from snakesee.exceptions import WorkflowNotFoundError


class TestValidateWorkflowDir:
    """Tests for _validate_workflow_dir function."""

    def test_valid_dir(self, snakemake_dir: Path, tmp_path: Path) -> None:
        """Test with valid .snakemake directory."""
        result = _validate_workflow_dir(tmp_path)
        assert result == tmp_path

    def test_missing_snakemake_dir(self, tmp_path: Path) -> None:
        """Test with missing .snakemake directory."""
        with pytest.raises(WorkflowNotFoundError) as exc_info:
            _validate_workflow_dir(tmp_path)
        assert tmp_path == exc_info.value.path


class TestWatch:
    """Tests for the watch command."""

    def test_watch_invalid_dir(self, tmp_path: Path) -> None:
        """Test watch with invalid directory."""
        with pytest.raises(SystemExit):
            watch(tmp_path)

    def test_watch_invalid_refresh_rate_low(self, snakemake_dir: Path, tmp_path: Path) -> None:
        """Test watch with refresh rate too low."""
        with pytest.raises(SystemExit):
            watch(tmp_path, refresh=0.1)

    def test_watch_invalid_refresh_rate_high(self, snakemake_dir: Path, tmp_path: Path) -> None:
        """Test watch with refresh rate too high."""
        with pytest.raises(SystemExit):
            watch(tmp_path, refresh=100.0)

    def test_watch_calls_tui(self, snakemake_dir: Path, tmp_path: Path) -> None:
        """Test that watch initializes and runs the TUI."""
        with patch("snakesee.tui.WorkflowMonitorTUI") as mock_tui:
            mock_instance = mock_tui.return_value
            watch(tmp_path, refresh=2.0, no_estimate=True)

            mock_tui.assert_called_once_with(
                workflow_dir=tmp_path,
                refresh_rate=2.0,
                use_estimation=False,
                profile_path=None,  # Auto-discovery returns None when no profile exists
                use_wildcard_conditioning=True,  # Now enabled by default
                weighting_strategy="index",
                half_life_logs=10,
                half_life_days=7.0,
                accessibility_config=None,
            )
            mock_instance.run.assert_called_once()

    def test_watch_calls_tui_in_accessible_mode(self, snakemake_dir: Path, tmp_path: Path) -> None:
        """Test that watch enables accessible rendering when requested."""
        from snakesee.tui.accessibility import ACCESSIBLE_CONFIG

        with patch("snakesee.tui.WorkflowMonitorTUI") as mock_tui:
            mock_instance = mock_tui.return_value
            watch(tmp_path, refresh=2.0, no_estimate=True, colorblind=True)

            mock_tui.assert_called_once_with(
                workflow_dir=tmp_path,
                refresh_rate=2.0,
                use_estimation=False,
                profile_path=None,
                use_wildcard_conditioning=True,
                weighting_strategy="index",
                half_life_logs=10,
                half_life_days=7.0,
                accessibility_config=ACCESSIBLE_CONFIG,
            )
            mock_instance.run.assert_called_once()


def _block_textual_import(monkeypatch: pytest.MonkeyPatch, *prefixes: str) -> None:
    """Make ``import textual`` fail as it would in a partial install.

    Drops every cached ``textual`` module and marks the package as known-absent
    (``sys.modules['textual'] = None``), then evicts the given snakesee module
    prefixes so their lazy ``from textual ... import`` statements re-run and fail.
    monkeypatch restores all of sys.modules after the test.

    Args:
        monkeypatch: The pytest monkeypatch fixture.
        prefixes: snakesee module prefixes whose cached entries must be evicted.
    """
    for name in [m for m in sys.modules if m == "textual" or m.startswith("textual.")]:
        monkeypatch.delitem(sys.modules, name, raising=False)
    monkeypatch.setitem(sys.modules, "textual", None)
    for prefix in prefixes:
        for name in [m for m in sys.modules if m.startswith(prefix)]:
            monkeypatch.delitem(sys.modules, name, raising=False)


class TestMissingTuiDependency:
    """Tests for graceful handling of a missing TUI dependency stack."""

    def test_is_missing_tui_dependency_true_for_tui_modules(self) -> None:
        """TUI stack modules (incl. submodules) are classified as TUI deps."""
        for name in ("textual", "textual.app", "rich_pixels", "PIL", "PIL.Image"):
            error = ModuleNotFoundError(f"No module named {name!r}", name=name)
            assert _is_missing_tui_dependency(error), name

    def test_is_missing_tui_dependency_false_for_other_modules(self) -> None:
        """A non-TUI missing module is not classified as a TUI dep."""
        error = ModuleNotFoundError("No module named 'snakesee.broken'", name="snakesee.broken")
        assert not _is_missing_tui_dependency(error)

    def test_is_missing_tui_dependency_handles_none_name(self) -> None:
        """An error with no module name is not treated as a TUI dep."""
        assert not _is_missing_tui_dependency(ModuleNotFoundError("nameless"))

    def test_exit_emits_actionable_message_to_stderr(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """The exit helper writes an install hint to stderr and exits non-zero."""
        error = ModuleNotFoundError("No module named 'textual'", name="textual")
        with pytest.raises(SystemExit) as exc_info:
            _exit_missing_tui_dependency(error)
        # mypy types the helper as NoReturn, so it flags the post-`with` body as
        # unreachable; at runtime pytest.raises catches the SystemExit and we do reach here.
        assert exc_info.value.code == 1  # type: ignore[unreachable]

        captured = capsys.readouterr()
        assert "textual" in captured.err
        assert "pip install" in captured.err
        # No traceback noise leaks to stdout.
        assert captured.out == ""

    def test_watch_reports_missing_textual(
        self,
        snakemake_dir: Path,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """watch exits cleanly with an actionable message when textual is absent."""
        _block_textual_import(monkeypatch, "snakesee.tui")

        with pytest.raises(SystemExit) as exc_info:
            watch(tmp_path)
        assert exc_info.value.code == 1

        captured = capsys.readouterr()
        assert "textual" in captured.err

    def test_demo_reports_missing_textual(
        self,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """demo exits cleanly with an actionable message when textual is absent."""
        _block_textual_import(monkeypatch, "snakesee.tui", "snakesee.demo")

        with pytest.raises(SystemExit) as exc_info:
            demo()
        assert exc_info.value.code == 1

        captured = capsys.readouterr()
        assert "textual" in captured.err


class TestStatus:
    """Tests for the status command."""

    def test_status_invalid_dir(self, tmp_path: Path) -> None:
        """Test status with invalid directory."""
        with pytest.raises(SystemExit):
            status(tmp_path)

    def test_status_empty_workflow(
        self, snakemake_dir: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Test status with empty workflow directory."""
        status(tmp_path)
        captured = capsys.readouterr()
        assert "Status:" in captured.out
        assert "Progress:" in captured.out

    def test_status_running_workflow(
        self, snakemake_dir: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Test status with running workflow."""
        # Create lock file
        (snakemake_dir / "locks" / "0.input.lock").write_text("/file")

        # Create log with progress
        log_file = snakemake_dir / "log" / "2024-01-01T120000.snakemake.log"
        log_file.write_text("5 of 10 steps (50%) done")

        status(tmp_path)
        captured = capsys.readouterr()
        assert "RUNNING" in captured.out
        assert "5/10" in captured.out
        assert "50.0%" in captured.out

    def test_status_with_estimation(
        self, snakemake_dir: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Test status includes ETA when estimation is enabled."""
        # Create log with progress
        log_file = snakemake_dir / "log" / "2024-01-01T120000.snakemake.log"
        log_file.write_text("5 of 10 steps (50%) done")

        status(tmp_path, no_estimate=False)
        captured = capsys.readouterr()
        assert "ETA:" in captured.out

    def test_status_without_estimation(
        self, snakemake_dir: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Test status excludes ETA when estimation is disabled."""
        # Create log with progress
        log_file = snakemake_dir / "log" / "2024-01-01T120000.snakemake.log"
        log_file.write_text("5 of 10 steps (50%) done")

        status(tmp_path, no_estimate=True)
        captured = capsys.readouterr()
        assert "ETA:" not in captured.out

    def test_status_shows_log_file(
        self, snakemake_dir: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Test status shows log file path."""
        log_file = snakemake_dir / "log" / "2024-01-01T120000.snakemake.log"
        log_file.write_text("5 of 10 steps (50%) done")

        status(tmp_path)
        captured = capsys.readouterr()
        assert "Log:" in captured.out


class TestProfileExport:
    """Tests for the profile-export command."""

    def test_export_no_metadata(self, snakemake_dir: Path, tmp_path: Path) -> None:
        """Test export fails with no metadata directory."""
        # Remove metadata dir
        (snakemake_dir / "metadata").rmdir()
        with pytest.raises(SystemExit):
            profile_export(tmp_path)

    def test_export_creates_profile(
        self, metadata_dir: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Test export creates profile file."""
        workflow_dir = metadata_dir.parent.parent  # .snakemake/metadata -> workflow_dir
        output_path = tmp_path / "test-profile.json"

        profile_export(workflow_dir, output=output_path)

        assert output_path.exists()
        data = json.loads(output_path.read_text())
        assert "rules" in data
        assert "align" in data["rules"]

        captured = capsys.readouterr()
        assert "Profile exported" in captured.out


class TestProfileShow:
    """Tests for the profile-show command."""

    def test_show_nonexistent(self, tmp_path: Path) -> None:
        """Test show fails with nonexistent profile."""
        with pytest.raises(SystemExit):
            profile_show(tmp_path / "nonexistent.json")

    def test_show_displays_profile(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Test show displays profile contents."""
        profile_path = tmp_path / "test-profile.json"
        profile_data = {
            "version": 1,
            "created": "2025-01-01T00:00:00Z",
            "updated": "2025-01-01T00:00:00Z",
            "machine": "test-machine",
            "rules": {
                "align": {
                    "rule": "align",
                    "sample_count": 5,
                    "mean_duration": 100.0,
                    "std_dev": 10.0,
                    "min_duration": 90.0,
                    "max_duration": 110.0,
                    "durations": [],
                    "timestamps": [],
                }
            },
        }
        profile_path.write_text(json.dumps(profile_data))

        profile_show(profile_path)
        captured = capsys.readouterr()
        assert "Profile:" in captured.out
        assert "align" in captured.out
        assert "n=5" in captured.out

    def test_show_invalid_profile(self, tmp_path: Path) -> None:
        """Test show fails with invalid profile format (unsupported version)."""
        profile_path = tmp_path / "invalid-profile.json"
        # Create a profile with unsupported version to trigger ValueError
        profile_path.write_text('{"version": 999, "created": "x", "updated": "x", "rules": {}}')
        with pytest.raises(SystemExit):
            profile_show(profile_path)


class TestWatchErrorPaths:
    """Additional error path tests for watch command."""

    def test_watch_invalid_half_life_logs(self, tmp_path: Path) -> None:
        """Test watch with invalid half_life_logs."""
        with pytest.raises(SystemExit):
            watch(tmp_path, half_life_logs=0)

    def test_watch_negative_half_life_logs(self, tmp_path: Path) -> None:
        """Test watch with negative half_life_logs."""
        with pytest.raises(SystemExit):
            watch(tmp_path, half_life_logs=-5)

    def test_watch_invalid_half_life_days(self, tmp_path: Path) -> None:
        """Test watch with invalid half_life_days."""
        with pytest.raises(SystemExit):
            watch(tmp_path, half_life_days=0.0)

    def test_watch_negative_half_life_days(self, tmp_path: Path) -> None:
        """Test watch with negative half_life_days."""
        with pytest.raises(SystemExit):
            watch(tmp_path, half_life_days=-1.0)


class TestStatusErrorPaths:
    """Additional error path tests for status command."""

    def test_status_with_incomplete_jobs(
        self, snakemake_dir: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Test status shows incomplete jobs."""
        # Create log with progress
        log_file = snakemake_dir / "log" / "2024-01-01T120000.snakemake.log"
        log_file.write_text("5 of 10 steps (50%) done")

        # Create incomplete markers
        incomplete_dir = snakemake_dir / "incomplete"
        incomplete_dir.mkdir(exist_ok=True)
        marker = incomplete_dir / "output.txt"
        marker.write_text(str(time.time()))

        status(tmp_path, no_estimate=True)
        captured = capsys.readouterr()
        # Check for specific expected format patterns
        assert "Incomplete: 1" in captured.out or "Status: INCOMPLETE" in captured.out

    def test_status_with_profile_path(
        self, snakemake_dir: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Test status with explicit profile path."""
        # Create a profile file
        profile_path = tmp_path / "test-profile.json"
        profile_data = {
            "version": 1,
            "created": "2025-01-01T00:00:00Z",
            "updated": "2025-01-01T00:00:00Z",
            "machine": "test-machine",
            "rules": {
                "align": {
                    "rule": "align",
                    "sample_count": 5,
                    "mean_duration": 100.0,
                    "std_dev": 10.0,
                    "min_duration": 90.0,
                    "max_duration": 110.0,
                    "durations": [100.0, 100.0, 100.0, 100.0, 100.0],
                    "timestamps": [1.0, 2.0, 3.0, 4.0, 5.0],
                }
            },
        }
        profile_path.write_text(json.dumps(profile_data))

        # Create log with progress
        log_file = snakemake_dir / "log" / "2024-01-01T120000.snakemake.log"
        log_file.write_text("5 of 10 steps (50%) done")

        status(tmp_path, profile=profile_path)
        captured = capsys.readouterr()
        assert "ETA:" in captured.out


class TestProfileExportErrorPaths:
    """Additional error path tests for profile-export command."""

    def test_export_with_merge(
        self, metadata_dir: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Test export with merge option."""
        workflow_dir = metadata_dir.parent.parent
        output_path = tmp_path / "test-profile.json"

        # First export
        profile_export(workflow_dir, output=output_path)
        assert output_path.exists()

        # Second export with merge
        profile_export(workflow_dir, output=output_path, merge=True)
        captured = capsys.readouterr()
        assert "Profile exported" in captured.out
