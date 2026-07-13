"""The Vault: the local Git repository holding every Entry's content.

This module owns the repository's *shape* - what is committed, what is checked out, and the
guarantee that it is clean between operations. The operations that move data through it
(Sync, Restore, Pull, Push, Rollback) build on top.

## Git is the journal (ADR-0005)

The Vault needs no journal of its own, because Git already is one. Anything a half-finished
operation leaves in the working tree is undone by

    git merge --abort   (if a merge was in flight)
    git reset --hard
    git clean -fd       # never -x

and that is `ensure_clean`, asserted before every operation rather than after (Invariant 2).
The `-x` is omitted deliberately and permanently: it would delete *ignored* files, and while
Invariant 8 says the Vault contains none, the entire local state of this application - the
Ledger, the journal, every backup - is one directory away. `-x` is the single flag that turns
a routine cleanup into a catastrophe, and it is never typed.

## Selective sync (ADR-0002)

Cloning is `--filter=blob:none --sparse`: the history arrives, the file *contents* do not,
and then sparse-checkout materializes only the Entries bound on this Machine. A Vault holding
forty games costs, on a laptop that plays four, four games' worth of disk.

The sparse set is expressed in **cone mode**, and it leans on one property of it that is worth
stating out loud because the whole design rests on it: cone mode includes every file lying
*directly in a listed directory's parents*. So listing `entries/<uuid>` pulls down that
Entry's content **and every Entry's sidecar** - which is exactly what we want, since the
sidecars are what let the app show you an Unlinked Entry you might like to bind.

That leaves one hole, and it is a real one: with **zero** Entries bound there is no
`entries/<uuid>` to list, so `entries/` is nobody's parent, and every sidecar disappears -
leaving a freshly set up second Machine showing an empty list and no way to bind anything.
`SIDECAR_PIN` is a directory that deliberately does not exist. Listing it costs nothing,
checks out nothing, and keeps `entries/` in the cone forever.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from core.config import MAX_SINGLE_FILE_BYTES, Config, MachineDescription
from core.git import NETWORK_TIMEOUT, Git
from core.jsonstore import read_json, write_json
from core.paths import Paths

SCHEMA = 1

GITATTRIBUTES = """\
# Save data is binary. Git must never translate line endings in it: a global
# core.autocrlf=true would silently rewrite every save file its heuristics guess is text,
# and the guess is wrong often enough to corrupt them.
* -text

# And it must never attempt a textual three-way merge inside a save. Conflicts are resolved
# at Entry granularity, by taking one side whole (ADR-0001); a merged save is a save that
# never existed on any machine.
/entries/** binary
"""

SIDECAR_PIN = "entries/.sidecars"
"""A directory that does not exist, and must not be created.

Its only job is to keep `entries/` inside the sparse cone when no Entry is bound, so that
every Entry's sidecar stays checked out and the app can offer them for binding. See the
module docstring.
"""


class NotAVault(Exception):
    """The repository is not a Vault. We will not commit save files into someone's project."""


class VaultTooNew(Exception):
    """The Vault was written by a newer build of this application. Refuse rather than corrupt.

    The one failure mode capable of damaging every Entry at once, prevented by one `if`.
    """


class FileTooLarge(Exception):
    """A file is too big for GitHub to accept, and we found out *before* committing it.

    Discovering this at push time leaves a Vault with a commit that can never be pushed, and
    a user who must be talked through a history rewrite to recover.
    """


class VaultDirty(Exception):
    """The working tree was not clean when it should have been (Invariant 2)."""


@dataclass(frozen=True)
class Marker:
    """`vault.json`: what makes a repository a Vault rather than an unrelated project."""

    schema: int = SCHEMA
    vault: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {"vault": self.vault, "schema": self.schema}


def git(paths: Paths) -> Git:
    return Git(work_tree=paths.vault_dir)


def commit_identity(config: Config, description: MachineDescription) -> tuple[str, ...]:
    """Author commits as this Machine, so `git log` answers "who made this?" directly.

    Passed per-invocation with `-c`, never written into `.git/config`.
    """
    return (
        f"user.name={description.hostname}",
        f"user.email={config.machine_id}@gsm.local",
    )


# --- the shape of the repository -----------------------------------------------------------


def read_marker(paths: Paths) -> Marker:
    """Read `vault.json`, refusing a repository that is not a Vault or is too new for us."""
    data = read_json(paths.vault_marker)
    if data is None or not data.get("vault"):
        raise NotAVault(
            f"{paths.vault_dir} does not contain a vault.json marker, so it is not a Vault. "
            "Refusing to touch it."
        )

    schema = data.get("schema", 0)
    if schema > SCHEMA:
        raise VaultTooNew(
            f"This Vault has schema {schema}; this build understands {SCHEMA}. "
            "Update the application before using it, or you risk damaging every Entry."
        )
    return Marker(schema=schema)


def machine_file_contents(config: Config, description: MachineDescription) -> dict[str, Any]:
    """This Machine's published file. Exactly one writer, so it can never conflict (ADR-0003)."""
    return {
        "schema": SCHEMA,
        "machine_id": config.machine_id,
        "hostname": description.hostname,
        "os": description.os_name,
    }


def init_structure(paths: Paths, config: Config, description: MachineDescription) -> None:
    """Write the files that make a fresh repository a Vault. Does not commit."""
    write_json(paths.vault_marker, Marker().to_dict())
    (paths.vault_dir / ".gitattributes").write_text(GITATTRIBUTES, encoding="utf-8")
    write_json(paths.machine_file(config.machine_id), machine_file_contents(config, description))


def register_machine(paths: Paths, config: Config, description: MachineDescription) -> None:
    """Publish this Machine into the Vault. The second-machine path. Does not commit."""
    write_json(paths.machine_file(config.machine_id), machine_file_contents(config, description))


# --- bringing a Vault into being --------------------------------------------------------------


def remote_url(repo: str) -> str:
    """Credential-free, always. The PAT is injected per-invocation and never stored here."""
    return f"https://github.com/{repo}.git"


def initialize(paths: Paths, config: Config, description: MachineDescription, branch: str) -> None:
    """Create a fresh Vault repository locally and write its structure. Does not commit.

    The branch is named explicitly rather than left to `init.defaultBranch`, which varies by
    Git version and by whatever the user has configured.
    """
    paths.vault_dir.mkdir(parents=True, exist_ok=True)
    git(paths).run("init", "-b", branch)
    init_structure(paths, config, description)


def clone(paths: Paths, repo: str, pat: str, entry_ids: Iterable[str] = ()) -> None:
    """Clone the Cloud Vault: history without file contents, then only the bound Entries.

    `--filter=blob:none` fetches every commit but no blob until something asks for one, and
    `--sparse` starts the working tree at root-level files only. `set_sparse` then widens it
    to exactly what this Machine has bound - so the download is proportional to what you play,
    not to what the Vault holds.
    """
    paths.data_dir.mkdir(parents=True, exist_ok=True)

    Git(work_tree=paths.data_dir).run(
        "clone",
        "--filter=blob:none",
        "--sparse",
        remote_url(repo),
        paths.vault_dir.name,
        pat=pat,
        timeout=NETWORK_TIMEOUT,
    )

    read_marker(paths)  # refuse a repository that is not a Vault, before we write to it
    set_sparse(paths, entry_ids)


def current_branch(paths: Paths) -> str:
    """The checked-out branch, including before the first commit exists.

    `rev-parse --abbrev-ref HEAD` is the usual idiom and it is wrong here: it resolves HEAD to
    a commit, so it fails outright on a freshly initialized Vault, which is precisely when we
    need to know the branch in order to make the first one.
    """
    return git(paths).run("branch", "--show-current").strip()


# --- Invariant 2: the Vault is clean between operations --------------------------------------


def is_clean(paths: Paths) -> bool:
    return git(paths).run("status", "--porcelain").strip() == ""


def ensure_clean(paths: Paths) -> None:
    """Return the working tree to HEAD, whatever a previous operation left behind.

    Asserted *before* every operation rather than after. An operation that crashed cannot be
    trusted to have cleaned up after itself - that is what crashing means - so the guarantee
    has to be re-established by the next one that needs it, not promised by the last one.
    """
    repo = git(paths)

    if (paths.vault_dir / ".git" / "MERGE_HEAD").exists():
        repo.run("merge", "--abort", check=False)

    repo.run("reset", "--hard")
    repo.run("clean", "-fd")  # NEVER -x. See the module docstring.


def assert_clean(paths: Paths) -> None:
    """Refuse to proceed with a dirty tree, rather than committing whatever is lying around."""
    if not is_clean(paths):
        raise VaultDirty(
            f"{paths.vault_dir} has uncommitted changes, which should be impossible. "
            "Nothing has been done. Restart the application to have them cleaned up."
        )


# --- selective sync (ADR-0002) ----------------------------------------------------------------


def sparse_directories(entry_ids: Iterable[str]) -> list[str]:
    """The cone: this Machine's Entries, plus everything every Machine needs.

    `machines/` and the sidecars are pinned unconditionally - the sidecars via `SIDECAR_PIN`,
    which keeps `entries/` in the cone even when nothing is bound.
    """
    bound = sorted(f"entries/{entry_id}" for entry_id in entry_ids)
    return ["machines", SIDECAR_PIN, *bound]


def set_sparse(paths: Paths, entry_ids: Iterable[str]) -> None:
    """Materialize exactly the bound Entries, and nothing else.

    Fully reversible: binding an Entry adds a directory to the cone and Git checks it out;
    unbinding removes it and Git deletes the working-tree copy. The history is untouched
    either way, so nothing is ever lost by narrowing the cone.
    """
    git(paths).run("sparse-checkout", "set", *sparse_directories(entry_ids))


# --- size guards: fail early, not at push time ----------------------------------------------


@dataclass(frozen=True)
class TooLarge:
    path: Path
    size_bytes: int


def oversized_files(root: Path, limit: int = MAX_SINGLE_FILE_BYTES) -> list[TooLarge]:
    """Every file GitHub would reject. Checked when an Entry is *added*, not when it is pushed.

    GitHub hard-rejects any file over 100 MB. Finding that out at push time leaves a commit
    in the Vault that can never leave it, and the only way out is a history rewrite.
    """
    found: list[TooLarge] = []
    if not root.exists():
        return found

    candidates = root.rglob("*") if root.is_dir() else [root]
    for path in candidates:
        if path.is_file() and not path.is_symlink():
            size = path.lstat().st_size
            if size > limit:
                found.append(TooLarge(path=path, size_bytes=size))
    return sorted(found, key=lambda f: f.size_bytes, reverse=True)


def vault_size_bytes(paths: Paths) -> int:
    """How much disk the Vault occupies, history included. Shown in the status area."""
    if not paths.vault_dir.exists():
        return 0
    return sum(p.lstat().st_size for p in paths.vault_dir.rglob("*") if p.is_file())
