"""The only code that overwrites a Live Save, and therefore the only code that can lose one.

The heart of this file is `CRASH_POINTS`: every instant at which the process can die during a
write. For each one the suite asserts the same three things, and they are the whole safety
story:

    1. The Live Save on disk is *entirely* the old content or *entirely* the new one - never
       a mixture. A half-copied save is the state a game will happily load and then corrupt.
    2. `recover` leaves it whole, and leaves no debris behind.
    3. The backup of the old content exists and holds exactly the old content.
"""

import os
from pathlib import Path

import pytest

from core import backups, transaction
from core.config import Config
from core.hashing import content_hash
from core.paths import Paths
from core.transaction import (
    Change,
    LiveParentMissing,
    Outcome,
    Stage,
    StalePreview,
    TreeSource,
    preview,
    recover,
    write_live,
)

ENTRY = "3f2a1b7c-0000-4000-8000-000000000001"
REASON = "restore"

OLD = {"slot1.sav": "my progress", "meta/profile.json": "{}"}
NEW = {"slot1.sav": "their progress", "meta/profile.json": "{}", "slot2.sav": "a second slot"}


class Crash(Exception):
    """Stands in for the power going out."""


@pytest.fixture
def paths(tmp_path):
    return Paths(root=tmp_path / "app")


@pytest.fixture
def config():
    return Config(machine_id="m", repo="owner/vault", backup_retention=3)


def tree(root: Path, files: dict[str, str]) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    for relative, contents in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(contents, encoding="utf-8")
    return root


@pytest.fixture
def world(tmp_path):
    """A Live Save holding OLD, and a Vault Entry holding NEW."""
    live = tree(tmp_path / "game" / "saves", OLD)
    vault = tree(tmp_path / "vault", NEW)
    return live, vault


# --- the ordinary write --------------------------------------------------------------------


def test_a_write_replaces_the_live_save_and_backs_up_what_it_replaced(paths, config, world):
    live, vault = world
    before = content_hash(live)

    written = write_live(paths, config, ENTRY, live, TreeSource(vault), REASON)

    assert content_hash(live) == content_hash(vault)
    assert written.backup is not None
    assert backups.archive_hash(written.backup.path) == before  # the old save, recoverable


def test_a_write_leaves_no_debris_beside_the_live_save(paths, config, world):
    """Staging happens *inside the game's save folder's parent*. Anything we leave there is
    litter in the user's home directory, and worse, could be mistaken for a save."""
    live, vault = world

    write_live(paths, config, ENTRY, live, TreeSource(vault), REASON)

    assert sorted(p.name for p in live.parent.iterdir()) == ["saves"]
    assert not paths.journal_file.exists()


def test_writing_content_the_live_save_already_holds_does_nothing_at_all(paths, config, world):
    """No write, and therefore no backup: a zip of a save we are not about to touch is noise
    that pushes a real rescue out of the retention window."""
    live, _ = world
    identical = tree(Path(str(live)).parent.parent / "identical", OLD)

    written = write_live(paths, config, ENTRY, live, TreeSource(identical), REASON)

    assert written.skipped
    assert written.backup is None
    assert backups.list_for(paths, ENTRY) == []


def test_a_write_refuses_if_the_live_save_moved_since_the_preview(paths, config, world):
    """The game saved while the confirmation dialog was open. What the user approved is no
    longer what would happen, so nothing happens."""
    live, vault = world
    approved = preview(paths, ENTRY, live, TreeSource(vault), REASON)

    (live / "slot1.sav").write_text("progress made while the dialog was open", encoding="utf-8")

    with pytest.raises(StalePreview):
        write_live(paths, config, ENTRY, live, TreeSource(vault), REASON, approved=approved)

    assert (live / "slot1.sav").read_text() == "progress made while the dialog was open"


def test_a_write_refuses_when_the_drive_is_not_mounted(paths, config, tmp_path):
    """Creating the folder would write the save onto the boot disk underneath the mount point,
    where the user will never find it and the real drive will shadow it on reconnection."""
    vault = tree(tmp_path / "vault", NEW)
    live = tmp_path / "not-mounted" / "Game" / "saves"

    with pytest.raises(LiveParentMissing):
        write_live(paths, config, ENTRY, live, TreeSource(vault), REASON)


def test_a_live_save_that_does_not_exist_yet_is_created_without_a_backup(paths, config, tmp_path):
    """The second-machine restore. There is nothing to back up, and nothing to lose."""
    vault = tree(tmp_path / "vault", NEW)
    live = tmp_path / "game" / "saves"
    live.parent.mkdir(parents=True)

    written = write_live(paths, config, ENTRY, live, TreeSource(vault), REASON)

    assert content_hash(live) == content_hash(vault)
    assert written.backup is None


# --- the preview (Invariant 7) ---------------------------------------------------------------


def test_the_preview_names_every_file_it_would_add_replace_and_remove(paths, world):
    live, vault = world

    plan = preview(paths, ENTRY, live, TreeSource(vault), REASON)
    by_path = {c.path: c.change for c in plan.changes}

    assert by_path == {
        "slot1.sav": Change.REPLACE,
        "meta/profile.json": Change.REPLACE,
        "slot2.sav": Change.ADD,
    }
    assert plan.will_back_up


def test_the_preview_names_the_files_a_write_would_destroy(paths, tmp_path):
    """The removals are the whole reason Invariant 7 exists. A user must never learn that a
    save slot was deleted by noticing it is gone."""
    live = tree(tmp_path / "game" / "saves", {"slot1.sav": "a", "slot9.sav": "the good one"})
    vault = tree(tmp_path / "vault", {"slot1.sav": "b"})

    plan = preview(Paths(root=tmp_path / "app"), ENTRY, live, TreeSource(vault), REASON)

    assert [c.path for c in plan.removed] == ["slot9.sav"]


# --- symlinked save folders --------------------------------------------------------------------


@pytest.mark.skipif(os.name == "nt", reason="symlinks need privileges on Windows")
def test_a_symlinked_save_folder_is_written_through_and_survives(paths, config, tmp_path):
    """The save folder is a link to a second drive. Swapping the *link* would replace it with
    a real directory, stranding every other program that follows it - and would stage on the
    wrong filesystem, where the rename is not atomic."""
    real = tree(tmp_path / "other-drive" / "saves", OLD)
    link = tmp_path / "game" / "saves"
    link.parent.mkdir(parents=True)
    link.symlink_to(real)
    vault = tree(tmp_path / "vault", NEW)

    write_live(paths, config, ENTRY, link, TreeSource(vault), REASON)

    assert link.is_symlink()  # still a link
    assert link.resolve() == real.resolve()  # still to the same place
    assert content_hash(real) == content_hash(vault)  # and the real folder holds the new save
    assert sorted(p.name for p in real.parent.iterdir()) == ["saves"]  # staged on that drive


# --- crashes ------------------------------------------------------------------------------------

CRASH_POINTS = [
    "after the backup, before anything is staged",
    "midway through staging the new content",
    "after staging, before the swap begins",
    "between the two renames, while the Live Save is absent",
    "after both renames, before the journal records it",
    "after the journal records the swap, before the old copy is deleted",
]


def arm(monkeypatch, paths: Paths, point: str) -> None:
    """Make `write_live` die at exactly `point`, as a power cut would."""
    real_record = transaction._record
    real_rename = os.rename
    real_remove = transaction._remove

    if point == CRASH_POINTS[0]:

        def record(p, journal, stage=None):
            real_record(p, journal, stage)
            if journal.stage == Stage.BACKED_UP:
                raise Crash(point)

        monkeypatch.setattr(transaction, "_record", record)

    elif point == CRASH_POINTS[1]:

        def materialize(self, destination):
            # A torn copy: the first file lands, the rest never does.
            destination.mkdir(parents=True)
            (destination / "slot1.sav").write_text(NEW["slot1.sav"], encoding="utf-8")
            raise Crash(point)

        monkeypatch.setattr(TreeSource, "materialize", materialize)

    elif point == CRASH_POINTS[2]:

        def record(p, journal, stage=None):
            real_record(p, journal, stage)
            if journal.stage == Stage.STAGED:
                raise Crash(point)

        monkeypatch.setattr(transaction, "_record", record)

    elif point == CRASH_POINTS[3]:
        calls = {"n": 0}

        def rename(src, dst):
            calls["n"] += 1
            if calls["n"] == 2:  # the second rename: staged -> target
                raise Crash(point)
            real_rename(src, dst)

        monkeypatch.setattr(transaction.os, "rename", rename)

    elif point == CRASH_POINTS[4]:

        def record(p, journal, stage=None):
            if stage == Stage.SWAPPED:
                raise Crash(point)  # both renames landed; the journal never learned
            real_record(p, journal, stage)

        monkeypatch.setattr(transaction, "_record", record)

    elif point == CRASH_POINTS[5]:

        def remove(path):
            if path.name.endswith(".gsm-old") and paths.journal_file.exists():
                raise Crash(point)
            real_remove(path)

        monkeypatch.setattr(transaction, "_remove", remove)


@pytest.mark.parametrize("point", CRASH_POINTS)
def test_a_crash_never_leaves_a_torn_live_save(monkeypatch, paths, config, world, point):
    """The property the whole design exists to provide, at every instant it could be violated.

    At no moment is the Live Save a mixture of the old save and the new one. It is one, or the
    other, or - for the single instant between the two renames - absent, and recoverable from
    both sides.
    """
    live, vault = world
    old_hash, new_hash = content_hash(live), content_hash(vault)

    arm(monkeypatch, paths, point)
    with pytest.raises(Crash):
        write_live(paths, config, ENTRY, live, TreeSource(vault), REASON)
    monkeypatch.undo()

    crashed = content_hash(live)
    assert crashed in (old_hash, new_hash, None), f"torn Live Save after a crash {point}"

    recovered = recover(paths)
    assert recovered is not None

    healed = content_hash(live)
    assert healed in (old_hash, new_hash), f"torn Live Save after recovering from a crash {point}"
    assert (healed == new_hash) == (recovered.outcome is Outcome.COMPLETED)


@pytest.mark.parametrize("point", CRASH_POINTS)
def test_the_backup_survives_every_crash_and_holds_the_old_save(
    monkeypatch, paths, config, world, point
):
    """The backup is taken before anything is touched, so it exists at every crash point - and
    for progress never yet Synced it is the only copy of that save in existence."""
    live, vault = world
    old_hash = content_hash(live)

    arm(monkeypatch, paths, point)
    with pytest.raises(Crash):
        write_live(paths, config, ENTRY, live, TreeSource(vault), REASON)
    monkeypatch.undo()
    recover(paths)

    archived = backups.list_for(paths, ENTRY)
    assert len(archived) == 1
    assert backups.archive_hash(archived[0].path) == old_hash


@pytest.mark.parametrize("point", CRASH_POINTS)
def test_recovery_leaves_no_debris(monkeypatch, paths, config, world, point):
    live, vault = world

    arm(monkeypatch, paths, point)
    with pytest.raises(Crash):
        write_live(paths, config, ENTRY, live, TreeSource(vault), REASON)
    monkeypatch.undo()
    recover(paths)

    assert not paths.journal_file.exists()
    assert sorted(p.name for p in live.parent.iterdir()) == ["saves"]


def test_the_dangerous_window_rolls_forward_rather_than_leaving_nothing(
    monkeypatch, paths, config, world
):
    """The single instant the Live Save is absent: between `target -> target.old` and
    `staged -> target`. The staged copy is complete and fsynced - the journal only reaches
    STAGED after that - so recovery finishes the job rather than abandoning it."""
    live, vault = world
    new_hash = content_hash(vault)

    arm(monkeypatch, paths, CRASH_POINTS[3])
    with pytest.raises(Crash):
        write_live(paths, config, ENTRY, live, TreeSource(vault), REASON)
    monkeypatch.undo()

    assert not live.exists()  # the Live Save really is gone at this instant

    recovered = recover(paths)

    assert recovered.outcome is Outcome.COMPLETED
    assert content_hash(live) == new_hash


def test_a_crash_while_staging_rolls_back_and_never_offers_the_torn_copy(
    monkeypatch, paths, config, world
):
    """The staged copy is half-written, so it is debris, not content. Rolling forward onto it
    would install a corrupt save that looks perfectly fine."""
    live, vault = world
    old_hash = content_hash(live)

    arm(monkeypatch, paths, CRASH_POINTS[1])
    with pytest.raises(Crash):
        write_live(paths, config, ENTRY, live, TreeSource(vault), REASON)
    monkeypatch.undo()

    recovered = recover(paths)

    assert recovered.outcome is Outcome.ROLLED_BACK
    assert content_hash(live) == old_hash


def test_a_half_written_copy_is_never_installed_as_a_brand_new_live_save(
    monkeypatch, paths, config, tmp_path
):
    """The second-machine restore, interrupted. There is no Live Save yet, so there is nothing
    to roll *back* to - and that is exactly what makes this dangerous. The staged copy is the
    only candidate on disk, and it is half-written. Installing it would hand the user a
    corrupt save that looks perfectly ordinary, with no backup, because there was nothing to
    back up.

    The journal is what prevents it: at BACKED_UP the staged copy is debris, not content, no
    matter how tempting it looks.
    """
    vault = tree(tmp_path / "vault", NEW)
    live = tmp_path / "game" / "saves"
    live.parent.mkdir(parents=True)

    arm(monkeypatch, paths, CRASH_POINTS[1])
    with pytest.raises(Crash):
        write_live(paths, config, ENTRY, live, TreeSource(vault), REASON)
    monkeypatch.undo()

    recovered = recover(paths)

    assert recovered.outcome is Outcome.ROLLED_BACK
    assert not live.exists()  # no save, rather than a corrupt one
    assert sorted(p.name for p in live.parent.iterdir()) == []


# --- retention is enforced only after success -------------------------------------------------


def test_a_write_prunes_the_oldest_backups_to_the_retention_limit(paths, config, world):
    live, vault = world

    for i in range(5):
        (live / "slot1.sav").write_text(f"progress {i}", encoding="utf-8")
        write_live(paths, config, ENTRY, live, TreeSource(vault), REASON)
        (live / "slot1.sav").write_text(f"progress {i}", encoding="utf-8")  # diverge again

    assert len(backups.list_for(paths, ENTRY)) == config.backup_retention


def test_a_failed_write_prunes_nothing_even_past_the_retention_limit(
    monkeypatch, paths, config, world
):
    """Pruning happens only after the operation it protected has succeeded. Pruning first, to
    make room, would delete the oldest copy of a save in order to overwrite the newest - and
    if the write then failed, we would have destroyed a backup for nothing.

    So a crash deliberately leaves *more* backups than the retention limit. That is correct.
    """
    live, vault = world
    for i in range(config.backup_retention):
        (live / "slot1.sav").write_text(f"progress {i}", encoding="utf-8")
        write_live(paths, config, ENTRY, live, TreeSource(vault), REASON)
        (live / "slot1.sav").write_text(f"progress {i}", encoding="utf-8")

    at_limit = backups.list_for(paths, ENTRY)
    assert len(at_limit) == config.backup_retention

    # Crash once. The backup taken for the failed write stays, so we are now *over* the limit.
    arm(monkeypatch, paths, CRASH_POINTS[2])
    with pytest.raises(Crash):
        write_live(paths, config, ENTRY, live, TreeSource(vault), REASON)
    monkeypatch.undo()
    recover(paths)

    over_limit = backups.list_for(paths, ENTRY)
    assert len(over_limit) == config.backup_retention + 1  # over the limit, on purpose
    assert set(at_limit).issubset(set(over_limit))

    # Now crash *again*, from that over-limit state. This is where pruning-to-make-room would
    # actually destroy something: it would delete the oldest save to clear space for a write
    # that is about to fail. Every archive that existed must still exist afterwards.
    (live / "slot1.sav").write_text("progress made after the first crash", encoding="utf-8")
    arm(monkeypatch, paths, CRASH_POINTS[2])
    with pytest.raises(Crash):
        write_live(paths, config, ENTRY, live, TreeSource(vault), REASON)
    monkeypatch.undo()
    recover(paths)

    assert set(over_limit).issubset(set(backups.list_for(paths, ENTRY)))


def test_recovery_on_a_clean_start_does_nothing(paths):
    assert recover(paths) is None


def test_recovery_is_idempotent(monkeypatch, paths, config, world):
    """It runs at every launch. Running it twice must not undo what it just did."""
    live, vault = world

    arm(monkeypatch, paths, CRASH_POINTS[3])
    with pytest.raises(Crash):
        write_live(paths, config, ENTRY, live, TreeSource(vault), REASON)
    monkeypatch.undo()

    recover(paths)
    after_first = content_hash(live)

    assert recover(paths) is None
    assert content_hash(live) == after_first


# --- restoring a backup goes through the very same path ---------------------------------------


def test_a_backup_can_be_restored_and_is_itself_backed_up_first(paths, config, world):
    """Restoring an old backup overwrites the current save, so the current save is archived
    too. Choosing the wrong backup must not be an irreversible act."""
    live, vault = world
    original = content_hash(live)

    write_live(paths, config, ENTRY, live, TreeSource(vault), REASON)
    assert content_hash(live) == content_hash(vault)

    [taken] = backups.list_for(paths, ENTRY)
    written = write_live(
        paths, config, ENTRY, live, transaction.BackupSource(taken), "backup-restore"
    )

    assert content_hash(live) == original  # the old save is back
    assert written.backup is not None
    assert backups.archive_hash(written.backup.path) == content_hash(vault)  # and so is the other
