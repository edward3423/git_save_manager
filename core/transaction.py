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

## A File Set is written as one

A File Set is several files in unrelated folders (`~/.skhdrc`, `~/.yabairc`) that are one
Entry. Each file is swapped exactly as above, beside itself, and they are swapped *together*:
one Backup holds every member about to be overwritten, every member is staged and fsynced
before any is swapped, and the journal records all of them under one stage. A crash part-way
through the swaps is therefore rolled forward for every member - each staged copy is complete
- so the files are never left half from one version and half from another.

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
from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol

from core import backups
from core.backups import Backup
from core.config import Config
from core.hashing import content_hash, file_set_hash
from core.jsonstore import fsync_dir, read_json, write_json
from core.logger import log
from core.paths import Paths

SCHEMA = 2

Layout = Path | Mapping[str, Path]
"""Where an Entry's Live Save is on this Machine: one path for a file or folder Entry, or
member name -> path for a File Set."""


class StalePreview(Exception):
    """The world moved between showing the preview and executing it. We refuse to proceed."""


class LiveParentMissing(Exception):
    """The folder that should contain the Live Save is missing, along with its own parent.

    One missing level is created: a game not yet installed here often lacks just its own
    save folder, and binding to `.../Projects/nut65_config` when `Projects` exists is a
    plain intent to keep the save there. Two or more missing levels are *not* created. The
    usual cause is an unmounted drive, and happily creating `/Volumes/Games/...` would
    write the save onto the boot disk, hidden under the mount point, where the user will
    never find it and the real drive will shadow it the moment it comes back.
    """


class NotAFile(Exception):
    """A File Set member's Live Save path is a folder. Swapping a file over it would replace a
    whole folder that no Backup holds, so nothing is written."""


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
    """Content bound for a Live Save. A Vault Entry, or a Backup archive.

    `member` names one file of a File Set; `None` means the whole of a file or folder Entry.
    """

    def digest(self) -> str | None: ...

    def files(self, member: str | None) -> dict[str, int]:
        """Relative posix path -> size in bytes, for the preview. Empty when the source holds
        nothing for `member` - and that member is then left untouched."""
        ...

    def materialize(self, member: str | None, destination: Path) -> None: ...


@dataclass(frozen=True)
class TreeSource:
    """An Entry's content in the Vault, or any plain file or folder.

    For a File Set, `path` is the folder holding the members and `members` names them, so
    anything else lying in that folder is neither hashed nor written.
    """

    path: Path
    members: tuple[str, ...] | None = None

    def digest(self) -> str | None:
        if self.members is None:
            return content_hash(self.path)
        return file_set_hash({name: self.path / name for name in self.members})

    def files(self, member: str | None) -> dict[str, int]:
        if member is None:
            return _measure(self.path)
        item = self.path / member
        return {member: item.lstat().st_size} if item.is_file() else {}

    def materialize(self, member: str | None, destination: Path) -> None:
        item = self.path if member is None else self.path / member
        if item.is_dir() and not item.is_symlink():
            shutil.copytree(item, destination, symlinks=True)
        else:
            shutil.copy2(item, destination, follow_symlinks=False)


@dataclass(frozen=True)
class BackupSource:
    """A backup archive, restored through the very same path as everything else."""

    backup: Backup

    def digest(self) -> str | None:
        return backups.archive_hash(self.backup.path)

    def files(self, member: str | None) -> dict[str, int]:
        found = backups.contents(self.backup.path)
        if member is None:
            return found
        return {member: found[member]} if member in found else {}

    def materialize(self, member: str | None, destination: Path) -> None:
        backups.extract(self.backup, destination, member)


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
class PartPreview:
    """What a write does to one path: the whole of a file or folder Entry, or one File Set
    member."""

    member: str | None
    live_path: Path
    target_path: Path
    changes: tuple[FileChange, ...]


@dataclass(frozen=True)
class Preview:
    """Exactly what a write would do, computed before anyone is asked to approve it.

    Invariant 7. The dialog renders this; the executor is handed the same object and refuses
    if the world has moved since - so what was approved is what happens, or nothing does.
    """

    entry_id: str
    reason: str
    parts: tuple[PartPreview, ...]
    live_hash: str | None
    source_hash: str | None
    will_back_up: bool

    @property
    def is_file_set(self) -> bool:
        return any(part.member is not None for part in self.parts)

    @property
    def changes(self) -> tuple[FileChange, ...]:
        return tuple(change for part in self.parts for change in part.changes)

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


def _part(member: str | None, live: Path, source: Source) -> PartPreview | None:
    target = resolve_target(live)
    after = source.files(member)

    if member is None:
        before = _measure(target)
    else:
        if not after:
            return None  # the source holds nothing for this member: it is not touched
        if target.is_dir():
            raise NotAFile(
                f"{live} is a folder, but it is bound as the file {member}. Nothing has been "
                "written. Move the folder aside, or bind the file somewhere else."
            )
        before = {member: target.lstat().st_size} if target.exists() else {}

    changes = [
        FileChange(path, Change.REPLACE if path in before else Change.ADD, size)
        for path, size in sorted(after.items())
    ]
    changes += [
        FileChange(path, Change.REMOVE, before[path])
        for path in sorted(before)
        if path not in after
    ]
    return PartPreview(member=member, live_path=live, target_path=target, changes=tuple(changes))


def preview(paths: Paths, entry_id: str, live: Layout, source: Source, reason: str) -> Preview:
    if isinstance(live, Mapping):
        found = [_part(member, path, source) for member, path in sorted(live.items())]
        parts = tuple(part for part in found if part is not None)
        # Only the members this write touches: they are what gets compared and backed up.
        live_hash = file_set_hash({part.member: part.target_path for part in parts})
    else:
        parts = (_part(None, live, source),)
        live_hash = content_hash(parts[0].target_path)

    return Preview(
        entry_id=entry_id,
        reason=reason,
        parts=parts,
        live_hash=live_hash,
        source_hash=source.digest(),
        will_back_up=live_hash is not None,
    )


# --- the journal --------------------------------------------------------------------------


@dataclass
class Swap:
    """One path being swapped: the bound path, the real path behind it, and its two siblings."""

    live_path: str
    target_path: str
    staged_path: str
    old_path: str


@dataclass
class Journal:
    """One in-flight Live Save write. At most one exists: `app.lock` makes the app single-instance
    and the UI runs one operation at a time. A File Set's write is still one write, with one
    swap per member."""

    entry_id: str
    swaps: list[Swap]
    reason: str
    stage: str
    started_at: str
    backup_path: str | None = None
    schema: int = SCHEMA

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Journal:
        if "swaps" in data:
            swaps = [Swap(**swap) for swap in data["swaps"]]
        else:
            # Schema 1, left by an older build: a single swap, flattened into the journal.
            swaps = [Swap(**{key: data[key] for key in Swap.__dataclass_fields__})]
        known = set(cls.__dataclass_fields__) - {"swaps"}
        return cls(swaps=swaps, **{k: v for k, v in data.items() if k in known})

    @property
    def live_paths(self) -> str:
        return ", ".join(swap.live_path for swap in self.swaps)


def _ensure_parent(target: Path) -> None:
    """Create the folder that will hold the Live Save, but only its last level.

    See `LiveParentMissing` for why a deeper gap is a refusal rather than a `mkdir -p`.
    """
    parent = target.parent
    if parent.is_dir():
        return
    if not parent.parent.is_dir():
        raise LiveParentMissing(
            f"{parent.parent} does not exist, so {target} cannot be written. "
            "If the save lives on another drive, connect it and try again."
        )
    parent.mkdir()
    log().info("Created %s", parent)


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


def _backup_subject(current: Preview) -> Path | dict[str, Path]:
    """What the Backup must hold: the folder or file, or every File Set member about to be
    overwritten - which is exactly the set `live_hash` was computed over."""
    if not current.is_file_set:
        return current.parts[0].target_path
    return {part.member: part.target_path for part in current.parts if part.target_path.is_file()}


def write_live(
    paths: Paths,
    config: Config,
    entry_id: str,
    live: Layout,
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
    shown = live if not isinstance(live, Mapping) else ", ".join(map(str, live.values()))

    if approved is not None and (
        approved.live_hash != current.live_hash or approved.source_hash != current.source_hash
    ):
        raise StalePreview(
            f"{shown} changed since it was previewed. Nothing has been written. "
            "Refresh and look again."
        )

    if current.is_noop:
        log().info("%s already matches; nothing to write.", shown)
        return Written(preview=current, backup=None, skipped=True)

    swaps = []
    for part in current.parts:
        target = part.target_path
        _ensure_parent(target)
        staged, old = _staging_paths(target)
        _remove(staged)
        _remove(old)
        swaps.append(Swap(str(part.live_path), str(target), str(staged), str(old)))

    # The backup comes first, and is verified against the very bytes we are about to destroy.
    backup = None
    if current.live_hash is not None:
        subject = _backup_subject(current)
        backup = backups.create(paths, entry_id, subject, reason, expected_hash=current.live_hash)
        archived = ", ".join(map(str, subject.values())) if isinstance(subject, dict) else subject
        log().info("Backed up %s to %s", archived, backup.path.name)

    journal = Journal(
        entry_id=entry_id,
        swaps=swaps,
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
    #
    # Every member is staged before any is swapped, so that once the first swap begins, every
    # staged copy is complete and recovery can finish the lot.
    for part, swap in zip(current.parts, swaps, strict=True):
        staged = Path(swap.staged_path)
        source.materialize(part.member, staged)
        _fsync_tree(staged)
    _record(paths, journal, Stage.STAGED)

    for swap in swaps:
        target, staged, old = Path(swap.target_path), Path(swap.staged_path), Path(swap.old_path)
        if target.exists() or target.is_symlink():
            os.rename(target, old)  # the save is now absent: the only unsafe instant
        os.rename(staged, target)  # and present again, whole
        fsync_dir(target.parent)
    _record(paths, journal, Stage.SWAPPED)

    for swap in swaps:
        _remove(Path(swap.old_path))
    paths.journal_file.unlink(missing_ok=True)

    pruned = backups.prune(paths, entry_id, config.backup_retention)
    log().info("Wrote %s (%s)", shown, reason)

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


def _present(path: Path) -> bool:
    return path.exists() or path.is_symlink()


def _swap_started(swap: Swap) -> bool:
    """Whether this path's swap has begun: the first rename landed, or the second did too, or
    there was never a Live Save to rename away and the staged copy is the only thing left to
    install. Read from the disk, never from the journal."""
    return (
        _present(Path(swap.old_path))
        or not _present(Path(swap.target_path))
        or not _present(Path(swap.staged_path))
    )


def _finish(swap: Swap) -> bool:
    """Roll one swap forward from wherever it stopped. False if it had to be put back instead."""
    target, staged, old = Path(swap.target_path), Path(swap.staged_path), Path(swap.old_path)

    if _present(staged):
        # The staged copy is complete - the journal reached STAGED, which is only written after
        # an fsync of every member - so install it, moving the old content aside if it is
        # still in place.
        if _present(target) and not _present(old):
            os.rename(target, old)
        os.rename(staged, target)
        _remove(old)
        return True

    if _present(target):
        # Both renames landed; only the old copy is left to clean up.
        _remove(old)
        return True

    if _present(old):
        # The first rename landed and the staged copy is gone. Put the original back.
        os.rename(old, target)
    return False


def recover(paths: Paths) -> Recovered | None:
    """Finish or undo a Live Save write that a crash interrupted. Run once, at startup.

    The journal says how far we *got*; the disk says what actually landed, and the disk wins,
    because the crash may have fallen between a rename and the journal write that records it.
    So the decision is made from the evidence: which of `staged`, `target` and `target.old`
    exist right now, for every path the write was swapping.

    The answer is the same for all of them. Before STAGED, a staged copy may be half-written,
    so it is debris and every path is left as it was. From STAGED on, every staged copy is
    complete: if no swap has begun, the write is abandoned whole; if any has, it is finished
    whole. A File Set is never left with some files from the new version and some from the old.
    """
    data = read_json(paths.journal_file)
    if data is None:
        return None

    journal = Journal.from_dict(data)

    if journal.stage == Stage.BACKED_UP or not any(_swap_started(s) for s in journal.swaps):
        for swap in journal.swaps:
            _remove(Path(swap.staged_path))
            _remove(Path(swap.old_path))
        outcome = Outcome.ROLLED_BACK
    else:
        finished = [_finish(swap) for swap in journal.swaps]
        outcome = Outcome.COMPLETED if all(finished) else Outcome.ROLLED_BACK

    for swap in journal.swaps:
        fsync_dir(Path(swap.target_path).parent)
    paths.journal_file.unlink(missing_ok=True)

    log().warning(
        "Recovered an interrupted write of %s: %s (backup: %s)",
        journal.live_paths,
        outcome.value,
        journal.backup_path or "none taken",
    )
    return Recovered(journal=journal, outcome=outcome)
