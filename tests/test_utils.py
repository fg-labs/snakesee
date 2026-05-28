"""Tests for utility functions."""

import json
import time
from pathlib import Path

import pytest

from snakesee.utils import MetadataCache
from snakesee.utils import _ScanCache
from snakesee.utils import get_metadata_cache
from snakesee.utils import get_scan_cache
from snakesee.utils import iterate_metadata_files
from snakesee.utils import json_loads
from snakesee.utils import safe_file_size
from snakesee.utils import safe_mtime
from snakesee.utils import safe_read_json
from snakesee.utils import safe_read_text


class TestSafeMtime:
    """Tests for safe_mtime function."""

    def test_existing_file(self, tmp_path: Path) -> None:
        """Test mtime for existing file."""
        test_file = tmp_path / "test.txt"
        test_file.write_text("hello")
        mtime = safe_mtime(test_file)
        assert mtime > 0

    def test_nonexistent_file(self, tmp_path: Path) -> None:
        """Test mtime for nonexistent file returns 0.0."""
        nonexistent = tmp_path / "nonexistent.txt"
        assert safe_mtime(nonexistent) == 0.0

    def test_directory(self, tmp_path: Path) -> None:
        """Test mtime for directory works."""
        mtime = safe_mtime(tmp_path)
        assert mtime > 0


class TestSafeReadText:
    """Tests for safe_read_text function."""

    def test_existing_file(self, tmp_path: Path) -> None:
        """Test reading existing file."""
        test_file = tmp_path / "test.txt"
        test_file.write_text("hello world")
        content = safe_read_text(test_file)
        assert content == "hello world"

    def test_nonexistent_file(self, tmp_path: Path) -> None:
        """Test reading nonexistent file returns default."""
        nonexistent = tmp_path / "nonexistent.txt"
        assert safe_read_text(nonexistent) == ""

    def test_nonexistent_file_custom_default(self, tmp_path: Path) -> None:
        """Test reading nonexistent file with custom default."""
        nonexistent = tmp_path / "nonexistent.txt"
        assert safe_read_text(nonexistent, default="N/A") == "N/A"

    def test_file_with_encoding_errors(self, tmp_path: Path) -> None:
        """Test reading file with encoding errors is handled."""
        test_file = tmp_path / "binary.txt"
        test_file.write_bytes(b"hello \xff\xfe world")
        # Should not raise, should handle encoding errors
        content = safe_read_text(test_file)
        assert "hello" in content
        assert "world" in content


class TestSafeReadJson:
    """Tests for safe_read_json function."""

    def test_valid_json_file(self, tmp_path: Path) -> None:
        """Test reading valid JSON file."""
        test_file = tmp_path / "test.json"
        data = {"key": "value", "count": 42}
        test_file.write_text(json.dumps(data))
        result = safe_read_json(test_file)
        assert result == data

    def test_nonexistent_file(self, tmp_path: Path) -> None:
        """Test reading nonexistent file returns None."""
        nonexistent = tmp_path / "nonexistent.json"
        assert safe_read_json(nonexistent) is None

    def test_nonexistent_file_custom_default(self, tmp_path: Path) -> None:
        """Test reading nonexistent file with custom default."""
        nonexistent = tmp_path / "nonexistent.json"
        default = {"default": True}
        assert safe_read_json(nonexistent, default=default) == default

    def test_invalid_json(self, tmp_path: Path) -> None:
        """Test reading invalid JSON returns None."""
        test_file = tmp_path / "invalid.json"
        test_file.write_text("not valid json {{{")
        assert safe_read_json(test_file) is None

    def test_empty_file(self, tmp_path: Path) -> None:
        """Test reading empty file returns None (invalid JSON)."""
        test_file = tmp_path / "empty.json"
        test_file.write_text("")
        assert safe_read_json(test_file) is None


class TestSafeFileSize:
    """Tests for safe_file_size function."""

    def test_existing_file(self, tmp_path: Path) -> None:
        """Test size for existing file."""
        test_file = tmp_path / "test.txt"
        content = "hello world"
        test_file.write_text(content)
        size = safe_file_size(test_file)
        assert size == len(content)

    def test_nonexistent_file(self, tmp_path: Path) -> None:
        """Test size for nonexistent file returns 0."""
        nonexistent = tmp_path / "nonexistent.txt"
        assert safe_file_size(nonexistent) == 0

    def test_empty_file(self, tmp_path: Path) -> None:
        """Test size for empty file returns 0."""
        test_file = tmp_path / "empty.txt"
        test_file.write_text("")
        assert safe_file_size(test_file) == 0

    def test_binary_file(self, tmp_path: Path) -> None:
        """Test size for binary file."""
        test_file = tmp_path / "binary.bin"
        data = b"\x00\x01\x02\x03" * 100
        test_file.write_bytes(data)
        assert safe_file_size(test_file) == len(data)


class TestJsonLoads:
    """Tests for json_loads function."""

    def test_parse_string(self) -> None:
        """Test parsing JSON string."""
        result = json_loads('{"key": "value"}')
        assert result == {"key": "value"}

    def test_parse_bytes(self) -> None:
        """Test parsing JSON bytes."""
        result = json_loads(b'{"key": "value"}')
        assert result == {"key": "value"}

    def test_parse_complex_json(self) -> None:
        """Test parsing complex JSON with nested structures."""
        data = '{"nested": {"list": [1, 2, 3], "bool": true, "null": null}}'
        result = json_loads(data)
        assert result["nested"]["list"] == [1, 2, 3]
        assert result["nested"]["bool"] is True
        assert result["nested"]["null"] is None

    def test_parse_unicode(self) -> None:
        """Test parsing JSON with unicode characters."""
        result = json_loads('{"emoji": "🐍", "japanese": "日本語"}')
        assert result["emoji"] == "🐍"
        assert result["japanese"] == "日本語"


class TestMetadataCache:
    """Tests for MetadataCache class."""

    def test_cache_miss(self) -> None:
        """Test cache miss returns None."""
        cache = MetadataCache()
        result = cache.get(Path("/nonexistent"), mtime=1000.0, inode=12345)
        assert result is None

    def test_cache_hit(self) -> None:
        """Test cache hit returns stored data."""
        cache = MetadataCache()
        path = Path("/some/path")
        data = {"rule": "test", "duration": 100}
        cache.put(path, mtime=1000.0, inode=12345, data=data)
        result = cache.get(path, mtime=1000.0, inode=12345)
        assert result == data

    def test_cache_stale_mtime(self) -> None:
        """Test cache returns None when mtime changes."""
        cache = MetadataCache()
        path = Path("/some/path")
        data = {"rule": "test"}
        cache.put(path, mtime=1000.0, inode=12345, data=data)
        # Different mtime
        result = cache.get(path, mtime=1001.0, inode=12345)
        assert result is None

    def test_cache_stale_inode(self) -> None:
        """Test cache returns None when inode changes."""
        cache = MetadataCache()
        path = Path("/some/path")
        data = {"rule": "test"}
        cache.put(path, mtime=1000.0, inode=12345, data=data)
        # Different inode
        result = cache.get(path, mtime=1000.0, inode=99999)
        assert result is None

    def test_cache_clear(self) -> None:
        """Test cache clear removes all entries."""
        cache = MetadataCache()
        path = Path("/some/path")
        cache.put(path, mtime=1000.0, inode=12345, data={"rule": "test"})
        assert len(cache) == 1
        cache.clear()
        assert len(cache) == 0

    def test_cache_len(self) -> None:
        """Test cache len returns correct count."""
        cache = MetadataCache()
        assert len(cache) == 0
        cache.put(Path("/path1"), mtime=1000.0, inode=1, data={})
        assert len(cache) == 1
        cache.put(Path("/path2"), mtime=1000.0, inode=2, data={})
        assert len(cache) == 2


class TestGetMetadataCache:
    """Tests for get_metadata_cache function."""

    def test_returns_global_instance(self) -> None:
        """Test returns the same global instance."""
        cache1 = get_metadata_cache()
        cache2 = get_metadata_cache()
        assert cache1 is cache2

    def test_instance_is_metadata_cache(self) -> None:
        """Test returned instance is MetadataCache."""
        cache = get_metadata_cache()
        assert isinstance(cache, MetadataCache)


class TestIterateMetadataFiles:
    """Tests for iterate_metadata_files function."""

    @pytest.fixture(autouse=True)
    def _clear_global_caches(self) -> None:
        """Clear global caches before each test to avoid cross-test contamination."""
        get_scan_cache().clear()
        get_metadata_cache().clear()

    def test_empty_directory(self, tmp_path: Path) -> None:
        """Test iterating empty metadata directory."""
        metadata_dir = tmp_path / "metadata"
        metadata_dir.mkdir()
        result = list(iterate_metadata_files(metadata_dir))
        assert result == []

    def test_nonexistent_directory(self, tmp_path: Path) -> None:
        """Test iterating nonexistent directory yields nothing."""
        metadata_dir = tmp_path / "nonexistent"
        result = list(iterate_metadata_files(metadata_dir))
        assert result == []

    def test_iterates_valid_json_files(self, tmp_path: Path) -> None:
        """Test iterating valid JSON metadata files."""
        metadata_dir = tmp_path / "metadata"
        metadata_dir.mkdir()
        data1 = {"rule": "align", "starttime": 1000.0, "endtime": 1100.0}
        data2 = {"rule": "sort", "starttime": 1100.0, "endtime": 1150.0}
        (metadata_dir / "file1.json").write_text(json.dumps(data1))
        (metadata_dir / "file2.json").write_text(json.dumps(data2))

        result = list(iterate_metadata_files(metadata_dir))
        assert len(result) == 2
        # Each result is (path, data) tuple
        rules = {r[1]["rule"] for r in result}
        assert rules == {"align", "sort"}

    def test_skips_invalid_json(self, tmp_path: Path) -> None:
        """Test skips files with invalid JSON."""
        metadata_dir = tmp_path / "metadata"
        metadata_dir.mkdir()
        (metadata_dir / "valid.json").write_text('{"rule": "test"}')
        (metadata_dir / "invalid.json").write_text("not json {{{")

        result = list(iterate_metadata_files(metadata_dir))
        assert len(result) == 1
        assert result[0][1]["rule"] == "test"

    def test_sorts_by_mtime_newest_first(self, tmp_path: Path) -> None:
        """Test files are sorted by mtime, newest first by default."""
        metadata_dir = tmp_path / "metadata"
        metadata_dir.mkdir()

        # Create files with different mtimes
        (metadata_dir / "old.json").write_text('{"order": "old"}')
        time.sleep(0.01)
        (metadata_dir / "new.json").write_text('{"order": "new"}')

        result = list(iterate_metadata_files(metadata_dir, newest_first=True))
        assert len(result) == 2
        assert result[0][1]["order"] == "new"
        assert result[1][1]["order"] == "old"

    def test_sorts_oldest_first_when_requested(self, tmp_path: Path) -> None:
        """Test files sorted oldest first when newest_first=False."""
        metadata_dir = tmp_path / "metadata"
        metadata_dir.mkdir()

        (metadata_dir / "old.json").write_text('{"order": "old"}')
        time.sleep(0.01)
        (metadata_dir / "new.json").write_text('{"order": "new"}')

        result = list(iterate_metadata_files(metadata_dir, newest_first=False))
        assert len(result) == 2
        assert result[0][1]["order"] == "old"
        assert result[1][1]["order"] == "new"

    def test_progress_callback(self, tmp_path: Path) -> None:
        """Test progress callback is called."""
        metadata_dir = tmp_path / "metadata"
        metadata_dir.mkdir()
        (metadata_dir / "file1.json").write_text('{"rule": "test1"}')
        (metadata_dir / "file2.json").write_text('{"rule": "test2"}')

        progress_calls: list[tuple[int, int]] = []

        def callback(current: int, total: int) -> None:
            progress_calls.append((current, total))

        list(iterate_metadata_files(metadata_dir, progress_callback=callback))

        assert len(progress_calls) == 2
        assert progress_calls[0] == (1, 2)
        assert progress_calls[1] == (2, 2)

    def test_iterates_subdirectories(self, tmp_path: Path) -> None:
        """Test iterating metadata files in subdirectories."""
        metadata_dir = tmp_path / "metadata"
        subdir = metadata_dir / "subdir"
        subdir.mkdir(parents=True)
        (metadata_dir / "top.json").write_text('{"level": "top"}')
        (subdir / "nested.json").write_text('{"level": "nested"}')

        result = list(iterate_metadata_files(metadata_dir))
        assert len(result) == 2
        levels = {r[1]["level"] for r in result}
        assert levels == {"top", "nested"}

    def test_uses_cache_when_enabled(self, tmp_path: Path) -> None:
        """Test caching behavior."""
        metadata_dir = tmp_path / "metadata"
        metadata_dir.mkdir()
        (metadata_dir / "file.json").write_text('{"rule": "test"}')

        # Clear the global cache first
        get_metadata_cache().clear()

        # First iteration should populate cache
        list(iterate_metadata_files(metadata_dir, use_cache=True))
        assert len(get_metadata_cache()) > 0

        # Clear cache for clean test
        get_metadata_cache().clear()

        # With use_cache=False, should not populate cache
        list(iterate_metadata_files(metadata_dir, use_cache=False))
        assert len(get_metadata_cache()) == 0


class TestScanCache:
    """Tests for _ScanCache class."""

    @pytest.fixture
    def cache(self) -> _ScanCache:
        """Create a fresh _ScanCache instance."""
        return _ScanCache()

    def test_first_call_performs_full_scan(self, tmp_path: Path, cache: _ScanCache) -> None:
        """First call scans the directory and returns all files."""
        d = tmp_path / "meta"
        d.mkdir()
        (d / "a.json").write_text("{}")
        (d / "b.json").write_text("{}")

        result = cache.get_files(d)
        assert len(result) == 2
        names = {f.path.name for f in result}
        assert names == {"a.json", "b.json"}

    def test_no_change_returns_cached(self, tmp_path: Path, cache: _ScanCache) -> None:
        """Subsequent call with no changes returns the same tuple object."""
        d = tmp_path / "meta"
        d.mkdir()
        (d / "a.json").write_text("{}")

        first = cache.get_files(d)
        second = cache.get_files(d)
        # Same object — no re-allocation
        assert first is second

    def test_adding_file_triggers_rescan(self, tmp_path: Path, cache: _ScanCache) -> None:
        """Adding a file to a directory triggers rescan of that directory."""
        d = tmp_path / "meta"
        d.mkdir()
        (d / "a.json").write_text("{}")

        result1 = cache.get_files(d)
        assert len(result1) == 1

        # Add a file — directory mtime changes
        time.sleep(0.05)
        (d / "b.json").write_text("{}")

        result2 = cache.get_files(d)
        assert len(result2) == 2
        names = {f.path.name for f in result2}
        assert names == {"a.json", "b.json"}

    def test_removing_file_triggers_rescan(self, tmp_path: Path, cache: _ScanCache) -> None:
        """Removing a file from a directory triggers rescan of that directory."""
        d = tmp_path / "meta"
        d.mkdir()
        (d / "a.json").write_text("{}")
        (d / "b.json").write_text("{}")

        result1 = cache.get_files(d)
        assert len(result1) == 2

        # Remove a file — directory mtime changes
        time.sleep(0.05)
        (d / "a.json").unlink()

        result2 = cache.get_files(d)
        assert len(result2) == 1
        assert result2[0].path.name == "b.json"

    def test_removing_directory_removes_its_files(self, tmp_path: Path, cache: _ScanCache) -> None:
        """Removing an entire subdirectory removes its files from the cache."""
        d = tmp_path / "meta"
        sub = d / "sub"
        sub.mkdir(parents=True)
        (d / "top.json").write_text("{}")
        (sub / "nested.json").write_text("{}")

        result1 = cache.get_files(d)
        assert len(result1) == 2

        # Remove subdirectory
        import shutil

        time.sleep(0.05)
        shutil.rmtree(sub)

        result2 = cache.get_files(d)
        assert len(result2) == 1
        assert result2[0].path.name == "top.json"

    def test_different_root_triggers_full_rescan(self, tmp_path: Path, cache: _ScanCache) -> None:
        """Calling get_files with a different directory triggers a full rescan."""
        d1 = tmp_path / "dir1"
        d2 = tmp_path / "dir2"
        d1.mkdir()
        d2.mkdir()
        (d1 / "a.json").write_text("{}")
        (d2 / "b.json").write_text("{}")

        result1 = cache.get_files(d1)
        assert len(result1) == 1
        assert result1[0].path.name == "a.json"

        result2 = cache.get_files(d2)
        assert len(result2) == 1
        assert result2[0].path.name == "b.json"

    def test_clear_resets_state(self, tmp_path: Path, cache: _ScanCache) -> None:
        """clear() resets all cached state."""
        d = tmp_path / "meta"
        d.mkdir()
        (d / "a.json").write_text("{}")

        cache.get_files(d)
        cache.clear()

        # After clear, internal state is empty
        assert cache._files == ()
        assert cache._dir_mtimes == {}
        assert cache._root is None

    def test_subdirectory_files_preserved_on_parent_change(
        self, tmp_path: Path, cache: _ScanCache
    ) -> None:
        """Files in unchanged subdirectories are preserved when parent changes."""
        d = tmp_path / "meta"
        sub = d / "sub"
        sub.mkdir(parents=True)
        (d / "top.json").write_text("{}")
        (sub / "nested.json").write_text("{}")

        result1 = cache.get_files(d)
        assert len(result1) == 2

        # Change only the parent directory (add a file)
        time.sleep(0.05)
        (d / "new.json").write_text("{}")

        result2 = cache.get_files(d)
        assert len(result2) == 3
        names = {f.path.name for f in result2}
        assert "nested.json" in names  # subdirectory file preserved

    @pytest.fixture(autouse=True)
    def _clear_global_scan_cache(self) -> None:
        """Clear the global scan cache before each test to avoid flaky tests."""
        get_scan_cache().clear()
