"""The single-instance lock: `data/app.lock`.

Two instances running Git against one Vault and both writing the Ledger corrupts state -
a lost Baseline means a wrong direction recommendation. The second instance must refuse to
start, and a lock left by a dead process must not wedge the app forever.
"""

import os
import subprocess
import sys

import pytest

from core import lock
from core.paths import Paths


@pytest.fixture
def paths(tmp_path):
    found = Paths(root=tmp_path)
    found.data_dir.mkdir(parents=True)
    return found


def dead_pid() -> int:
    """A PID guaranteed not to be running: a real process that has already exited."""
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    return proc.pid  # wait() has returned, so this PID is free


def test_the_first_instance_acquires_and_the_second_is_refused(paths):
    held = lock.acquire(paths)

    with pytest.raises(lock.AlreadyRunning):
        lock.acquire(paths)

    held.release()


def test_releasing_lets_the_next_instance_in(paths):
    lock.acquire(paths).release()

    lock.acquire(paths).release()  # would raise if the first were still held


def test_a_lock_left_by_a_dead_process_is_taken_over(paths):
    """A crash leaves the lock behind - there is no atexit cleanup to trust. The next launch
    must start normally, not wedge until someone deletes a file by hand."""
    paths.lock_file.write_text(str(dead_pid()), encoding="utf-8")

    held = lock.acquire(paths)

    assert paths.lock_file.read_text(encoding="utf-8") == str(os.getpid())
    held.release()


def test_a_garbled_lock_file_is_treated_as_stale(paths):
    """A crash mid-write, or a stray editor. Refusing over unreadable bytes would wedge the
    app exactly like a dead PID would."""
    paths.lock_file.write_text("not a pid", encoding="utf-8")

    lock.acquire(paths).release()


def test_the_refusal_names_the_holder_and_leaves_its_lock_alone(paths):
    held = lock.acquire(paths)

    with pytest.raises(lock.AlreadyRunning) as caught:
        lock.acquire(paths)

    assert str(os.getpid()) in str(caught.value)
    assert paths.lock_file.read_text(encoding="utf-8") == str(os.getpid())
    held.release()
