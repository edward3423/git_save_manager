"""The Entry sidecar: `entries/<uuid>.json`, committed, shared by every Machine.

An Entry's **identity** is a UUID and its **Display Name** is just a label (ADR-0004). The
UUID is the directory name; the name lives in here, where renaming it is a one-line commit
that moves no data and breaks no Binding.

The sidecar is a **sibling** of the content directory, never inside it. Restoring copies the
content directory into the game's save folder verbatim, so anything we keep in there gets
injected into the game's save folder on every single restore (Invariant 5).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from core.jsonstore import read_json, write_json
from core.paths import Paths

SCHEMA = 1


class UnknownEntry(KeyError):
    """No Entry with that id exists in the Vault."""


@dataclass
class Entry:
    entry_id: str
    name: str
    schema: int = SCHEMA

    def to_dict(self) -> dict[str, Any]:
        return {"schema": self.schema, "name": self.name}


def new_id() -> str:
    """A fresh Entry identity.

    A UUID rather than a slug of the name: names contain characters Windows will not put in a
    path, collide case-insensitively on macOS while Git treats them as distinct, and change.
    """
    return str(uuid.uuid4())


def read(paths: Paths, entry_id: str) -> Entry | None:
    data = read_json(paths.entry_sidecar(entry_id))
    if data is None:
        return None
    return Entry(entry_id=entry_id, name=data.get("name", entry_id), schema=data.get("schema", 0))


def require(paths: Paths, entry_id: str) -> Entry:
    entry = read(paths, entry_id)
    if entry is None:
        raise UnknownEntry(entry_id)
    return entry


def write(paths: Paths, entry: Entry) -> None:
    write_json(paths.entry_sidecar(entry.entry_id), entry.to_dict())


def list_all(paths: Paths) -> list[Entry]:
    """Every Entry in the Vault, whether or not this Machine has bound it.

    The sidecars are pinned into the sparse checkout unconditionally (`vault.SIDECAR_PIN`),
    so an Unlinked Entry is always visible and always bindable - even on a Machine that has
    downloaded no save data at all.
    """
    if not paths.entries_dir.is_dir():
        return []

    found = [
        entry
        for path in sorted(paths.entries_dir.glob("*.json"))
        if (entry := read(paths, path.stem)) is not None
    ]
    return sorted(found, key=lambda e: e.name.casefold())
