"""Download: the Vault's copy of an Entry, into a plain folder, without binding anything.

The desktop clones over a `file://` URL on purpose. Over a bare path Git clones locally and
ignores `--filter`, so every blob would already be present and the "not on this Machine yet"
path - the whole reason Download fetches anything - would never be exercised.
"""

import subprocess

import pytest

from core import download, entries, operations, vault
from core.config import Config, MachineDescription
from core.ledger import Ledger
from core.paths import Paths

MACHINE_A = "9c8b7a65-0000-4000-8000-00000000000a"
MACHINE_B = "9c8b7a65-0000-4000-8000-00000000000b"
PAT = "unused-for-file-urls"
IDENTITY = ("user.name=t", "user.email=t@gsm.local")


@pytest.fixture
def cloud_vault(tmp_path):
    bare = tmp_path / "cloud.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(bare)], check=True)
    subprocess.run(["git", "-C", str(bare), "config", "uploadpack.allowFilter", "true"], check=True)
    return bare


@pytest.fixture
def laptop(tmp_path, cloud_vault):
    """Publishes a folder Entry and a single-file Entry to the Cloud Vault."""
    paths = Paths(root=tmp_path / "laptop")
    config = Config(machine_id=MACHINE_A, repo="owner/vault", default_branch="main")
    description = MachineDescription(hostname="laptop", os_name="Darwin")
    vault.initialize(paths, config, description, branch="main")
    repo = vault.git(paths)
    repo.run("add", "-A")
    repo.run("commit", "-q", "-m", "init", config=IDENTITY)
    repo.run("remote", "add", "origin", str(cloud_vault))

    the_ledger = Ledger()
    hades = tmp_path / "live" / "Hades"
    (hades / "Profiles").mkdir(parents=True)
    (hades / "Profile1.sav").write_bytes(b"\x00\x01progress")
    (hades / "Profiles" / "meta.json").write_text("{}", encoding="utf-8")
    ini = tmp_path / "live" / "settings.ini"
    ini.write_text("[video]\nvsync=1\n", encoding="utf-8")

    found = {}
    for name, live in (("Hades", hades), ("Game Settings", ini)):
        entry = operations.add_entry(paths, config, description, the_ledger, name, live)
        operations.sync_to_vault(paths, config, description, the_ledger, entry.entry_id)
        found[name] = entry.entry_id
    repo.run("push", "-q", "origin", "main")
    return paths, found


@pytest.fixture
def desktop(tmp_path, cloud_vault, laptop, monkeypatch):
    """A second Machine with nothing bound: a blobless clone holding no save content."""
    paths = Paths(root=tmp_path / "desktop")
    monkeypatch.setattr(vault, "remote_url", lambda _repo: cloud_vault.as_uri())
    vault.clone(paths, "owner/vault", pat=PAT, entry_ids=())
    return paths


@pytest.fixture
def downloads(tmp_path):
    folder = tmp_path / "Downloads"
    folder.mkdir()
    return folder


def test_an_unbound_folder_is_fetched_and_copied_without_binding_it(laptop, desktop, downloads):
    _, ids = laptop
    sparse_before = vault.git(desktop).run("sparse-checkout", "list")
    assert download.missing_blobs(vault.git(desktop), ids["Hades"])  # truly not here yet

    landed = download.download(desktop, ids["Hades"], downloads, pat=PAT)

    assert landed == downloads / "Hades"
    assert (landed / "Profile1.sav").read_bytes() == b"\x00\x01progress"
    assert (landed / "Profiles" / "meta.json").read_text(encoding="utf-8") == "{}"
    # Not bound: the cone and the working tree are exactly as they were.
    assert vault.git(desktop).run("sparse-checkout", "list") == sparse_before
    assert not desktop.entry_content_dir(ids["Hades"]).exists()
    assert vault.is_clean(desktop)


def test_a_single_file_entry_downloads_as_that_file(laptop, desktop, downloads):
    _, ids = laptop

    landed = download.download(desktop, ids["Game Settings"], downloads, pat=PAT)

    assert landed == downloads / "settings.ini"
    assert landed.read_text(encoding="utf-8") == "[video]\nvsync=1\n"


def test_a_bound_entry_is_copied_from_what_is_already_here_with_no_network(laptop, downloads):
    paths, ids = laptop

    # No token at all: a bound Entry's blobs are held, so nothing may need fetching.
    landed = download.download(paths, ids["Hades"], downloads, pat=None)

    assert (landed / "Profile1.sav").read_bytes() == b"\x00\x01progress"


def test_content_not_yet_here_without_a_token_asks_for_a_sign_in(laptop, desktop, downloads):
    _, ids = laptop

    with pytest.raises(download.NeedsSignIn):
        download.download(desktop, ids["Hades"], downloads, pat=None)
    assert list(downloads.iterdir()) == []


def test_nothing_at_the_destination_is_ever_overwritten(laptop, desktop, downloads):
    _, ids = laptop
    (downloads / "Hades").mkdir()
    (downloads / "Hades" / "mine.txt").write_text("keep me", encoding="utf-8")
    (downloads / "settings.ini").write_text("mine", encoding="utf-8")

    folder = download.download(desktop, ids["Hades"], downloads, pat=PAT)
    again = download.download(desktop, ids["Hades"], downloads, pat=PAT)
    single = download.download(desktop, ids["Game Settings"], downloads, pat=PAT)

    assert (folder.name, again.name, single.name) == ("Hades (1)", "Hades (2)", "settings (1).ini")
    assert (downloads / "Hades" / "mine.txt").read_text(encoding="utf-8") == "keep me"
    assert (downloads / "settings.ini").read_text(encoding="utf-8") == "mine"
    # And no staging directory is left behind.
    assert not [p for p in downloads.iterdir() if p.name.startswith(download.STAGING_PREFIX)]


def test_an_entry_never_synced_has_nothing_to_download(laptop, downloads):
    paths, _ = laptop
    entries.write(paths, entries.Entry(entry_id="0" * 8, name="Never Synced"))

    with pytest.raises(download.NothingToDownload):
        download.download(paths, "0" * 8, downloads, pat=PAT)


def test_a_missing_downloads_folder_is_created(laptop, tmp_path):
    paths, ids = laptop
    fresh = tmp_path / "nowhere" / "Downloads"

    landed = download.download(paths, ids["Game Settings"], fresh, pat=None)

    assert landed == fresh / "settings.ini"
