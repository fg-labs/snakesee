"""Shared utility functions for snakesee.

This module consolidates common utilities used across multiple modules
to avoid duplication and ensure consistent behavior.
"""

from __future__ import annotations

import logging
import os
import threading
from collections.abc import Iterator
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING
from typing import Any

import orjson

from snakesee.constants import MAX_METADATA_FILE_SIZE

if TYPE_CHECKING:
    from snakesee.types import ProgressCallback

logger = logging.getLogger(__name__)

# Default number of workers for parallel metadata reading
DEFAULT_METADATA_WORKERS = 4

# Threshold for using parallel reading (number of files)
# Set high because parallel overhead exceeds benefit for small local files
# Only beneficial for network storage or very large directories
PARALLEL_READ_THRESHOLD = 1000


@dataclass(slots=True)
class _MetadataFileInfo:
    """Metadata file info for sorting and caching."""

    path: Path
    mtime: float
    size: int
    inode: int


class MetadataCache:
    """Thread-safe cache for parsed metadata files.

    Tracks file mtimes to skip re-reading unchanged files.
    """

    __slots__ = ("_cache", "_lock")

    def __init__(self) -> None:
        """Initialize empty cache."""
        self._cache: dict[Path, tuple[float, int, dict[str, Any]]] = {}
        self._lock = threading.Lock()

    def get(self, path: Path, mtime: float, inode: int) -> dict[str, Any] | None:
        """Get cached data if file hasn't changed.

        Args:
            path: Path to the metadata file.
            mtime: Current file modification time.
            inode: Current file inode.

        Returns:
            Cached data if valid, None if cache miss or stale.
        """
        with self._lock:
            cached = self._cache.get(path)
            if cached is not None:
                cached_mtime, cached_inode, data = cached
                if cached_mtime == mtime and cached_inode == inode:
                    return data
        return None

    def put(self, path: Path, mtime: float, inode: int, data: dict[str, Any]) -> None:
        """Store parsed data in cache.

        Args:
            path: Path to the metadata file.
            mtime: File modification time.
            inode: File inode.
            data: Parsed JSON data.
        """
        with self._lock:
            self._cache[path] = (mtime, inode, data)

    def clear(self) -> None:
        """Clear all cached data."""
        with self._lock:
            self._cache.clear()

    def __len__(self) -> int:
        """Number of cached entries."""
        with self._lock:
            return len(self._cache)


# Global metadata cache instance
_metadata_cache = MetadataCache()


def get_metadata_cache() -> MetadataCache:
    """Get the global metadata cache instance."""
    return _metadata_cache


def json_loads(data: str | bytes) -> Any:
    """Parse JSON using orjson for better performance.

    Args:
        data: JSON string or bytes to parse.

    Returns:
        Parsed JSON data.

    Raises:
        orjson.JSONDecodeError: If the data is not valid JSON.
    """
    if isinstance(data, str):
        data = data.encode("utf-8")
    return orjson.loads(data)


def safe_mtime(path: Path) -> float:
    """Get file modification time, returning 0.0 if file doesn't exist.

    This handles the common race condition where a file may be deleted
    between checking for existence and reading its mtime.

    Args:
        path: Path to the file.

    Returns:
        The file's modification time as a Unix timestamp, or 0.0 if the
        file doesn't exist.
    """
    try:
        return path.stat().st_mtime
    except (FileNotFoundError, OSError):
        return 0.0


def safe_read_text(path: Path, default: str = "", errors: str = "ignore") -> str:
    """Safely read text from a file, returning default on error.

    Handles common race conditions and encoding issues gracefully.

    Args:
        path: Path to the file.
        default: Value to return if file cannot be read.
        errors: How to handle encoding errors (passed to read_text).

    Returns:
        File contents as string, or default if reading fails.
    """
    try:
        return path.read_text(errors=errors)
    except (FileNotFoundError, OSError, PermissionError):
        return default


def safe_read_json(path: Path, default: dict[str, Any] | None = None) -> dict[str, Any] | None:
    """Safely read and parse JSON from a file.

    Handles file access errors and JSON parse errors gracefully.

    Args:
        path: Path to the JSON file.
        default: Value to return if file cannot be read or parsed.

    Returns:
        Parsed JSON as dict, or default if reading/parsing fails.
    """
    try:
        content = path.read_bytes()
        result: dict[str, Any] = orjson.loads(content)
        return result
    except (FileNotFoundError, OSError, PermissionError, orjson.JSONDecodeError):
        return default


def safe_file_size(path: Path) -> int:
    """Safely get file size in bytes, returning 0 on error.

    Args:
        path: Path to the file.

    Returns:
        File size in bytes, or 0 if file doesn't exist or can't be accessed.
    """
    try:
        return path.stat().st_size
    except (FileNotFoundError, OSError):
        return 0


class _ScanCache:
    """Cache for directory scan results to avoid re-stat-ing thousands of files.

    Tracks directory mtimes to detect changes (additions, modifications, or
    deletions). A directory's mtime updates whenever its direct entries change,
    so this catches all file-level mutations within that directory.

    Note: This cache is designed for a single root directory. Calling get_files
    with a different directory than the first call will trigger a full rescan.
    """

    __slots__ = ("_dir_mtimes", "_files", "_lock", "_root")

    def __init__(self) -> None:
        self._files: tuple[_MetadataFileInfo, ...] = ()
        self._dir_mtimes: dict[str, float] = {}
        self._lock = threading.Lock()
        self._root: str | None = None

    def get_files(self, directory: Path) -> tuple[_MetadataFileInfo, ...]:
        """Return cached file tuple, rescanning only changed directories."""
        dir_str = str(directory)
        with self._lock:
            if self._root is None or self._root != dir_str:
                # First call or different directory — full scan
                self._root = dir_str
                files_list, self._dir_mtimes = _full_scandir(directory)
                self._files = tuple(files_list)
                return self._files

            # Check which directories have changed
            changed_dirs: list[str] = []
            current_mtimes: dict[str, float] = {}
            _collect_dir_mtimes(dir_str, current_mtimes)

            for dir_path, mtime in current_mtimes.items():
                old_mtime = self._dir_mtimes.get(dir_path)
                if old_mtime is None or old_mtime != mtime:
                    changed_dirs.append(dir_path)

            # Also detect removed directories
            removed_dirs: list[str] = []
            for dir_path in self._dir_mtimes:
                if dir_path not in current_mtimes:
                    removed_dirs.append(dir_path)

            if not changed_dirs and not removed_dirs:
                # Directory-only fast path: O(directories), not O(files).
                # Snakemake metadata files are write-once, so in-place rewrites
                # are not expected. If file-level freshness is needed, callers
                # should bypass the cache via use_scan_cache=False.
                return self._files

            # Evict only direct children of changed/removed dirs
            changed_set = {Path(d) for d in changed_dirs}
            removed_prefixes = [d + os.sep for d in removed_dirs]
            kept = [
                f
                for f in self._files
                if f.path.parent not in changed_set
                and not any(str(f.path).startswith(p) for p in removed_prefixes)
            ]
            # Scan changed directories for new/updated files
            new_files: list[_MetadataFileInfo] = []
            for dir_path in changed_dirs:
                if dir_path in current_mtimes:
                    _scan_single_dir(Path(dir_path), new_files)

            self._files = tuple(kept + new_files)
            self._dir_mtimes = current_mtimes
            return self._files

    def clear(self) -> None:
        """Clear the scan cache."""
        with self._lock:
            self._files = ()
            self._dir_mtimes.clear()
            self._root = None


def _collect_dir_mtimes(dir_path: str, result: dict[str, float]) -> None:
    """Collect mtimes for a directory tree (dirs only, no file stats)."""
    try:
        stat_result = os.stat(dir_path)
        result[dir_path] = stat_result.st_mtime
        with os.scandir(dir_path) as entries:
            for entry in entries:
                try:
                    if entry.is_dir(follow_symlinks=False):
                        _collect_dir_mtimes(entry.path, result)
                except OSError:
                    continue
    except OSError:
        pass


def _scan_single_dir(dir_path: Path, files: list[_MetadataFileInfo]) -> None:
    """Scan a single directory (non-recursive) for files."""
    try:
        with os.scandir(dir_path) as entries:
            for entry in entries:
                try:
                    if entry.is_file(follow_symlinks=False):
                        stat_result = entry.stat(follow_symlinks=False)
                        files.append(
                            _MetadataFileInfo(
                                path=Path(entry.path),
                                mtime=stat_result.st_mtime,
                                size=stat_result.st_size,
                                inode=stat_result.st_ino,
                            )
                        )
                except OSError:
                    continue
    except OSError:
        pass


def _full_scandir(directory: Path) -> tuple[list[_MetadataFileInfo], dict[str, float]]:
    """Full recursive scan, returning both file list and directory mtimes."""
    files: list[_MetadataFileInfo] = []
    dir_mtimes: dict[str, float] = {}

    def _scan_recursive(dir_path: Path) -> None:
        try:
            dir_str = str(dir_path)
            dir_mtimes[dir_str] = dir_path.stat().st_mtime
            with os.scandir(dir_path) as entries:
                for entry in entries:
                    try:
                        if entry.is_file(follow_symlinks=False):
                            stat_result = entry.stat(follow_symlinks=False)
                            files.append(
                                _MetadataFileInfo(
                                    path=Path(entry.path),
                                    mtime=stat_result.st_mtime,
                                    size=stat_result.st_size,
                                    inode=stat_result.st_ino,
                                )
                            )
                        elif entry.is_dir(follow_symlinks=False):
                            _scan_recursive(Path(entry.path))
                    except OSError:
                        continue
        except OSError:
            pass

    _scan_recursive(directory)
    return files, dir_mtimes


# Global scan cache instance
_scan_cache = _ScanCache()


def get_scan_cache() -> _ScanCache:
    """Get the global scan cache instance."""
    return _scan_cache


def _scandir_files(directory: Path, *, use_scan_cache: bool = True) -> Sequence[_MetadataFileInfo]:
    """Recursively scan directory for files, using cached results when possible.

    On the first call, performs a full recursive scan. On subsequent calls,
    only rescans directories whose mtime has changed, avoiding re-stat-ing
    thousands of unchanged files.

    Args:
        directory: Directory to scan.
        use_scan_cache: If False, bypass the scan cache and do a full scan.

    Returns:
        Sequence of _MetadataFileInfo for all files found.
    """
    if not use_scan_cache:
        files, _ = _full_scandir(directory)
        return files
    return _scan_cache.get_files(directory)


def _read_metadata_file(
    file_info: _MetadataFileInfo,
    cache: MetadataCache | None = None,
) -> tuple[Path, dict[str, Any]] | None:
    """Read and parse a single metadata file.

    Args:
        file_info: File info from scandir.
        cache: Optional cache to check/update.

    Returns:
        Tuple of (path, data) if successful, None otherwise.
    """
    if file_info.size > MAX_METADATA_FILE_SIZE:
        logger.debug(
            "Skipping oversized metadata file %s: %d bytes (max %d)",
            file_info.path,
            file_info.size,
            MAX_METADATA_FILE_SIZE,
        )
        return None

    # Check cache first
    if cache is not None:
        cached_data = cache.get(file_info.path, file_info.mtime, file_info.inode)
        if cached_data is not None:
            return file_info.path, cached_data

    # Read and parse file
    try:
        data = orjson.loads(file_info.path.read_bytes())
        # Update cache
        if cache is not None:
            cache.put(file_info.path, file_info.mtime, file_info.inode, data)
        return file_info.path, data
    except orjson.JSONDecodeError as e:
        logger.debug("Malformed JSON in metadata file %s: %s", file_info.path, e)
        return None
    except OSError as e:
        logger.debug("Error reading metadata file %s: %s", file_info.path, e)
        return None


def iterate_metadata_files(
    metadata_dir: Path,
    progress_callback: ProgressCallback | None = None,
    *,
    sort_by_mtime: bool = True,
    newest_first: bool = True,
    use_cache: bool = True,
    use_parallel: bool = True,
    max_workers: int = DEFAULT_METADATA_WORKERS,
) -> Iterator[tuple[Path, dict[str, Any]]]:
    """Iterate metadata files with optional progress reporting.

    Iterates over all files in the metadata directory, parsing each as JSON.
    Invalid files (non-JSON or unreadable) are silently skipped with debug logging.

    Performance optimizations:
    - Uses os.scandir instead of rglob (6-7x faster directory iteration)
    - Sorts by mtime to process newest files first (better for recent data)
    - Caches parsed files to skip re-reading unchanged files
    - Uses parallel I/O for very large directories (>=1000 files)

    Args:
        metadata_dir: Path to .snakemake/metadata/ directory.
        progress_callback: Optional callback(current, total) for progress reporting.
        sort_by_mtime: Sort files by modification time.
        newest_first: If sorting, put newest files first.
        use_cache: Use global cache to skip unchanged files.
        use_parallel: Use parallel I/O for large directories.
        max_workers: Maximum number of parallel workers.

    Yields:
        Tuples of (file_path, parsed_json_data) for each valid metadata file.
    """
    if not metadata_dir.exists():
        return

    # Use fast scandir-based recursive scan (bypass scan cache when use_cache=False)
    files = _scandir_files(metadata_dir, use_scan_cache=use_cache)
    if not files:
        return

    # Sort by mtime (newest first by default)
    if sort_by_mtime:
        files = sorted(files, key=lambda f: f.mtime, reverse=newest_first)

    total = len(files)
    cache = get_metadata_cache() if use_cache else None

    # Use parallel reading for large directories
    if use_parallel and total >= PARALLEL_READ_THRESHOLD:
        yield from _iterate_metadata_parallel(files, cache, progress_callback, total, max_workers)
    else:
        yield from _iterate_metadata_sequential(files, cache, progress_callback, total)


def _iterate_metadata_sequential(
    files: Sequence[_MetadataFileInfo],
    cache: MetadataCache | None,
    progress_callback: ProgressCallback | None,
    total: int,
) -> Iterator[tuple[Path, dict[str, Any]]]:
    """Sequential metadata file iteration."""
    for i, file_info in enumerate(files):
        if progress_callback is not None:
            progress_callback(i + 1, total)

        result = _read_metadata_file(file_info, cache)
        if result is not None:
            yield result


def _iterate_metadata_parallel(
    files: Sequence[_MetadataFileInfo],
    cache: MetadataCache | None,
    progress_callback: ProgressCallback | None,
    total: int,
    max_workers: int,
) -> Iterator[tuple[Path, dict[str, Any]]]:
    """Parallel metadata file iteration using ThreadPoolExecutor.

    Maintains order by processing in batches.
    """
    # Process in batches to maintain approximate ordering while parallelizing
    batch_size = max(max_workers * 4, 20)
    processed = 0

    # Reuse executor across batches to avoid repeated creation overhead
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        for batch_start in range(0, len(files), batch_size):
            batch = files[batch_start : batch_start + batch_size]

            # Submit all files in batch
            future_to_idx = {
                executor.submit(_read_metadata_file, file_info, cache): i
                for i, file_info in enumerate(batch)
            }

            # Collect results, maintaining order within batch
            results: list[tuple[Path, dict[str, Any]] | None] = [None] * len(batch)
            for future in as_completed(future_to_idx):
                idx = future_to_idx[future]
                try:
                    results[idx] = future.result()
                except Exception as e:
                    logger.debug("Error reading metadata file: %s", e)
                    results[idx] = None

            # Yield results in order
            for result in results:
                processed += 1
                if progress_callback is not None:
                    progress_callback(processed, total)
                if result is not None:
                    yield result
