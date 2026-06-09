"""Tests for EventWriter."""

import threading
from pathlib import Path
from unittest.mock import patch

from snakemake_logger_plugin_snakesee.events import EventType, SnakeseeEvent
from snakemake_logger_plugin_snakesee.writer import EventWriter


class TestEventWriter:
    """Tests for EventWriter class."""

    def test_write_single_event(self, tmp_path: Path) -> None:
        """Test writing a single event."""
        event_file = tmp_path / "events.jsonl"
        event = SnakeseeEvent(
            event_type=EventType.PROGRESS,
            timestamp=1234567890.123,
            completed_jobs=1,
            total_jobs=10,
        )

        with EventWriter(event_file) as writer:
            writer.write(event)

        content = event_file.read_text()
        assert content.strip() == event.to_json()

    def test_write_multiple_events(self, tmp_path: Path) -> None:
        """Test writing multiple events."""
        event_file = tmp_path / "events.jsonl"
        events = [
            SnakeseeEvent(
                event_type=EventType.WORKFLOW_STARTED,
                timestamp=1234567890.0,
            ),
            SnakeseeEvent(
                event_type=EventType.JOB_SUBMITTED,
                timestamp=1234567890.1,
                job_id=1,
                rule_name="align",
            ),
            SnakeseeEvent(
                event_type=EventType.JOB_STARTED,
                timestamp=1234567890.2,
                job_id=1,
            ),
        ]

        with EventWriter(event_file) as writer:
            for event in events:
                writer.write(event)

        lines = event_file.read_text().strip().split("\n")
        assert len(lines) == 3

        for i, line in enumerate(lines):
            parsed = SnakeseeEvent.from_json(line)
            assert parsed.timestamp == events[i].timestamp

    def test_buffering(self, tmp_path: Path) -> None:
        """Test that buffering works correctly."""
        event_file = tmp_path / "events.jsonl"

        # Buffer size of 3 means events are written in batches
        writer = EventWriter(event_file, buffer_size=3)

        # Write 2 events - should not be flushed yet
        writer.write(SnakeseeEvent(event_type=EventType.PROGRESS, timestamp=1.0, total_jobs=10))
        writer.write(SnakeseeEvent(event_type=EventType.PROGRESS, timestamp=2.0, total_jobs=10))

        # File should not exist yet or be empty
        if event_file.exists():
            assert event_file.read_text() == ""

        # Write 3rd event - should trigger flush
        writer.write(SnakeseeEvent(event_type=EventType.PROGRESS, timestamp=3.0, total_jobs=10))

        # Now all 3 events should be written
        lines = event_file.read_text().strip().split("\n")
        assert len(lines) == 3

        writer.close()

    def test_close_flushes_buffer(self, tmp_path: Path) -> None:
        """Test that close() flushes remaining buffered events."""
        event_file = tmp_path / "events.jsonl"

        writer = EventWriter(event_file, buffer_size=10)
        writer.write(SnakeseeEvent(event_type=EventType.PROGRESS, timestamp=1.0, total_jobs=10))

        # Before close, file might not have content
        writer.close()

        # After close, event should be written
        lines = event_file.read_text().strip().split("\n")
        assert len(lines) == 1

    def test_context_manager(self, tmp_path: Path) -> None:
        """Test context manager usage."""
        event_file = tmp_path / "events.jsonl"

        with EventWriter(event_file) as writer:
            writer.write(SnakeseeEvent(event_type=EventType.PROGRESS, timestamp=1.0, total_jobs=10))

        # After exiting context, file should have content
        assert event_file.exists()
        lines = event_file.read_text().strip().split("\n")
        assert len(lines) == 1

    def test_creates_parent_directories(self, tmp_path: Path) -> None:
        """Test that parent directories are created if they don't exist."""
        event_file = tmp_path / "nested" / "path" / "events.jsonl"

        with EventWriter(event_file) as writer:
            writer.write(SnakeseeEvent(event_type=EventType.PROGRESS, timestamp=1.0, total_jobs=10))

        assert event_file.exists()

    def test_append_to_existing_file(self, tmp_path: Path) -> None:
        """Test appending to an existing file."""
        event_file = tmp_path / "events.jsonl"

        # Write first event
        with EventWriter(event_file) as writer:
            writer.write(SnakeseeEvent(event_type=EventType.PROGRESS, timestamp=1.0, total_jobs=10))

        # Append second event
        with EventWriter(event_file) as writer:
            writer.write(SnakeseeEvent(event_type=EventType.PROGRESS, timestamp=2.0, total_jobs=10))

        # Both events should be present
        lines = event_file.read_text().strip().split("\n")
        assert len(lines) == 2

    def test_immediate_flush_default(self, tmp_path: Path) -> None:
        """Test that default buffer_size=1 causes immediate flush."""
        event_file = tmp_path / "events.jsonl"

        writer = EventWriter(event_file)  # Default buffer_size=1
        writer.write(SnakeseeEvent(event_type=EventType.PROGRESS, timestamp=1.0, total_jobs=10))

        # Should be immediately written
        lines = event_file.read_text().strip().split("\n")
        assert len(lines) == 1

        writer.close()

    def test_truncate_clears_existing_file(self, tmp_path: Path) -> None:
        """Test that truncate clears existing file content."""
        event_file = tmp_path / "events.jsonl"

        # Write some initial events
        with EventWriter(event_file) as writer:
            writer.write(SnakeseeEvent(event_type=EventType.PROGRESS, timestamp=1.0, total_jobs=10))
            writer.write(SnakeseeEvent(event_type=EventType.PROGRESS, timestamp=2.0, total_jobs=10))

        # Verify initial content
        lines = event_file.read_text().strip().split("\n")
        assert len(lines) == 2

        # Truncate and write new event
        writer = EventWriter(event_file)
        writer.truncate()
        writer.write(SnakeseeEvent(event_type=EventType.WORKFLOW_STARTED, timestamp=3.0))
        writer.close()

        # Only new event should be present
        lines = event_file.read_text().strip().split("\n")
        assert len(lines) == 1
        parsed = SnakeseeEvent.from_json(lines[0])
        assert parsed.event_type == EventType.WORKFLOW_STARTED
        assert parsed.timestamp == 3.0

    def test_concurrent_write_and_close_loses_nothing(self, tmp_path: Path) -> None:
        """close() during concurrent writes must not lose events or raise.

        Reproduces Snakemake's race: cleanup calls handler.close() from the
        main thread while the QueueListener background thread is still
        delivering events via emit()/write(). Every event must reach the file
        regardless of how the close() interleaves with the concurrent writes.
        """
        event_file = tmp_path / "events.jsonl"
        writer = EventWriter(event_file)
        num_events = 200

        # Write one event first to ensure the file exists
        writer.write(SnakeseeEvent(event_type=EventType.WORKFLOW_STARTED, timestamp=0.0))

        barrier = threading.Barrier(2)

        def writer_thread() -> None:
            barrier.wait()
            for i in range(num_events):
                writer.write(
                    SnakeseeEvent(
                        event_type=EventType.JOB_STARTED,
                        timestamp=float(i + 1),
                        job_id=i,
                    )
                )

        t = threading.Thread(target=writer_thread)
        t.start()
        barrier.wait()
        # Close from main thread while writer thread is active
        writer.close()
        t.join()
        # Final close, mirroring snakemake's post-drain / atexit cleanup.
        writer.close()

        # Nothing may be dropped: the initial event plus every concurrent write.
        lines = event_file.read_text().strip().split("\n")
        assert len(lines) == num_events + 1

    def test_write_after_close_reopens_and_appends(self, tmp_path: Path) -> None:
        """write() after close() reopens the file and appends (no loss, no raise).

        See ``test_events_delivered_after_close_are_not_lost`` for the
        snakemake lifecycle this guards against.
        """
        event_file = tmp_path / "events.jsonl"
        writer = EventWriter(event_file)
        writer.write(SnakeseeEvent(event_type=EventType.WORKFLOW_STARTED, timestamp=1.0))
        writer.close()

        # Write after close must not raise and must not be dropped.
        writer.write(SnakeseeEvent(event_type=EventType.JOB_STARTED, timestamp=2.0, job_id=1))
        writer.close()

        lines = event_file.read_text().strip().split("\n")
        assert len(lines) == 2

    def test_events_delivered_after_close_are_not_lost(self, tmp_path: Path) -> None:
        """Events delivered after close() must be written, not dropped.

        Regression test for silent event loss under load. Snakemake closes
        file-writing handlers (``cleanup_logfile()``) BEFORE it drains the
        logging ``QueueListener`` (``stop()``). Every record still queued at
        that moment is delivered to the handler — and therefore ``write()`` —
        *after* ``close()`` has run. Those late events must still reach the
        file; dropping them silently loses the tail of the event stream
        (job completions, final progress) whenever the listener lags under
        load, which is exactly what happens on a busy CI runner.
        """
        event_file = tmp_path / "events.jsonl"
        writer = EventWriter(event_file)

        writer.write(SnakeseeEvent(event_type=EventType.WORKFLOW_STARTED, timestamp=1.0))
        writer.close()  # snakemake cleanup_logfile() closes us first

        # snakemake's QueueListener.stop() now drains queued records to us
        post_close = [
            SnakeseeEvent(event_type=EventType.JOB_STARTED, timestamp=2.0, job_id=1),
            SnakeseeEvent(event_type=EventType.JOB_FINISHED, timestamp=3.0, job_id=1),
        ]
        for event in post_close:
            writer.write(event)
        writer.close()

        lines = event_file.read_text().strip().split("\n")
        assert len(lines) == 3, f"expected 3 events, lost some: {lines}"
        types = [SnakeseeEvent.from_json(line).event_type for line in lines]
        assert types == [
            EventType.WORKFLOW_STARTED,
            EventType.JOB_STARTED,
            EventType.JOB_FINISHED,
        ]

    def test_buffered_late_events_flush_on_trailing_close(self, tmp_path: Path) -> None:
        """With buffer_size > 1, a post-close event must flush on the trailing close().

        Guards the ``_closed`` reset in ``write()``. ``buffer_size`` is
        configurable (``LogHandlerSettings.buffer_size``); when > 1 a late event
        is buffered, not flushed immediately, so the reset is what lets the
        trailing ``close()`` flush it instead of early-returning and dropping it.
        The ``buffer_size=1`` cases above flush each write immediately and so
        would still pass even if the reset were missing — this case would not.
        """
        event_file = tmp_path / "events.jsonl"
        writer = EventWriter(event_file, buffer_size=3)

        writer.write(SnakeseeEvent(event_type=EventType.WORKFLOW_STARTED, timestamp=1.0))
        writer.close()  # flushes the buffered first event
        assert len(event_file.read_text().strip().split("\n")) == 1

        # Delivered after close() and buffered (buffer not yet full → not flushed).
        writer.write(SnakeseeEvent(event_type=EventType.JOB_STARTED, timestamp=2.0, job_id=1))
        assert len(event_file.read_text().strip().split("\n")) == 1

        writer.close()  # trailing close() must flush the buffered late event
        lines = event_file.read_text().strip().split("\n")
        assert len(lines) == 2
        assert SnakeseeEvent.from_json(lines[1]).event_type == EventType.JOB_STARTED

    def test_double_close(self, tmp_path: Path) -> None:
        """Test that close() can be called multiple times safely."""
        event_file = tmp_path / "events.jsonl"
        writer = EventWriter(event_file)
        writer.write(SnakeseeEvent(event_type=EventType.WORKFLOW_STARTED, timestamp=1.0))
        writer.close()
        writer.close()  # Should not raise

        lines = event_file.read_text().strip().split("\n")
        assert len(lines) == 1

    def test_close_releases_file_handle_on_flush_error(self, tmp_path: Path) -> None:
        """Test that close() releases the file handle even if flush raises."""
        event_file = tmp_path / "events.jsonl"
        writer = EventWriter(event_file, buffer_size=10)
        writer.write(SnakeseeEvent(event_type=EventType.WORKFLOW_STARTED, timestamp=1.0))

        # Force an error during the flush inside close().
        # The exception propagates (caller is responsible for catching),
        # but the file handle must still be released.
        with patch.object(writer, "_flush_locked", side_effect=OSError("disk full")):
            try:
                writer.close()
            except OSError:
                pass

        # File handle must be released despite the flush error
        assert writer._file is None
        assert writer._closed is True
