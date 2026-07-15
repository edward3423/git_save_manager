"""Redo Initialization: the most destructive button in the app, and its refusals.

Wipes what is reconstructible (PAT, config, Ledger, the vault clone). Never touches the two
things that are not: `backups/` - the only copy of Live progress overwritten before it was
ever Synced - and any Live Save (Invariant 1). Refuses outright while the Vault holds
commits the Cloud does not: deleting the clone would destroy them permanently, the only
place in the design where committed content can vanish.
"""

import pytest

from core import ledger, redo, startup, vault
from core.config import Config, MachineDescription
from core.ledger import Ledger
from core.paths import Paths

MACHINE = "9c8b7a65-0000-4000-8000-00000000000a"


class FakeStore:
    def __init__(self) -> None:
        self.token: str | None = "ghp_stored"
        self.deleted = False

    def get_pat(self) -> str | None:
        return self.token

    def set_pat(self, token: str) -> None:
        self.token = token

    def delete_pat(self) -> None:
        self.token = None
        self.deleted = True


@pytest.fixture
def machine(tmp_path):
    """A set-up Machine with a pushed Vault, a Ledger, a backup, and a Live Save."""
    import subprocess

    paths = Paths(root=tmp_path / "m")
    config = Config(machine_id=MACHINE, repo="owner/vault", default_branch="main")
    description = MachineDescription(hostname="laptop", os_name="X")

    bare = tmp_path / "cloud.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(bare)], check=True)

    vault.initialize(paths, config, description, branch="main")
    repo = vault.git(paths)
    repo.run("add", "-A")
    repo.run("commit", "-q", "-m", "init", config=("user.name=t", "user.email=t@x"))
    repo.run("remote", "add", "origin", str(bare))
    repo.run("push", "-q", "-u", "origin", "main")

    from core import config as config_module

    config_module.save(paths, config)
    ledger.save(paths, Ledger())
    paths.backups_dir.mkdir(parents=True)
    (paths.backups_dir / "precious.zip").write_text("the only copy", encoding="utf-8")

    live = paths.root / "live" / "Elden Ring"
    live.mkdir(parents=True)
    (live / "slot1.sav").write_text("progress", encoding="utf-8")

    return paths, config, live


def test_the_plan_enumerates_what_will_go_and_only_what_exists(machine):
    paths, config, live = machine

    plan = redo.plan(paths)

    assert paths.config_file in plan.deletions
    assert paths.ledger_file in plan.deletions
    assert paths.vault_dir in plan.deletions
    assert paths.backups_dir not in plan.deletions  # never the safety net
    assert live not in plan.deletions  # never a Live Save
    assert plan.keyring_entries == (redo.PAT_ENTRY,)
    assert paths.journal_file not in plan.deletions  # it does not exist, so it is not listed


def test_execute_wipes_the_reconstructible_and_nothing_else(machine):
    paths, config, live = machine
    store = FakeStore()

    redo.execute(paths, store)

    assert not paths.config_file.exists()
    assert not paths.ledger_file.exists()
    assert not paths.vault_dir.exists()
    assert store.deleted  # the PAT left the keyring
    assert (paths.backups_dir / "precious.zip").exists()  # the safety net survives
    assert (live / "slot1.sav").exists()  # Invariant 1

    # And the next launch is simply a first launch: fresh identity, first-run setup.
    app = startup.start(paths)
    app.shutdown()
    assert app.config.machine_id != MACHINE
    assert app.config.is_set_up is False


def test_redo_refuses_while_the_vault_is_ahead_of_the_cloud(machine):
    """Unpushed commits exist on this Machine and nowhere else. Not a warning - a refusal;
    discarding them is a separate, explicitly chosen act."""
    paths, config, live = machine
    repo = vault.git(paths)
    (paths.vault_dir / "vault.json").write_text(
        '{"vault": true, "schema": 1, "note": "unpushed"}', encoding="utf-8"
    )
    repo.run("add", "-A")
    repo.run("commit", "-q", "-m", "unpushed", config=("user.name=t", "user.email=t@x"))
    store = FakeStore()

    with pytest.raises(redo.VaultAhead) as caught:
        redo.execute(paths, store)

    assert "1" in str(caught.value)  # it says how many commits would be destroyed
    assert paths.vault_dir.exists()  # nothing was touched
    assert store.token is not None

    redo.execute(paths, store, discard_unpushed=True)  # the separate, chosen act

    assert not paths.vault_dir.exists()


def test_a_half_set_up_machine_can_always_redo(tmp_path):
    """No Vault yet, or a clone with no upstream: there is nothing unpushed to protect, and
    a wedged setup must always be escapable."""
    paths = Paths(root=tmp_path)
    paths.data_dir.mkdir(parents=True)
    (paths.data_dir / "config.json").write_text("{}", encoding="utf-8")
    store = FakeStore()

    redo.execute(paths, store)

    assert not paths.config_file.exists()
