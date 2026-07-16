"""The state machine. Every recommendation the app ever makes comes from here, so this is
tested exhaustively rather than by example.

Only the *equality pattern* of (Live, Vault, Baseline) matters, never the hash values, so
the entire state space is 3x3x3 = 27 cases with each value drawn from {absent, A, B}. All
27 are enumerated below with their expected state, and the safety properties are asserted
across all of them at once.
"""

import itertools

import pytest

from core.entry_state import Action, EntryState, evaluate, evaluate_entry

A = "hash-a"
B = "hash-b"
_ = None  # no content: the path is absent, or it is a directory holding no files

# (Live, Vault, Baseline) -> expected state. The whole space. Read this table, not the code.
TABLE = {
    # Nothing anywhere. Never "In Sync": there is no content to be in sync about.
    (_, _, _): EntryState.NO_CONTENT,
    (_, _, A): EntryState.NO_CONTENT,
    (_, _, B): EntryState.NO_CONTENT,
    # No Live Save. With no Baseline this is the ordinary second-machine bind, and the Vault
    # is simply ahead. With one, the Live Save has *vanished* - and must not be Synced away.
    (_, A, _): EntryState.VAULT_AHEAD,
    (_, A, A): EntryState.LIVE_SAVE_MISSING,
    (_, A, B): EntryState.LIVE_SAVE_MISSING,
    (_, B, _): EntryState.VAULT_AHEAD,
    (_, B, A): EntryState.LIVE_SAVE_MISSING,
    (_, B, B): EntryState.LIVE_SAVE_MISSING,
    # No Vault content. With no Baseline it was simply never Synced. With one, another
    # Machine removed it - and it must not be Restored over a real Live Save.
    (A, _, _): EntryState.LOCAL_AHEAD,
    (A, _, A): EntryState.REMOVED_FROM_VAULT,
    (A, _, B): EntryState.REMOVED_FROM_VAULT,
    (B, _, _): EntryState.LOCAL_AHEAD,
    (B, _, A): EntryState.REMOVED_FROM_VAULT,
    (B, _, B): EntryState.REMOVED_FROM_VAULT,
    # The two sides agree. In Sync whatever the Baseline says - and the Baseline is healed.
    (A, A, _): EntryState.IN_SYNC,
    (A, A, A): EntryState.IN_SYNC,
    (A, A, B): EntryState.IN_SYNC,
    (B, B, _): EntryState.IN_SYNC,
    (B, B, A): EntryState.IN_SYNC,
    (B, B, B): EntryState.IN_SYNC,
    # The two sides differ, and the Baseline says which one moved.
    (A, B, _): EntryState.CONFLICT,  # no Baseline: both read as moved. Ask for a direction.
    (A, B, A): EntryState.VAULT_AHEAD,  # Live untouched, Vault moved on (a Pull landed).
    (A, B, B): EntryState.LOCAL_AHEAD,  # Live moved, Vault untouched (you played the game).
    (B, A, _): EntryState.CONFLICT,
    (B, A, A): EntryState.LOCAL_AHEAD,
    (B, A, B): EntryState.VAULT_AHEAD,
}

ALL_CASES = list(itertools.product([_, A, B], repeat=3))


def test_the_table_covers_the_entire_state_space():
    """If this fails, a case exists that no one has thought about."""
    assert set(TABLE) == set(ALL_CASES)
    assert len(TABLE) == 27


@pytest.mark.parametrize(("live", "vault", "baseline"), ALL_CASES)
def test_every_case_derives_its_expected_state(live, vault, baseline):
    assert evaluate(live, vault, baseline).state == TABLE[(live, vault, baseline)]


# --- the safety properties, asserted across the whole space ----------------------------


@pytest.mark.parametrize(("live", "vault", "baseline"), ALL_CASES)
def test_syncing_is_never_offered_without_a_live_save(live, vault, baseline):
    """Invariant 3, the Vault is append-only. Syncing an absent Live Save would commit the
    absence and erase the Entry's content in the Vault. An unplugged drive must not be able
    to do that, and neither must an uninstalled game that left an empty save folder."""
    status = evaluate(live, vault, baseline)
    if Action.SYNC_TO_VAULT in status.offered:
        assert live is not None


@pytest.mark.parametrize(("live", "vault", "baseline"), ALL_CASES)
def test_restoring_is_never_offered_without_vault_content(live, vault, baseline):
    """Invariant 1, the application never deletes a Live Save. Restoring from an absent Vault
    Entry would write nothing over a real save."""
    status = evaluate(live, vault, baseline)
    if Action.RESTORE_TO_LIVE in status.offered:
        assert vault is not None


@pytest.mark.parametrize(("live", "vault", "baseline"), ALL_CASES)
def test_the_recommended_action_is_always_one_of_the_offered_ones(live, vault, baseline):
    status = evaluate(live, vault, baseline)
    if status.recommended is not None:
        assert status.recommended in status.offered


@pytest.mark.parametrize(("live", "vault", "baseline"), ALL_CASES)
def test_nothing_is_ever_recommended_where_a_human_must_choose(live, vault, baseline):
    """The states that mean "something is gone" have no safe default: an unplugged drive and
    a deliberate deletion are indistinguishable from here."""
    status = evaluate(live, vault, baseline)
    if status.state in (EntryState.LIVE_SAVE_MISSING, EntryState.REMOVED_FROM_VAULT):
        assert status.recommended is None
        assert status.offered  # but something is always still possible


@pytest.mark.parametrize(("live", "vault", "baseline"), ALL_CASES)
def test_a_baseline_is_only_ever_repaired_to_content_both_sides_already_hold(live, vault, baseline):
    """The repair may restore a lost Baseline; it may never invent one. So it only fires
    where Live and Vault are identical, which is the post-condition of a completed Sync."""
    status = evaluate(live, vault, baseline)
    if status.baseline_repair is not None:
        assert status.state is EntryState.IN_SYNC
        assert status.baseline_repair == live == vault
        assert status.baseline_repair != baseline  # a no-op repair is not a repair


@pytest.mark.parametrize(("live", "vault", "baseline"), ALL_CASES)
def test_in_sync_always_means_the_two_sides_hold_identical_content(live, vault, baseline):
    """In Sync is the one state that promises the user there is nothing to do. It must never
    be reachable while the two sides differ - including when both are empty."""
    status = evaluate(live, vault, baseline)
    if status.state is EntryState.IN_SYNC:
        assert live is not None
        assert live == vault
        assert not status.offered


# --- the disasters, named ---------------------------------------------------------------


def test_a_pulled_vault_over_an_untouched_live_save_is_vault_ahead_not_local_ahead():
    """The scenario a naive design gets fatally wrong, and the reason the Baseline exists.

    You Synced (Baseline = A). You did not play. Another Machine Synced and pushed, and you
    Pulled, so the Vault now holds B. The Baseline must still be A, because *no data moved
    between your Live Save and your Vault* - and so the state is Vault Ahead: take B.

    Advance the Baseline on Pull, as is tempting, and it reads B instead. The Live Save (A)
    then looks *changed* against it, the app reports Local Ahead, and it offers to Sync your
    stale save over the other Machine's progress - and then push it. That is how you lose a
    save file and are told it worked.
    """
    assert evaluate(live=A, vault=B, baseline=A).state is EntryState.VAULT_AHEAD

    # The bug, made explicit: the same world, with the Baseline wrongly advanced by the Pull.
    assert evaluate(live=A, vault=B, baseline=B).state is EntryState.LOCAL_AHEAD


def test_a_stale_baseline_heals_instead_of_raising_a_false_conflict():
    """A crash between "the files landed" and "the Baseline was recorded" leaves the Baseline
    behind. Both sides hold identical content, so the Sync provably completed; the record of
    it is simply missing. Without the short-circuit this resurfaces as a Conflict forever."""
    status = evaluate(live=A, vault=A, baseline=B)

    assert status.state is EntryState.IN_SYNC
    assert status.baseline_repair == A


def test_binding_onto_a_matching_live_path_needs_no_prompt():
    status = evaluate(live=A, vault=A, baseline=None)

    assert status.state is EntryState.IN_SYNC
    assert status.baseline_repair == A  # adopted, not invented: the two sides already agree


def test_binding_onto_a_differing_live_path_asks_for_a_direction():
    status = evaluate(live=A, vault=B, baseline=None)

    assert status.state is EntryState.CONFLICT
    assert status.recommended is Action.RESOLVE


def test_a_vanished_live_save_is_never_answered_by_emptying_the_vault():
    """The external drive is unplugged. The three-way table alone would call this Local Ahead
    - Live "changed", Vault did not - and offer to Sync the absence into the Vault."""
    status = evaluate(live=None, vault=A, baseline=A)

    assert status.state is EntryState.LIVE_SAVE_MISSING
    assert Action.SYNC_TO_VAULT not in status.offered
    assert status.offered == (Action.RESTORE_TO_LIVE, Action.UNBIND)


def test_an_entry_another_machine_removed_is_never_answered_by_deleting_the_live_save():
    """The three-way table alone would call this Vault Ahead - Vault "changed", Live did not
    - and offer to Restore, writing nothing over a real save."""
    status = evaluate(live=A, vault=None, baseline=A)

    assert status.state is EntryState.REMOVED_FROM_VAULT
    assert Action.RESTORE_TO_LIVE not in status.offered
    assert status.offered == (Action.SYNC_TO_VAULT, Action.UNBIND)


# --- against real files ------------------------------------------------------------------


def write(root, files: dict[str, str]):
    root.mkdir(parents=True, exist_ok=True)
    for relative, contents in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(contents, encoding="utf-8")
    return root


def test_the_four_ordinary_states_from_real_files_on_disk(tmp_path):
    live = write(tmp_path / "live", {"slot1.sav": "played"})
    vault = write(tmp_path / "vault", {"slot1.sav": "played"})

    in_sync = evaluate_entry(live, vault, baseline=None)
    assert in_sync.state is EntryState.IN_SYNC
    synced = in_sync.baseline_repair

    (live / "slot1.sav").write_text("played more", encoding="utf-8")
    assert evaluate_entry(live, vault, synced).state is EntryState.LOCAL_AHEAD

    (live / "slot1.sav").write_text("played", encoding="utf-8")
    (vault / "slot1.sav").write_text("other machine played", encoding="utf-8")
    assert evaluate_entry(live, vault, synced).state is EntryState.VAULT_AHEAD

    (live / "slot1.sav").write_text("played more", encoding="utf-8")
    assert evaluate_entry(live, vault, synced).state is EntryState.CONFLICT


def test_an_empty_live_folder_is_not_local_ahead_and_cannot_erase_the_vault(tmp_path):
    """The uninstalled game. It removed its saves but left the folder, so the Live Save is
    *empty*, not absent - and Git cannot represent an empty directory, so it is the same
    content as none at all. If this reported Local Ahead, the next Sync would commit the
    emptiness and wipe the Entry's content in the Vault."""
    live = write(tmp_path / "live", {"slot1.sav": "progress"})
    vault = write(tmp_path / "vault", {"slot1.sav": "progress"})
    baseline = evaluate_entry(live, vault, None).baseline_repair

    (live / "slot1.sav").unlink()  # the folder remains, and is now empty

    status = evaluate_entry(live, vault, baseline)

    assert status.state is EntryState.LIVE_SAVE_MISSING
    assert Action.SYNC_TO_VAULT not in status.offered


def test_a_live_save_that_does_not_exist_yet_is_vault_ahead(tmp_path):
    """The second-machine path: the Entry came down in the clone, and the game has never run
    here, so its save folder does not exist. Restore is exactly right."""
    vault = write(tmp_path / "vault", {"slot1.sav": "progress"})

    status = evaluate_entry(tmp_path / "not-installed-yet", vault, baseline=None)

    assert status.state is EntryState.VAULT_AHEAD
    assert status.recommended is Action.RESTORE_TO_LIVE


def test_a_single_file_entry_moves_through_the_states(tmp_path):
    """Entries are not always directories: an application settings file is one file. In the
    Vault it lives inside its Entry directory under its own name (`entries/<id>/settings.ini`),
    so the Live file and that one-file directory hash identically and In Sync is reachable."""
    live = tmp_path / "settings.ini"
    vault = tmp_path / "vault"
    vault.mkdir()
    live.write_text("volume=11", encoding="utf-8")
    (vault / "settings.ini").write_text("volume=11", encoding="utf-8")

    baseline = evaluate_entry(live, vault, None).baseline_repair
    assert evaluate_entry(live, vault, baseline).state is EntryState.IN_SYNC

    live.write_text("volume=3", encoding="utf-8")
    assert evaluate_entry(live, vault, baseline).state is EntryState.LOCAL_AHEAD
