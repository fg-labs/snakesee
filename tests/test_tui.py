"""Public API and integration tests for the snakesee TUI package.

Per-feature behaviors are tested in tests/test_app_*.py files (shell, layout,
filter, sort, screens, tables, toggles, refresh + log nav).  This file covers
the package-level public API contract: re-exported constants, the LayoutMode
enum, the backward-compat alias, App construction, and a single smoke test
that the full DOM mounts.
"""

from pathlib import Path

from snakesee.tui import ACCESSIBLE_CONFIG
from snakesee.tui import DEFAULT_CONFIG
from snakesee.tui import DEFAULT_REFRESH_RATE
from snakesee.tui import FG_BLUE
from snakesee.tui import FG_GREEN
from snakesee.tui import MAX_REFRESH_RATE
from snakesee.tui import MIN_REFRESH_RATE
from snakesee.tui import AccessibilityConfig
from snakesee.tui import LayoutMode
from snakesee.tui import SnakeseeApp
from snakesee.tui import WorkflowMonitorTUI


class TestBranding:
    """Fulcrum Genomics brand colors are re-exported from ``snakesee.tui``."""

    def test_fg_blue(self) -> None:
        assert FG_BLUE == "#26a8e0"

    def test_fg_green(self) -> None:
        assert FG_GREEN == "#38b44a"


class TestLayoutMode:
    """``LayoutMode`` exposes the three supported layouts."""

    def test_full_member(self) -> None:
        assert LayoutMode.FULL.value == "full"

    def test_compact_member(self) -> None:
        assert LayoutMode.COMPACT.value == "compact"

    def test_minimal_member(self) -> None:
        assert LayoutMode.MINIMAL.value == "minimal"

    def test_three_modes(self) -> None:
        assert len(list(LayoutMode)) == 3


class TestBackwardCompatAlias:
    """``WorkflowMonitorTUI`` is preserved as an alias for ``SnakeseeApp``."""

    def test_alias_is_app(self) -> None:
        assert WorkflowMonitorTUI is SnakeseeApp


class TestPublicConstants:
    """Refresh-rate constants are re-exported from ``snakesee.tui``."""

    def test_default_refresh_rate_positive(self) -> None:
        assert DEFAULT_REFRESH_RATE > 0

    def test_min_refresh_rate_positive(self) -> None:
        assert MIN_REFRESH_RATE > 0

    def test_max_at_least_min(self) -> None:
        assert MAX_REFRESH_RATE >= MIN_REFRESH_RATE

    def test_default_within_bounds(self) -> None:
        assert MIN_REFRESH_RATE <= DEFAULT_REFRESH_RATE <= MAX_REFRESH_RATE


class TestAccessibilityConfigs:
    """Accessibility configs are re-exported and have distinct identities."""

    def test_default_config_exists(self) -> None:
        assert isinstance(DEFAULT_CONFIG, AccessibilityConfig)

    def test_accessible_config_exists(self) -> None:
        assert isinstance(ACCESSIBLE_CONFIG, AccessibilityConfig)

    def test_configs_are_distinct(self) -> None:
        assert DEFAULT_CONFIG is not ACCESSIBLE_CONFIG


class TestAppConstruction:
    """``SnakeseeApp.__init__`` accepts default and custom arguments."""

    def test_default_construction(self, snakemake_dir: Path, tmp_path: Path) -> None:
        """Default construction (without ``run_test``) wires the data source defaults."""
        app = SnakeseeApp(workflow_dir=tmp_path)
        assert app.refresh_rate == DEFAULT_REFRESH_RATE
        assert app._data.use_estimation is True
        assert app._accessibility_config is DEFAULT_CONFIG

    async def test_custom_construction(self, snakemake_dir: Path, tmp_path: Path) -> None:
        """Custom kwargs are honored.  Wrapped in ``run_test`` because a non-default
        refresh_rate triggers ``watch_refresh_rate`` which schedules a Timer that
        requires a running event loop."""
        app = SnakeseeApp(
            workflow_dir=tmp_path,
            refresh_rate=5.0,
            use_estimation=False,
            accessibility_config=ACCESSIBLE_CONFIG,
        )
        async with app.run_test() as pilot:
            assert app.refresh_rate == 5.0
            assert app._data.use_estimation is False
            assert app._accessibility_config is ACCESSIBLE_CONFIG
            await pilot.press("q")

    def test_initial_reactive_defaults(self, snakemake_dir: Path, tmp_path: Path) -> None:
        """Reactive attributes have the documented defaults at construction."""
        app = SnakeseeApp(workflow_dir=tmp_path)
        assert app.paused is False
        assert app.layout_mode == LayoutMode.FULL
        assert app.sort_table is None
        assert app.sort_column == 0
        assert app.sort_ascending is True
        assert app.filter_text is None
        assert app.accessibility_mode is False
        assert app.current_log_index == 0


class TestSmokeIntegration:
    """End-to-end smoke test that the App boots with its full DOM."""

    async def test_app_mounts_full_dom(self, snakemake_dir: Path, tmp_path: Path) -> None:
        app = SnakeseeApp(workflow_dir=tmp_path)
        async with app.run_test() as pilot:
            assert app.query_one("#header") is not None
            assert app.query_one("#progress") is not None
            assert app.query_one("#summary") is not None
            assert app.query_one("#filter") is not None
            for table_id in ("running", "completions", "pending", "failed", "incomplete", "stats"):
                assert app.query_one(f"#{table_id}") is not None
            await pilot.press("q")
