"""What this Machine remembers: `data/ledger.json`.

The Ledger holds, for each Entry bound on this Machine, its **Binding** (where the Live Save
lives here) and its **Baseline** (the content hash at the last completed Sync). Local,
never committed, never shared - a Binding is a fact about *this* Machine's disk, and the
Baseline is a fact about what *this* Machine last did.

The Baseline is the single most dangerous value in the application. Set it wrongly and the
state machine will confidently recommend overwriting a save. So the guarantee is made
structural, not documentary: **`record_sync` is the only method that writes a new Baseline**,
and it is named after the only operation permitted to (Invariant 6). One other method,
`repair_baseline`, restores a Baseline the crash recovery path proved was lost - it cannot
invent one, only heal one, and it is named so that an auditor finds both in a single grep.

Nothing else in the codebase may assign to `Binding.baseline`. In particular, Push and Pull
never touch it: they move commits between the Vault and the Cloud Vault, and the Baseline
records what moved between the Live Save and the Vault (ADR-0001).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from core import entries, entry_state
from core.entries import Entry
from core.entry_state import EntryStatus
from core.hashing import HashCache, file_set_hash
from core.jsonstore import read_json, write_json
from core.paths import Paths

SCHEMA = 2
"""2 added File Set Bindings (`files`)."""


class SyncDirection(StrEnum):
    TO_VAULT = "to_vault"
    TO_LIVE = "to_live"


class LedgerTooNew(Exception):
    """The Ledger was written by a newer build. Refuse rather than misread a Baseline."""


class NotBound(KeyError):
    """No Binding for this Entry on this Machine, so there is nothing to act on."""


@dataclass
class Binding:
    """One Entry, as this Machine knows it."""

    entry_id: str
    live_path: str | None = None
    """Where a file or folder Entry lives here. `None` for a File Set."""

    files: dict[str, str] | None = None
    """Where each member of a File Set lives here: member name -> path. `None` otherwise.

    A member missing from this map is not bound on this Machine yet - typically one another
    Machine added to the set since. Such an Entry Needs Binding and is not synced until it is.
    """

    baseline: str | None = None
    """Content hash at the last completed Sync. `None` until one completes."""

    last_sync_at: str | None = None  # ISO 8601, UTC. Display only.
    last_sync_direction: str | None = None  # Display only.

    @property
    def live(self) -> Path:
        """The bound path of a file or folder Entry. Not meaningful for a File Set."""
        if self.live_path is None:
            raise TypeError(f"{self.entry_id} is a File Set; it has no single live path")
        return Path(self.live_path)

    @property
    def published(self) -> str | dict[str, str]:
        """What this Binding looks like in the Machine's published file."""
        return dict(self.files) if self.files is not None else str(self.live_path)


def normalize_live_path(live_path: Path | str) -> str:
    """Expand `~`, but never resolve symlinks.

    `Path.resolve()` would follow them, and games' save folders are routinely symlinked to
    another drive. Resolving would silently rebind the Entry to wherever the link happened
    to point on the day it was bound, so that re-pointing the link later would strand the
    Binding on the old target.
    """
    return str(Path(live_path).expanduser())


@dataclass
class Ledger:
    bindings: dict[str, Binding] = field(default_factory=dict)
    schema: int = SCHEMA

    def get(self, entry_id: str) -> Binding | None:
        return self.bindings.get(entry_id)

    def require(self, entry_id: str) -> Binding:
        binding = self.bindings.get(entry_id)
        if binding is None:
            raise NotBound(entry_id)
        return binding

    def is_bound(self, entry_id: str) -> bool:
        return entry_id in self.bindings

    def bind(self, entry_id: str, live_path: Path | str) -> Binding:
        """Bind an Entry to a path on this Machine.

        Deliberately leaves the Baseline `None`: binding moves no data, so it has nothing to
        record. The state machine reads that `None` correctly - it compares the two sides and
        either finds them equal (In Sync, Baseline healed on the spot) or asks for a
        direction. Setting a Baseline here would be a lie about a Sync that never happened.
        """
        binding = Binding(entry_id=entry_id, live_path=normalize_live_path(live_path))
        self.bindings[entry_id] = binding
        return binding

    def bind_files(self, entry_id: str, files: Mapping[str, Path | str]) -> Binding:
        """Bind a File Set: each member to a path on this Machine. Like `bind`, no Baseline."""
        binding = Binding(
            entry_id=entry_id,
            files={name: normalize_live_path(path) for name, path in files.items()},
        )
        self.bindings[entry_id] = binding
        return binding

    def set_member(self, entry_id: str, name: str, live_path: Path | str) -> Binding:
        """Bind one member of a bound File Set, keeping its Baseline.

        The Baseline survives because it is still true: it records what the set held at the
        last Sync, and binding a path for one more member moves no data. If the new member's
        content differs from the Vault's, the state machine says so from that Baseline.
        """
        binding = self.require(entry_id)
        if binding.files is None:
            raise TypeError(f"{entry_id} is not a File Set")
        binding.files[name] = normalize_live_path(live_path)
        return binding

    def drop_member(self, entry_id: str, name: str) -> None:
        """Forget where one member lives here. Touches no file."""
        binding = self.get(entry_id)
        if binding is not None and binding.files is not None:
            binding.files.pop(name, None)

    def to_file_set(self, entry_id: str, member: str) -> Binding:
        """Turn a single-file Binding into a File Set Binding of that one member, keeping its
        Baseline: the hash of a one-member File Set *is* the single-file hash
        (`hashing.file_set_hash`), so the record is exactly as true afterwards."""
        binding = self.require(entry_id)
        binding.files = {member: str(binding.live_path)}
        binding.live_path = None
        return binding

    def unbind(self, entry_id: str) -> None:
        """Forget the Entry on this Machine. Touches no Live Save and no Vault content."""
        self.bindings.pop(entry_id, None)

    def record_sync(
        self,
        entry_id: str,
        baseline: str,
        direction: SyncDirection,
        at: datetime | None = None,
    ) -> Binding:
        """Record a **completed** Sync. The only writer of a new Baseline (Invariant 6).

        Call this after the data has landed and not one moment sooner: a Baseline written
        before the copy finishes describes a state that does not exist, and a crash then
        leaves the app certain it is In Sync when it is not. Written afterwards, the same
        crash leaves a *stale* Baseline, which the equality short-circuit heals for free.
        """
        binding = self.require(entry_id)
        binding.baseline = baseline
        binding.last_sync_at = (at or datetime.now(UTC)).isoformat()
        binding.last_sync_direction = direction.value
        return binding

    def repair_baseline(self, entry_id: str, baseline: str) -> Binding:
        """Heal a Baseline lost to a crash, to a value the two sides *already* agree on.

        Not an exception to Invariant 6 so much as its recovery path: it is only ever called
        with `EntryStatus.baseline_repair`, which the state machine sets only when Live and
        Vault are identical - the post-condition of a Sync that provably completed. It
        restores a record; it cannot create one.
        """
        binding = self.require(entry_id)
        binding.baseline = baseline
        return binding

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "bindings": {eid: asdict(b) for eid, b in self.bindings.items()},
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Ledger:
        known = set(Binding.__dataclass_fields__)
        bindings = {
            entry_id: Binding(**{k: v for k, v in raw.items() if k in known})
            for entry_id, raw in data.get("bindings", {}).items()
        }
        return cls(bindings=bindings, schema=data.get("schema", SCHEMA))


def load(paths: Paths) -> Ledger:
    """Load the Ledger, or an empty one if there is none yet.

    A *corrupt* Ledger is not silently replaced with an empty one: `read_json` raises, and we
    let it. An empty Ledger would make every Entry look never-synced, which is recoverable,
    but it would also silently discard every Binding the user set up - and we do not destroy
    the user's state to avoid showing them an error.
    """
    data = read_json(paths.ledger_file)
    if data is None:
        return Ledger()

    found = data.get("schema", 0)
    if found > SCHEMA:
        raise LedgerTooNew(
            f"{paths.ledger_file} has schema {found}; this build understands {SCHEMA}. "
            "Update the application."
        )
    return Ledger.from_dict(data)


def save(paths: Paths, ledger: Ledger) -> None:
    write_json(paths.ledger_file, ledger.to_dict())


def refresh(
    paths: Paths,
    ledger: Ledger,
    entry_id: str,
    cache: HashCache | None = None,
) -> EntryStatus:
    """Derive an Entry's current state, healing a lost Baseline if one is found.

    The single place the repair is applied, so that no caller can forget to: a state machine
    that reported In Sync but left the stale Baseline on disk would report a false Conflict
    again on the very next launch.
    """
    binding = ledger.require(entry_id)
    entry = entries.read(paths, entry_id)
    if entry is not None and entry.is_file_set:
        # A member that is missing, or not bound here, makes the whole Live side absent: a
        # File Set short of one file is a missing save, never a smaller one (ADR-0006).
        status = entry_state.evaluate(
            live=file_set_hash(live_members(entry, binding), cache, complete=True)
            if not unbound_members(entry, binding)
            else None,
            vault=file_set_hash(entries.vault_members(paths, entry), cache),
            baseline=binding.baseline,
        )
    else:
        vault_path = (
            entries.content_path(paths, entry)
            if entry is not None
            else paths.entry_content_dir(entry_id)
        )
        status = entry_state.evaluate_entry(
            live_path=binding.live,
            vault_path=vault_path,
            baseline=binding.baseline,
            cache=cache,
        )

    if status.baseline_repair is not None:
        ledger.repair_baseline(entry_id, status.baseline_repair)
        save(paths, ledger)

    return status


def live_members(entry: Entry, binding: Binding) -> dict[str, Path]:
    """A File Set's members as bound on this Machine: member name -> path. Members the set no
    longer has are left out; members not bound here are missing (see `unbound_members`).

    A Binding made before the Entry became a File Set (on another Machine) holds one plain
    path. It is read as the member of the same name - the name a single-file Entry is stored
    under - and as nothing at all otherwise, so it can never be taken for a different file.
    """
    if binding.files is not None:
        bound = binding.files
    elif binding.live_path is not None:
        bound = {Path(binding.live_path).name: binding.live_path}
    else:
        bound = {}
    return {name: Path(bound[name]) for name in entry.files or () if name in bound}


def unbound_members(entry: Entry, binding: Binding | None) -> list[str]:
    """The File Set members this Machine has no path for. Empty for any other Entry."""
    if not entry.is_file_set:
        return []
    bound = live_members(entry, binding) if binding is not None else {}
    return [name for name in entry.files or () if name not in bound]


def live_layout(entry: Entry, binding: Binding) -> Path | dict[str, Path]:
    """Where the Entry's Live Save is on this Machine, in the shape the write path takes."""
    return live_members(entry, binding) if entry.is_file_set else binding.live
