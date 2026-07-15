"""What happens before the window opens: writability, the lock, recovery, Invariant 2.

An operation that crashed cannot be trusted to have cleaned up after itself - that is what
crashing means - so startup re-establishes every guarantee rather than assuming the last
run kept its promises.
"""

import pytest

from core import ledger, startup, transaction, vault
from core.config import Config, MachineDescription
from core.jsonstore import write_json
from core.lock import AlreadyRunning
from core.paths import Paths
from core.transaction import Outcome

MACHINE = "9c8b7a65-0000-4000-8000-00000000000a"


@pytest.fixture
def paths(tmp_path):
    return Paths(root=tmp_path)  # no data/ yet: the very first launch creates it


def test_a_fresh_workspace_starts_with_an_identity_and_the_lock_held(paths):
    app = startup.start(paths)

    assert app.config.machine_id  # generated once, here, and persisted
    assert app.config.is_set_up is False  # which is what triggers first-run setup
    assert app.recovered is None  # nothing was interrupted, because nothing ever ran

    with pytest.raises(AlreadyRunning):
        startup.start(paths)

    app.shutdown()
    startup.start(paths).shutdown()  # the lock came back off


def test_the_identity_survives_a_restart(paths):
    app = startup.start(paths)
    app.shutdown()

    again = startup.start(paths)
    again.shutdown()

    assert again.config.machine_id == app.config.machine_id


def test_startup_finishes_the_write_a_crash_interrupted(paths):
    """A crash between the two renames of the swap: the staged copy is complete (STAGED is
    only journaled after the fsync) and the Live Save is missing. Startup must finish the
    job - the user's save is fully new, never absent and never torn."""
    paths.data_dir.mkdir(parents=True)
    live = paths.root / "live" / "Elden Ring"
    live.parent.mkdir(parents=True)
    staged = live.parent / f".{live.name}.gsm-new"
    staged.mkdir()
    (staged / "slot1.sav").write_text("the new save, fully written", encoding="utf-8")

    write_json(
        paths.journal_file,
        {
            "schema": 1,
            "entry_id": "some-entry",
            "live_path": str(live),
            "target_path": str(live),
            "staged_path": str(staged),
            "old_path": str(live.parent / f".{live.name}.gsm-old"),
            "reason": "restore",
            "stage": "staged",
            "started_at": "2026-07-15T00:00:00+00:00",
            "backup_path": None,
        },
    )

    app = startup.start(paths)
    app.shutdown()

    assert app.recovered is not None
    assert app.recovered.outcome is Outcome.COMPLETED
    assert (live / "slot1.sav").read_text(encoding="utf-8") == "the new save, fully written"
    assert not staged.exists()
    assert not paths.journal_file.exists()


def test_startup_discards_vault_wreckage(paths):
    """Invariant 2: dirt found at startup is an interrupted operation, and is discarded."""
    config = Config(machine_id=MACHINE, repo="owner/vault", default_branch="main")
    description = MachineDescription(hostname="laptop", os_name="X")
    vault.initialize(paths, config, description, branch="main")
    repo = vault.git(paths)
    repo.run("add", "-A")
    repo.run("commit", "-q", "-m", "init", config=("user.name=t", "user.email=t@x"))
    (paths.vault_dir / "wreckage.tmp").write_text("half an operation", encoding="utf-8")

    app = startup.start(paths)
    app.shutdown()

    assert vault.is_clean(paths)


def test_a_failure_during_startup_releases_the_lock(paths, monkeypatch):
    """Half a startup must not leave the lock behind: the user's next attempt - likely
    seconds later, after fixing whatever broke - has to be able to run."""

    def boom(_paths):
        raise RuntimeError("simulated")

    monkeypatch.setattr(ledger, "load", boom)
    with pytest.raises(RuntimeError):
        startup.start(paths)
    monkeypatch.undo()

    startup.start(paths).shutdown()  # not AlreadyRunning: the failed attempt let go


def test_reset_brings_the_app_back_to_first_run_without_a_restart(paths):
    """After a Redo Initialization the running window must behave like a first launch:
    fresh identity, empty Ledger, a Cloud that is neither offline nor remembering."""
    from core import config as config_module
    from core import redo

    class NoStore:
        def get_pat(self):
            return None

        def set_pat(self, token):
            pass

        def delete_pat(self):
            pass

    app = startup.start(paths)
    was = app.config.machine_id
    app.config.repo = "owner/vault"
    config_module.save(paths, app.config)

    redo.execute(paths, NoStore())
    app.reset()
    app.shutdown()

    assert app.config.machine_id != was  # a fresh identity was generated and persisted
    assert app.config.is_set_up is False
    assert app.the_ledger.bindings == {}
    assert app.cloud.offline is None
    assert app.cloud.last_status is None


def test_startup_does_not_invent_a_vault(paths):
    """Before setup there is no Vault, and startup must not create one - `git init` here
    would make every later bootstrap path think it is joining an existing repository."""
    app = startup.start(paths)
    app.shutdown()

    assert not paths.vault_dir.exists()
    assert transaction.recover(paths) is None  # and no journal was fabricated either
