"""Backups: a zip of a Live Save, taken immediately before anything overwrites it.

For a Live Save that was overwritten before it was ever Synced, **the zip is the only copy of
that progress in existence**. Not one of several. The only one. Everything in this module
follows from that:

- **It is verified before it is trusted.** After writing, the archive is hashed under the
  same scheme as the Live Save it came from (`hashing.directory_digest`), and must match. A
  backup that silently failed to capture the save is worse than no backup at all, because we
  go on to overwrite the original believing we have it.
- **It appears atomically.** Written to a sibling `.tmp` and renamed into place, so a crash
  leaves no half-written zip that looks like a rescue.
- **It is pruned only after the operation it protected has succeeded.** Pruning first, to
  make room, would delete the oldest copy of a save in order to overwrite the newest.
- **It is a plain zip, in a plain folder, named for its timestamp and cause.** Reachable with
  Finder and unzippable by hand when this application is broken - which is exactly when
  someone will need it.
"""

from __future__ import annotations

import contextlib
import os
import re
import stat
import zipfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from core.hashing import (
    directory_digest,
    file_digest,
    hash_stream,
    hash_symlink,
)
from core.jsonstore import staging_path
from core.paths import Paths

STAMP = "%Y%m%dT%H%M%SZ"
_NAME = re.compile(r"^(?P<stamp>\d{8}T\d{6}Z)(?:-(?P<counter>\d+))?-(?P<reason>[a-z0-9-]+)\.zip$")

DIRECTORY = b"gsm-dir"
SINGLE_FILE = b"gsm-file"
"""The archive comment records which kind of Entry this was.

A one-file directory and a single-file Entry hold the same bytes and must not restore as one
another - the first is a folder containing a save, the second *is* the save.
"""


class BackupUnverified(Exception):
    """The archive did not hash back to the content it was taken from. It is not a backup."""


@dataclass(frozen=True)
class Backup:
    """One archived copy of an Entry's Live Save, as the Backups view shows it."""

    path: Path
    entry_id: str
    taken_at: datetime
    reason: str
    size_bytes: int


def directory_for(paths: Paths, entry_id: str) -> Path:
    return paths.backups_dir / entry_id


def _members(archive: zipfile.ZipFile) -> list[zipfile.ZipInfo]:
    return [info for info in archive.infolist() if not info.is_dir()]


def _is_symlink(info: zipfile.ZipInfo) -> bool:
    return stat.S_ISLNK(info.external_attr >> 16)


def _member_digest(archive: zipfile.ZipFile, info: zipfile.ZipInfo) -> str:
    if _is_symlink(info):
        return hash_symlink(archive.read(info).decode("utf-8"))
    with archive.open(info) as reader:
        return hash_stream(reader)


def archive_hash(path: Path) -> str | None:
    """The content hash of what is *inside* an archive, under the Entry hashing scheme.

    Comparable directly against `hashing.content_hash` of a Live Save, which is the whole
    point: it is how we prove a backup captured what it claims to have captured.
    """
    with zipfile.ZipFile(path) as archive:
        members = _members(archive)
        if not members:
            return None
        if archive.comment == SINGLE_FILE:
            return file_digest(_member_digest(archive, members[0]))
        return directory_digest((info.filename, _member_digest(archive, info)) for info in members)


def contents(path: Path) -> dict[str, int]:
    """Relative posix path -> uncompressed size, for the restore preview."""
    with zipfile.ZipFile(path) as archive:
        return {info.filename: info.file_size for info in _members(archive)}


def _write_archive(destination: Path, live: Path) -> None:
    """Zip a Live Save. Symlinks are stored as links, never as the content they point at."""
    with zipfile.ZipFile(destination, "w", zipfile.ZIP_DEFLATED) as archive:
        if live.is_dir() and not live.is_symlink():
            archive.comment = DIRECTORY
            for current, dirs, files in os.walk(live, followlinks=False):
                dirs.sort()
                for name in sorted(files):
                    absolute = Path(current) / name
                    relative = absolute.relative_to(live).as_posix()
                    _add(archive, absolute, relative)
        else:
            archive.comment = SINGLE_FILE
            _add(archive, live, live.name)


def _add(archive: zipfile.ZipFile, source: Path, arcname: str) -> None:
    if source.is_symlink():
        # Stored as a link. `ZipFile.write` would follow it and bake in the target's bytes,
        # which would both bloat the archive and quietly turn a link into a real file on
        # restore - changing the Entry's content hash and stranding it as never In Sync.
        info = zipfile.ZipInfo(arcname)
        info.create_system = 3  # Unix, so the mode bits below are read back
        info.external_attr = (stat.S_IFLNK | 0o777) << 16
        archive.writestr(info, os.readlink(source))
    else:
        archive.write(source, arcname)


def extract(backup: Backup | Path, destination: Path) -> None:
    """Materialize an archive's contents at `destination`, recreating symlinks as links."""
    path = backup.path if isinstance(backup, Backup) else backup

    with zipfile.ZipFile(path) as archive:
        members = _members(archive)

        if archive.comment == SINGLE_FILE:
            destination.parent.mkdir(parents=True, exist_ok=True)
            _extract_one(archive, members[0], destination)
            return

        destination.mkdir(parents=True, exist_ok=True)
        for info in members:
            target = destination / info.filename
            target.parent.mkdir(parents=True, exist_ok=True)
            _extract_one(archive, info, target)


def _extract_one(archive: zipfile.ZipFile, info: zipfile.ZipInfo, target: Path) -> None:
    if _is_symlink(info):
        target.symlink_to(archive.read(info).decode("utf-8"))
        return
    with archive.open(info) as reader, target.open("wb") as writer:
        while chunk := reader.read(1024 * 1024):
            writer.write(chunk)


def _unique_path(folder: Path, stamp: str, reason: str) -> Path:
    """A distinct name even for two backups within the same second."""
    candidate = folder / f"{stamp}-{reason}.zip"
    counter = 2
    while candidate.exists():
        candidate = folder / f"{stamp}-{counter}-{reason}.zip"
        counter += 1
    return candidate


def create(
    paths: Paths,
    entry_id: str,
    live: Path,
    reason: str,
    expected_hash: str,
    at: datetime | None = None,
) -> Backup:
    """Archive a Live Save, and refuse to return one that does not hash back to it.

    `expected_hash` is the caller's `content_hash` of the very bytes it is about to overwrite.
    We re-derive the hash from *inside the finished archive* and require a match, so a backup
    is never merely assumed to have worked. A failed verification deletes the archive and
    raises: better to abort the operation with the save untouched than to proceed protected
    by a zip that does not hold it.
    """
    if not _NAME.match(f"20000101T000000Z-{reason}.zip"):
        raise ValueError(f"unusable backup reason: {reason!r}")

    folder = directory_for(paths, entry_id)
    folder.mkdir(parents=True, exist_ok=True)

    stamp = (at or datetime.now(UTC)).strftime(STAMP)
    destination = _unique_path(folder, stamp, reason)
    staging = staging_path(destination)

    try:
        _write_archive(staging, live)

        found = archive_hash(staging)
        if found != expected_hash:
            raise BackupUnverified(
                f"The backup of {live} does not match the save it was taken from "
                f"(expected {expected_hash}, archive holds {found}). Nothing has been "
                "overwritten. The Live Save may be changing on disk - close the game."
            )

        os.replace(staging, destination)
    except BaseException:
        staging.unlink(missing_ok=True)
        raise

    return _describe(destination, entry_id)


def _describe(path: Path, entry_id: str) -> Backup:
    match = _NAME.match(path.name)
    if match is None:
        raise ValueError(f"not a backup filename: {path.name}")
    return Backup(
        path=path,
        entry_id=entry_id,
        taken_at=datetime.strptime(match["stamp"], STAMP).replace(tzinfo=UTC),
        reason=match["reason"],
        size_bytes=path.stat().st_size,
    )


def list_for(paths: Paths, entry_id: str) -> list[Backup]:
    """Every backup of this Entry, newest first. Unrecognized files are ignored, not deleted."""
    folder = directory_for(paths, entry_id)
    if not folder.is_dir():
        return []

    found = [
        _describe(path, entry_id) for path in sorted(folder.iterdir()) if _NAME.match(path.name)
    ]
    # Sorted by name, not by mtime: the timestamp is in the name, and a file's mtime is not
    # evidence of anything (a copied backups/ folder would reorder them all).
    found.sort(key=lambda backup: backup.path.name, reverse=True)
    return found


def prune(paths: Paths, entry_id: str, keep: int) -> list[Backup]:
    """Delete all but the newest `keep` backups. **Only ever after a successful operation.**

    Pruning to make room, before the write, would delete the oldest copy of a save in order
    to overwrite the newest - and if the write then failed, we would have destroyed a backup
    for nothing.
    """
    if keep < 1:
        raise ValueError("keep at least one backup")

    doomed = list_for(paths, entry_id)[keep:]
    for backup in doomed:
        with contextlib.suppress(OSError):
            backup.path.unlink()
    return doomed
