"""What the window shows, computed away from Qt so it can be tested headless.

The window renders; this module decides. Rows, captions, and ages are derived here from the
same core calls every operation uses, so the sidebar can never disagree with the state
machine about what an Entry is.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from core import entries, ledger, redo, vault
from core.cloud import Cloud, CloudState
from core.config import Config
from core.entry_state import EntryState, EntryStatus
from core.ledger import Ledger
from core.paths import Paths
from core.transaction import Change, Preview

STATE_CAPTIONS = {
    EntryState.IN_SYNC: "In Sync",
    EntryState.LOCAL_AHEAD: "Local Ahead",
    EntryState.VAULT_AHEAD: "Vault Ahead",
    EntryState.CONFLICT: "Conflict",
    EntryState.LIVE_SAVE_MISSING: "Live Save Missing",
    EntryState.REMOVED_FROM_VAULT: "Removed from Vault",
    EntryState.NO_CONTENT: "No Content",
}

UNLINKED = "Unlinked"
"""Visible but unsyncable: hiding an Unlinked Entry would conceal that Vault data exists."""


@dataclass(frozen=True)
class Row:
    """One sidebar line: an Entry, and what can be said about it on this Machine."""

    entry_id: str
    name: str
    caption: str
    status: EntryStatus | None
    """None when Unlinked: without a Binding there is no live path to derive a state from."""


def rows(paths: Paths, the_ledger: Ledger) -> list[Row]:
    """Every Entry in the Vault, bound or not, in the sidebar's order."""
    found = []
    for entry in entries.list_all(paths):
        if entry.entry_id in the_ledger.bindings:
            status = ledger.refresh(paths, the_ledger, entry.entry_id)
            caption = STATE_CAPTIONS[status.state]
        else:
            status = None
            caption = UNLINKED
        found.append(Row(entry_id=entry.entry_id, name=entry.name, caption=caption, status=status))
    return found


def age(then: datetime, now: datetime) -> str:
    """`2h ago`, for the human reading an indicator, not a log."""
    seconds = int((now - then).total_seconds())
    if seconds < 60:
        return "just now"
    if seconds < 3600:
        return f"{seconds // 60}m ago"
    if seconds < 86400:
        return f"{seconds // 3600}h ago"
    return f"{seconds // 86400}d ago"


def cloud_caption(cloud: Cloud, now: datetime) -> str:
    """The status indicator, including the sticky Offline Mode with its reason.

    Going offline keeps the last known Cloud state on show - `Offline (no network) - last
    checked 2h ago: Behind` - because "we cannot check" is not evidence anything changed.
    """
    last = cloud.last_status

    if cloud.offline is not None:
        reason = {
            "no_network": "no network",
            "auth_failed": "authentication failed",
        }[cloud.offline.reason.value]
        found = f"Offline ({reason})"
        if last is not None:
            state = STATE_WORDS[last.state]
            found += f" - last checked {age(last.checked_at, now)}: {state}"
        return found

    if last is None:
        return "Cloud: not checked yet"

    if last.state is CloudState.UP_TO_DATE:
        return "Up to date"
    if last.state is CloudState.AHEAD:
        return f"Ahead by {last.ahead} - Push to publish"
    if last.state is CloudState.BEHIND:
        return f"Behind by {last.behind} - Pull to catch up"
    return f"Diverged: {last.ahead} to push, {last.behind} to pull"


STATE_WORDS = {
    CloudState.UP_TO_DATE: "Up to date",
    CloudState.AHEAD: "Ahead",
    CloudState.BEHIND: "Behind",
    CloudState.DIVERGED: "Diverged",
}


# --- the preview, rendered (Invariant 7) --------------------------------------------------------


def size_text(size_bytes: int) -> str:
    """`2.0 KB`, for a human weighing an operation, not a disk."""
    if size_bytes < 1024:
        return f"{size_bytes} B"
    for unit in ("KB", "MB", "GB", "TB"):
        size_bytes /= 1024
        if size_bytes < 1024 or unit == "TB":
            return f"{size_bytes:.1f} {unit}"
    raise AssertionError("unreachable")


CHANGE_WORDS = {
    Change.ADD: "+ add      ",
    Change.REPLACE: "~ overwrite",
    Change.REMOVE: "- delete   ",
}


def preview_lines(preview: Preview) -> list[str]:
    """Every path a write would touch, and where the Backup lands - Invariant 7, as text.

    One renderer for every destructive dialog: Restore, conflict resolution toward the
    Vault, and Backup restore all show exactly this.
    """
    if preview.is_noop:
        return [
            f"{preview.live_path} already holds exactly this content.",
            "Nothing will be written, and no Backup is needed.",
        ]

    found = [f"This will write to {preview.live_path}:"]
    found += [
        f"  {CHANGE_WORDS[change.change]}  {change.path}  ({size_text(change.size_bytes)})"
        for change in preview.changes
    ]
    found.append("")
    found.append(
        "The current Live Save is archived to a Backup first."
        if preview.will_back_up
        else "No Backup is taken: the Live Save holds no content to archive."
    )
    return found


def redo_lines(plan: redo.Plan) -> list[str]:
    """The Redo Initialization confirmation: every path and keyring entry, enumerated -
    and what is never touched, said out loud."""
    found = ["This will permanently delete:"]
    found += [f"  - {path}" for path in plan.deletions]
    found += [f"  - {entry}" for entry in plan.keyring_entries]
    found += ["", "Never touched: every Backup in backups/, and every Live Save."]
    return found


def bind_hints(paths: Paths, config: Config, entry_id: str) -> list[str]:
    """Where the *other* Machines keep this save - read-only, never acted on.

    Shown in the Bind dialog so the user on a second Machine can see 'the laptop keeps this
    at C:\\...' while choosing a folder here. This Machine's own Binding is not a hint.
    """
    found = []
    for machine in vault.list_machines(paths):
        if machine.get("machine_id") == config.machine_id:
            continue
        published = machine.get("bindings", {})
        if entry_id in published:
            found.append(
                f"{machine.get('hostname', '?')} ({machine.get('os', '?')}): {published[entry_id]}"
            )
    return found
