"""Backups. For a save overwritten before it was ever Synced, the zip is the only copy."""

import os
from datetime import UTC, datetime
from pathlib import Path

import pytest

from core import backups
from core.backups import BackupUnverified
from core.hashing import content_hash
from core.paths import Paths

ENTRY = "3f2a1b7c-0000-4000-8000-000000000001"


@pytest.fixture
def paths(tmp_path):
    return Paths(root=tmp_path / "app")


def tree(root: Path, files: dict[str, str]) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    for relative, contents in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(contents, encoding="utf-8")
    return root


def take(paths, live, reason="restore", at=None):
    return backups.create(paths, ENTRY, live, reason, expected_hash=content_hash(live), at=at)


# --- an archive is not trusted until it is verified -------------------------------------------


def test_an_archive_hashes_back_to_the_save_it_was_taken_from(paths, tmp_path):
    """The property that makes a backup a backup. It is checked on every single one, because
    a zip that silently failed to capture the save is worse than no zip at all: we go on to
    overwrite the original believing we have it."""
    live = tree(tmp_path / "saves", {"slot1.sav": "progress", "meta/p.json": "{}"})

    backup = take(paths, live)

    assert backups.archive_hash(backup.path) == content_hash(live)


def test_an_archive_that_does_not_match_is_refused_and_deleted(paths, tmp_path):
    """Verification is not decoration. If it fails, the operation aborts with the Live Save
    untouched, rather than proceeding under a backup that does not hold it."""
    live = tree(tmp_path / "saves", {"slot1.sav": "progress"})

    with pytest.raises(BackupUnverified):
        backups.create(paths, ENTRY, live, "restore", expected_hash="a-hash-it-will-not-match")

    assert backups.list_for(paths, ENTRY) == []
    assert list(backups.directory_for(paths, ENTRY).iterdir()) == []  # not even a stray .tmp


def test_a_single_file_entry_never_restores_as_a_folder(paths, tmp_path):
    """A one-file folder and a single-file Entry hold the same bytes. The first *contains* the
    save; the second *is* it. Restoring one as the other puts a directory where a game expects
    a file."""
    live = tmp_path / "settings.ini"
    live.write_text("volume=11", encoding="utf-8")

    backup = take(paths, live)
    destination = tmp_path / "restored.ini"
    backups.extract(backup, destination)

    assert destination.is_file()
    assert destination.read_text(encoding="utf-8") == "volume=11"
    assert backups.archive_hash(backup.path) == content_hash(live)


def test_a_round_trip_reproduces_the_save_exactly(paths, tmp_path):
    live = tree(tmp_path / "saves", {"slot1.sav": "progress", "deep/nested/slot2.sav": "more"})

    backup = take(paths, live)
    restored = tmp_path / "restored"
    backups.extract(backup, restored)

    assert content_hash(restored) == content_hash(live)


@pytest.mark.skipif(os.name == "nt", reason="symlinks need privileges on Windows")
def test_a_symlink_inside_a_save_is_archived_as_a_link_not_as_its_target(paths, tmp_path):
    """`ZipFile.write` follows symlinks. Left alone, it would bake the target's bytes into the
    archive, and restore would turn the link into a real file - changing the Entry's content
    hash, so that a restored save could never be In Sync with the Vault it came from."""
    outside = tmp_path / "outside.dat"
    outside.write_text("not part of the save", encoding="utf-8")

    live = tree(tmp_path / "saves", {"slot1.sav": "progress"})
    (live / "link.sav").symlink_to(outside)

    backup = take(paths, live)
    restored = tmp_path / "restored"
    backups.extract(backup, restored)

    assert (restored / "link.sav").is_symlink()
    assert os.readlink(restored / "link.sav") == str(outside)
    assert content_hash(restored) == content_hash(live)


# --- the catalogue ------------------------------------------------------------------------------


def test_backups_are_listed_newest_first(paths, tmp_path):
    live = tree(tmp_path / "saves", {"slot1.sav": "a"})
    take(paths, live, at=datetime(2026, 7, 10, 12, 0, tzinfo=UTC))
    (live / "slot1.sav").write_text("b", encoding="utf-8")
    take(paths, live, at=datetime(2026, 7, 12, 12, 0, tzinfo=UTC))

    found = backups.list_for(paths, ENTRY)

    assert [b.taken_at.day for b in found] == [12, 10]


def test_a_backup_carries_its_timestamp_reason_and_size(paths, tmp_path):
    """The Backups view shows these, and the filename carries them - so they survive the app
    being broken, which is exactly when someone needs to find the zip by hand."""
    live = tree(tmp_path / "saves", {"slot1.sav": "progress"})

    backup = take(paths, live, reason="conflict-vault", at=datetime(2026, 7, 12, 9, 30, tzinfo=UTC))

    assert backup.reason == "conflict-vault"
    assert backup.taken_at == datetime(2026, 7, 12, 9, 30, tzinfo=UTC)
    assert backup.size_bytes > 0
    assert backup.path.name == "20260712T093000Z-conflict-vault.zip"


def test_two_backups_in_the_same_second_do_not_overwrite_each_other(paths, tmp_path):
    live = tree(tmp_path / "saves", {"slot1.sav": "a"})
    at = datetime(2026, 7, 12, 9, 30, tzinfo=UTC)

    first = take(paths, live, at=at)
    second = take(paths, live, at=at)

    assert first.path != second.path
    assert len(backups.list_for(paths, ENTRY)) == 2


def test_an_unrecognized_file_in_the_backups_folder_is_ignored_never_deleted(paths, tmp_path):
    """The user's own copy of something, dropped in there. Not ours to tidy away."""
    live = tree(tmp_path / "saves", {"slot1.sav": "a"})
    take(paths, live)
    stray = backups.directory_for(paths, ENTRY) / "my-own-copy.zip"
    stray.write_text("mine", encoding="utf-8")

    assert len(backups.list_for(paths, ENTRY)) == 1
    backups.prune(paths, ENTRY, keep=1)

    assert stray.exists()


# --- retention ------------------------------------------------------------------------------------


def test_pruning_keeps_the_newest_and_deletes_the_oldest(paths, tmp_path):
    live = tree(tmp_path / "saves", {"slot1.sav": "a"})
    for day in (10, 11, 12, 13):
        (live / "slot1.sav").write_text(f"day {day}", encoding="utf-8")
        take(paths, live, at=datetime(2026, 7, day, 12, 0, tzinfo=UTC))

    pruned = backups.prune(paths, ENTRY, keep=2)

    assert [b.taken_at.day for b in pruned] == [11, 10]
    assert [b.taken_at.day for b in backups.list_for(paths, ENTRY)] == [13, 12]


def test_pruning_to_nothing_is_refused(paths):
    """Retention of zero would mean the app deletes the backup it just took to protect a write
    it is about to perform."""
    with pytest.raises(ValueError):
        backups.prune(paths, ENTRY, keep=0)


def test_the_archive_is_never_visible_half_written(paths, tmp_path, monkeypatch):
    """A crash mid-zip must not leave something that looks like a rescue but is not one."""
    live = tree(tmp_path / "saves", {"slot1.sav": "progress"})

    def die(destination, _live):
        destination.write_bytes(b"PK\x03\x04 half a zip")
        raise OSError("the disk filled up")

    monkeypatch.setattr(backups, "_write_archive", die)

    with pytest.raises(OSError, match="disk filled up"):
        take(paths, live)

    assert backups.list_for(paths, ENTRY) == []
    assert list(backups.directory_for(paths, ENTRY).iterdir()) == []


def test_an_empty_save_folder_archives_to_nothing_and_is_caught_by_verification(paths, tmp_path):
    """`content_hash` of an empty folder is None, so there is nothing to back up - and the
    caller (`transaction.write_live`) skips the backup entirely rather than taking an empty
    zip. If it ever did call us, verification would refuse it rather than produce a rescue
    archive holding no save."""
    live = tmp_path / "saves"
    live.mkdir()

    with pytest.raises(BackupUnverified):
        backups.create(paths, ENTRY, live, "restore", expected_hash="anything-at-all")

    assert backups.list_for(paths, ENTRY) == []
