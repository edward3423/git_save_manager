"""Where everything lives on disk.

All runtime state sits under a single gitignored `data/` root. The Vault is a *sibling*
of the Ledger, journal, and backups, never their parent: local state must survive the
Vault being deleted or re-cloned, and the Vault must contain no ignored files so that
`git clean` in the recovery path is harmless (ADR-0005).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent


class WorkspaceNotWritable(Exception):
    """The workspace cannot be written to, so the app must not start.

    Failing at the start of an operation is safe; failing in the middle is what the
    journal exists to clean up. We would rather refuse to launch.
    """


@dataclass(frozen=True)
class Paths:
    """The `data/` layout, rooted anywhere. Tests point it at a temp directory."""

    root: Path

    @classmethod
    def default(cls) -> Paths:
        return cls(root=PROJECT_ROOT)

    @property
    def data_dir(self) -> Path:
        return self.root / "data"

    @property
    def config_file(self) -> Path:
        return self.data_dir / "config.json"

    @property
    def ledger_file(self) -> Path:
        return self.data_dir / "ledger.json"

    @property
    def journal_file(self) -> Path:
        return self.data_dir / "journal.json"

    @property
    def lock_file(self) -> Path:
        return self.data_dir / "app.lock"

    @property
    def backups_dir(self) -> Path:
        return self.data_dir / "backups"

    @property
    def vault_dir(self) -> Path:
        return self.data_dir / "vault"

    # The committed Vault layout. Defined here, once, so that the state machine, the Git
    # driver, and the sparse-checkout set can never disagree about where an Entry lives.

    @property
    def vault_marker(self) -> Path:
        """`vault.json` - what makes a repo a Vault rather than an unrelated project."""
        return self.vault_dir / "vault.json"

    @property
    def entries_dir(self) -> Path:
        return self.vault_dir / "entries"

    @property
    def machines_dir(self) -> Path:
        return self.vault_dir / "machines"

    def entry_content_dir(self, entry_id: str) -> Path:
        """An Entry's save data, and nothing else (Invariant 5): restoring copies it verbatim."""
        return self.entries_dir / entry_id

    def entry_sidecar(self, entry_id: str) -> Path:
        """The Entry's metadata, a *sibling* of its content, never inside it (ADR-0004)."""
        return self.entries_dir / f"{entry_id}.json"

    def machine_file(self, machine_id: str) -> Path:
        """This Machine's published file. One writer, so it can never merge-conflict (ADR-0003)."""
        return self.machines_dir / f"{machine_id}.json"


def check_writable(paths: Paths) -> None:
    """Raise `WorkspaceNotWritable` unless we can actually create `data/` and write in it.

    Probes by writing a real file rather than consulting permission bits, which lie under
    ACLs, read-only mounts, and sandboxes.
    """
    try:
        paths.data_dir.mkdir(parents=True, exist_ok=True)
        probe = paths.data_dir / ".write-probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
    except OSError as exc:
        raise WorkspaceNotWritable(
            f"Cannot write to {paths.data_dir}. Move the application somewhere writable."
        ) from exc


def is_root_user() -> bool:
    """True when file permissions are not enforced against us (used to skip a test)."""
    return hasattr(os, "geteuid") and os.geteuid() == 0
