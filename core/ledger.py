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

from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from core import entries, entry_state
from core.entry_state import EntryStatus
from core.hashing import HashCache
from core.jsonstore import read_json, write_json
from core.paths import Paths

SCHEMA = 1


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
    live_path: str

    baseline: str | None = None
    """Content hash at the last completed Sync. `None` until one completes."""

    last_sync_at: str | None = None  # ISO 8601, UTC. Display only.
    last_sync_direction: str | None = None  # Display only.

    @property
    def live(self) -> Path:
        return Path(self.live_path)


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
