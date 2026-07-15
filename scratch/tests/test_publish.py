"""Each Machine publishes its Bindings in its machine file (ADR-0003: one writer per file).

This is what lets a second Machine's Bind dialog show where the first Machine keeps a save,
as a read-only hint - and what Unbind removes again, so a Machine that dropped an Entry
stops being cited as holding it.
"""

import pytest

from core import operations, vault
from core.config import Config, MachineDescription
from core.ledger import Ledger, NotBound
from core.paths import Paths

MACHINE = "9c8b7a65-0000-4000-8000-00000000000a"


@pytest.fixture
def machine(tmp_path):
    paths = Paths(root=tmp_path)
    config = Config(machine_id=MACHINE, repo="owner/vault", default_branch="main")
    description = MachineDescription(hostname="laptop", os_name="X")
    vault.initialize(paths, config, description, branch="main")
    repo = vault.git(paths)
    repo.run("add", "-A")
    repo.run("commit", "-q", "-m", "init", config=("user.name=t", "user.email=t@x"))
    return paths, config, description


def committed_machine_file(paths: Paths) -> dict:
    """The machine file as the *Vault* holds it, not as the working tree does."""
    raw = vault.git(paths).run("show", f"HEAD:machines/{MACHINE}.json")
    import json

    return json.loads(raw)


def test_adding_an_entry_publishes_its_binding(machine):
    paths, config, description = machine
    the_ledger = Ledger()
    live = paths.root / "live" / "Elden Ring"
    live.mkdir(parents=True)
    (live / "slot1.sav").write_text("progress", encoding="utf-8")

    entry = operations.add_entry(paths, config, description, the_ledger, "Elden Ring", live)

    published = committed_machine_file(paths)["bindings"]
    assert published == {entry.entry_id: str(live)}
    assert vault.is_clean(paths)  # published means committed, not left lying in the tree


def test_binding_an_unlinked_entry_publishes_and_unbinding_retracts(machine):
    paths, config, description = machine
    the_ledger = Ledger()
    live = paths.root / "live" / "Elden Ring"
    live.mkdir(parents=True)
    (live / "slot1.sav").write_text("progress", encoding="utf-8")
    entry = operations.add_entry(paths, config, description, the_ledger, "Elden Ring", live)

    operations.unbind_entry(paths, config, description, the_ledger, entry.entry_id)

    assert committed_machine_file(paths)["bindings"] == {}
    with pytest.raises(NotBound):
        the_ledger.require(entry.entry_id)
    assert (live / "slot1.sav").exists()  # Invariant 1: the Live Save is untouched
    assert paths.entry_sidecar(entry.entry_id).exists()  # the Vault keeps the Entry
    assert vault.is_clean(paths)

    operations.bind_entry(paths, config, description, the_ledger, entry.entry_id, live)

    assert committed_machine_file(paths)["bindings"] == {entry.entry_id: str(live)}
    assert the_ledger.require(entry.entry_id).live == live
    assert vault.is_clean(paths)


def test_unbinding_never_writes_a_baseline_into_the_next_bind(machine):
    """The Binding returns without a Baseline: rebinding claims no Sync that never happened."""
    paths, config, description = machine
    the_ledger = Ledger()
    live = paths.root / "live" / "Elden Ring"
    live.mkdir(parents=True)
    (live / "slot1.sav").write_text("progress", encoding="utf-8")
    entry = operations.add_entry(paths, config, description, the_ledger, "Elden Ring", live)
    operations.sync_to_vault(paths, config, description, the_ledger, entry.entry_id)

    operations.unbind_entry(paths, config, description, the_ledger, entry.entry_id)
    operations.bind_entry(paths, config, description, the_ledger, entry.entry_id, live)

    assert the_ledger.require(entry.entry_id).baseline is None


def test_another_machines_published_binding_is_readable_as_a_hint(machine):
    """What the Bind dialog shows: 'the laptop keeps this save at ...' - read-only."""
    paths, config, description = machine
    the_ledger = Ledger()
    live = paths.root / "live" / "Elden Ring"
    live.mkdir(parents=True)
    (live / "slot1.sav").write_text("progress", encoding="utf-8")
    entry = operations.add_entry(paths, config, description, the_ledger, "Elden Ring", live)

    other = "9c8b7a65-0000-4000-8000-00000000000b"
    machines = vault.list_machines(paths)
    assert [m["machine_id"] for m in machines] == [MACHINE]
    assert machines[0]["hostname"] == "laptop"
    assert machines[0]["bindings"][entry.entry_id] == str(live)
    assert other not in [m["machine_id"] for m in machines]
