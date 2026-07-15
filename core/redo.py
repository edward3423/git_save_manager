"""Redo Initialization: wipe what is reconstructible, refuse to destroy what is not.

Everything deleted here can be rebuilt by running setup again: the config (a fresh Machine
identity), the Ledger (Bindings are re-chosen, Baselines re-derived by the next Sync), and
the vault clone (cloned again from the Cloud Vault). Two things are *not* reconstructible
and are therefore never touched:

- **`backups/`** - for Live progress overwritten before it was ever Synced, the zip in
  there is the only copy in existence.
- **Any Live Save** (Invariant 1).

And one refusal: while the Vault holds commits the Cloud does not, deleting the clone would
destroy them permanently - the only place in the whole design where committed content can
vanish. Not a warning - a refusal, with "discard N commits" as a separate, chosen act.
"""

from __future__ import annotations

import os
import shutil
import stat
from dataclasses import dataclass
from pathlib import Path

from core.credentials import CredentialStore
from core.git import GitError
from core.logger import log
from core.paths import Paths
from core.vault import git

PAT_ENTRY = "github-pat (OS keyring)"


class VaultAhead(Exception):
    """Unpushed commits exist on this Machine and nowhere else. Redo refuses to eat them."""


@dataclass(frozen=True)
class Plan:
    """Exactly what a Redo would delete - rendered verbatim in the confirmation dialog."""

    deletions: tuple[Path, ...]
    keyring_entries: tuple[str, ...] = (PAT_ENTRY,)


def plan(paths: Paths) -> Plan:
    """What exists and would go. `backups/` is deliberately absent, and always will be."""
    candidates = (paths.config_file, paths.ledger_file, paths.journal_file, paths.vault_dir)
    return Plan(deletions=tuple(found for found in candidates if found.exists()))


def unpushed_commits(paths: Paths) -> int:
    """Commits on HEAD that the Cloud Vault does not have, counted from local refs only.

    No network: the comparison is against what the last fetch (or push) recorded. A Machine
    that never fetched still has the refs its own clone or push created. Where there is no
    Vault, no upstream, or no commits at all, there is nothing unpushed to protect - a
    wedged half-setup must always be escapable.
    """
    if not (paths.vault_dir / ".git").exists():
        return 0
    try:
        raw = git(paths).run("rev-list", "--count", "HEAD", "--not", "--remotes=origin")
        return int(raw.strip())
    except GitError:
        return 0


def execute(paths: Paths, store: CredentialStore, discard_unpushed: bool = False) -> Plan:
    """Delete everything in the plan, after the one refusal that protects committed content."""
    ahead = unpushed_commits(paths)
    if ahead and not discard_unpushed:
        raise VaultAhead(
            f"The Vault holds {ahead} commit(s) that exist on this Machine and nowhere "
            "else. Push them first - or discard them, as an explicit, separate choice."
        )

    doomed = plan(paths)
    store.delete_pat()

    for found in doomed.deletions:
        if found.is_dir():
            shutil.rmtree(found, onexc=_force_writable)
        else:
            found.unlink(missing_ok=True)

    log().warning(
        "Redo Initialization complete. Deleted: %s. Kept: every Backup, every Live Save.",
        ", ".join(str(found) for found in doomed.deletions) or "nothing (already clean)",
    )
    return doomed


def _force_writable(func, path, _exc):
    """Git marks its object files read-only; on Windows that fails a plain rmtree."""
    os.chmod(path, stat.S_IWRITE)
    func(path)
