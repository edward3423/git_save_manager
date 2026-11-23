"""The single-instance lock: `data/app.lock`, held by the operating system.

Two instances running Git against one Vault and both writing the Ledger corrupts state - a
lost Baseline means a wrong direction recommendation - so the second instance refuses to
start. That covers the window and the command line alike: `gsm sync` while the window is
open would have its Ledger changes overwritten by the window's next save.

## Why the operating system holds it

With the command line, a second instance is no longer a stray double-click. A script or an
agent can start several `gsm` commands in the same instant, and a lock taken by "read the
file, see no live owner, write my PID" lets two processes that start together both read it
free and both proceed. So the lock is the OS's own - `flock` on POSIX, a byte-range lock on
Windows - taken on an open handle in a single step that exactly one process can win.

It also dies with its owner, however the owner dies. A crash cannot leave a stale lock
behind, so there is no staleness to judge and no PID liveness to guess at. The file still
records the owner's PID, but only so the refusal can name it.

The file itself is never deleted. Unlinking a lock file lets a process that opened the old
file just before the unlink lock it while a newcomer creates and locks a new one - two
owners. Leaving the file in place costs nothing; its existence means nothing on its own.
"""

from __future__ import annotations

import contextlib
import os
import sys
from dataclasses import dataclass
from pathlib import Path

from core.paths import Paths

_WINDOWS_LOCK_OFFSET = 1 << 30
"""Windows locks a byte range, and a locked range cannot be read by anyone else - not even to
learn the owner's PID. So the lock sits on one byte far past the end of the file, where it
covers nothing that is ever written."""


class AlreadyRunning(Exception):
    """Another live instance holds the lock. This one must not touch anything."""


def _try_lock(fd: int) -> bool:
    """Take the lock without waiting. False if another handle holds it."""
    try:
        if sys.platform == "win32":
            import msvcrt

            os.lseek(fd, _WINDOWS_LOCK_OFFSET, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return False
    return True


def _unlock(fd: int) -> None:
    if sys.platform == "win32":
        import msvcrt

        os.lseek(fd, _WINDOWS_LOCK_OFFSET, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        fcntl.flock(fd, fcntl.LOCK_UN)


@dataclass
class Lock:
    """Held for the lifetime of the process; released on clean shutdown, or by its exit."""

    path: Path
    fd: int | None

    def release(self) -> None:
        """Let the next instance in. Safe to call more than once."""
        if self.fd is None:
            return
        fd, self.fd = self.fd, None
        with contextlib.suppress(OSError):
            os.ftruncate(fd, 0)  # while still held: the PID of a finished run names no one
            _unlock(fd)
        os.close(fd)


def _holder(path: Path) -> int | None:
    """The PID recorded in the lock file, or None if it is unreadable or not written yet."""
    try:
        return int(path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None


def acquire(paths: Paths) -> Lock:
    """Take the lock, or raise `AlreadyRunning` if another instance holds it."""
    path = paths.lock_file
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)

    if not _try_lock(fd):
        os.close(fd)
        holder = _holder(path)
        who = f" (PID {holder})" if holder is not None else ""
        raise AlreadyRunning(
            f"Another instance is already running{who} - the window, or another gsm command. "
            "Two instances writing one Vault would corrupt it."
        )

    # Ours now. Whatever an earlier owner left in the file is replaced by our own PID.
    os.ftruncate(fd, 0)
    os.lseek(fd, 0, os.SEEK_SET)
    os.write(fd, str(os.getpid()).encode("ascii"))
    return Lock(path=path, fd=fd)
