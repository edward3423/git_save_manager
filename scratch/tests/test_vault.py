"""The Vault repository: its shape, its cleanliness, and selective sync.

Real Git, in temp directories. A second Machine is a second clone of the same origin. No
network and no GitHub: the Cloud Vault is a bare repository on disk, reached over `file://`.
"""

import subprocess
from pathlib import Path

import pytest

from core import vault
from core.config import Config, MachineDescription
from core.git import Git
from core.paths import Paths
from core.vault import NotAVault, VaultDirty, VaultTooNew

AAA = "3f2a1b7c-0000-4000-8000-00000000aaaa"
BBB = "3f2a1b7c-0000-4000-8000-00000000bbbb"

MACHINE = "9c8b7a65-0000-4000-8000-000000000001"


@pytest.fixture
def config():
    return Config(machine_id=MACHINE, repo="owner/vault", default_branch="main")


@pytest.fixture
def description():
    return MachineDescription(hostname="laptop", os_name="Darwin")


def commit_everything(work_tree: Path, message: str) -> None:
    Git(work_tree).run("add", "-A")
    Git(work_tree).run(
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@gsm.local",
        "commit",
        "-m",
        message,
        config=(),
    )


@pytest.fixture
def origin(tmp_path, config, description):
    """A Cloud Vault: a bare repo holding two Entries, one of which we will never bind."""
    seed = Paths(root=tmp_path / "seed")
    vault.initialize(seed, config, description, branch="main")

    for entry_id, name in ((AAA, "Elden Ring"), (BBB, "Hades")):
        content = seed.entry_content_dir(entry_id)
        content.mkdir(parents=True)
        (content / "slot1.sav").write_text(f"progress in {name}", encoding="utf-8")
        seed.entry_sidecar(entry_id).write_text(f'{{"name": "{name}"}}\n', encoding="utf-8")

    commit_everything(seed.vault_dir, "seed")

    bare = tmp_path / "cloud.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(bare)], check=True)
    # Partial clone is refused by a server that has not opted into it, and file:// servers have
    # not - so the local Cloud Vault has to say yes, exactly as GitHub does.
    subprocess.run(["git", "-C", str(bare), "config", "uploadpack.allowFilter", "true"], check=True)
    Git(seed.vault_dir).run("remote", "add", "origin", str(bare))
    Git(seed.vault_dir).run("push", "-q", "origin", "main")

    return bare


@pytest.fixture
def cloned(tmp_path, origin, monkeypatch, config, description):
    """This Machine, having cloned the Cloud Vault with nothing bound yet."""
    paths = Paths(root=tmp_path / "machine")
    monkeypatch.setattr(vault, "remote_url", lambda _repo: str(origin))

    vault.clone(paths, config.repo, pat="unused-for-file-urls", entry_ids=())
    return paths


def checked_out(paths: Paths) -> set[str]:
    return {
        p.relative_to(paths.vault_dir).as_posix()
        for p in paths.vault_dir.rglob("*")
        if p.is_file() and ".git" not in p.parts
    }


# --- what makes a repository a Vault ----------------------------------------------------------


def test_a_fresh_vault_carries_its_marker_and_this_machine(tmp_path, config, description):
    paths = Paths(root=tmp_path)
    vault.initialize(paths, config, description, branch="main")

    assert vault.read_marker(paths).schema == vault.SCHEMA
    assert paths.machine_file(MACHINE).exists()
    assert vault.current_branch(paths) == "main"  # before any commit exists


def test_a_repository_that_is_not_a_vault_is_refused(tmp_path):
    """Point the app at your dissertation repo and it must not commit save files into it."""
    paths = Paths(root=tmp_path)
    paths.vault_dir.mkdir(parents=True)
    (paths.vault_dir / "README.md").write_text("my dissertation", encoding="utf-8")

    with pytest.raises(NotAVault):
        vault.read_marker(paths)


def test_a_vault_from_a_newer_build_is_refused_rather_than_corrupted(tmp_path, config, description):
    """The single failure mode capable of damaging every Entry at once, stopped by one `if`."""
    paths = Paths(root=tmp_path)
    vault.initialize(paths, config, description, branch="main")
    paths.vault_marker.write_text('{"vault": true, "schema": 99}', encoding="utf-8")

    with pytest.raises(VaultTooNew):
        vault.read_marker(paths)


def test_save_data_is_marked_binary_so_git_never_rewrites_it(tmp_path, config, description):
    """A global core.autocrlf=true rewrites line endings in anything Git guesses is text. Save
    files are binary, the guess is a heuristic, and a wrong guess corrupts the save."""
    paths = Paths(root=tmp_path)
    vault.initialize(paths, config, description, branch="main")

    attributes = (paths.vault_dir / ".gitattributes").read_text(encoding="utf-8")

    assert "* -text" in attributes
    assert "/entries/** binary" in attributes


# --- Invariant 2: clean between operations ------------------------------------------------------


def test_ensure_clean_discards_whatever_a_crashed_operation_left_behind(cloned):
    (cloned.vault_dir / "half-copied.sav").write_text("debris", encoding="utf-8")
    (cloned.vault_marker).write_text("clobbered", encoding="utf-8")

    vault.ensure_clean(cloned)

    assert vault.is_clean(cloned)
    assert not (cloned.vault_dir / "half-copied.sav").exists()
    assert vault.read_marker(cloned).schema == vault.SCHEMA  # restored


def test_ensure_clean_never_deletes_ignored_files(cloned):
    """`git clean -fdx` would. The Vault holds no ignored files (Invariant 8), so `-x` looks
    free - and the day that invariant slips, `-x` is what turns a routine cleanup into the
    loss of everything it touches. It is never typed."""
    (cloned.vault_dir / ".gitignore").write_text("precious/\n", encoding="utf-8")
    precious = cloned.vault_dir / "precious"
    precious.mkdir()
    (precious / "irreplaceable.zip").write_text("the only copy", encoding="utf-8")

    vault.ensure_clean(cloned)

    assert (precious / "irreplaceable.zip").exists()


def test_a_dirty_tree_is_refused_rather_than_committed(cloned):
    (cloned.vault_dir / "stray.sav").write_text("where did this come from", encoding="utf-8")

    with pytest.raises(VaultDirty):
        vault.assert_clean(cloned)


# --- selective sync (ADR-0002) -------------------------------------------------------------------


def test_a_clone_with_nothing_bound_still_shows_every_entry_that_exists(cloned):
    """The hole `SIDECAR_PIN` exists to plug. With no Entry bound there is no `entries/<uuid>`
    in the cone, so `entries/` is nobody's parent and every sidecar vanishes - leaving a newly
    set up second Machine with an empty list and nothing to bind."""
    files = checked_out(cloned)

    assert f"entries/{AAA}.json" in files
    assert f"entries/{BBB}.json" in files
    assert not any(f.startswith(f"entries/{AAA}/") for f in files)  # and no save data at all


def test_binding_an_entry_checks_out_that_entry_and_no_other(cloned):
    vault.set_sparse(cloned, [AAA])
    files = checked_out(cloned)

    assert f"entries/{AAA}/slot1.sav" in files
    assert not any(f.startswith(f"entries/{BBB}/") for f in files)  # the game we do not play
    assert f"entries/{BBB}.json" in files  # but we still know it is there


def test_unbinding_removes_the_working_copy_and_keeps_the_history(cloned):
    """Narrowing the cone is fully reversible: it changes what is checked out, never what is
    committed. Nothing is ever lost by unbinding."""
    vault.set_sparse(cloned, [AAA, BBB])
    assert f"entries/{BBB}/slot1.sav" in checked_out(cloned)

    vault.set_sparse(cloned, [AAA])
    assert f"entries/{BBB}/slot1.sav" not in checked_out(cloned)

    listed = Git(cloned.vault_dir).run("ls-tree", "-r", "--name-only", "HEAD")
    assert f"entries/{BBB}/slot1.sav" in listed  # still committed, still there

    vault.set_sparse(cloned, [AAA, BBB])
    assert f"entries/{BBB}/slot1.sav" in checked_out(cloned)  # and it comes straight back


def test_the_machines_directory_is_always_checked_out(cloned):
    """Every Machine needs to see every other Machine, whatever it has bound."""
    assert any(f.startswith("machines/") for f in checked_out(cloned))


def test_a_clone_fetches_history_without_the_save_data(cloned):
    """`--filter=blob:none`: a Vault holding forty games costs, on a laptop that plays four,
    four games' worth of disk."""
    promised = Git(cloned.vault_dir).run("config", "--get", "remote.origin.promisor").strip()
    filtered = Git(cloned.vault_dir).run("config", "--get", "remote.origin.partialclonefilter")

    assert promised == "true"
    assert filtered.strip() == "blob:none"


def test_the_sidecar_pin_is_never_created_on_disk(cloned):
    """It is a directory that does not exist, and must not start existing: an empty directory
    inside `entries/` would be neither an Entry nor storable by Git."""
    vault.set_sparse(cloned, [AAA])

    assert not (cloned.vault_dir / vault.SIDECAR_PIN).exists()


# --- size guards: fail early, not at push time ------------------------------------------------


def test_a_file_too_big_for_github_is_found_before_it_is_committed(tmp_path):
    """GitHub hard-rejects anything over 100 MB. Discovering that at push time leaves a commit
    in the Vault that can never leave it, recoverable only by rewriting history."""
    entry = tmp_path / "saves"
    entry.mkdir()
    (entry / "small.sav").write_text("fine", encoding="utf-8")
    (entry / "enormous.bin").write_bytes(b"\0" * 2048)

    found = vault.oversized_files(entry, limit=1024)

    assert [f.path.name for f in found] == ["enormous.bin"]


def test_an_entry_within_the_limit_reports_nothing(tmp_path):
    entry = tmp_path / "saves"
    entry.mkdir()
    (entry / "slot1.sav").write_text("progress", encoding="utf-8")

    assert vault.oversized_files(entry) == []
