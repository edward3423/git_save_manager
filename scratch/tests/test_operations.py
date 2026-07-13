"""The verbs: Add, Sync, Restore, Resolve, Rollback, Remove.

Real Git in temp directories. The centrepiece is the stable-read guard: a game writing its
save *while we copy it* must abort the Sync with nothing committed, and the Vault left clean.
"""

import shutil

import pytest

from core import backups, entries, ledger, operations, vault
from core.config import Config, MachineDescription
from core.entry_state import Action, EntryState
from core.hashing import content_hash
from core.ledger import Ledger
from core.operations import NothingToSync, SyncAborted
from core.paths import Paths

MACHINE = "9c8b7a65-0000-4000-8000-000000000001"


@pytest.fixture
def config():
    return Config(machine_id=MACHINE, repo="owner/vault", default_branch="main", backup_retention=3)


@pytest.fixture
def description():
    return MachineDescription(hostname="laptop", os_name="Darwin")


@pytest.fixture
def paths(tmp_path, config, description):
    paths = Paths(root=tmp_path / "app")
    vault.initialize(paths, config, description, branch="main")
    vault.git(paths).run("add", "-A")
    vault.git(paths).run("commit", "-m", "init", config=("user.name=t", "user.email=t@gsm.local"))
    return paths


@pytest.fixture
def the_ledger():
    return Ledger()


@pytest.fixture
def live(tmp_path):
    """A game's save folder, with progress in it."""
    folder = tmp_path / "game" / "saves"
    folder.mkdir(parents=True)
    (folder / "slot1.sav").write_text("my progress", encoding="utf-8")
    (folder / "meta" / "profile.json").parent.mkdir()
    (folder / "meta" / "profile.json").write_text("{}", encoding="utf-8")
    return folder


@pytest.fixture
def entry(paths, config, description, the_ledger, live):
    return operations.add_entry(paths, config, description, the_ledger, "Elden Ring", live)


def state(paths, the_ledger, entry_id):
    return ledger.refresh(paths, the_ledger, entry_id)


# --- adding --------------------------------------------------------------------------------------


def test_adding_an_entry_commits_its_sidecar_and_leaves_the_vault_clean(paths, entry):
    """An uncommitted file in the Vault breaches Invariant 2, and the *next* operation would
    dutifully clean it away - taking the new Entry with it."""
    assert vault.is_clean(paths)
    assert entries.require(paths, entry.entry_id).name == "Elden Ring"


def test_a_new_entry_is_local_ahead_with_no_baseline(paths, the_ledger, entry):
    """Born with a Vault holding nothing and no Baseline. The first Sync is the user's."""
    status = state(paths, the_ledger, entry.entry_id)

    assert status.state is EntryState.LOCAL_AHEAD
    assert status.recommended is Action.SYNC_TO_VAULT
    assert the_ledger.get(entry.entry_id).baseline is None


def test_an_oversized_file_is_refused_at_add_not_at_push(
    paths, config, description, the_ledger, tmp_path
):
    """A commit GitHub rejects can never leave the Vault, and the only way out is a history
    rewrite. Far better to refuse it before it is ever made."""
    live = tmp_path / "huge"
    live.mkdir()
    (live / "enormous.bin").write_bytes(b"\0" * 2048)

    with pytest.raises(vault.FileTooLarge):
        operations.add_entry(
            paths, config, description, the_ledger, "Huge", live, max_file_bytes=1024
        )


# --- Sync: Live -> Vault -------------------------------------------------------------------------


def test_a_sync_commits_the_save_and_records_a_baseline(
    paths, config, description, the_ledger, live, entry
):
    synced = operations.sync_to_vault(paths, config, description, the_ledger, entry.entry_id)

    assert content_hash(paths.entry_content_dir(entry.entry_id)) == content_hash(live)
    assert the_ledger.get(entry.entry_id).baseline == synced.baseline
    assert state(paths, the_ledger, entry.entry_id).state is EntryState.IN_SYNC
    assert vault.is_clean(paths)


def test_the_commit_is_authored_by_the_machine_that_made_it(
    paths, config, description, the_ledger, entry
):
    """`git log --format=%an` must answer "which machine synced this?" directly."""
    operations.sync_to_vault(paths, config, description, the_ledger, entry.entry_id)

    [commit] = [
        c for c in operations.history(paths, entry.entry_id) if c.subject.startswith("sync")
    ]

    assert commit.machine == "laptop"
    assert commit.subject == "sync(Elden Ring): from laptop"


def test_syncing_unchanged_content_commits_nothing(paths, config, description, the_ledger, entry):
    """The Baseline's quiet superpower: we only commit when the content actually changed, so
    the Cloud Vault does not grow by a full copy of the save every time the app is opened."""
    operations.sync_to_vault(paths, config, description, the_ledger, entry.entry_id)
    before = vault.git(paths).run("rev-parse", "HEAD").strip()

    again = operations.sync_to_vault(paths, config, description, the_ledger, entry.entry_id)

    assert again.commit is None
    assert vault.git(paths).run("rev-parse", "HEAD").strip() == before


def test_syncing_an_empty_live_save_is_refused(paths, config, description, the_ledger, live, entry):
    """Invariant 3. Committing the absence would empty the Entry in the Vault - and an
    uninstalled game that left its folder behind must not be able to do that."""
    shutil.rmtree(live)
    live.mkdir()

    with pytest.raises(NothingToSync):
        operations.sync_to_vault(paths, config, description, the_ledger, entry.entry_id)


# --- the stable-read guard -----------------------------------------------------------------------


def test_a_game_writing_its_save_mid_copy_aborts_the_sync(
    monkeypatch, paths, config, description, the_ledger, live, entry
):
    """The reason the guard exists. The game writes while we are copying, so what we captured
    is a mixture of before and after - a save that never existed. Nothing may be committed.

    H1 == H2 proves the source held still *for exactly the interval of the copy*. That is why
    no artificial delay is used or wanted: the copy **is** the window under test.
    """
    real_copy = operations._copy_into_vault

    def copy_while_the_game_saves(source, destination):
        real_copy(source, destination)
        (live / "slot1.sav").write_text("progress made mid-copy", encoding="utf-8")

    monkeypatch.setattr(operations, "_copy_into_vault", copy_while_the_game_saves)

    with pytest.raises(SyncAborted):
        operations.sync_to_vault(paths, config, description, the_ledger, entry.entry_id)

    assert vault.is_clean(paths)  # nothing left in the working tree
    assert not any(c.subject.startswith("sync") for c in operations.history(paths, entry.entry_id))
    assert the_ledger.get(entry.entry_id).baseline is None  # and no Baseline was written


def test_a_torn_copy_aborts_the_sync_even_though_the_game_is_closed(
    monkeypatch, paths, config, description, the_ledger, entry
):
    """H3 == H1 is not the same check as H1 == H2. The source held perfectly still; the *copy*
    came out wrong - a full disk, an I/O error. Committing it would put a corrupt save in the
    Vault and, worse, record a Baseline saying it is the real one."""

    def copy_badly(source, destination):
        destination.mkdir(parents=True, exist_ok=True)
        (destination / "slot1.sav").write_text("only half of it arrived", encoding="utf-8")

    monkeypatch.setattr(operations, "_copy_into_vault", copy_badly)

    with pytest.raises(SyncAborted):
        operations.sync_to_vault(paths, config, description, the_ledger, entry.entry_id)

    assert vault.is_clean(paths)
    assert the_ledger.get(entry.entry_id).baseline is None


def test_an_aborted_sync_leaves_a_previously_synced_entry_exactly_as_it_was(
    monkeypatch, paths, config, description, the_ledger, live, entry
):
    """The Vault must not be left holding a half-copied save from a Sync that failed."""
    operations.sync_to_vault(paths, config, description, the_ledger, entry.entry_id)
    good = content_hash(paths.entry_content_dir(entry.entry_id))
    baseline = the_ledger.get(entry.entry_id).baseline

    (live / "slot1.sav").write_text("more progress", encoding="utf-8")

    real = operations._copy_into_vault

    def copy_while_the_game_saves(source, destination):
        real(source, destination)
        (live / "slot1.sav").write_text("written mid-copy", encoding="utf-8")

    monkeypatch.setattr(operations, "_copy_into_vault", copy_while_the_game_saves)

    with pytest.raises(SyncAborted):
        operations.sync_to_vault(paths, config, description, the_ledger, entry.entry_id)

    assert content_hash(paths.entry_content_dir(entry.entry_id)) == good  # untouched
    assert the_ledger.get(entry.entry_id).baseline == baseline  # unmoved


def test_a_failed_commit_leaves_no_baseline_and_no_wreckage(
    monkeypatch, paths, config, description, the_ledger, live, entry
):
    """The Baseline is written strictly *after* the commit lands, and this is why.

    Write it first and it survives a failed commit - a full disk, a rejected hook - claiming a
    Sync that never happened. The Live Save would then match the Baseline while the Vault did
    not, so the app would report **Vault Ahead** and cheerfully offer to restore the *old* save
    over the new one. The user would lose the progress they had just tried to back up.

    Nor may the copy be left in the Vault's working tree: uncommitted content there breaches
    Invariant 2, and the next status refresh would read it as though it were the Vault.
    """
    operations.sync_to_vault(paths, config, description, the_ledger, entry.entry_id)
    in_the_vault = content_hash(paths.entry_content_dir(entry.entry_id))
    baseline = the_ledger.get(entry.entry_id).baseline

    (live / "slot1.sav").write_text("progress I am trying to save", encoding="utf-8")
    played_on = content_hash(live)

    def the_disk_fills_up(*_args, **_kwargs):
        raise OSError("no space left on device")

    monkeypatch.setattr(operations, "_commit", the_disk_fills_up)

    with pytest.raises(OSError, match="no space left"):
        operations.sync_to_vault(paths, config, description, the_ledger, entry.entry_id)
    monkeypatch.undo()

    assert vault.is_clean(paths)
    assert content_hash(paths.entry_content_dir(entry.entry_id)) == in_the_vault  # no wreckage
    assert the_ledger.get(entry.entry_id).baseline == baseline  # and no Baseline was written

    status = state(paths, the_ledger, entry.entry_id)
    assert status.state is EntryState.LOCAL_AHEAD  # try the Sync again
    assert status.recommended is Action.SYNC_TO_VAULT
    assert content_hash(live) == played_on  # the save is exactly where the user left it


# --- Restore: Vault -> Live ----------------------------------------------------------------------


def test_a_restore_writes_the_vault_over_the_live_save_and_backs_it_up(
    paths, config, description, the_ledger, live, entry
):
    operations.sync_to_vault(paths, config, description, the_ledger, entry.entry_id)
    in_the_vault = content_hash(paths.entry_content_dir(entry.entry_id))

    (live / "slot1.sav").write_text("progress I am about to discard", encoding="utf-8")
    doomed = content_hash(live)

    written = operations.restore_to_live(paths, config, the_ledger, entry.entry_id)

    assert content_hash(live) == in_the_vault
    assert backups.archive_hash(written.backup.path) == doomed  # the discarded save, kept
    assert state(paths, the_ledger, entry.entry_id).state is EntryState.IN_SYNC


def test_restoring_records_a_baseline_because_data_moved_between_live_and_vault(
    paths, config, description, the_ledger, live, entry
):
    operations.sync_to_vault(paths, config, description, the_ledger, entry.entry_id)
    (live / "slot1.sav").write_text("local change", encoding="utf-8")

    operations.restore_to_live(paths, config, the_ledger, entry.entry_id)

    assert the_ledger.get(entry.entry_id).baseline == content_hash(live)
    assert the_ledger.get(entry.entry_id).last_sync_direction == "to_live"


# --- restoring a Backup is NOT a Sync ------------------------------------------------------------


def test_restoring_a_backup_does_not_move_the_baseline(
    paths, config, description, the_ledger, live, entry
):
    """No data moved between the Live Save and the Vault - it came out of a zip. Recording a
    Baseline here would claim a Sync that never happened, and the app would report In Sync
    while the two sides differ. The Entry must read Local Ahead: the user still has to decide
    whether to Sync this old save up.
    """
    # Sync, play on, then Restore the Vault over the top - which archives the local progress.
    operations.sync_to_vault(paths, config, description, the_ledger, entry.entry_id)
    in_the_vault = content_hash(live)

    (live / "slot1.sav").write_text("local progress I then threw away", encoding="utf-8")
    thrown_away = content_hash(live)

    operations.restore_to_live(paths, config, the_ledger, entry.entry_id)
    assert content_hash(live) == in_the_vault
    baseline = the_ledger.get(entry.entry_id).baseline
    assert baseline == in_the_vault

    # "Actually, I wanted that local progress after all." It is in the backup, and only there.
    [archived] = operations.list_backups(paths, entry.entry_id)
    assert backups.archive_hash(archived.path) == thrown_away

    operations.restore_backup(paths, config, the_ledger, entry.entry_id, archived)

    assert content_hash(live) == thrown_away  # the save is back on disk
    assert the_ledger.get(entry.entry_id).baseline == baseline  # and the Baseline did not move

    status = state(paths, the_ledger, entry.entry_id)
    assert status.state is EntryState.LOCAL_AHEAD  # the user still decides whether to Sync it up
    assert status.recommended is Action.SYNC_TO_VAULT


# --- history and rollback ------------------------------------------------------------------------


def test_history_lists_every_commit_that_touched_the_entry(
    paths, config, description, the_ledger, live, entry
):
    operations.sync_to_vault(paths, config, description, the_ledger, entry.entry_id)
    (live / "slot1.sav").write_text("later progress", encoding="utf-8")
    operations.sync_to_vault(paths, config, description, the_ledger, entry.entry_id)

    found = operations.history(paths, entry.entry_id)

    assert [c.subject for c in found] == [
        "sync(Elden Ring): from laptop",
        "sync(Elden Ring): from laptop",
        "add(Elden Ring): from laptop",
    ]


def test_rollback_is_a_forward_commit_that_lands_the_entry_in_vault_ahead(
    paths, config, description, the_ledger, live, entry
):
    """Never a reset, never a force-push. The version rolled away from stays in the log, so a
    rollback can itself be rolled back. And it writes no save file: it changes the Vault, which
    lands the Entry in Vault Ahead, and restoring it is then the ordinary Restore - so exactly
    one piece of code in this application ever overwrites a Live Save.
    """
    operations.sync_to_vault(paths, config, description, the_ledger, entry.entry_id)
    good = [c for c in operations.history(paths, entry.entry_id) if c.subject.startswith("sync")][0]
    old_content = content_hash(live)

    (live / "slot1.sav").write_text("a save I will come to regret", encoding="utf-8")
    operations.sync_to_vault(paths, config, description, the_ledger, entry.entry_id)
    regretted = content_hash(live)

    operations.rollback(paths, config, description, entry.entry_id, good.sha)

    assert content_hash(paths.entry_content_dir(entry.entry_id)) == old_content
    assert content_hash(live) == regretted  # the Live Save is untouched by a rollback

    status = state(paths, the_ledger, entry.entry_id)
    assert status.state is EntryState.VAULT_AHEAD
    assert status.recommended is Action.RESTORE_TO_LIVE

    # and the regretted version is still reachable
    assert len(operations.history(paths, entry.entry_id)) == 4


def test_rolling_back_with_unsynced_local_changes_lands_in_conflict(
    paths, config, description, the_ledger, live, entry
):
    """No special case, and nothing at risk: both sides moved, so the human is asked."""
    operations.sync_to_vault(paths, config, description, the_ledger, entry.entry_id)
    good = [c for c in operations.history(paths, entry.entry_id) if c.subject.startswith("sync")][0]

    (live / "slot1.sav").write_text("progress", encoding="utf-8")
    operations.sync_to_vault(paths, config, description, the_ledger, entry.entry_id)
    (live / "slot1.sav").write_text("unsynced progress", encoding="utf-8")

    operations.rollback(paths, config, description, entry.entry_id, good.sha)

    assert state(paths, the_ledger, entry.entry_id).state is EntryState.CONFLICT


# --- removal -------------------------------------------------------------------------------------


def test_removing_from_the_vault_never_touches_a_live_save(
    paths, config, description, the_ledger, live, entry
):
    operations.sync_to_vault(paths, config, description, the_ledger, entry.entry_id)
    before = content_hash(live)

    operations.remove_from_vault(paths, config, description, the_ledger, entry.entry_id)

    assert content_hash(live) == before  # Invariant 1, even here
    assert not paths.entry_content_dir(entry.entry_id).exists()
    assert not the_ledger.is_bound(entry.entry_id)
    assert vault.is_clean(paths)


def test_a_removed_entry_is_still_recoverable_from_history(
    paths, config, description, the_ledger, entry
):
    """A forward commit, so the content remains in the log. Invariant 3."""
    operations.sync_to_vault(paths, config, description, the_ledger, entry.entry_id)

    operations.remove_from_vault(paths, config, description, the_ledger, entry.entry_id)

    assert len(operations.history(paths, entry.entry_id)) == 3  # add, sync, remove


# --- renaming ------------------------------------------------------------------------------------


def test_renaming_moves_no_data_and_keeps_the_binding(
    paths, config, description, the_ledger, live, entry
):
    """Identity is the UUID. The name is a label, and renaming it must not disturb a thing."""
    operations.sync_to_vault(paths, config, description, the_ledger, entry.entry_id)
    baseline = the_ledger.get(entry.entry_id).baseline

    operations.rename_entry(paths, config, description, entry.entry_id, "Elden Ring (NG+)")

    assert entries.require(paths, entry.entry_id).name == "Elden Ring (NG+)"
    assert the_ledger.get(entry.entry_id).live_path == str(live)
    assert the_ledger.get(entry.entry_id).baseline == baseline
    assert state(paths, the_ledger, entry.entry_id).state is EntryState.IN_SYNC


# --- single-file Entries -------------------------------------------------------------------------


def test_a_single_file_entry_syncs_and_restores(paths, config, description, the_ledger, tmp_path):
    """An application's settings file, not a save folder. The Vault mirrors the Live Save's
    kind exactly - a file stays a file - so the two hash identically and In Sync is reachable."""
    settings = tmp_path / "app" / "settings.ini"
    settings.parent.mkdir(parents=True, exist_ok=True)
    settings.write_text("volume=11", encoding="utf-8")

    entry = operations.add_entry(paths, config, description, the_ledger, "Settings", settings)
    operations.sync_to_vault(paths, config, description, the_ledger, entry.entry_id)

    assert paths.entry_content_dir(entry.entry_id).is_file()
    assert state(paths, the_ledger, entry.entry_id).state is EntryState.IN_SYNC

    settings.write_text("volume=3", encoding="utf-8")
    operations.restore_to_live(paths, config, the_ledger, entry.entry_id)

    assert settings.read_text(encoding="utf-8") == "volume=11"
    assert settings.is_file()
