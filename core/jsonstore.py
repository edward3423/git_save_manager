"""Atomic JSON reads and writes for local state (config, Ledger, journal).

A half-written Ledger is a lost Baseline, and a lost Baseline is a wrong direction
recommendation - so these files are never written in place. We stage to a sibling of the
destination, fsync, and swap with `os.replace`.

The sibling matters: an atomic rename only holds within one filesystem. Staging anywhere
else (a temp dir, the app folder) risks a cross-device rename, which raises `EXDEV` at best
and silently degrades to a non-atomic copy at worst.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any


class CorruptJson(Exception):
    """The file exists but does not contain valid JSON."""


def staging_path(path: Path) -> Path:
    """The path we write to before swapping. Always a sibling, so the swap is atomic."""
    return path.with_name(path.name + ".tmp")


def read_json(path: Path) -> dict[str, Any] | None:
    """Return the parsed contents, or None if the file does not exist."""
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise CorruptJson(f"{path} is not valid JSON: {exc}") from exc


def write_json(path: Path, data: dict[str, Any]) -> None:
    """Write `data` to `path` atomically: a reader sees either the old file or the new one."""
    path.parent.mkdir(parents=True, exist_ok=True)
    staging = staging_path(path)

    with staging.open("w", encoding="utf-8") as handle:
        json.dump(data, handle, indent=2, sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())

    os.replace(staging, path)
    _fsync_dir(path.parent)


def _fsync_dir(directory: Path) -> None:
    """Persist the rename itself, so a crash cannot leave the swap half-done.

    Not supported on every platform (notably Windows); harmless to skip where it isn't.
    """
    try:
        fd = os.open(directory, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)
