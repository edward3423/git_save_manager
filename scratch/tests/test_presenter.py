"""What the window shows, tested without a window.

The presenter is pure: Entries and the Ledger in, captions and rows out. It imports no Qt,
which is what keeps the paths that describe save data testable headless (the plan's Section
8 rule: no GUI, no network in `scratch/tests`).
"""

from datetime import UTC, datetime, timedelta

import pytest

from core import entries, operations, vault
from core.cloud import Cloud, CloudState, CloudStatus, OfflineMode, OfflineReason
from core.config import Config, MachineDescription
from core.entry_state import EntryState
from core.ledger import Ledger
from core.paths import Paths
from ui import presenter

MACHINE = "9c8b7a65-0000-4000-8000-00000000000a"
NOW = datetime(2026, 7, 15, 12, 0, 0, tzinfo=UTC)


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


def test_a_bound_entry_shows_its_state_and_an_unlinked_one_says_so(machine):
    paths, config, description = machine
    the_ledger = Ledger()

    live = paths.root / "live" / "Elden Ring"
    live.mkdir(parents=True)
    (live / "slot1.sav").write_text("progress", encoding="utf-8")
    operations.add_entry(paths, config, description, the_ledger, "Elden Ring", live)

    unlinked = entries.Entry(entry_id="0" * 8, name="Hades")
    entries.write(paths, unlinked)

    rows = presenter.rows(paths, the_ledger)

    assert [(row.name, row.caption) for row in rows] == [
        ("Elden Ring", "Local Ahead"),
        ("Hades", "Unlinked"),
    ]
    assert rows[0].status is not None
    assert rows[0].status.state is EntryState.LOCAL_AHEAD
    assert rows[1].status is None  # nothing derivable without a Binding


def status(state: CloudState, ahead: int = 0, behind: int = 0) -> CloudStatus:
    return CloudStatus(state=state, ahead=ahead, behind=behind, checked_at=NOW)


def test_the_cloud_caption_says_where_head_stands():
    paths = Paths(root=None)  # never touched by the caption
    config = Config(machine_id=MACHINE, repo="owner/vault", default_branch="main")

    def caption(last_status, offline=None):
        cloud = Cloud(paths=paths, config=config, offline=offline, last_status=last_status)
        return presenter.cloud_caption(cloud, now=NOW)

    assert caption(None) == "Cloud: not checked yet"
    assert caption(status(CloudState.UP_TO_DATE)) == "Up to date"
    assert caption(status(CloudState.AHEAD, ahead=1)) == "Ahead by 1 - Push to publish"
    assert caption(status(CloudState.BEHIND, behind=2)) == "Behind by 2 - Pull to catch up"
    assert caption(status(CloudState.DIVERGED, ahead=1, behind=2)) == (
        "Diverged: 1 to push, 2 to pull"
    )


def test_the_offline_caption_carries_the_reason_and_the_last_known_state():
    """Plan Section 4: the indicator keeps showing what we knew -
    "Offline (last checked 2h ago: Behind)" - because going offline does not erase it."""
    paths = Paths(root=None)
    config = Config(machine_id=MACHINE, repo="owner/vault", default_branch="main")
    checked = CloudStatus(
        state=CloudState.BEHIND, ahead=0, behind=2, checked_at=NOW - timedelta(hours=2)
    )

    def caption(offline, last_status):
        cloud = Cloud(paths=paths, config=config, offline=offline, last_status=last_status)
        return presenter.cloud_caption(cloud, now=NOW)

    lost = OfflineMode(reason=OfflineReason.NO_NETWORK, since=NOW)
    revoked = OfflineMode(reason=OfflineReason.AUTH_FAILED, since=NOW)

    assert caption(lost, checked) == "Offline (no network) - last checked 2h ago: Behind"
    assert caption(revoked, checked) == (
        "Offline (authentication failed) - last checked 2h ago: Behind"
    )
    assert caption(lost, None) == "Offline (no network)"


def test_ages_read_like_a_human_wrote_them():
    assert presenter.age(NOW - timedelta(seconds=30), NOW) == "just now"
    assert presenter.age(NOW - timedelta(minutes=5), NOW) == "5m ago"
    assert presenter.age(NOW - timedelta(hours=2, minutes=10), NOW) == "2h ago"
    assert presenter.age(NOW - timedelta(days=3), NOW) == "3d ago"
