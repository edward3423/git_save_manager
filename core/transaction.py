"""The only code in this application that ever overwrites a Live Save.

Every path that writes to a game's save folder - Restore from the Vault, resolving a Conflict
toward the Vault, restoring a Backup, and (via Rollback landing the Entry in Vault Ahead)
rolling back - funnels through `write_live`. One function to review, one function to get
right, and no second implementation to drift out of agreement with it.

## Never torn

A save is overwritten by **staging a complete copy beside it and swapping**, never by writing
into it. The swap is two atomic renames:

    1. target  -> target.old      (the save is now absent, and this is the only unsafe instant)
    2. staged  -> target          (the save is present again, with the new content)
    3. delete target.old

At *every* instant the Live Save is therefore one of: entirely the old content, entirely the
new content, or absent - and absent is recoverable from whichever of `staged` or `target.old`
is on disk. It is never a half-copied mixture of both, which is the state a game will happily
load and then corrupt.

Both `staged` and `target.old` are **siblings of the target**, and that is load-bearing rather
than tidy. `os.rename` is atomic only within a single filesystem; across one it raises
`EXDEV`, and `shutil.move` quietly degrades to a non-atomic copy-then-delete - which is
precisely the torn write this design exists to prevent.

The *resolved* path is what gets swapped. A save folder that is a symlink to a second drive
must have its staging sit on that drive, next to the real directory - otherwise the rename
crosses a filesystem, and swapping the link itself would replace the user's symlink with a
real folder and strand every other program that follows it.

## The journal remembers only what we did

Git protects the Vault (ADR-0005), so the journal covers Live Save writes and nothing else.
It records the paths involved and how far we got; on startup, `recover` reads it and then
*looks at the disk*, because which of the three renames landed is a question the filesystem
can answer and the journal cannot - the crash could have happened between the rename and the
journal write. Remember only what you did; observe everything else.
"""

from __future__ import annotations

import contextlib
import os
import shutil
from collections.abc import Iterable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol

from core import backups
from core.backups import Backup
from core.config import Config
from core.hashing import content_hash
from core.jsonstore import fsync_dir, read_json, write_json
from core.logger import log
from core.paths import Paths

SCHEMA = 1


class StalePreview(Exception):
    """The world moved between showing the preview and executing it. We refuse to proceed."""


class LiveParentMissing(Exception):
    """The folder that should contain the Live Save does not exist, so we refuse to write.

    Deliberately *not* solved by creating it. The usual cause is an unmounted drive, and
    happily creating `/Volumes/Games/...` would write the save onto the boot disk, hidden
    under the mount point, where the user will never find it and the real drive will shadow
    it the moment it comes back.
    """


class Stage(StrEnum):
    """How far a Live Save write got. What we *did*, never what we observed."""

    BACKED_UP = "backed_up"
    """The old content is safely archived. Staging may be half-written; the save is untouched."""

    STAGED = "staged"
    """The new content is complete and fsynced beside the save. The swap may be in progress."""

    SWAPPED = "swapped"
    """Both renames landed. The save is the new content; only `target.old` remains to delete."""


class Outcome(StrEnum):
    COMPLETED = "completed"
    ROLLED_BACK = "rolled_back"


# --- what we are about to write ---------------------------------------------------------


class Source(Protocol):
    """Content bound for a Live Save. A Vault Entry, or a Backup archive."""

    def digest(self) -> str | None: ...

    def files(self) -> dict[str, int]:
        """Relative posix path -> size in bytes. For the preview."""
        ...

    def materialize(self, destination: Path) -> None: ...


@dataclass(frozen=True)
class TreeSource:
    """An Entry's content directory in the Vault, or any plain file or folder."""

    path: Path

    def digest(self) -> str | None:
        return content_hash(self.path)

    def files(self) -> dict[str, int]:
        return _measure(self.path)

    def materialize(self, destination: Path) -> None:
        if self.path.is_dir() and not self.path.is_symlink():
            shutil.copytree(self.path, destination, symlinks=True)
        else:
            shutil.copy2(self.path, destination, follow_symlinks=False)


@dataclass(frozen=True)
class BackupSource:
    """A backup archive, restored through the very same path as everything else."""

    backup: Backup

    def digest(self) -> str | None:
        return backups.archive_hash(self.backup.path)

    def files(self) -> dict[str, int]:
        return backups.contents(self.backup.path)

    def materialize(self, destination: Path) -> None:
        backups.extract(self.backup, destination)


def _measure(root: Path) -> dict[str, int]:
    if not root.exists() and not root.is_symlink():
        return {}
    if root.is_dir() and not root.is_symlink():
        found: dict[str, int] = {}
        for current, dirs, names in os.walk(root, followlinks=False):
            dirs.sort()
            for name in sorted(names):
                absolute = Path(current) / name
                found[absolute.relative_to(root).as_posix()] = absolute.lstat().st_size
        return found
    return {root.name: root.lstat().st_size}


# --- the preview (Invariant 7) -----------------------------------------------------------


class Change(StrEnum):
    ADD = "add"
    REPLACE = "replace"
    REMOVE = "remove"


@dataclass(frozen=True)
class FileChange:
    path: str
    change: Change
    size_bytes: int


@dataclass(frozen=True)
class Preview:
    """Exactly what a write would do, computed before anyone is asked to approve it.

    Invariant 7. The dialog renders this; the executor is handed the same object and refuses
    if the world has moved since - so what was approved is what happens, or nothing does.
    """

    entry_id: str
    live_path: Path
    target_path: Path
    reason: str
    changes: tuple[FileChange, ...]
    live_hash: str | None
    source_hash: str | None
    will_back_up: bool

    @property
    def is_noop(self) -> bool:
        """The Live Save already holds exactly this content. No write, and so no backup."""
        return self.live_hash is not None and self.live_hash == self.source_hash

    @property
    def removed(self) -> tuple[FileChange, ...]:
        return tuple(c for c in self.changes if c.change is Change.REMOVE)


def resolve_target(live: Path) -> Path:
    """The real path the swap operates on. A symlinked save folder is followed to its target.

    See the module docstring: staging must land on the same filesystem as the thing it will
    replace, and the user's symlink must survive the operation.
    """
    return live.resolve() if live.is_symlink() else live


def preview(paths: Paths, entry_id: str, live: Path, source: Source, reason: str) -> Preview:
    target = resolve_target(live)

    before = _measure(target)
    after = source.files()

    changes = [
        FileChange(path, Change.REPLACE if path in before else Change.ADD, size)
        for path, size in sorted(after.items())
    ]
    changes += [
        FileChange(path, Change.REMOVE, before[path])
        for path in sorted(before)
        if path not in after
    ]

    live_hash = content_hash(target)
    return Preview(
        entry_id=entry_id,
        live_path=live,
        target_path=target,
        reason=reason,
        changes=tuple(changes),
        live_hash=live_hash,
        source_hash=source.digest(),
        will_back_up=live_hash is not None,
    )


# --- the journal --------------------------------------------------------------------------


@dataclass
class Journal:
    """One in-flight Live Save write. At most one exists: `app.lock` makes the app single-instance
    and the UI runs one operation at a time."""

    entry_id: str
    live_path: str
    target_path: str
    staged_path: str
    old_path: str
    reason: str
    stage: str
    started_at: str
    backup_path: str | None = None
    schema: int = SCHEMA

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Journal:
        known = set(cls.__dataclass_fields__)
        return cls(**{k: v for k, v in data.items() if k in known})


def _staging_paths(target: Path) -> tuple[Path, Path]:
    """Siblings of the target, so both renames stay within one filesystem."""
    return (
        target.parent / f".{target.name}.gsm-new",
        target.parent / f".{target.name}.gsm-old",
    )


def _remove(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink(missing_ok=True)
    elif path.is_dir():
        shutil.rmtree(path, ignore_errors=True)


def _fsync_tree(root: Path) -> None:
    """Force the staged copy to disk before we start swapping it into place.

    Without this, the renames can land while the bytes they refer to are still in the page
    cache - and a power cut leaves a file that exists, is the right size, and is full of
    zeroes.
    """
    targets: Iterable[Path] = [root] if root.is_file() else root.rglob("*")
    for path in targets:
        if path.is_file() and not path.is_symlink():
            with contextlib.suppress(OSError), path.open("rb") as handle:
                os.fsync(handle.fileno())
    if root.is_dir():
        fsync_dir(root)
    fsync_dir(root.parent)


# --- the write ------------------------------------------------------------------------------


@dataclass(frozen=True)
class Written:
    preview: Preview
    backup: Backup | None
    pruned: tuple[Backup, ...] = field(default=())
    skipped: bool = False


def write_live(
    paths: Paths,
    config: Config,
    entry_id: str,
    live: Path,
    source: Source,
    reason: str,
    approved: Preview | None = None,
) -> Written:
    """Overwrite a Live Save, safely, or do nothing at all.

    `approved` is the Preview the user was shown. We recompute it and refuse if it has
    changed, so the operation that runs is the operation that was agreed to - a game that
    saved while the confirmation dialog was open must not have its new progress silently
    swept away by a preview computed before it wrote.
    """
    current = preview(paths, entry_id, live, source, reason)

    if approved is not None and (
        approved.live_hash != current.live_hash or approved.source_hash != current.source_hash
    ):
        raise StalePreview(
            f"{live} changed since it was previewed. Nothing has been written. "
            "Refresh and look again."
        )

    if current.is_noop:
        log().info("%s already matches; nothing to write.", live)
        return Written(preview=current, backup=None, skipped=True)

    target = current.target_path
    if not target.parent.is_dir():
        raise LiveParentMissing(
            f"{target.parent} does not exist, so {target} cannot be written. "
            "If the save lives on another drive, connect it and try again."
        )

    staged, old = _staging_paths(target)
    _remove(staged)
    _remove(old)

    # The backup comes first, and is verified against the very bytes we are about to destroy.
    backup = None
    if current.live_hash is not None:
        backup = backups.create(paths, entry_id, target, reason, expected_hash=current.live_hash)
        log().info("Backed up %s to %s", live, backup.path.name)

    journal = Journal(
        entry_id=entry_id,
        live_path=str(live),
        target_path=str(target),
        staged_path=str(staged),
        old_path=str(old),
        reason=reason,
        stage=Stage.BACKED_UP,
        started_at=datetime.now(UTC).isoformat(),
        backup_path=str(backup.path) if backup else None,
    )
    _record(paths, journal)

    # Nothing below is wrapped in a rollback handler, and that is deliberate. If any of it
    # fails, the journal is left exactly as it stands and `recover` sorts it out at the next
    # launch - reading both the journal *and the disk*, which is the only way to know which
    # of the renames actually landed. A handler here would be guessing, and a crash (rather
    # than an exception) would not run it anyway, so it would be a second, less-tested
    # recovery path for exactly the same states.
    source.materialize(staged)
    _fsync_tree(staged)
    _record(paths, journal, Stage.STAGED)

    if target.exists() or target.is_symlink():
        os.rename(target, old)  # the save is now absent: the only unsafe instant
    os.rename(staged, target)  # and present again, whole
    fsync_dir(target.parent)
    _record(paths, journal, Stage.SWAPPED)

    _remove(old)
    paths.journal_file.unlink(missing_ok=True)

    pruned = backups.prune(paths, entry_id, config.backup_retention)
    log().info("Wrote %s (%s)", live, reason)

    return Written(preview=current, backup=backup, pruned=tuple(pruned))


def _record(paths: Paths, journal: Journal, stage: Stage | None = None) -> None:
    if stage is not None:
        journal.stage = stage
    write_json(paths.journal_file, journal.to_dict())


# --- recovery -------------------------------------------------------------------------------


@dataclass(frozen=True)
class Recovered:
    journal: Journal
    outcome: Outcome


def recover(paths: Paths) -> Recovered | None:
    """Finish or undo a Live Save write that a crash interrupted. Run once, at startup.

    The journal says how far we *got*; the disk says what actually landed, and the disk wins,
    because the crash may have fallen between a rename and the journal write that records it.
    So the decision is made from the evidence: which of `staged`, `target` and `target.old`
    exist right now.
    """
    data = read_json(paths.journal_file)
    if data is None:
        return None

    journal = Journal.from_dict(data)
    target = Path(journal.target_path)
    staged = Path(journal.staged_path)
    old = Path(journal.old_path)

    exists = target.exists() or target.is_symlink()

    if journal.stage == Stage.BACKED_UP:
        # The staged copy may be half-written, so it is not content - it is debris. The Live
        # Save has not been touched, and the swap cannot have started.
        _remove(staged)
        _remove(old)
        outcome = Outcome.ROLLED_BACK

    elif exists:
        # The Live Save is whole. Either the swap never started (and `staged` is still there,
        # to be discarded) or it finished and we died before recording it (and `old` is there,
        # to be cleaned up). Both are safe; neither can be torn.
        outcome = Outcome.COMPLETED if old.exists() else Outcome.ROLLED_BACK
        _remove(staged)
        _remove(old)

    elif staged.exists() or staged.is_symlink():
        # Caught inside the swap window, between the two renames. `staged` is complete - the
        # journal reached STAGED, which is only written after an fsync - so finish the job.
        os.rename(staged, target)
        _remove(old)
        outcome = Outcome.COMPLETED

    elif old.exists() or old.is_symlink():
        # The first rename landed and the second did not, and the staged copy is gone. Put
        # the original save back exactly where it was.
        os.rename(old, target)
        outcome = Outcome.ROLLED_BACK

    else:
        # Nothing anywhere: the Live Save did not exist before this write, and never got one.
        outcome = Outcome.ROLLED_BACK

    fsync_dir(target.parent)
    paths.journal_file.unlink(missing_ok=True)

    log().warning(
        "Recovered an interrupted write of %s: %s (backup: %s)",
        journal.live_path,
        outcome.value,
        journal.backup_path or "none taken",
    )
    return Recovered(journal=journal, outcome=outcome)
