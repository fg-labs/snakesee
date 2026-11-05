"""Incremental reading of a growing text file and of file tails."""

from __future__ import annotations

import os
from pathlib import Path


class FileTailer:
    """Return only the complete lines appended to a file since the previous call.

    Keeps a byte offset and any trailing partial line between calls. If the file
    shrinks (truncated or replaced) it starts again from the beginning and
    ``reset`` is reported so the caller can discard derived state.
    """

    def __init__(self, path: Path, max_chunk: int = 8 * 1024 * 1024) -> None:
        self.path = path
        self.max_chunk = max_chunk
        self._offset = 0
        self._partial = b""
        self._inode: int | None = None

    @property
    def offset(self) -> int:
        """Bytes consumed so far."""
        return self._offset

    def read_new_lines(self) -> tuple[list[str], bool, bool]:
        """Read up to ``max_chunk`` new bytes.

        Returns:
            ``(lines, reset, more)``: complete new lines; whether the file was
            truncated/replaced (caller should discard state first); and whether
            more data remains to be read right away.
        """
        try:
            st = os.stat(self.path)
        except OSError:
            return [], False, False
        reset = False
        if (self._inode is not None and st.st_ino != self._inode) or st.st_size < self._offset:
            self._offset = 0
            self._partial = b""
            reset = True
        self._inode = st.st_ino
        if st.st_size == self._offset:
            return [], reset, False
        with open(self.path, "rb") as fh:
            fh.seek(self._offset)
            data = fh.read(self.max_chunk)
        self._offset += len(data)
        data = self._partial + data
        parts = data.split(b"\n")
        self._partial = parts.pop()
        lines = [p.decode("utf-8", errors="replace").rstrip("\r") for p in parts]
        return lines, reset, self._offset < st.st_size


def read_tail(path: Path, max_bytes: int = 2 * 1024 * 1024) -> tuple[list[str], bool]:
    """Read the last ``max_bytes`` of a file as lines.

    Returns:
        ``(lines, truncated)`` where ``truncated`` is True if earlier content was skipped.

    Raises:
        OSError: if the file cannot be read.
    """
    with open(path, "rb") as fh:
        fh.seek(0, os.SEEK_END)
        size = fh.tell()
        start = max(0, size - max_bytes)
        fh.seek(start)
        data = fh.read()
    lines = data.decode("utf-8", errors="replace").splitlines()
    if start > 0 and lines:
        lines = lines[1:]  # first line is probably partial
    return lines, start > 0
