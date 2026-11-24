"""The command line, driven the way a person or an agent drives it: argv in; stdout, stderr
and an exit code out.

Real Git against a bare repository standing in for the Cloud Vault, and two Machines, as in
`test_cloud.py`. Each `gsm` call starts the application from disk exactly as the installed
command does - lock, recovery, Invariant 2 - and shuts it down again.

What matters most here is that the command line keeps the window's promises: a destructive
command never acts on silence, an `--approve` token refuses a world that moved after its
preview, every command re-checks the state before acting, and a Pull through the command line
moves no Baseline either.
"""

from __future__ import annotations

import io
import json
import os
import runpy
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from cli.main import main
from cli.support import is_terminal
from core import config as config_module
from core import entries, ledger, lock, vault
from core.config import Config, MachineDescription
from core.credentials import Credential
from core.jsonstore import write_json
from core.paths import Paths

MACHINE_A = "9c8b7a65-0000-4000-8000-00000000000a"
MACHINE_B = "9c8b7a65-0000-4000-8000-00000000000b"
PROJECT_ROOT = Path(__file__).resolve().parents[2]


class MemoryStore:
    """The keyring, in memory. Signed in from the start: a token that never expires, so no
    renewal ever reaches for the network."""

    def __init__(self, token: str | None = "unused-for-file-remotes") -> None:
        self.credential = Credential(access_token=token) if token else None

    def load(self) -> Credential | None:
        return self.credential

    def save(self, credential: Credential) -> None:
        self.credential = credential

    def delete(self) -> None:
        self.credential = None


class Tty(io.StringIO):
    """Typed answers, from a terminal: the commands may ask."""

    def isatty(self) -> bool:
        return True


@dataclass
class Ran:
    code: int
    out: str
    err: str

    @property
    def json(self) -> dict:
        return json.loads(self.out)


@dataclass
class Machine:
    paths: Paths
    description: MachineDescription
    store: MemoryStore = field(default_factory=MemoryStore)

    def gsm(self, *argv: str, stdin: io.StringIO | None = None) -> Ran:
        out, err = io.StringIO(), io.StringIO()
        code = main(
            list(argv),
            paths=self.paths,
            store=self.store,
            description=self.description,
            stdin=stdin or io.StringIO(""),
            stdout=out,
            stderr=err,
        )
        return Ran(code, out.getvalue(), err.getvalue())

    def live(self, name: str) -> Path:
        return self.paths.root / "live" / name

    def play(self, name: str, progress: str) -> Path:
        """The game writes its save."""
        folder = self.live(name)
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "slot1.sav").write_text(progress, encoding="utf-8")
        return folder

    def save_text(self, name: str) -> str:
        return (self.live(name) / "slot1.sav").read_text(encoding="utf-8")

    def state(self, name: str) -> str:
        found = self.gsm("--json", "status", name)
        assert found.code == 0, found.err
        return found.json["entries"][0]["state"]

    def baseline(self, entry_id: str) -> str | None:
        return ledger.load(self.paths).require(entry_id).baseline

    def head(self) -> str:
        return vault.git(self.paths).run("rev-parse", "HEAD").strip()


def _machine(root: Path, machine_id: str, hostname: str) -> Machine:
    paths = Paths(root=root)
    paths.data_dir.mkdir(parents=True)
    config_module.save(
        paths, Config(machine_id=machine_id, repo="owner/vault", default_branch="main")
    )
    return Machine(paths=paths, description=MachineDescription(hostname=hostname, os_name="Test"))


@pytest.fixture
def cloud_vault(tmp_path):
    bare = tmp_path / "cloud.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(bare)], check=True)
    subprocess.run(["git", "-C", str(bare), "config", "uploadpack.allowFilter", "true"], check=True)
    return bare


@pytest.fixture
def laptop(tmp_path, cloud_vault):
    """The first Machine: set up, with a Vault it created and published."""
    machine = _machine(tmp_path / "laptop", MACHINE_A, "laptop")
    config = config_module.load(machine.paths)
    vault.initialize(machine.paths, config, machine.description, branch="main")
    repo = vault.git(machine.paths)
    repo.run("add", "-A")
    repo.run("commit", "-q", "-m", "init vault", config=("user.name=t", "user.email=t@gsm.local"))
    repo.run("remote", "add", "origin", str(cloud_vault))
    repo.run("push", "-q", "-u", "origin", "main")
    return machine


@pytest.fixture
def desktop(tmp_path, cloud_vault, laptop, monkeypatch):
    """The second Machine: a clone of whatever the laptop has published so far - so a test
    that wants an Entry already there names its fixture before this one."""
    machine = _machine(tmp_path / "desktop", MACHINE_B, "desktop")
    monkeypatch.setattr(vault, "remote_url", lambda _repo: str(cloud_vault))
    vault.clone(machine.paths, "owner/vault", pat="unused", entry_ids=())
    return machine


@pytest.fixture
def hades(laptop):
    """An Entry on the laptop, Synced and pushed: In Sync everywhere."""
    laptop.play("Hades", "act 1")
    assert laptop.gsm("add", "Hades", str(laptop.live("Hades"))).code == 0
    assert laptop.gsm("sync", "Hades").code == 0
    assert laptop.gsm("push").code == 0
    return entries.list_all(laptop.paths)[0]


# --- looking -----------------------------------------------------------------------------------


def test_status_lists_each_entry_with_its_state_and_next_step(laptop, hades):
    laptop.play("Hades", "act 2")
    laptop.play("Celeste", "chapter 1")
    laptop.gsm("add", "Celeste", str(laptop.live("Celeste")))

    found = laptop.gsm("status")

    assert found.code == 0
    lines = found.out.splitlines()
    assert lines[0].startswith("Cloud Vault owner/vault (main): ")
    assert any(line.split() == ["Celeste", "Local", "Ahead", "sync"] for line in lines)
    assert any(line.split() == ["Hades", "Local", "Ahead", "sync"] for line in lines)


def test_json_status_is_one_object_on_stdout_and_nothing_else(laptop, hades):
    found = laptop.gsm("status", "--json")

    assert found.code == 0
    body = found.json  # would raise if anything else were mixed into stdout
    assert body["ok"] is True and body["command"] == "status"
    assert body["cloud"]["state"] == "up_to_date" and body["cloud"]["fresh"] is False
    (entry,) = body["entries"]
    assert entry == {
        "id": hades.entry_id,
        "name": "Hades",
        "state": "in_sync",
        "caption": "In Sync",
        "bound": True,
        "file_set": False,
        "actions": [],
        "recommended": None,
    }


def test_a_command_finishes_a_crashed_write_first_and_warns_on_stderr_only(laptop, hades):
    """Startup recovery runs for a command exactly as for the window - and what it has to
    say goes to stderr, so `--json` stdout is still one object a machine can parse."""
    live = laptop.live("Hades")
    staged = live.parent / f".{live.name}.gsm-new"
    staged.mkdir()
    (staged / "slot1.sav").write_text("the restore that was interrupted", encoding="utf-8")
    write_json(
        laptop.paths.journal_file,
        {
            "schema": 2,
            "entry_id": hades.entry_id,
            "swaps": [
                {
                    "live_path": str(live),
                    "target_path": str(live),
                    "staged_path": str(staged),
                    "old_path": str(live.parent / f".{live.name}.gsm-old"),
                }
            ],
            "reason": "restore",
            "stage": "staged",
            "started_at": "2026-11-23T00:00:00+00:00",
            "backup_path": None,
        },
    )
    live.rename(live.parent / f".{live.name}.gsm-old")  # the crash fell between the renames

    found = laptop.gsm("--json", "status")

    assert found.code == 0
    assert found.json["ok"] is True
    assert "warning: Recovered an interrupted write" in found.err
    assert laptop.save_text("Hades") == "the restore that was interrupted"


def test_status_reports_unpushed_commits_without_the_network(laptop, hades):
    laptop.play("Hades", "act 2")
    laptop.gsm("sync", "Hades")

    body = laptop.gsm("--json", "status").json

    assert body["cloud"]["state"] == "ahead" and body["cloud"]["ahead"] == 1


def test_an_entry_is_named_by_name_in_any_case_by_id_or_by_id_prefix(laptop, hades):
    for token in ("hades", hades.entry_id, hades.entry_id[:8]):
        found = laptop.gsm("--json", "status", token)
        assert found.code == 0, found.err
        assert [e["id"] for e in found.json["entries"]] == [hades.entry_id]


def test_two_entries_sharing_a_name_are_refused_as_ambiguous_not_guessed(laptop, hades):
    laptop.play("Other", "x")
    laptop.gsm("add", "Hades", str(laptop.live("Other")))

    found = laptop.gsm("--json", "sync", "Hades")

    assert found.code == 1
    assert found.json["error"]["kind"] == "ambiguous_entry"
    assert hades.entry_id in found.json["error"]["message"]


def test_an_unknown_entry_is_refused_by_name(laptop, hades):
    found = laptop.gsm("sync", "Celeste")

    assert found.code == 1
    assert "No Entry is named Celeste" in found.err


def test_a_machine_not_yet_set_up_is_told_how_to_set_up(tmp_path):
    machine = Machine(
        paths=Paths(root=tmp_path), description=MachineDescription(hostname="x", os_name="Test")
    )

    found = machine.gsm("--json", "status")

    assert found.code == 1
    assert found.json["error"]["kind"] == "not_set_up"
    assert "gsm setup" in found.json["error"]["message"]


def test_history_marks_the_current_and_unpushed_commits_and_carries_notes(laptop, hades):
    laptop.play("Hades", "boss slain")
    laptop.gsm("sync", "Hades", "-m", "beat the final boss")

    commits = laptop.gsm("--json", "history", "Hades").json["commits"]

    assert commits[0]["current"] is True and commits[0]["unpushed"] is True
    assert commits[0]["note"] == "beat the final boss"
    assert commits[1]["unpushed"] is False


# --- Sync ------------------------------------------------------------------------------------


def test_add_then_sync_captures_the_save_and_records_a_baseline(laptop):
    laptop.play("Hades", "act 1")

    added = laptop.gsm("add", "Hades", str(laptop.live("Hades")))
    assert added.code == 0
    assert "Nothing has been copied into the Vault yet" in added.out
    assert laptop.state("Hades") == "local_ahead"

    synced = laptop.gsm("--json", "sync", "Hades")

    assert synced.code == 0
    assert synced.json["entries"][0]["result"] == "synced"
    assert laptop.state("Hades") == "in_sync"
    entry_id = synced.json["entries"][0]["id"]
    assert laptop.baseline(entry_id) is not None


def test_a_relative_path_is_bound_as_an_absolute_one(laptop, monkeypatch):
    laptop.play("Hades", "act 1")
    monkeypatch.chdir(laptop.live("Hades").parent)

    added = laptop.gsm("--json", "add", "Hades", "Hades")

    assert added.json["live"] == [str(laptop.live("Hades"))]
    binding = ledger.load(laptop.paths).require(added.json["id"])
    assert binding.live == laptop.live("Hades")


def test_sync_refuses_a_conflict_and_points_at_resolve(laptop, hades, desktop):
    desktop.gsm("bind", "Hades", str(desktop.live("Hades")))
    desktop.gsm("restore", "Hades", "--yes")
    desktop.play("Hades", "desktop progress")
    desktop.gsm("sync", "Hades")
    desktop.gsm("push")
    laptop.play("Hades", "laptop progress")
    laptop.gsm("pull")
    before = laptop.head()

    found = laptop.gsm("sync", "Hades")

    assert found.code == 1
    assert "Conflict" in found.err and "gsm resolve Hades --keep live" in found.err
    assert laptop.head() == before  # nothing was committed


def test_sync_all_takes_only_the_local_ahead_entries(laptop, hades):
    laptop.play("Celeste", "chapter 1")
    laptop.gsm("add", "Celeste", str(laptop.live("Celeste")))
    laptop.play("Hades", "act 2")

    found = laptop.gsm("--json", "sync", "--all", "-m", "evening session")

    assert found.code == 0
    assert sorted(e["name"] for e in found.json["entries"]) == ["Celeste", "Hades"]
    assert laptop.state("Celeste") == "in_sync" and laptop.state("Hades") == "in_sync"
    assert laptop.gsm("--json", "sync", "--all").json["entries"] == []


def test_sync_needs_entries_or_all(laptop, hades):
    assert laptop.gsm("sync").code == 1
    assert laptop.gsm("sync", "--all", "Hades").code == 1


# --- Restore: the preview, and nothing on silence ----------------------------------------------


@pytest.fixture
def vault_ahead(laptop, hades, desktop):
    """The desktop holds an old save; the laptop's newer one has arrived by Pull."""
    desktop.play("Hades", "act 1")
    assert desktop.gsm("bind", "Hades", str(desktop.live("Hades"))).code == 0
    laptop.play("Hades", "act 2")
    laptop.gsm("sync", "Hades")
    laptop.gsm("push")
    assert desktop.gsm("pull").code == 0
    assert desktop.state("Hades") == "vault_ahead"
    return desktop


def test_a_restore_without_confirmation_does_nothing_and_exits_3(vault_ahead):
    found = vault_ahead.gsm("restore", "Hades")

    assert found.code == 3
    assert "This will write to" in found.out  # the preview is still shown
    assert "--yes" in found.err
    assert vault_ahead.save_text("Hades") == "act 1"


def test_a_dry_run_shows_the_preview_and_its_approval_and_writes_nothing(vault_ahead):
    found = vault_ahead.gsm("--json", "restore", "Hades", "--dry-run")

    assert found.code == 0
    body = found.json
    assert body["dry_run"] is True
    assert len(body["approval"]) == 12
    (write,) = body["writes"]
    assert write["backup_first"] is True
    assert {"path": "slot1.sav", "change": "replace"}.items() <= write["writes"][0].items()
    assert vault_ahead.save_text("Hades") == "act 1"


def test_a_save_written_after_the_preview_is_never_overwritten(vault_ahead):
    """The game saved between the preview and the approval. Both sides have now moved, so the
    state re-checked at the last moment is Conflict, and Restore is refused outright."""
    approval = vault_ahead.gsm("--json", "restore", "Hades", "--dry-run").json["approval"]
    vault_ahead.play("Hades", "act 1, plus an hour nobody previewed")

    found = vault_ahead.gsm("--json", "restore", "Hades", "--approve", approval)

    assert found.code == 1
    assert found.json["error"]["kind"] == "not_offered"
    assert vault_ahead.save_text("Hades") == "act 1, plus an hour nobody previewed"


def test_an_approval_refuses_a_vault_that_moved_after_its_preview(laptop, vault_ahead):
    """Still Vault Ahead, but no longer the version that was approved: another Pull brought
    a newer one. What was approved is what happens, or nothing does."""
    approval = vault_ahead.gsm("--json", "restore", "Hades", "--dry-run").json["approval"]
    laptop.play("Hades", "act 3")
    laptop.gsm("sync", "Hades")
    laptop.gsm("push")
    vault_ahead.gsm("pull")
    assert vault_ahead.state("Hades") == "vault_ahead"

    found = vault_ahead.gsm("--json", "restore", "Hades", "--approve", approval)

    assert found.code == 1
    assert found.json["error"]["kind"] == "stale_preview"
    assert found.json["approval"] != approval  # the fresh preview's, to approve instead
    assert vault_ahead.save_text("Hades") == "act 1"


def test_a_matching_approval_writes_backs_up_and_records_the_baseline(vault_ahead, hades):
    approval = vault_ahead.gsm("--json", "restore", "Hades", "--dry-run").json["approval"]

    found = vault_ahead.gsm("--json", "restore", "Hades", "--approve", approval)

    assert found.code == 0, found.out
    (result,) = found.json["entries"]
    assert result["result"] == "written" and Path(result["backup"]).is_file()
    assert vault_ahead.save_text("Hades") == "act 2"
    assert vault_ahead.state("Hades") == "in_sync"


def test_a_person_at_a_terminal_is_asked_and_no_means_no(vault_ahead):
    found = vault_ahead.gsm("restore", "Hades", stdin=Tty("n\n"))

    assert found.code == 1 and "Cancelled" in found.err
    assert vault_ahead.save_text("Hades") == "act 1"

    found = vault_ahead.gsm("restore", "Hades", stdin=Tty("y\n"))

    assert found.code == 0
    assert vault_ahead.save_text("Hades") == "act 2"


def test_input_that_ends_without_an_answer_is_no_answer_not_a_cancel(vault_ahead):
    found = vault_ahead.gsm("restore", "Hades", stdin=Tty(""))

    assert found.code == 3 and "needs confirmation" in found.err
    assert vault_ahead.save_text("Hades") == "act 1"


def test_the_null_device_is_not_a_person_at_a_terminal():
    """On Windows, `isatty` says True for NUL - which is what schedulers, scripts and agents
    attach as stdin. Taking it for a terminal would ask a question nobody can answer."""
    with open(os.devnull, encoding="utf-8") as null:
        assert is_terminal(null) is False


def test_restore_refuses_an_entry_whose_state_does_not_offer_it(laptop, hades):
    laptop.play("Hades", "act 2")  # Local Ahead: restoring would discard this

    found = laptop.gsm("restore", "Hades", "--yes")

    assert found.code == 1 and "does not offer restore" in found.err
    assert laptop.save_text("Hades") == "act 2"


def test_restore_all_takes_every_vault_ahead_entry_behind_one_preview(vault_ahead):
    found = vault_ahead.gsm("--json", "restore", "--all", "--yes")

    assert found.code == 0
    assert [e["name"] for e in found.json["entries"]] == ["Hades"]
    assert vault_ahead.save_text("Hades") == "act 2"


def test_a_backup_is_restored_through_the_same_preview_and_leaves_the_baseline(vault_ahead, hades):
    vault_ahead.gsm("restore", "Hades", "--yes")  # archives "act 1"
    listed = vault_ahead.gsm("--json", "backups", "Hades").json["backups"]
    assert listed[0]["number"] == 1
    baseline = vault_ahead.baseline(hades.entry_id)

    assert vault_ahead.gsm("restore", "Hades", "--backup", "1").code == 3
    found = vault_ahead.gsm("restore", "Hades", "--backup", "1", "--yes")

    assert found.code == 0, found.err
    assert vault_ahead.save_text("Hades") == "act 1"
    assert vault_ahead.baseline(hades.entry_id) == baseline
    assert vault_ahead.state("Hades") == "local_ahead"


# --- Conflicts --------------------------------------------------------------------------------


@pytest.fixture
def conflicted(laptop, hades, desktop):
    """Both Machines moved since the laptop's Baseline: a Sync Conflict on the laptop."""
    desktop.gsm("bind", "Hades", str(desktop.live("Hades")))
    desktop.gsm("restore", "Hades", "--yes")
    desktop.play("Hades", "desktop progress")
    desktop.gsm("sync", "Hades")
    desktop.gsm("push")
    laptop.play("Hades", "laptop progress")
    laptop.gsm("pull")
    assert laptop.state("Hades") == "conflict"
    return laptop


def test_resolving_toward_live_keeps_this_save_and_syncs_it(conflicted):
    found = conflicted.gsm("resolve", "Hades", "--keep", "live")

    assert found.code == 0
    assert conflicted.state("Hades") == "in_sync"
    assert conflicted.save_text("Hades") == "laptop progress"


def test_resolving_toward_the_vault_is_previewed_and_needs_confirmation(conflicted):
    assert conflicted.gsm("resolve", "Hades", "--keep", "vault").code == 3
    assert conflicted.save_text("Hades") == "laptop progress"

    found = conflicted.gsm("--json", "resolve", "Hades", "--keep", "vault", "--yes")

    assert found.code == 0
    assert Path(found.json["backup"]).is_file()  # the losing save is kept
    assert conflicted.save_text("Hades") == "desktop progress"


# --- Pull and Push --------------------------------------------------------------------------


def test_a_pull_through_the_command_line_never_moves_the_baseline(laptop, hades, desktop):
    desktop.play("Hades", "act 1")
    desktop.gsm("bind", "Hades", str(desktop.live("Hades")))
    desktop.gsm("restore", "Hades", "--yes")
    baseline = desktop.baseline(hades.entry_id)
    laptop.play("Hades", "act 2")
    laptop.gsm("sync", "Hades")
    laptop.gsm("push")

    found = desktop.gsm("pull")

    assert found.code == 0
    assert "Pulled 1 commit." in found.out and "ready to Restore: Hades" in found.out
    assert desktop.baseline(hades.entry_id) == baseline
    assert desktop.state("Hades") == "vault_ahead"  # never In Sync over a stale save


@pytest.fixture
def merge_conflict(laptop, hades, desktop):
    """Both Machines Synced Hades from the same commit; the desktop pushed first."""
    desktop.gsm("bind", "Hades", str(desktop.live("Hades")))
    desktop.gsm("restore", "Hades", "--yes")
    desktop.play("Hades", "desktop line")
    desktop.gsm("sync", "Hades")
    desktop.gsm("push")
    laptop.play("Hades", "laptop line")
    laptop.gsm("sync", "Hades")
    return laptop


def test_a_contested_pull_without_choices_aborts_whole_and_names_them(merge_conflict):
    before = merge_conflict.head()

    found = merge_conflict.gsm("--json", "pull")

    assert found.code == 1
    assert found.json["error"]["kind"] == "merge_conflict"
    assert [c["name"] for c in found.json["conflicts"]] == ["Hades"]
    assert "--take Hades=vault|cloud" in found.json["error"]["message"]
    assert merge_conflict.head() == before
    assert not vault.merging(merge_conflict.paths) and vault.is_clean(merge_conflict.paths)


def test_a_contested_pull_with_a_side_for_each_entry_merges(merge_conflict):
    found = merge_conflict.gsm("--json", "pull", "--take", "hades=cloud")

    assert found.code == 0, found.out
    assert found.json["merged"] == {"Hades": "cloud"}
    # The Vault now holds the other Machine's line, and this Live Save is still the one its
    # Baseline recorded: Vault Ahead, and Restore (backed up) is the explicit next step.
    assert merge_conflict.state("Hades") == "vault_ahead"
    assert merge_conflict.gsm("push").code == 0


def test_push_publishes_and_then_has_nothing_left_to_push(laptop, hades):
    laptop.play("Hades", "act 2")
    laptop.gsm("sync", "Hades")

    assert laptop.gsm("push").code == 0
    found = laptop.gsm("push")

    assert found.code == 0 and "Nothing to push" in found.out
    assert laptop.gsm("--json", "status").json["cloud"]["state"] == "up_to_date"


def test_a_rejected_push_says_to_pull_first(merge_conflict):
    found = merge_conflict.gsm("--json", "push")

    assert found.code == 1
    assert found.json["error"]["kind"] == "push_rejected"


def test_cloud_commands_need_a_sign_in(laptop, hades):
    laptop.store.delete()

    found = laptop.gsm("--json", "push")

    assert found.code == 1 and found.json["error"]["kind"] == "not_signed_in"


# --- Entries -----------------------------------------------------------------------------------


def test_binding_on_a_second_machine_reads_vault_ahead_and_says_what_next(hades, desktop):
    assert desktop.state("Hades") == "unlinked"
    desktop.live("Hades").parent.mkdir(parents=True)

    found = desktop.gsm("bind", "Hades", str(desktop.live("Hades")))

    assert found.code == 0
    assert "It reads Vault Ahead." in found.out and "gsm restore Hades" in found.out
    assert desktop.gsm("restore", "Hades", "--yes").code == 0
    assert desktop.save_text("Hades") == "act 1"


def test_a_single_file_entry_bound_to_a_folder_is_bound_to_its_file_there(laptop, desktop):
    settings = laptop.paths.root / "cfg" / "settings.ini"
    settings.parent.mkdir(parents=True)
    settings.write_text("vsync=on", encoding="utf-8")
    laptop.gsm("add", "Settings", str(settings))
    laptop.gsm("sync", "Settings")
    laptop.gsm("push")
    desktop.gsm("pull")
    folder = desktop.paths.root / "elsewhere"
    folder.mkdir()

    found = desktop.gsm("--json", "bind", "Settings", str(folder))

    assert found.code == 0
    assert found.json["live"] == [str(folder / "settings.ini")]


def test_binding_an_entry_already_bound_is_refused(laptop, hades):
    found = laptop.gsm("bind", "Hades", str(laptop.paths.root / "elsewhere"))

    assert found.code == 1 and "already bound" in found.err


def test_remove_needs_confirmation_and_never_touches_the_live_save(laptop, hades):
    assert laptop.gsm("remove", "Hades").code == 3
    assert laptop.gsm("--json", "status").json["entries"]

    assert laptop.gsm("remove", "Hades", "--yes").code == 0

    assert laptop.gsm("--json", "status").json["entries"] == []
    assert laptop.save_text("Hades") == "act 1"


def test_unbind_and_rename_move_no_data(laptop, hades):
    assert laptop.gsm("rename", "Hades", "Hades II").code == 0
    assert laptop.gsm("unbind", "hades ii").code == 0

    assert laptop.state("Hades II") == "unlinked"
    assert laptop.save_text("Hades") == "act 1"


def test_rollback_is_previewed_then_lands_the_entry_in_vault_ahead(laptop, hades):
    first = laptop.gsm("--json", "history", "Hades").json["commits"][0]["short"]
    laptop.play("Hades", "act 2")
    laptop.gsm("sync", "Hades")

    assert laptop.gsm("rollback", "Hades", first).code == 3
    found = laptop.gsm("rollback", "Hades", first, "--yes")

    assert found.code == 0, found.err
    assert laptop.state("Hades") == "vault_ahead"
    assert laptop.save_text("Hades") == "act 2"  # the Live Save is Restore's business


def test_a_file_set_is_added_and_bound_member_by_member(laptop, desktop):
    home = laptop.paths.root / "home"
    home.mkdir()
    (home / ".yabairc").write_text("a", encoding="utf-8")
    (home / ".skhdrc").write_text("b", encoding="utf-8")
    added = laptop.gsm("--json", "add", "Dotfiles", str(home / ".yabairc"), str(home / ".skhdrc"))
    assert added.code == 0
    laptop.gsm("sync", "Dotfiles")
    laptop.gsm("push")
    desktop.gsm("pull")
    there = desktop.paths.root / "home"
    there.mkdir()

    wrong = desktop.gsm("bind", "Dotfiles", str(there))
    found = desktop.gsm(
        "bind", "Dotfiles", f".yabairc={there / '.yabairc'}", f".skhdrc={there / '.skhdrc'}"
    )

    assert wrong.code == 1 and "MEMBER=PATH" in wrong.err
    assert found.code == 0, found.err
    assert desktop.state("Dotfiles") == "vault_ahead"


# --- setup and sign-in ------------------------------------------------------------------------


def test_setup_with_a_piped_token_creates_the_cloud_vault_and_stores_the_sign_in(
    tmp_path, monkeypatch
):
    from test_github import FakeGitHub

    hub = FakeGitHub(hosting=tmp_path / "github")
    hub.hosting.mkdir()
    monkeypatch.setattr(vault, "remote_url", lambda repo: str(hub.bare_path(repo)))
    store = MemoryStore(token=None)
    out, err = io.StringIO(), io.StringIO()
    arguments = ["--json", "setup", "edward/saves", "--token-stdin"]

    code = main(
        arguments,
        paths=Paths(root=tmp_path / "new"),
        store=store,
        description=MachineDescription(hostname="new", os_name="Test"),
        stdin=io.StringIO("ghp_piped-token\n"),
        stdout=out,
        stderr=err,
        api=hub,
    )

    assert code == 0, out.getvalue() + err.getvalue()
    body = json.loads(out.getvalue())
    assert body["outcome"] == "created" and body["branch"] == "main"
    assert store.credential == Credential(access_token="ghp_piped-token")
    assert hub.tokens_seen == {"ghp_piped-token"}
    assert "ghp_piped-token" not in out.getvalue() + err.getvalue()


def test_setup_refuses_a_machine_already_set_up(laptop):
    found = laptop.gsm("--json", "setup", "owner/other", "--token-stdin", stdin=io.StringIO("t\n"))

    assert found.code == 1 and found.json["error"]["kind"] == "already_set_up"


def test_a_new_sign_in_is_kept_only_once_it_reaches_the_cloud_vault(laptop, cloud_vault):
    found = laptop.gsm("sign-in", "--token-stdin", stdin=io.StringIO("ghp_new\n"))

    assert found.code == 0, found.err
    assert laptop.store.credential == Credential(access_token="ghp_new")

    vault.git(laptop.paths).run("remote", "set-url", "origin", str(cloud_vault) + "-gone")
    found = laptop.gsm("sign-in", "--token-stdin", stdin=io.StringIO("ghp_wrong\n"))

    assert found.code == 1 and "previous sign-in was kept" in found.err
    assert laptop.store.credential == Credential(access_token="ghp_new")


# --- the lock, and the entry point ---------------------------------------------------------------


def test_a_command_is_refused_while_another_instance_holds_the_lock(laptop, hades):
    held = lock.acquire(laptop.paths)
    try:
        found = laptop.gsm("--json", "sync", "Hades")
    finally:
        held.release()

    assert found.code == 1
    assert found.json["error"]["kind"] == "already_running"
    assert laptop.gsm("status").code == 0  # and released after every command


def test_a_bad_command_line_exits_2(laptop):
    assert laptop.gsm("frobnicate").code == 2
    assert laptop.gsm("resolve", "Hades").code == 2  # --keep is required


def test_a_command_never_loads_qt():
    """Commands run on machines with no display, and start fast: the window's toolkit stays
    out of the process entirely."""
    code = (
        "import runpy, sys\n"
        "sys.argv = ['main.py', 'status', '--help']\n"
        "try:\n"
        f"    runpy.run_path({str(PROJECT_ROOT / 'main.py')!r}, run_name='__main__')\n"
        "except SystemExit:\n"
        "    pass\n"
        "print('QT LOADED' if any(m.startswith('PyQt6') for m in sys.modules) else 'NO QT')\n"
    )
    found = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        cwd=PROJECT_ROOT,
        env={**os.environ, "PYTHONPATH": str(PROJECT_ROOT)},
        check=True,
    )

    assert found.stdout.strip().endswith("NO QT")
    assert "usage: gsm status" in found.stdout


def test_main_py_hands_arguments_to_the_command_line(monkeypatch):
    seen = []
    monkeypatch.setattr("cli.main.main", lambda argv: seen.append(argv) or 0)
    monkeypatch.setattr(sys, "argv", ["main.py", "status", "--json"])

    with pytest.raises(SystemExit) as exited:
        runpy.run_path(str(PROJECT_ROOT / "main.py"), run_name="__main__")

    assert exited.value.code == 0
    assert seen == [["status", "--json"]]
