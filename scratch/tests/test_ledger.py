"""The Ledger, and above all the discipline around the Baseline.

A wrong Baseline does not produce an error; it produces a *confident, wrong recommendation*
to overwrite a save. So the tests here are less about persistence than about who is allowed
to write one, and when.
"""

import os
from datetime import UTC, datetime
from pathlib import Path

import pytest

from core.entry_state import Action, EntryState
from core.hashing import content_hash
from core.jsonstore import CorruptJson
from core.ledger import (
    Ledger,
    LedgerTooNew,
    NotBound,
    SyncDirection,
    load,
    normalize_live_path,
    refresh,
    save,
)
from core.paths import Paths

ENTRY = "3f2a1b7c-0000-4000-8000-000000000001"


@pytest.fixture
def paths(tmp_path):
    return Paths(root=tmp_path)


def write(root: Path, files: dict[str, str]) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    for relative, contents in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(contents, encoding="utf-8")
    return root


# --- who may write a Baseline ------------------------------------------------------------


def test_binding_does_not_set_a_baseline():
    """Binding moves no data, so it has nothing to record. A Baseline written here would
    assert that a Sync happened when none did, and the app would report In Sync while the
    two sides differ."""
    ledger = Ledger()
    binding = ledger.bind(ENTRY, "/games/save")

    assert binding.baseline is None


def test_only_a_completed_sync_writes_a_baseline():
    ledger = Ledger()
    ledger.bind(ENTRY, "/games/save")

    ledger.record_sync(ENTRY, "hash-a", SyncDirection.TO_VAULT)

    assert ledger.get(ENTRY).baseline == "hash-a"


def test_a_sync_records_when_and_which_way():
    ledger = Ledger()
    ledger.bind(ENTRY, "/games/save")
    at = datetime(2026, 7, 12, 9, 30, tzinfo=UTC)

    ledger.record_sync(ENTRY, "hash-a", SyncDirection.TO_LIVE, at=at)

    binding = ledger.get(ENTRY)
    assert binding.last_sync_at == "2026-07-12T09:30:00+00:00"
    assert binding.last_sync_direction == "to_live"


def test_recording_a_sync_for_an_unbound_entry_refuses():
    """A Baseline with no Binding describes a Live Save at no known path."""
    with pytest.raises(NotBound):
        Ledger().record_sync(ENTRY, "hash-a", SyncDirection.TO_VAULT)


def test_unbinding_drops_the_baseline_with_the_binding():
    """A Baseline outliving its Binding would be applied to whatever path the Entry is bound
    to next, and would claim a Sync that never happened between them."""
    ledger = Ledger()
    ledger.bind(ENTRY, "/games/save")
    ledger.record_sync(ENTRY, "hash-a", SyncDirection.TO_VAULT)

    ledger.unbind(ENTRY)
    ledger.bind(ENTRY, "/games/save")

    assert ledger.get(ENTRY).baseline is None


def test_unbinding_an_unbound_entry_is_harmless():
    Ledger().unbind(ENTRY)  # must not raise


# --- the repair path ---------------------------------------------------------------------


def test_refresh_heals_a_baseline_lost_to_a_crash(paths):
    """The crash between "the files landed" and "the Baseline was recorded". Both sides hold
    identical content, so the Sync provably completed - the record of it is simply gone."""
    live = write(paths.root / "live", {"slot1.sav": "progress"})
    vault = write(paths.entry_content_dir(ENTRY), {"slot1.sav": "progress"})

    ledger = Ledger()
    ledger.bind(ENTRY, live)
    ledger.record_sync(ENTRY, "a-stale-hash-from-before-the-crash", SyncDirection.TO_VAULT)

    status = refresh(paths, ledger, ENTRY)

    assert status.state is EntryState.IN_SYNC
    assert ledger.get(ENTRY).baseline == content_hash(vault)


def test_the_healed_baseline_is_persisted_immediately(paths):
    """If the repair lived only in memory, the false Conflict would return on the next launch
    - and every launch after it."""
    live = write(paths.root / "live", {"slot1.sav": "progress"})
    write(paths.entry_content_dir(ENTRY), {"slot1.sav": "progress"})

    ledger = Ledger()
    ledger.bind(ENTRY, live)
    ledger.record_sync(ENTRY, "stale", SyncDirection.TO_VAULT)
    save(paths, ledger)

    refresh(paths, ledger, ENTRY)

    assert load(paths).get(ENTRY).baseline == ledger.get(ENTRY).baseline
    assert load(paths).get(ENTRY).baseline != "stale"


def test_refresh_does_not_touch_the_baseline_when_the_sides_differ(paths):
    """The Baseline is only ever healed to content both sides already hold. Where they
    differ, there is nothing to heal it *to*, and guessing would destroy the one piece of
    evidence that says which side moved."""
    live = write(paths.root / "live", {"slot1.sav": "played more"})
    write(paths.entry_content_dir(ENTRY), {"slot1.sav": "progress"})

    ledger = Ledger()
    ledger.bind(ENTRY, live)
    ledger.record_sync(ENTRY, "hash-at-last-sync", SyncDirection.TO_VAULT)

    status = refresh(paths, ledger, ENTRY)

    assert status.state is EntryState.CONFLICT
    assert ledger.get(ENTRY).baseline == "hash-at-last-sync"


def test_refresh_writes_nothing_when_there_is_nothing_to_repair(paths):
    live = write(paths.root / "live", {"slot1.sav": "progress"})
    vault = write(paths.entry_content_dir(ENTRY), {"slot1.sav": "progress"})

    ledger = Ledger()
    ledger.bind(ENTRY, live)
    ledger.record_sync(ENTRY, content_hash(vault), SyncDirection.TO_VAULT)

    assert refresh(paths, ledger, ENTRY).state is EntryState.IN_SYNC
    assert not paths.ledger_file.exists()  # no repair was needed, so no write happened


def test_refresh_of_an_unbound_entry_refuses(paths):
    with pytest.raises(NotBound):
        refresh(paths, Ledger(), ENTRY)


# --- what a Pull may and may not do -------------------------------------------------------


def test_a_pull_does_not_move_the_baseline(paths):
    """Push and Pull move commits between the Vault and the Cloud Vault. The Baseline records
    what moved between the Live Save and the Vault. Nothing about a Pull is evidence that the
    Live Save is current, and treating it as such is how another Machine's progress gets
    overwritten and pushed. Here the Vault is updated exactly as a Pull would - the Ledger is
    not consulted at all - and the Entry must come out Vault Ahead."""
    live = write(paths.root / "live", {"slot1.sav": "my progress"})
    vault = write(paths.entry_content_dir(ENTRY), {"slot1.sav": "my progress"})

    ledger = Ledger()
    ledger.bind(ENTRY, live)
    ledger.record_sync(ENTRY, content_hash(vault), SyncDirection.TO_VAULT)
    baseline_before = ledger.get(ENTRY).baseline

    # A Pull lands another Machine's work in the Vault. It touches no Ledger method.
    (vault / "slot1.sav").write_text("their progress", encoding="utf-8")

    status = refresh(paths, ledger, ENTRY)

    assert status.state is EntryState.VAULT_AHEAD
    assert status.recommended is Action.RESTORE_TO_LIVE
    assert ledger.get(ENTRY).baseline == baseline_before  # unmoved


# --- persistence ---------------------------------------------------------------------------


def test_a_ledger_round_trips_through_disk(paths):
    ledger = Ledger()
    ledger.bind(ENTRY, "/games/save")
    ledger.record_sync(ENTRY, "hash-a", SyncDirection.TO_VAULT)
    save(paths, ledger)

    loaded = load(paths)

    assert loaded.get(ENTRY).baseline == "hash-a"
    assert loaded.get(ENTRY).live == Path("/games/save")  # same place, whatever the separator
    assert loaded.get(ENTRY).last_sync_direction == "to_vault"


def test_a_missing_ledger_loads_as_empty(paths):
    assert load(paths).bindings == {}


def test_a_corrupt_ledger_raises_rather_than_silently_discarding_every_binding(paths):
    paths.ledger_file.parent.mkdir(parents=True, exist_ok=True)
    paths.ledger_file.write_text("{not json", encoding="utf-8")

    with pytest.raises(CorruptJson):
        load(paths)


def test_a_newer_ledger_is_refused_rather_than_misread(paths):
    save(paths, Ledger(schema=99))

    with pytest.raises(LedgerTooNew):
        load(paths)


def test_a_home_relative_path_is_expanded_once_at_bind_time():
    """Stored expanded, so the Binding does not silently mean a different folder if the app
    is ever run as another user."""
    assert not normalize_live_path("~/saves").startswith("~")


@pytest.mark.skipif(os.name == "nt", reason="symlinks need privileges on Windows")
def test_a_symlinked_save_folder_is_not_resolved_away(tmp_path):
    """Games' save folders are routinely symlinked to another drive. Resolving the link at
    bind time would pin the Binding to whatever it pointed at that day, so re-pointing it
    later would strand the Entry on the old target."""
    real = tmp_path / "elsewhere"
    real.mkdir()
    link = tmp_path / "saves"
    link.symlink_to(real)

    assert normalize_live_path(link) == str(link)
