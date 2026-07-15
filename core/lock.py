"""The single-instance lock: `data/app.lock`, holding the owner's PID.

Two instances running Git against one Vault and both writing the Ledger corrupts state - a
lost Baseline means a wrong direction recommendation - so the second instance refuses to
start. The lock outlives a crash by design (there is no atexit cleanup to trust), which is
why it records the owning PID: a lock whose owner is dead is stale, and stale locks are
taken over rather than wedging the app until someone deletes a file by hand.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from core.paths import Paths


class AlreadyRunning(Exception):
    """Another live instance holds the lock. This one must not touch anything."""


@dataclass(frozen=True)
class Lock:
    """Held for the lifetime of the process; released on clean shutdown."""

    path: Path

    def release(self) -> None:
        self.path.unlink(missing_ok=True)


def _alive(pid: int) -> bool:
    """Is any process running under this PID? `kill(pid, 0)` delivers no signal; it only
    checks. A PermissionError means the PID exists but belongs to someone else - alive."""
    try:
        os.kill(pid, 0)
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _holder(path: Path) -> int | None:
    """The PID recorded in the lock file, or None if the file is missing or garbled.

    Garbage in the file - a crash mid-write, a stray editor - is treated as stale: refusing
    to start over an unreadable file would wedge the app exactly like a dead PID would.
    """
    try:
        return int(path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None


def acquire(paths: Paths) -> Lock:
    """Take the lock, or raise `AlreadyRunning` if a live process already holds it."""
    path = paths.lock_file

    holder = _holder(path)
    if holder is not None and _alive(holder):
        raise AlreadyRunning(
            f"Another instance is already running (PID {holder}). "
            "Two instances writing one Vault would corrupt it."
        )

    path.write_text(str(os.getpid()), encoding="utf-8")
    return Lock(path=path)
