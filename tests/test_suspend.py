"""ctrl+z in a real pseudo-terminal under an interactive bash, with and without a parent
process between the shell and snakesee (as with ``uv run`` / ``uvx``)."""

from __future__ import annotations

import fcntl
import os
import pty
import select
import shlex
import shutil
import signal
import struct
import sys
import termios
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")


class Terminal:
    def __init__(self) -> None:
        self.pid, self.fd = pty.fork()
        if self.pid == 0:  # child: interactive shell with job control
            os.environ.update(TERM="xterm-256color", PS1="PROMPT$ ")
            os.execvp("bash", ["bash", "--norc", "--noprofile", "-i"])
        fcntl.ioctl(self.fd, termios.TIOCSWINSZ, struct.pack("HHHH", 30, 120, 0, 0))
        self.buf = b""

    def send(self, data: bytes) -> None:
        os.write(self.fd, data)

    def expect(self, needle: bytes, timeout: float = 10.0) -> None:
        deadline = time.monotonic() + timeout
        while needle not in self.buf:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise AssertionError(f"{needle!r} not seen; got tail {self.buf[-500:]!r}")
            ready, _, _ = select.select([self.fd], [], [], remaining)
            if ready:
                try:
                    self.buf += os.read(self.fd, 65536)
                except OSError:
                    break

    def close(self) -> None:
        try:
            os.kill(self.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        os.waitpid(self.pid, 0)
        os.close(self.fd)


@pytest.mark.parametrize("wrapped", [False, True], ids=["direct", "behind-parent"])
def test_single_ctrl_z_returns_to_shell_and_fg_resumes(workflow: Path, wrapped: bool) -> None:
    cmd = f"{shlex.quote(sys.executable)} -m snakesee {shlex.quote(str(workflow))}"
    if wrapped:  # a parent that stays alive, like uv: sh runs the child, then `true`
        cmd = f"sh -c {shlex.quote(cmd + '; true')}"
    term = Terminal()
    try:
        term.expect(b"PROMPT$ ")
        term.send(f"cd {shlex.quote(os.getcwd())}; {cmd}\r".encode())
        term.expect(b"steps")
        for _ in range(2):  # two full cycles
            term.buf = b""
            term.send(b"\x1a")  # ctrl+z
            term.expect(b"Stopped")
            term.expect(b"PROMPT$ ")
            term.buf = b""
            term.send(b"fg\r")
            term.expect(b"steps")  # redrawn
        term.buf = b""
        term.send(b"q")
        term.expect(b"PROMPT$ ")
    finally:
        term.close()
