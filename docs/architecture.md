# Snakesee Architecture

This document describes the architecture and key design decisions in snakesee.

## Module Structure

```text
snakesee/
├── __init__.py          # Public API exports
├── cli.py               # Command-line interface (defopt-based)
├── constants.py         # Centralized configuration constants
├── events.py            # Event file reading and streaming
├── exceptions.py        # Application-specific exceptions
├── models.py            # Core data models (JobInfo, WorkflowProgress, etc.)
├── estimator.py         # Time estimation orchestration
├── formatting.py        # Duration and time formatting utilities
├── profile.py           # Portable timing profile storage
├── utils.py             # Shared utility functions
├── validation.py        # State comparison utilities
├── variance.py          # Variance calculation for confidence
│
├── tui/                 # Terminal user interface (Textual-based)
│   ├── __init__.py      # TUI module exports
│   ├── app.py           # SnakeseeApp — main Textual App class
│   ├── app.tcss         # CSS layout and theming
│   ├── data_source.py   # WorkflowDataSource — pure-data layer
│   ├── renderables.py   # Rich renderables (header, progress bar, footer)
│   ├── tables.py        # DataTable row builders
│   ├── screens.py       # Modal screens (help, easter egg, job log)
│   └── accessibility.py # Colorblind-accessible rendering helpers
│
├── parser/              # Log parsing and metadata extraction
│   ├── __init__.py      # Public parser API
│   ├── core.py          # Core parsing functions
│   ├── line_parser.py   # Log line parsing
│   ├── log_reader.py    # Log file reading with caching
│   └── patterns.py      # Centralized regex patterns
│
├── estimation/          # Time estimation algorithms
│   ├── __init__.py
│   ├── estimator.py     # Main TimeEstimator class
│   ├── data_loader.py   # Data loading from metadata/events
│   └── strategies.py    # Weighting strategies (index/time)
│
├── plugins/             # Tool-specific progress parsing
│   ├── __init__.py      # Plugin registry and API
│   ├── base.py          # ToolProgressPlugin base class
│   ├── loader.py        # File-based plugin loading
│   ├── discovery.py     # Entry point plugin discovery
│   ├── registry.py      # Plugin lookup functions
│   └── (tool plugins)   # BWA, samtools, STAR, etc.
│
└── state/               # Unified workflow state management
    ├── __init__.py      # State module exports
    ├── clock.py         # Injectable clock for testability
    ├── config.py        # EstimationConfig and related
    ├── paths.py         # WorkflowPaths centralized path resolution
    ├── job_registry.py  # JobRegistry - job state tracking
    ├── rule_registry.py # RuleRegistry - rule statistics
    └── workflow_state.py # WorkflowState - top-level container
```

## Key Design Patterns

### 1. Dependency Injection for Testability

The `Clock` protocol enables deterministic testing of time-dependent code:

```python
from snakesee.state import FrozenClock, set_clock

def test_elapsed_time():
    clock = FrozenClock(1000.0)
    set_clock(clock)

    # Test with frozen time
    clock.advance(60.0)  # Advance by 1 minute
```

### 2. Deferred Imports to Avoid Circular Dependencies

Many modules use deferred imports inside functions to break circular dependencies:

```python
def my_function():
    # Deferred import to avoid circular dependency
    from snakesee.models import JobInfo
    ...
```

This pattern is intentional and documented in `TYPE_CHECKING` blocks for type hints.

### 3. Plugin Architecture

Plugins are discovered from multiple sources:

1. **Built-in plugins**: Shipped with snakesee (BWA, samtools, etc.)
2. **User plugins**: `~/.snakesee/plugins/*.py`
3. **Entry points**: Third-party packages via `pyproject.toml`

Plugins must implement `ToolProgressPlugin` and are validated for:
- API version compatibility
- Required interface methods
- Valid property values

### 4. Centralized Configuration

Constants are organized in `constants.py` using frozen dataclasses:

```python
@dataclass(frozen=True)
class RefreshRateConfig:
    min_rate: float = 0.5
    max_rate: float = 60.0
    default_rate: float = 1.0

REFRESH_RATE_CONFIG = RefreshRateConfig()
```

Estimation-specific configuration is in `state/config.py`.

### 5. Application-Specific Exceptions

Custom exception hierarchy for precise error handling:

```text
SnakeseeError (base)
├── WorkflowError
│   ├── WorkflowNotFoundError
│   └── WorkflowParseError
├── ProfileError
│   ├── ProfileNotFoundError
│   └── InvalidProfileError
├── PluginError
│   ├── PluginLoadError
│   └── PluginExecutionError
└── ConfigurationError
```

## Data Flow

### Workflow Monitoring

1. **CLI** receives user command (`watch`, `status`, etc.)
2. **Parser** reads `.snakemake/` directory metadata
3. **State** modules maintain current workflow state
4. **Estimator** calculates time estimates from historical data
5. **TUI** renders real-time dashboard

### Time Estimation

1. **DataLoader** loads timing data from metadata files or events
2. **RuleRegistry** tracks per-rule statistics
3. **Estimator** applies weighting strategies (index or time-based)
4. **Variance** calculates confidence intervals

## Testing Strategy

- **Unit tests**: Test individual components in isolation
- **Property-based tests**: Use Hypothesis for edge cases
- **Benchmark tests**: Track performance regressions
- **Integration tests**: Test end-to-end workflows

Minimum coverage requirement: 65%

## Future Refactoring Notes

### parser/core.py Split (Recommended)
The `parser/core.py` could be split into focused modules:

1. **parser/metadata.py**: Metadata file parsing
   - `MetadataRecord`, `parse_metadata_files`, `parse_metadata_files_full`
   - `collect_rule_code_hashes`, `_calculate_input_size`

2. **parser/stats.py**: Timing statistics collection
   - `collect_rule_timing_stats`, `collect_wildcard_timing_stats`
   - `_build_wildcard_stats_for_key`

3. **parser/workflow.py**: Workflow state assembly
   - `parse_workflow_state`, `is_workflow_running`
   - `_determine_final_workflow_status`, `_reconcile_job_lists`

4. **parser/utils.py**: Utility functions
   - `_parse_wildcards`, `_parse_positive_int`, `_parse_non_negative_int`
   - `calculate_input_size`, `estimate_input_size_from_output`

The `parser/__init__.py` already acts as a facade, so this split would be
backward-compatible.

## Security Considerations

- **File size limits**: Prevent DoS from malicious files
- **Plugin security**: Warn on symlinks and world-writable directories
- **Input validation**: Validate all external input (metadata, logs)
