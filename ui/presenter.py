"""What the window shows, computed away from Qt so it can be tested headless.

The window renders; this module decides. Rows, captions, and ages are derived here from the
same core calls every operation uses, so the sidebar can never disagree with the state
machine about what an Entry is.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import PurePath, PurePosixPath, PureWindowsPath

from core import entries, ledger, operations, redo, vault
from core.cloud import Cloud, CloudState
from core.config import Config
from core.entry_state import Action, EntryState, EntryStatus
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


# --- filtering the sidebar, and the bulk actions a filter unlocks -------------------------------


BULK_ACTIONS = {
    EntryState.LOCAL_AHEAD: (Action.SYNC_TO_VAULT, "Push All to Vault"),
    EntryState.VAULT_AHEAD: (Action.RESTORE_TO_LIVE, "Pull All from Vault"),
}
"""The two states worth filtering to, and the one action each offers for every Entry at once.

Only these two states qualify. A bulk action is safe exactly when the state names which side
moved and every Entry in it wants the same thing done - which is true of Local Ahead and Vault
Ahead and of nothing else. Conflict needs a human per Entry, In Sync has nothing to do, and the
three absence states exist precisely to stop a sweep from propagating a deletion.
"""


def visible_rows(rows: list[Row], states: set[EntryState]) -> list[Row]:
    """The rows a filter leaves on show. An empty filter means everything, unfiltered.

    Unlinked Entries drop out of any filter: without a Binding there is no state to match on.
    That does not contradict `UNLINKED` - an Unlinked Entry stays visible by default, and only
    a filter the user switched on themselves can hide it.
    """
    if not states:
        return list(rows)
    return [row for row in rows if row.status is not None and row.status.state in states]


def bulk_targets(rows: list[Row], action: Action) -> list[Row]:
    """Of these rows, the ones whose own state still offers this action.

    The state machine is asked per Entry rather than trusted per filter. A row can be Local
    Ahead and still not offer Sync - `_guard` withdraws it the moment the Live Save has no
    content - and a bulk action that assumed otherwise would be the one place in the app that
    propagates an absence.
    """
    return [row for row in rows if row.status is not None and action in row.status.offered]


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


def bulk_preview_lines(previews: list[tuple[str, Preview]]) -> list[str]:
    """Invariant 7 across a whole selection: every Entry a Pull All touches, named, with the
    same per-Entry rendering the single-Entry dialog shows.

    One approval covers the sweep, so the sweep has to show everything the sweep will do. The
    per-Entry lines come from `preview_lines` rather than a second renderer, so a bulk approval
    can never describe a write differently from the dialog that approves it one at a time.
    """
    found = [f"Pull {len(previews)} Entr{'y' if len(previews) == 1 else 'ies'} from the Vault."]
    for name, preview in previews:
        found.append("")
        found.append(f"{name}:")
        found += [f"  {line}" if line else "" for line in preview_lines(preview)]
    return found


def rollback_lines(
    name: str, short_sha: str, changes: list[operations.RollbackChange]
) -> list[str]:
    """The Rollback confirmation, as text: every file the Vault copy would change, and the
    reassurances that make Rollback safe - no Live Save is touched, and nothing is rewritten.

    Sizes are omitted (the engine diffs history, not a working tree); the paths are enough.
    """
    found = [f"This changes the Vault copy of {name} to {short_sha}:"]
    found += [f"  {CHANGE_WORDS[change.change]}  {change.path}" for change in changes]
    found.append("")
    found.append(
        "No Live Save is touched, and nothing is rewritten: the version you roll away from "
        "stays in history. The Entry will read Vault Ahead; restoring it is the next step."
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


def bind_file_name(paths: Paths, entry_id: str) -> str | None:
    """The file name to bind under, when this Entry's save lives as a single file.

    Read from the Vault content it already holds, so a save that lives as one file (a lone
    `.sav`, a `settings.ini`) lets the Bind dialog ask only for the containing folder and
    append this name itself. That is the name every other Machine's copy uses, so deriving it
    rules out binding to a path the game will not read.

    None means the shape is not known to be a single file - an Entry never Synced anywhere, or
    one whose content is a folder - and the dialog keeps asking folder-or-file.
    """
    entry = entries.read(paths, entry_id)
    if entry is None or entry.content_name is None:
        return None
    if not vault.content_is_file(paths, entry_id, entry.content_name):
        return None
    return entry.content_name


@dataclass(frozen=True)
class BindHint:
    """Where one *other* Machine keeps a save - read-only, never acted on by itself.

    The path is the string that Machine published, on its own disk and in its own OS's
    spelling. The only thing derived from it is a *suggestion* for this Machine's Bind
    dialog, which the user still confirms.
    """

    hostname: str
    os_name: str
    path: str

    @property
    def machine(self) -> str:
        return f"{self.hostname} ({self.os_name})"

    @property
    def label(self) -> str:
        return f"{self.machine}: {self.path}"

    def suggested_folder(self, file_name: str | None) -> str:
        """The text to pre-fill in the Bind dialog's folder box when reusing this Binding.

        A single-file Entry publishes the file's full path, but the dialog asks for the
        containing folder, so the last component is dropped. The published spelling is
        kept: a Windows path is parsed as one even when read on a POSIX Machine, since the
        common case for reuse is a second Machine of the same OS.
        """
        pure = _pure_path(self.path)
        if file_name and pure.name == file_name:
            return str(pure.parent)
        return str(pure)


def _pure_path(text: str) -> PurePath:
    looks_windows = "\\" in text or re.match(r"^[A-Za-z]:", text) is not None
    return PureWindowsPath(text) if looks_windows else PurePosixPath(text)


def bind_hints(paths: Paths, config: Config, entry_id: str) -> list[BindHint]:
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
                BindHint(
                    hostname=machine.get("hostname", "?"),
                    os_name=machine.get("os", "?"),
                    path=str(published[entry_id]),
                )
            )
    return found
