"""Push, Pull, Cloud status, and Offline Mode.

Real Git against a bare repository on disk - no network, no GitHub. Two Machines, `laptop`
and `desktop`, are two clones of the same Cloud Vault, which is how another Machine's work
arrives in every scenario here.

The one assertion that matters more than all the others: **a Pull never moves a Baseline.**
A Pull that does makes a stale Live Save look In Sync, and the next Sync then uploads old
progress over another Machine's new progress and reports success.
"""

import subprocess
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from core import cloud, entries, ledger, operations, vault
from core.cloud import Cloud, CloudOffline, CloudState, OfflineReason, PushRejected, classify
from core.config import Config, MachineDescription
from core.entries import Entry
from core.entry_state import Action, EntryState
from core.git import GitError, GitTimeout
from core.ledger import Ledger
from core.paths import Paths

MACHINE_A = "9c8b7a65-0000-4000-8000-00000000000a"
MACHINE_B = "9c8b7a65-0000-4000-8000-00000000000b"

PAT = "unused-for-file-paths"


@dataclass
class Machine:
    """One participating computer: a Vault clone, a Ledger, and a folder of Live Saves."""

    paths: Paths
    config: Config
    description: MachineDescription
    the_ledger: Ledger = field(default_factory=Ledger)
    cloud: Cloud = field(init=False)

    def __post_init__(self) -> None:
        self.cloud = Cloud(paths=self.paths, config=self.config)

    def live(self, name: str) -> Path:
        return self.paths.root / "live" / name

    def play(self, name: str, progress: str) -> Path:
        """The game writes its save."""
        folder = self.live(name)
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "slot1.sav").write_text(progress, encoding="utf-8")
        return folder

    def add(self, name: str) -> Entry:
        return operations.add_entry(
            self.paths, self.config, self.description, self.the_ledger, name, self.live(name)
        )

    def sync(self, entry_id: str):
        return operations.sync_to_vault(
            self.paths, self.config, self.description, self.the_ledger, entry_id
        )

    def bind(self, entry_id: str, name: str) -> None:
        """Bind an Entry another Machine published to a live path here, and widen the cone."""
        # The game's own folder may not exist yet, but the folder it would live in does -
        # restore refuses to invent missing parents, which could paper over an unplugged drive.
        self.live(name).parent.mkdir(parents=True, exist_ok=True)
        self.the_ledger.bind(entry_id, self.live(name))
        vault.set_sparse(self.paths, self.the_ledger.bindings)

    def restore(self, entry_id: str):
        return operations.restore_to_live(self.paths, self.config, self.the_ledger, entry_id)

    def state(self, entry_id: str):
        return ledger.refresh(self.paths, self.the_ledger, entry_id)

    def vaulted(self, entry_id: str) -> Path:
        """The Entry's stored folder itself (`entries/<id>/<name>`), not the cone wrapper."""
        return entries.content_path(self.paths, entries.require(self.paths, entry_id))

    def register(self) -> None:
        """Commit this Machine's published file - the second-machine path, by hand for now."""
        vault.register_machine(self.paths, self.config, self.description)
        repo = vault.git(self.paths)
        repo.run("add", "-A")
        repo.run(
            "commit",
            "-q",
            "-m",
            f"register {self.description.hostname}",
            config=("user.name=t", "user.email=t@gsm.local"),
        )


@pytest.fixture
def cloud_vault(tmp_path):
    """The Cloud Vault: a bare repository reached over a plain file path."""
    bare = tmp_path / "cloud.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(bare)], check=True)
    # Partial clone must be opted into by the server, exactly as GitHub does.
    subprocess.run(["git", "-C", str(bare), "config", "uploadpack.allowFilter", "true"], check=True)
    return bare


@pytest.fixture
def laptop(tmp_path, cloud_vault):
    """The first Machine: creates the Vault and publishes it to the Cloud Vault."""
    machine = Machine(
        paths=Paths(root=tmp_path / "laptop"),
        config=Config(machine_id=MACHINE_A, repo="owner/vault", default_branch="main"),
        description=MachineDescription(hostname="laptop", os_name="Darwin"),
    )
    vault.initialize(machine.paths, machine.config, machine.description, branch="main")
    repo = vault.git(machine.paths)
    repo.run("add", "-A")
    repo.run("commit", "-q", "-m", "init vault", config=("user.name=t", "user.email=t@gsm.local"))
    repo.run("remote", "add", "origin", str(cloud_vault))
    repo.run("push", "-q", "origin", "main")
    return machine


@pytest.fixture
def desktop(tmp_path, cloud_vault, laptop, monkeypatch):
    """The second Machine: clones whatever the laptop has published so far."""
    machine = Machine(
        paths=Paths(root=tmp_path / "desktop"),
        config=Config(machine_id=MACHINE_B, repo="owner/vault", default_branch="main"),
        description=MachineDescription(hostname="desktop", os_name="Windows"),
    )
    monkeypatch.setattr(vault, "remote_url", lambda _repo: str(cloud_vault))
    vault.clone(machine.paths, machine.config.repo, pat=PAT, entry_ids=())
    return machine


# --- Cloud status: fetch and compare, never act -----------------------------------------------


def test_a_fresh_clone_is_up_to_date(desktop):
    status = desktop.cloud.fetch_status(pat=PAT)

    assert status.state is CloudState.UP_TO_DATE
    assert (status.ahead, status.behind) == (0, 0)


def test_unpushed_local_commits_read_as_ahead(desktop):
    desktop.register()

    status = desktop.cloud.fetch_status(pat=PAT)

    assert status.state is CloudState.AHEAD
    assert (status.ahead, status.behind) == (1, 0)


def test_unpushed_commits_are_reported_until_they_are_pushed(laptop):
    """History colours the local-ahead commits: the ones on HEAD the Cloud Vault does not yet
    have. Every commit is unpushed until a Push, and none afterwards."""
    laptop.play("Elden Ring", "progress")
    entry = laptop.add("Elden Ring")
    laptop.sync(entry.entry_id)

    synced = next(
        c for c in operations.history(laptop.paths, entry.entry_id) if c.subject.startswith("sync")
    )
    assert synced.sha in operations.unpushed_commits(laptop.paths)

    laptop.cloud.push(pat=PAT)
    assert operations.unpushed_commits(laptop.paths) == set()  # all caught up


def test_another_machines_pushed_work_reads_as_behind_and_is_not_pulled(laptop, desktop):
    """Never auto-pull: the status is an observation, not an action."""
    laptop.play("Elden Ring", "progress")
    entry = laptop.add("Elden Ring")
    laptop.sync(entry.entry_id)
    laptop.cloud.push(pat=PAT)

    status = desktop.cloud.fetch_status(pat=PAT)

    assert status.state is CloudState.BEHIND
    assert (status.ahead, status.behind) == (0, 2)  # add + sync
    assert not desktop.paths.entry_sidecar(entry.entry_id).exists()  # observed, not acted on


def test_commits_on_both_sides_read_as_diverged(laptop, desktop):
    desktop.register()
    laptop.play("Elden Ring", "progress")
    laptop.add("Elden Ring")
    laptop.cloud.push(pat=PAT)

    status = desktop.cloud.fetch_status(pat=PAT)

    assert status.state is CloudState.DIVERGED
    assert (status.ahead, status.behind) == (1, 1)


# --- Push ---------------------------------------------------------------------------------------


def test_a_push_from_behind_is_rejected_and_nothing_is_forced(laptop, desktop, cloud_vault):
    """A rejected push means another Machine published first. The answer is a Pull and a
    merge, never a force: forcing would erase the other Machine's commits from the Cloud."""
    desktop.register()
    desktop.cloud.push(pat=PAT)
    published = vault.git(desktop.paths).run("rev-parse", "HEAD").strip()

    laptop.play("Elden Ring", "progress")
    laptop.add("Elden Ring")

    with pytest.raises(PushRejected):
        laptop.cloud.push(pat=PAT)

    still_there = subprocess.run(
        ["git", "-C", str(cloud_vault), "rev-parse", "main"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    assert still_there == published


# --- Pull: the Baseline discipline --------------------------------------------------------------


@pytest.fixture
def shared_entry(laptop, desktop):
    """One Entry, In Sync on both Machines - the everyday two-machine starting point."""
    laptop.play("Elden Ring", "old progress")
    entry = laptop.add("Elden Ring")
    laptop.sync(entry.entry_id)
    laptop.cloud.push(pat=PAT)

    desktop.cloud.pull(pat=PAT, description=desktop.description)
    desktop.bind(entry.entry_id, "Elden Ring")
    desktop.restore(entry.entry_id)
    return entry


def test_a_pull_never_moves_the_baseline(laptop, desktop, shared_entry):
    """THE test. The desktop's Live Save has not moved since its last Sync, and a Pull has
    just brought down the laptop's newer save. That must read as Vault Ahead - restore me -
    and never as In Sync or Local Ahead. A design that advances the Baseline on Pull reads
    Local Ahead here, offers to Sync, and uploads the stale save over the laptop's progress
    - the single worst bug this application can have."""
    entry_id = shared_entry.entry_id
    baseline_before = desktop.the_ledger.require(entry_id).baseline

    laptop.play("Elden Ring", "new progress from the laptop")
    laptop.sync(entry_id)
    laptop.cloud.push(pat=PAT)

    pulled = desktop.cloud.pull(pat=PAT, description=desktop.description)

    assert pulled.conflicts == ()
    assert desktop.the_ledger.require(entry_id).baseline == baseline_before
    # And the Ledger on disk agrees. A Pull that quietly persisted a Baseline would poison
    # the next launch even while this session's in-memory copy stayed honest.
    assert ledger.load(desktop.paths).require(entry_id).baseline == baseline_before

    status = desktop.state(entry_id)
    assert status.state is EntryState.VAULT_AHEAD
    assert status.recommended is Action.RESTORE_TO_LIVE


def test_a_pull_merges_and_never_rebases(laptop, desktop):
    """Rewriting published history across Machines is how a Vault loses a commit. After a
    Pull, both Machines' original commits must still exist, joined by a merge - the desktop's
    own commit keeps its hash, and the laptop's pushed commit is an ancestor."""
    laptop.play("Elden Ring", "laptop progress")
    laptop.add("Elden Ring")
    laptop.cloud.push(pat=PAT)
    laptop_head = vault.git(laptop.paths).run("rev-parse", "HEAD").strip()

    desktop.play("Hades", "desktop progress")
    desktop.add("Hades")
    desktop_head = vault.git(desktop.paths).run("rev-parse", "HEAD").strip()

    pulled = desktop.cloud.pull(pat=PAT, description=desktop.description)

    assert pulled.conflicts == ()  # one writer per file: disjoint Entries merge silently
    repo = vault.git(desktop.paths)
    assert repo.run("rev-parse", "HEAD^1").strip() == desktop_head  # not rewritten
    assert repo.run("rev-parse", "HEAD^2").strip() == laptop_head  # not lost
    assert vault.is_clean(desktop.paths)


def test_the_merge_commit_is_authored_by_the_pulling_machine(laptop, desktop):
    laptop.play("Elden Ring", "laptop progress")
    laptop.add("Elden Ring")
    laptop.cloud.push(pat=PAT)
    desktop.register()

    desktop.cloud.pull(pat=PAT, description=desktop.description)

    author = vault.git(desktop.paths).run("log", "-1", "--format=%an").strip()
    assert author == "desktop"


# --- Merge Conflicts: both Machines synced the same Entry ----------------------------------------


@pytest.fixture
def contested(laptop, desktop, shared_entry):
    """Both Machines synced the same Entry since they last agreed, and the desktop pulls."""
    laptop.play("Elden Ring", "the laptop's line of progress")
    laptop.sync(shared_entry.entry_id)
    laptop.cloud.push(pat=PAT)

    desktop.play("Elden Ring", "the desktop's line of progress")
    desktop.sync(shared_entry.entry_id)

    pulled = desktop.cloud.pull(pat=PAT, description=desktop.description)
    return shared_entry, pulled


def test_a_contested_entry_surfaces_as_a_merge_conflict_at_entry_granularity(contested):
    entry, pulled = contested

    assert pulled.conflicts == (entry.entry_id,)


def test_resolving_toward_the_cloud_takes_their_save_whole_and_keeps_ours_in_history(
    desktop, contested
):
    """Non-destructive: the losing line of progress stays reachable in history, and no Live
    Save is touched - the desktop's own line is still on disk, still restorable by Sync."""
    entry, _ = contested
    ours = vault.git(desktop.paths).run("rev-parse", "HEAD").strip()

    cloud.resolve_merge(desktop.paths, entry.entry_id, cloud.Side.CLOUD)
    cloud.finish_merge(desktop.paths, desktop.config, desktop.description)

    repo = vault.git(desktop.paths)
    assert vault.is_clean(desktop.paths)
    assert not cloud.merging(desktop.paths)

    vaulted = desktop.vaulted(entry.entry_id) / "slot1.sav"
    assert vaulted.read_text(encoding="utf-8") == "the laptop's line of progress"
    assert repo.run("rev-parse", "HEAD^1").strip() == ours  # the losing side is a parent,
    live = desktop.live("Elden Ring") / "slot1.sav"  # and the Live Save is untouched
    assert live.read_text(encoding="utf-8") == "the desktop's line of progress"

    # The desktop's line is on disk and the laptop's is in the Vault: an ordinary Vault
    # Ahead, whose Restore is the user's explicit next step - not ours.
    assert desktop.state(entry.entry_id).state is EntryState.VAULT_AHEAD


def test_resolving_toward_the_vault_keeps_our_save_and_the_cloud_line_in_history(
    desktop, contested
):
    entry, _ = contested
    theirs = vault.git(desktop.paths).run("rev-parse", "MERGE_HEAD").strip()

    cloud.resolve_merge(desktop.paths, entry.entry_id, cloud.Side.VAULT)
    cloud.finish_merge(desktop.paths, desktop.config, desktop.description)

    repo = vault.git(desktop.paths)
    assert vault.is_clean(desktop.paths)

    vaulted = desktop.vaulted(entry.entry_id) / "slot1.sav"
    assert vaulted.read_text(encoding="utf-8") == "the desktop's line of progress"
    assert repo.run("rev-parse", "HEAD^2").strip() == theirs  # the cloud line is a parent

    assert desktop.state(entry.entry_id).state is EntryState.IN_SYNC  # our side, kept whole


def test_a_pull_discards_stray_files_rather_than_merging_them_in(laptop, desktop):
    """Invariant 2: dirt found at the start of an operation is an interrupted operation, and
    is discarded - never quietly folded into a merge commit."""
    laptop.play("Elden Ring", "progress")
    laptop.add("Elden Ring")
    laptop.cloud.push(pat=PAT)

    stray = desktop.paths.vault_dir / "machines" / "wreckage.json"
    stray.write_text("{}", encoding="utf-8")

    pulled = desktop.cloud.pull(pat=PAT, description=desktop.description)

    assert pulled.commits == 1  # the laptop's add
    assert not stray.exists()
    assert vault.is_clean(desktop.paths)
    tracked = vault.git(desktop.paths).run("ls-files")
    assert "wreckage" not in tracked


def test_finishing_a_merge_with_choices_still_pending_is_refused(desktop, contested):
    with pytest.raises(cloud.MergeUnfinished):
        cloud.finish_merge(desktop.paths, desktop.config, desktop.description)

    assert cloud.merging(desktop.paths)  # still resolvable; nothing was committed


def test_aborting_the_merge_walks_away_with_nothing_changed(desktop, contested):
    entry, _ = contested

    cloud.abort_merge(desktop.paths)

    assert not cloud.merging(desktop.paths)
    assert vault.is_clean(desktop.paths)
    vaulted = desktop.vaulted(entry.entry_id) / "slot1.sav"
    assert vaulted.read_text(encoding="utf-8") == "the desktop's line of progress"


def test_a_removal_contested_by_a_sync_can_go_either_way(laptop, desktop, shared_entry):
    """The laptop removed the Entry; the desktop synced new progress into it. Taking the
    cloud's side must stage the *deletion* - which is why resolution replaces the Entry's
    tree wholesale instead of checking out conflict stages."""
    operations.remove_from_vault(
        laptop.paths, laptop.config, laptop.description, laptop.the_ledger, shared_entry.entry_id
    )
    laptop.cloud.push(pat=PAT)

    desktop.play("Elden Ring", "progress the desktop still cares about")
    desktop.sync(shared_entry.entry_id)

    pulled = desktop.cloud.pull(pat=PAT, description=desktop.description)
    assert pulled.conflicts == (shared_entry.entry_id,)

    cloud.resolve_merge(desktop.paths, shared_entry.entry_id, cloud.Side.CLOUD)
    cloud.finish_merge(desktop.paths, desktop.config, desktop.description)

    assert vault.is_clean(desktop.paths)
    assert not desktop.paths.entry_content_dir(shared_entry.entry_id).exists()
    assert not desktop.paths.entry_sidecar(shared_entry.entry_id).exists()
    live = desktop.live("Elden Ring") / "slot1.sav"  # Invariant 1, as always
    assert live.read_text(encoding="utf-8") == "progress the desktop still cares about"


def test_a_conflict_outside_entries_aborts_the_merge_and_refuses(laptop, desktop):
    """ADR-0003 makes this impossible from inside the app - every file outside entries/ has
    one writer - so if it happens anyway, the Vault has been edited by hand and we refuse to
    guess. The merge is aborted rather than left half-open."""
    marker = f"machines/{MACHINE_A}.json"
    for machine, text in ((laptop, '{"hand": "edited"}'), (desktop, '{"also": "edited"}')):
        (machine.paths.vault_dir / marker).write_text(text, encoding="utf-8")
        repo = vault.git(machine.paths)
        repo.run("add", "-A")
        repo.run("commit", "-q", "-m", "vandalism", config=("user.name=t", "user.email=t@x"))
    laptop.cloud.push(pat=PAT)

    with pytest.raises(cloud.ForeignConflict):
        desktop.cloud.pull(pat=PAT, description=desktop.description)

    assert not cloud.merging(desktop.paths)
    assert vault.is_clean(desktop.paths)


# --- Offline Mode: sticky, reasoned, and exited only by a successful Check Connection ------------


@pytest.fixture
def unplugged(cloud_vault):
    """Pull the network cable: the Cloud Vault stops existing until plugged back in."""
    hidden = cloud_vault.with_name("unplugged.git")

    def plug(back_in: bool) -> None:
        if back_in and hidden.exists():
            hidden.rename(cloud_vault)
        elif not back_in and cloud_vault.exists():
            cloud_vault.rename(hidden)

    yield plug
    plug(True)


def test_a_failed_cloud_operation_drops_into_offline_mode(desktop, unplugged):
    unplugged(False)

    with pytest.raises(CloudOffline):
        desktop.cloud.fetch_status(pat=PAT)

    assert desktop.cloud.offline is not None
    assert desktop.cloud.offline.reason is OfflineReason.NO_NETWORK


def test_offline_mode_is_sticky_even_after_the_network_returns(desktop, unplugged):
    """No operation may quietly heal it - not even one that would now succeed. The user is
    told they are offline, and the user says when to check again."""
    unplugged(False)
    with pytest.raises(CloudOffline):
        desktop.cloud.fetch_status(pat=PAT)
    unplugged(True)

    with pytest.raises(CloudOffline):
        desktop.cloud.push(pat=PAT)
    with pytest.raises(CloudOffline):
        desktop.cloud.pull(pat=PAT, description=desktop.description)
    with pytest.raises(CloudOffline):
        desktop.cloud.fetch_status(pat=PAT)


def test_a_failed_push_also_drops_into_offline_mode(desktop, unplugged):
    desktop.register()
    unplugged(False)

    with pytest.raises(CloudOffline):
        desktop.cloud.push(pat=PAT)

    assert desktop.cloud.offline is not None
    assert desktop.cloud.offline.reason is OfflineReason.NO_NETWORK


def test_everything_purely_local_keeps_working_offline(desktop, unplugged):
    unplugged(False)
    with pytest.raises(CloudOffline):
        desktop.cloud.fetch_status(pat=PAT)

    desktop.play("Hades", "progress made on the train")
    entry = desktop.add("Hades")
    synced = desktop.sync(entry.entry_id)

    assert synced.commit is not None
    assert desktop.state(entry.entry_id).state is EntryState.IN_SYNC


def test_a_failed_check_connection_stays_offline(desktop, unplugged):
    unplugged(False)
    with pytest.raises(CloudOffline):
        desktop.cloud.fetch_status(pat=PAT)

    assert desktop.cloud.check_connection(pat=PAT) is False
    assert desktop.cloud.offline is not None


def test_a_successful_check_connection_is_the_way_back_online(desktop, unplugged):
    unplugged(False)
    with pytest.raises(CloudOffline):
        desktop.cloud.fetch_status(pat=PAT)
    unplugged(True)

    assert desktop.cloud.check_connection(pat=PAT) is True
    assert desktop.cloud.offline is None
    assert desktop.cloud.fetch_status(pat=PAT).state is CloudState.UP_TO_DATE


def test_going_offline_keeps_the_last_known_cloud_state(desktop, laptop, unplugged):
    """The indicator keeps showing what we knew: "Offline (last checked 2h ago: Behind)".
    Entering Offline Mode must not clear `last_status` - it is the "last checked" half."""
    laptop.play("Elden Ring", "progress")
    laptop.add("Elden Ring")
    laptop.cloud.push(pat=PAT)
    before = desktop.cloud.fetch_status(pat=PAT)
    assert before.state is CloudState.BEHIND

    unplugged(False)
    with pytest.raises(CloudOffline):
        desktop.cloud.pull(pat=PAT, description=desktop.description)

    assert desktop.cloud.last_status == before
    assert desktop.cloud.offline.since >= before.checked_at


def test_a_rejected_push_is_not_a_connectivity_failure(laptop, desktop):
    """The Cloud answered - the answer was no. Going offline over it would grey out the very
    Pull that fixes it."""
    desktop.register()
    desktop.cloud.push(pat=PAT)
    laptop.play("Elden Ring", "progress")
    laptop.add("Elden Ring")

    with pytest.raises(PushRejected):
        laptop.cloud.push(pat=PAT)

    assert laptop.cloud.offline is None


def test_failures_are_classified_by_what_the_user_can_do_about_them():
    """Auth failures offer "re-enter your PAT"; everything else is "check your connection".
    GitHub reports a private repo to a bad token as not found, so that counts as auth."""

    def error(stderr: str) -> GitError:
        return GitError(argv=["git", "fetch"], returncode=128, stderr=stderr)

    prompts_disabled = (
        "fatal: could not read Username for 'https://github.com': terminal prompts disabled"
    )
    auth_shaped = (
        "fatal: Authentication failed for 'https://github.com/o/r.git/'",
        "remote: Invalid username or password.",
        "remote: Repository not found.",
        "fatal: unable to access 'x': The requested URL returned error: 403",
        prompts_disabled,
    )
    network_shaped = (
        "fatal: unable to access 'x': Could not resolve host: github.com",
        "fatal: unable to access 'x': Failed to connect to github.com port 443",
        "fatal: 'cloud.git' does not appear to be a git repository",
        # A locked pack file is not an auth problem: telling this user to re-enter a
        # perfectly good PAT would send them in circles.
        "error: unable to create file .git/objects/pack/tmp_pack: Permission denied",
    )

    for stderr in auth_shaped:
        assert classify(error(stderr)) is OfflineReason.AUTH_FAILED, stderr
    for stderr in network_shaped:
        assert classify(error(stderr)) is OfflineReason.NO_NETWORK, stderr
    assert classify(GitTimeout("`fetch` did not finish in time.")) is OfflineReason.NO_NETWORK
