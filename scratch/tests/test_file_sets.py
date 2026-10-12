"""File Sets: several individual files, in unrelated folders, that are one Entry.

The motivating case is a window manager's configuration - `~/.yabairc` and `~/.skhdrc` - which
is meaningless half-restored. So everything a single Entry guarantees must hold for the set as
a whole: one state, one Sync, one Backup, and a Restore that leaves every file old or every
file new, even across a crash.

Two Machines, `laptop` and `desktop`, are two clones of one Cloud Vault on disk, and the
desktop keeps the files somewhere else entirely - which is the reason a File Set binds each
file separately.
"""

import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from core import backups, download, entries, ledger, operations, transaction, vault
from core.cloud import Cloud
from core.config import Config, MachineDescription
from core.entry_state import Action, EntryState
from core.hashing import file_set_hash, hash_entry
from core.jsonstore import read_json
from core.ledger import Ledger
from core.paths import Paths
from core.transaction import Outcome, Stage
from ui import presenter

MACHINE_A = "9c8b7a65-0000-4000-8000-00000000000a"
MACHINE_B = "9c8b7a65-0000-4000-8000-00000000000b"
PAT = "unused-for-file-paths"
IDENTITY = ("user.name=t", "user.email=t@gsm.local")


@dataclass
class Machine:
    paths: Paths
    config: Config
    description: MachineDescription
    home: Path
    the_ledger: Ledger = field(default_factory=Ledger)

    @property
    def cloud(self) -> Cloud:
        return Cloud(paths=self.paths, config=self.config)

    def write(self, relative: str, text: str) -> Path:
        path = self.home / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def add(self, name: str, files: list[Path]):
        return operations.add_file_set(
            self.paths, self.config, self.description, self.the_ledger, name, files
        )

    def sync(self, entry_id: str):
        return operations.sync_to_vault(
            self.paths, self.config, self.description, self.the_ledger, entry_id
        )

    def restore(self, entry_id: str):
        return operations.restore_to_live(self.paths, self.config, self.the_ledger, entry_id)

    def bind(self, entry_id: str, files: dict[str, Path]) -> None:
        operations.bind_entry(
            self.paths, self.config, self.description, self.the_ledger, entry_id, files, pat=PAT
        )

    def state(self, entry_id: str):
        return ledger.refresh(self.paths, self.the_ledger, entry_id)

    def stored(self, entry_id: str) -> list[str]:
        return sorted(p.name for p in self.paths.entry_content_dir(entry_id).iterdir())


@pytest.fixture
def cloud_vault(tmp_path):
    bare = tmp_path / "cloud.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(bare)], check=True)
    subprocess.run(["git", "-C", str(bare), "config", "uploadpack.allowFilter", "true"], check=True)
    return bare


@pytest.fixture
def laptop(tmp_path, cloud_vault):
    machine = Machine(
        paths=Paths(root=tmp_path / "laptop"),
        config=Config(
            machine_id=MACHINE_A, repo="owner/vault", default_branch="main", backup_retention=5
        ),
        description=MachineDescription(hostname="laptop", os_name="Darwin"),
        home=tmp_path / "laptop-home",
    )
    vault.initialize(machine.paths, machine.config, machine.description, branch="main")
    repo = vault.git(machine.paths)
    repo.run("add", "-A")
    repo.run("commit", "-q", "-m", "init vault", config=IDENTITY)
    repo.run("remote", "add", "origin", str(cloud_vault))
    repo.run("push", "-q", "origin", "main")
    return machine


@pytest.fixture
def desktop(tmp_path, cloud_vault, laptop, monkeypatch):
    machine = Machine(
        paths=Paths(root=tmp_path / "desktop"),
        config=Config(machine_id=MACHINE_B, repo="owner/vault", default_branch="main"),
        description=MachineDescription(hostname="desktop", os_name="Darwin"),
        home=tmp_path / "desktop-home",
    )
    monkeypatch.setattr(vault, "remote_url", lambda _repo: str(cloud_vault))
    vault.clone(machine.paths, machine.config.repo, pat=PAT, entry_ids=())
    return machine


@pytest.fixture
def yabai(laptop):
    """The laptop's window manager configuration: two dotfiles, added as one Entry."""
    files = [
        laptop.write(".yabairc", "yabai -m config layout bsp\n"),
        laptop.write(".skhdrc", "alt - h : yabai -m window --focus west\n"),
    ]
    return laptop.add("yabai", files)


# --- hashing -----------------------------------------------------------------------------------


def test_a_file_set_of_one_hashes_exactly_like_a_single_file_entry(tmp_path):
    """Which is what lets a single-file Entry become a File Set without its Baseline lying."""
    path = tmp_path / "settings.ini"
    path.write_text("vsync=1", encoding="utf-8")

    assert file_set_hash({"settings.ini": path}) == hash_entry(path)


def test_a_member_is_hashed_under_its_member_name_not_its_basename_here(tmp_path):
    """`~/.config/skhd/skhdrc` here and `~/.skhdrc` there are the same member."""
    here = tmp_path / "a" / "skhdrc"
    there = tmp_path / "b" / ".skhdrc"
    for path in (here, there):
        path.parent.mkdir(parents=True)
        path.write_text("same", encoding="utf-8")

    assert file_set_hash({".skhdrc": here}) == file_set_hash({".skhdrc": there})


def test_a_missing_member_makes_a_complete_hash_absent_but_not_a_partial_one(tmp_path):
    present = tmp_path / "a"
    present.write_text("x", encoding="utf-8")
    members = {"a": present, "b": tmp_path / "missing"}

    assert file_set_hash(members, complete=True) is None
    assert file_set_hash(members) == file_set_hash({"a": present})


# --- adding ------------------------------------------------------------------------------------


def test_adding_a_file_set_binds_each_file_and_publishes_them(laptop, yabai):
    entry = entries.require(laptop.paths, yabai.entry_id)
    assert entry.files == [".yabairc", ".skhdrc"]
    assert entry.content_name is None

    binding = laptop.the_ledger.require(yabai.entry_id)
    assert binding.files == {
        ".yabairc": str(laptop.home / ".yabairc"),
        ".skhdrc": str(laptop.home / ".skhdrc"),
    }
    published = read_json(laptop.paths.machine_file(MACHINE_A))["bindings"]
    assert published[yabai.entry_id] == binding.files
    assert vault.is_clean(laptop.paths)
    assert laptop.state(yabai.entry_id).state is EntryState.LOCAL_AHEAD


def test_a_folder_cannot_be_one_of_the_files(laptop, tmp_path):
    folder = laptop.home / "yabai"
    folder.mkdir(parents=True)
    with pytest.raises(operations.InvalidFileSet, match="is a folder"):
        laptop.add("yabai", [laptop.write(".yabairc", "x"), folder])


def test_two_files_with_one_name_are_refused(laptop):
    """They would be one file in the Vault - and on macOS, even differing only in case."""
    with pytest.raises(operations.InvalidFileSet, match="both be stored as"):
        laptop.add("cfg", [laptop.write("a/config", "1"), laptop.write("b/Config", "2")])


def test_the_same_file_twice_is_refused(laptop):
    real = laptop.write("dotfiles/yabairc", "x")
    link = laptop.home / ".yabairc"
    link.symlink_to(real)
    with pytest.raises(operations.InvalidFileSet, match="same file"):
        laptop.add("yabai", [real, link])


# --- Sync and Restore --------------------------------------------------------------------------


def test_a_sync_stores_every_file_flat_in_one_commit(laptop, yabai):
    commits_before = len(operations.history(laptop.paths, yabai.entry_id))

    laptop.sync(yabai.entry_id)

    assert laptop.stored(yabai.entry_id) == [".skhdrc", ".yabairc"]
    assert len(operations.history(laptop.paths, yabai.entry_id)) == commits_before + 1
    assert laptop.state(yabai.entry_id).state is EntryState.IN_SYNC


def test_changing_one_file_makes_the_whole_entry_local_ahead(laptop, yabai):
    laptop.sync(yabai.entry_id)
    laptop.write(".skhdrc", "alt - l : yabai -m window --focus east\n")

    assert laptop.state(yabai.entry_id).state is EntryState.LOCAL_AHEAD


def test_a_missing_file_is_a_missing_save_and_is_never_synced_away(laptop, yabai):
    """ADR-0006 for a set: one file gone must not read as a smaller set, or a Sync would erase
    that file from the Vault."""
    laptop.sync(yabai.entry_id)
    (laptop.home / ".skhdrc").unlink()

    status = laptop.state(yabai.entry_id)
    assert status.state is EntryState.LIVE_SAVE_MISSING
    assert Action.SYNC_TO_VAULT not in status.offered
    with pytest.raises(operations.NothingToSync, match=".skhdrc"):
        laptop.sync(yabai.entry_id)
    assert laptop.stored(yabai.entry_id) == [".skhdrc", ".yabairc"]


def test_restoring_brings_back_the_missing_file_and_backs_up_the_other(laptop, yabai):
    laptop.sync(yabai.entry_id)
    (laptop.home / ".skhdrc").unlink()
    laptop.write(".yabairc", "an edit since\n")

    preview = operations.preview_restore(laptop.paths, laptop.the_ledger, yabai.entry_id)
    written = operations.restore_to_live(
        laptop.paths, laptop.config, laptop.the_ledger, yabai.entry_id, approved=preview
    )

    assert (laptop.home / ".skhdrc").read_text() == "alt - h : yabai -m window --focus west\n"
    assert (laptop.home / ".yabairc").read_text() == "yabai -m config layout bsp\n"
    assert backups.contents(written.backup.path) == {".yabairc": len("an edit since\n")}
    assert laptop.state(yabai.entry_id).state is EntryState.IN_SYNC


def test_the_preview_names_each_file_by_its_full_path(laptop, yabai):
    laptop.sync(yabai.entry_id)
    laptop.write(".skhdrc", "changed\n")

    preview = operations.preview_restore(laptop.paths, laptop.the_ledger, yabai.entry_id)
    lines = presenter.preview_lines(preview)

    assert lines[0] == "This will write:"
    assert any(str(laptop.home / ".skhdrc") in line and "overwrite" in line for line in lines)
    assert any(str(laptop.home / ".yabairc") in line for line in lines)
    assert lines[-1] == "The current Live Save is archived to a Backup first."


def test_a_backup_of_a_file_set_restores_each_file_to_its_own_place(laptop, yabai):
    laptop.sync(yabai.entry_id)
    laptop.write(".skhdrc", "precious local edit\n")
    laptop.write(".yabairc", "another local edit\n")
    taken = laptop.restore(yabai.entry_id).backup

    operations.restore_backup(laptop.paths, laptop.config, laptop.the_ledger, yabai.entry_id, taken)

    assert (laptop.home / ".skhdrc").read_text() == "precious local edit\n"
    assert (laptop.home / ".yabairc").read_text() == "another local edit\n"
    # From a zip, not the Vault: no Baseline moves, so the edits read as Local Ahead.
    assert laptop.state(yabai.entry_id).state is EntryState.LOCAL_AHEAD


@pytest.mark.skipif(os.name == "nt", reason="symlinks need privileges on Windows")
def test_a_symlinked_dotfile_is_synced_as_its_contents_and_restored_through_the_link(laptop):
    """Dotfiles are routinely links into a dotfiles repository. The Vault stores the file the
    link leads to, and a Restore writes through the link, leaving it a link."""
    real = laptop.write("dotfiles/yabairc", "original\n")
    link = laptop.home / ".yabairc"
    link.symlink_to(real)
    entry = laptop.add("yabai", [link, laptop.write(".skhdrc", "keys\n")])
    laptop.sync(entry.entry_id)
    assert (laptop.paths.entry_content_dir(entry.entry_id) / ".yabairc").read_text() == "original\n"

    real.write_text("edited\n", encoding="utf-8")
    laptop.restore(entry.entry_id)

    assert link.is_symlink()
    assert real.read_text() == "original\n"


# --- a second Machine ----------------------------------------------------------------------------


def test_a_second_machine_binds_each_file_somewhere_else_and_restores_them(laptop, desktop, yabai):
    laptop.sync(yabai.entry_id)
    laptop.cloud.push(pat=PAT)
    desktop.cloud.pull(PAT, desktop.description)

    hints = presenter.bind_hints(desktop.paths, desktop.config, yabai.entry_id)
    assert [h.files for h in hints] == [laptop.the_ledger.require(yabai.entry_id).files]

    # The desktop keeps skhd's file in XDG style, under another basename.
    (desktop.home / ".config" / "skhd").mkdir(parents=True)
    desktop.bind(
        yabai.entry_id,
        {".yabairc": desktop.home / ".yabairc", ".skhdrc": desktop.home / ".config/skhd/skhdrc"},
    )
    assert desktop.state(yabai.entry_id).state is EntryState.VAULT_AHEAD

    desktop.restore(yabai.entry_id)

    assert (desktop.home / ".config/skhd/skhdrc").read_text() == (
        "alt - h : yabai -m window --focus west\n"
    )
    assert desktop.state(yabai.entry_id).state is EntryState.IN_SYNC


def test_a_file_added_on_another_machine_needs_binding_here_first(laptop, desktop, yabai):
    laptop.sync(yabai.entry_id)
    laptop.cloud.push(pat=PAT)
    desktop.cloud.pull(PAT, desktop.description)
    desktop.bind(
        yabai.entry_id,
        {".yabairc": desktop.home / ".yabairc", ".skhdrc": desktop.home / ".skhdrc"},
    )
    desktop.restore(yabai.entry_id)
    desktop.cloud.push(pat=PAT)

    laptop.cloud.pull(PAT, laptop.description)
    border = laptop.write(".config/borders/bordersrc", "width=6\n")
    operations.add_file(
        laptop.paths, laptop.config, laptop.description, laptop.the_ledger, yabai.entry_id, border
    )
    laptop.sync(yabai.entry_id)
    laptop.cloud.push(pat=PAT)
    desktop.cloud.pull(PAT, desktop.description)

    row = next(r for r in presenter.rows(desktop.paths, desktop.the_ledger))
    assert row.caption == presenter.NEEDS_BINDING
    assert row.status is None and row.bound
    with pytest.raises(operations.NeedsBinding, match="bordersrc"):
        desktop.sync(yabai.entry_id)

    (desktop.home / ".config" / "borders").mkdir(parents=True)
    desktop.bind(yabai.entry_id, {"bordersrc": desktop.home / ".config/borders/bordersrc"})

    # The Baseline was kept. The new file does not exist here yet, and the set has been synced
    # before, so this reads as a missing file: Restore is offered, never Sync.
    assert desktop.the_ledger.require(yabai.entry_id).baseline is not None
    status = desktop.state(yabai.entry_id)
    assert status.state is EntryState.LIVE_SAVE_MISSING
    assert status.offered == (Action.RESTORE_TO_LIVE, Action.UNBIND)
    desktop.restore(yabai.entry_id)
    assert (desktop.home / ".config/borders/bordersrc").read_text() == "width=6\n"


# --- changing which files are in the set ---------------------------------------------------------


def test_a_single_file_entry_becomes_a_file_set_keeping_its_history_and_baseline(laptop):
    yabairc = laptop.write(".yabairc", "layout bsp\n")
    entry = operations.add_entry(
        laptop.paths, laptop.config, laptop.description, laptop.the_ledger, "yabai", yabairc
    )
    laptop.sync(entry.entry_id)
    baseline = laptop.the_ledger.require(entry.entry_id).baseline
    synced_at = operations.history(laptop.paths, entry.entry_id)[0].sha

    operations.add_file(
        laptop.paths,
        laptop.config,
        laptop.description,
        laptop.the_ledger,
        entry.entry_id,
        laptop.write(".skhdrc", "keys\n"),
    )

    converted = entries.require(laptop.paths, entry.entry_id)
    assert converted.files == [".yabairc", ".skhdrc"]
    assert laptop.the_ledger.require(entry.entry_id).baseline == baseline
    # Only the new file is unsynced: the stored one still matches its Baseline.
    assert laptop.state(entry.entry_id).state is EntryState.LOCAL_AHEAD

    laptop.sync(entry.entry_id)
    assert laptop.state(entry.entry_id).state is EntryState.IN_SYNC
    assert synced_at in [c.sha for c in operations.history(laptop.paths, entry.entry_id)]


def test_a_folder_entry_cannot_take_files(laptop):
    folder = laptop.home / "saves"
    folder.mkdir(parents=True)
    (folder / "slot1.sav").write_text("x", encoding="utf-8")
    entry = operations.add_entry(
        laptop.paths, laptop.config, laptop.description, laptop.the_ledger, "Game", folder
    )
    laptop.sync(entry.entry_id)

    with pytest.raises(operations.InvalidFileSet, match="folder"):
        operations.add_file(
            laptop.paths,
            laptop.config,
            laptop.description,
            laptop.the_ledger,
            entry.entry_id,
            laptop.write("x.ini", "x"),
        )
    assert entries.require(laptop.paths, entry.entry_id).files is None


def test_a_name_already_in_the_set_is_refused_and_nothing_changes(laptop, yabai):
    before = laptop.the_ledger.require(yabai.entry_id).files.copy()
    with pytest.raises(operations.InvalidFileSet, match="already has a file named"):
        operations.add_file(
            laptop.paths,
            laptop.config,
            laptop.description,
            laptop.the_ledger,
            yabai.entry_id,
            laptop.write("elsewhere/.yabairc", "x"),
        )
    assert laptop.the_ledger.require(yabai.entry_id).files == before
    assert vault.is_clean(laptop.paths)


def test_a_failed_commit_leaves_the_ledger_as_it_was(laptop, yabai, monkeypatch):
    before = laptop.the_ledger.require(yabai.entry_id).files.copy()

    def broken(*_args, **_kwargs):
        raise RuntimeError("disk full")

    monkeypatch.setattr(operations, "_commit", broken)
    with pytest.raises(RuntimeError):
        operations.add_file(
            laptop.paths,
            laptop.config,
            laptop.description,
            laptop.the_ledger,
            yabai.entry_id,
            laptop.write(".config/borders/bordersrc", "x"),
        )

    assert laptop.the_ledger.require(yabai.entry_id).files == before
    assert entries.require(laptop.paths, yabai.entry_id).files == [".yabairc", ".skhdrc"]
    assert vault.is_clean(laptop.paths)


def test_removing_a_file_drops_it_from_the_vault_and_never_touches_it(laptop, yabai):
    laptop.sync(yabai.entry_id)

    operations.remove_file(
        laptop.paths,
        laptop.config,
        laptop.description,
        laptop.the_ledger,
        yabai.entry_id,
        ".skhdrc",
    )

    assert (laptop.home / ".skhdrc").read_text() == "alt - h : yabai -m window --focus west\n"
    assert laptop.stored(yabai.entry_id) == [".yabairc"]
    assert ".skhdrc" not in laptop.the_ledger.require(yabai.entry_id).files
    assert laptop.state(yabai.entry_id).state is EntryState.IN_SYNC


def test_the_last_file_cannot_be_removed(laptop, yabai):
    operations.remove_file(
        laptop.paths,
        laptop.config,
        laptop.description,
        laptop.the_ledger,
        yabai.entry_id,
        ".skhdrc",
    )
    with pytest.raises(operations.InvalidFileSet, match="Remove from Vault"):
        operations.remove_file(
            laptop.paths,
            laptop.config,
            laptop.description,
            laptop.the_ledger,
            yabai.entry_id,
            ".yabairc",
        )


def test_rolling_back_never_brings_back_a_removed_file(laptop, yabai):
    laptop.sync(yabai.entry_id)
    first = operations.history(laptop.paths, yabai.entry_id)[0].sha
    operations.remove_file(
        laptop.paths,
        laptop.config,
        laptop.description,
        laptop.the_ledger,
        yabai.entry_id,
        ".skhdrc",
    )
    laptop.write(".yabairc", "newer\n")
    laptop.sync(yabai.entry_id)

    operations.rollback(laptop.paths, laptop.config, laptop.description, yabai.entry_id, first)

    assert laptop.stored(yabai.entry_id) == [".yabairc"]
    assert vault.is_clean(laptop.paths)
    assert laptop.state(yabai.entry_id).state is EntryState.VAULT_AHEAD


# --- Download ------------------------------------------------------------------------------------


def test_a_file_set_downloads_as_a_folder_named_for_the_entry(laptop, desktop, tmp_path):
    files = [laptop.write(".yabairc", "bsp\n"), laptop.write(".skhdrc", "keys\n")]
    entry = laptop.add("yabai: tiling", files)
    laptop.sync(entry.entry_id)
    laptop.cloud.push(pat=PAT)
    desktop.cloud.pull(PAT, desktop.description)
    downloads = tmp_path / "Downloads"

    assert presenter.downloadable(desktop.paths, entry.entry_id)
    landed = download.download(desktop.paths, entry.entry_id, downloads, pat=PAT)

    assert landed == downloads / "yabai_ tiling"
    assert sorted(p.name for p in landed.iterdir()) == [".skhdrc", ".yabairc"]
    assert (landed / ".skhdrc").read_text() == "keys\n"


# --- the Ledger ----------------------------------------------------------------------------------


def test_a_file_set_binding_survives_a_save_and_load(laptop, yabai):
    loaded = ledger.load(laptop.paths)
    assert loaded.require(yabai.entry_id).files == laptop.the_ledger.require(yabai.entry_id).files
    assert loaded.require(yabai.entry_id).live_path is None


def test_a_binding_from_before_the_entry_became_a_file_set_is_matched_by_name_only(tmp_path):
    """Another Machine turned a single-file Entry into a File Set. This Machine's plain path is
    that same file - matched by its name - and never mistaken for a different member."""
    entry = entries.Entry(entry_id="e", name="yabai", files=[".yabairc", ".skhdrc"])
    plain = ledger.Binding(entry_id="e", live_path=str(tmp_path / ".yabairc"))

    assert ledger.live_members(entry, plain) == {".yabairc": tmp_path / ".yabairc"}
    assert ledger.unbound_members(entry, plain) == [".skhdrc"]

    renamed = entries.Entry(entry_id="e", name="yabai", files=[".skhdrc"])
    assert ledger.live_members(renamed, plain) == {}


# --- crashes across files ----------------------------------------------------------------------


def _arm_after_first_swap(monkeypatch):
    """Die after the first file's two renames, before the second file's swap begins."""
    real_rename = os.rename
    calls = {"n": 0}

    def rename(src, dst):
        calls["n"] += 1
        if calls["n"] == 3:
            raise RuntimeError("power cut")
        real_rename(src, dst)

    monkeypatch.setattr(transaction.os, "rename", rename)


def _arm_after_staging(monkeypatch):
    real_record = transaction._record

    def record(p, journal, stage=None):
        real_record(p, journal, stage)
        if journal.stage == Stage.STAGED:
            raise RuntimeError("power cut")

    monkeypatch.setattr(transaction, "_record", record)


@pytest.mark.parametrize(
    ("arm", "outcome"),
    [(_arm_after_first_swap, Outcome.COMPLETED), (_arm_after_staging, Outcome.ROLLED_BACK)],
)
def test_a_crash_between_files_never_leaves_a_mixture(laptop, yabai, monkeypatch, arm, outcome):
    """One file new and the other old is a configuration that never existed. Recovery finishes
    every file once any swap has begun, and abandons every file when none has."""
    laptop.sync(yabai.entry_id)
    old = {
        ".yabairc": laptop.write(".yabairc", "local yabai\n").read_text(),
        ".skhdrc": laptop.write(".skhdrc", "local skhd\n").read_text(),
    }
    new = {
        ".yabairc": "yabai -m config layout bsp\n",
        ".skhdrc": "alt - h : yabai -m window --focus west\n",
    }

    arm(monkeypatch)
    with pytest.raises(RuntimeError):
        laptop.restore(yabai.entry_id)
    monkeypatch.undo()

    recovered = transaction.recover(laptop.paths)

    assert recovered.outcome is outcome
    expected = new if outcome is Outcome.COMPLETED else old
    for name, text in expected.items():
        assert (laptop.home / name).read_text() == text
    assert sorted(p.name for p in laptop.home.iterdir()) == [".skhdrc", ".yabairc"]
    assert not laptop.paths.journal_file.exists()


def test_binding_back_to_the_paths_already_published_commits_nothing_and_succeeds(laptop, yabai):
    """The published file already says exactly this, and Git refuses an empty commit."""
    files = dict(laptop.the_ledger.require(yabai.entry_id).files)
    laptop.the_ledger.drop_member(yabai.entry_id, ".skhdrc")
    head = vault.git(laptop.paths).run("rev-parse", "HEAD")

    laptop.bind(yabai.entry_id, {".skhdrc": Path(files[".skhdrc"])})

    assert laptop.the_ledger.require(yabai.entry_id).files == files
    assert vault.git(laptop.paths).run("rev-parse", "HEAD") == head
    assert vault.is_clean(laptop.paths)
