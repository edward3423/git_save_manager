"""What the user actually does: Add, Sync, Restore, Resolve, Rollback, Remove.

Every verb in the application lives here, and each one begins by re-establishing Invariant 2
(the Vault is clean) rather than trusting the last one to have tidied up - because an
operation that crashed cannot be trusted to have tidied up, that being what crashing means.

## Two directions, and only one of them writes a Baseline

Sync moves data **Live -> Vault**. Restore moves it **Vault -> Live**. Both move data between
the Live Save and the Vault, so both complete by recording a Baseline (Invariant 6). Nothing
else here does: renaming an Entry moves no data, and neither does restoring a Backup - which
overwrites the Live Save from a *zip*, not from the Vault, and therefore correctly leaves the
Entry reading Local Ahead afterwards.

Restore and conflict-resolution-toward-the-Vault do not write a single byte of the Live Save
themselves. They hand a `Source` to `transaction.write_live`, which is the only code in the
application permitted to touch a save file, and which backs it up, journals it, and swaps it
atomically. Rollback does not write a Live Save at all: it makes a forward commit in the
Vault, which lands the Entry in Vault Ahead, and restoring it is then the ordinary Restore.
One overwrite path, exercised by everything.

## The stable-read guard

A game may be writing its save at the very moment we copy it. The guard is three hashes:

    H1 = hash(Live)                 # before the copy
    copy Live -> the Vault working tree
    H3 = hash(the copy)             # what we actually captured
    H2 = hash(Live)                 # after the copy
    require H1 == H2 == H3

`H1 == H2` proves the source did not change *for exactly the interval of the copy* - so no
artificial delay is used, or wanted: the copy **is** the window under test, and a sleep would
only test an interval in which nothing was happening. `H3 == H1` proves the copy is faithful,
which catches a torn read from a full disk or an I/O error, not merely a running game.

On failure nothing is committed: `ensure_clean` returns the working tree to HEAD, and the user
is told to close the game. Retry is offered but **never automatic** - a running game would
simply spin forever.
"""

from __future__ import annotations

import contextlib
import copy
import shutil
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from core import backups, entries, ledger, transaction, vault
from core.backups import Backup
from core.config import MAX_SINGLE_FILE_BYTES, Config, MachineDescription
from core.entries import Entry
from core.hashing import content_hash, file_set_hash
from core.ledger import Ledger, SyncDirection
from core.logger import log
from core.paths import Paths
from core.transaction import BackupSource, Preview, TreeSource, Written
from core.vault import FileTooLarge, git

RESTORE = "restore"
CONFLICT_VAULT = "conflict-vault"
BACKUP_RESTORE = "backup-restore"


class SyncAborted(Exception):
    """The Live Save changed while we were copying it, so nothing was committed.

    Almost always a running game. Not an error to retry automatically: the game would still be
    running, and we would spin.
    """


class NothingToSync(Exception):
    """The Live Save holds no content. Syncing would commit its absence and empty the Entry."""


class InvalidFileSet(Exception):
    """The files chosen for a File Set cannot be one: a folder among them, two files that
    would share a name in the Vault, or the same file twice."""


class NeedsBinding(Exception):
    """A File Set member has no path on this Machine yet, so the Entry cannot be moved."""


class GitRepoNotAllowed(Exception):
    """The chosen folder is itself a Git repository, which the Vault cannot store: Git collapses
    a repository nested inside it to an empty submodule reference rather than tracking its files."""


# --- commits carry the Machine that made them ---------------------------------------------


def _commit(
    paths: Paths,
    config: Config,
    description: MachineDescription,
    message: str,
    body: str | None = None,
) -> str:
    """Commit the staged tree. `message` is the structured machine subject; `body`, when given,
    is the user's own note, carried as the commit body - a second `-m` git renders as
    `subject\\n\\nbody`, leaving the scannable subject line untouched."""
    repo = git(paths)
    extra = ["-m", body] if body else []
    repo.run("commit", "-m", message, *extra, config=vault.commit_identity(config, description))
    return repo.run("rev-parse", "HEAD").strip()


def _publish_bindings(
    paths: Paths, config: Config, description: MachineDescription, the_ledger: Ledger
) -> None:
    """Rewrite and stage this Machine's published file so it matches the Ledger.

    Published means committed: the caller commits. One writer per file (ADR-0003), so this
    can never merge-conflict with anything another Machine publishes.
    """
    published = {entry_id: binding.published for entry_id, binding in the_ledger.bindings.items()}
    vault.write_machine_file(paths, config, description, published)
    git(paths).run("add", "-A", "--", "machines")


def _stage(paths: Paths, entry_id: str) -> bool:
    """Stage an Entry's content and sidecar. True if anything actually changed.

    `git add` fails outright on a pathspec that matches *nothing* - not in the working tree and
    not in the index - and a brand-new Entry has a sidecar but no content directory yet. So the
    pathspecs are filtered down to those that exist or are tracked. A tracked path that has
    been deleted stays in the list, which is what stages the deletion.
    """
    repo = git(paths)
    specs = [f"entries/{entry_id}", f"entries/{entry_id}.json"]

    tracked = repo.run("ls-files", "--", *specs).split()
    present = [
        spec
        for spec in specs
        if (paths.vault_dir / spec).exists()
        or any(path == spec or path.startswith(f"{spec}/") for path in tracked)
    ]
    if not present:
        return False

    repo.run("add", "-A", "--", *present)
    return repo.run("diff", "--cached", "--name-only").strip() != ""


# --- adding, renaming, removing --------------------------------------------------------------


def add_entry(
    paths: Paths,
    config: Config,
    description: MachineDescription,
    the_ledger: Ledger,
    name: str,
    live_path: Path,
    max_file_bytes: int = MAX_SINGLE_FILE_BYTES,
) -> Entry:
    """Create an Entry, bind it to a path on this Machine, and commit its sidecar.

    The sidecar is committed immediately rather than left lying in the working tree, because
    an uncommitted file in the Vault is a breach of Invariant 2 that the next operation would
    dutifully clean away - taking the new Entry with it.

    No content is committed here. The Entry is born with a Vault holding nothing and no
    Baseline, which the state machine reads as Local Ahead: the first Sync is the user's.
    """
    vault.ensure_clean(paths)

    target = transaction.resolve_target(live_path)
    if target.is_dir() and (target / ".git").exists():
        raise GitRepoNotAllowed(
            f"{target.name} is a Git repository - it contains a .git. The Vault keeps saves in "
            "Git, and Git cannot store a repository inside itself: it would be reduced to an "
            "empty submodule reference, and none of the files would be saved. Choose a folder "
            "that is not a repository, or point at the specific files you want to keep."
        )
    oversized = vault.oversized_files(target, limit=max_file_bytes)
    if oversized:
        biggest = oversized[0]
        raise FileTooLarge(
            f"{biggest.path.name} is {biggest.size_bytes / 1_048_576:.0f} MB. GitHub rejects "
            "anything over 100 MB, and a commit it rejects can never leave the Vault."
        )

    entry = Entry(entry_id=entries.new_id(), name=name, content_name=_content_name(live_path))
    entries.write(paths, entry)

    the_ledger.bind(entry.entry_id, live_path)
    ledger.save(paths, the_ledger)
    vault.set_sparse(paths, the_ledger.bindings)

    _stage(paths, entry.entry_id)
    _publish_bindings(paths, config, description, the_ledger)
    _commit(paths, config, description, f"add({name}): from {description.hostname}")

    log().info("Added %s, bound to %s", name, live_path)
    return entry


def _check_file_set(
    files: list[tuple[str, Path]], max_file_bytes: int = MAX_SINGLE_FILE_BYTES
) -> None:
    """Refuse a File Set that cannot be stored faithfully. A missing file is fine - the program
    may not have written it yet - but a folder, a shared name, or one file twice is not.

    Pairs rather than a mapping, so that two files of the same name can be seen to collide.
    """
    if not files:
        raise InvalidFileSet("A File Set needs at least one file.")

    seen_names: list[str] = []
    seen_targets: dict[Path, Path] = {}
    for name, live in files:
        if any(entries.same_name(other, name) for other in seen_names):
            raise InvalidFileSet(
                f"Two files would both be stored as {name}. The files of one Entry need "
                "distinct names; put the other in an Entry of its own."
            )
        seen_names.append(name)

        target = transaction.resolve_target(live)
        if target.is_dir():
            raise InvalidFileSet(
                f"{live} is a folder. An Entry of several files holds individual files only; "
                "add the folder as an Entry of its own."
            )
        if target in seen_targets:
            raise InvalidFileSet(f"{live} is the same file as {seen_targets[target]}.")
        seen_targets[target] = live

        oversized = vault.oversized_files(target, limit=max_file_bytes)
        if oversized:
            raise FileTooLarge(
                f"{live.name} is {oversized[0].size_bytes / 1_048_576:.0f} MB. GitHub rejects "
                "anything over 100 MB, and a commit it rejects can never leave the Vault."
            )


def add_file_set(
    paths: Paths,
    config: Config,
    description: MachineDescription,
    the_ledger: Ledger,
    name: str,
    files: list[Path],
    max_file_bytes: int = MAX_SINGLE_FILE_BYTES,
) -> Entry:
    """Create an Entry of several individual files, bind each here, and commit its sidecar.

    Exactly `add_entry` for more than one path: no content is committed, and the first Sync
    is the user's.
    """
    vault.ensure_clean(paths)

    # Each file is stored under its own name - the name of the link, for a symlinked
    # dotfile, since that is the name the user picked and the one their programs read.
    pairs = [(path.name, path) for path in files]
    _check_file_set(pairs, max_file_bytes)
    named = dict(pairs)

    entry = Entry(entry_id=entries.new_id(), name=name, files=list(named))
    entries.write(paths, entry)

    the_ledger.bind_files(entry.entry_id, named)
    ledger.save(paths, the_ledger)
    vault.set_sparse(paths, the_ledger.bindings)

    _stage(paths, entry.entry_id)
    _publish_bindings(paths, config, description, the_ledger)
    _commit(paths, config, description, f"add({name}): from {description.hostname}")

    log().info("Added %s, %d files: %s", name, len(named), ", ".join(map(str, files)))
    return entry


def can_hold_files(paths: Paths, the_ledger: Ledger, entry_id: str) -> bool:
    """Whether files can be added to this Entry here: it is a File Set, or a single file that
    would become one. Never a folder Entry, whose hash a second member would change."""
    entry = entries.read(paths, entry_id)
    binding = the_ledger.get(entry_id)
    if entry is None or binding is None:
        return False
    if entry.is_file_set:
        return True
    if entry.content_name is not None:
        return vault.content_is_file(paths, entry_id, entry.content_name)
    return transaction.resolve_target(binding.live).is_file()


def add_file(
    paths: Paths,
    config: Config,
    description: MachineDescription,
    the_ledger: Ledger,
    entry_id: str,
    live_path: Path,
    max_file_bytes: int = MAX_SINGLE_FILE_BYTES,
) -> str:
    """Add one more file to an Entry, bound here, and commit the new membership.

    A single-file Entry becomes a File Set of two. Its stored file keeps its name and place in
    the Vault (`entries/<id>/<name>`) and its hash is unchanged, so its history and Baseline
    carry straight over. No content is copied: the Entry reads Local Ahead, and the next Sync
    captures the new file alongside the others.

    Returns the new member's name.
    """
    vault.ensure_clean(paths)
    entry = entries.require(paths, entry_id)
    binding = the_ledger.require(entry_id)
    if not can_hold_files(paths, the_ledger, entry_id):
        raise InvalidFileSet(f"{entry.name} is a folder; files can only join a file.")

    if entry.is_file_set:
        members = list(entry.files or ())
        current = ledger.live_members(entry, binding)
    else:
        # A single file becomes a File Set of one, under the name it is already stored as.
        only = entry.content_name or transaction.resolve_target(binding.live).name
        members = [only]
        current = {only: binding.live}

    name = live_path.name
    if any(entries.same_name(member, name) for member in members):
        raise InvalidFileSet(
            f"{entry.name} already has a file named {name}. The files of one Entry need "
            "distinct names; put this one in an Entry of its own."
        )
    _check_file_set([*current.items(), (name, live_path)], max_file_bytes)

    with _ledger_restored_on_failure(paths, the_ledger, entry_id):
        if not entry.is_file_set:
            the_ledger.to_file_set(entry_id, members[0])
        entry.content_name = None
        entry.files = [*members, name]
        entries.write(paths, entry)
        the_ledger.set_member(entry_id, name, live_path)

        _stage(paths, entry_id)
        _publish_bindings(paths, config, description, the_ledger)
        _commit(
            paths,
            config,
            description,
            f"add-file({entry.name}): {name} from {description.hostname}",
        )
    ledger.save(paths, the_ledger)

    log().info("Added %s to %s. Sync to capture it.", live_path, entry.name)
    return name


@contextlib.contextmanager
def _ledger_restored_on_failure(paths: Paths, the_ledger: Ledger, entry_id: str):
    """Make a membership change all-or-nothing. The Ledger is only saved after the commit
    lands; if anything fails first, the Vault is returned to HEAD and this Entry's Binding to
    what it was, so the Ledger never describes a File Set the Vault does not hold."""
    before = copy.deepcopy(the_ledger.get(entry_id))
    try:
        yield
    except BaseException:
        if before is not None:
            the_ledger.bindings[entry_id] = before
        vault.ensure_clean(paths)
        raise


def remove_file(
    paths: Paths,
    config: Config,
    description: MachineDescription,
    the_ledger: Ledger,
    entry_id: str,
    member: str,
) -> None:
    """Take one file out of a File Set, for every Machine, as a forward commit.

    Its stored copy leaves the Vault (and stays in history); **the file itself is not touched**
    here or on any other Machine (Invariant 1). The last file cannot be removed this way - that
    is Remove from Vault, which says what it means.
    """
    vault.ensure_clean(paths)
    entry = entries.require(paths, entry_id)
    if not entry.is_file_set or member not in (entry.files or ()):
        raise InvalidFileSet(f"{member} is not one of the files of {entry.name}.")
    if len(entry.files or ()) < 2:
        raise InvalidFileSet(
            f"{member} is the only file of {entry.name}. Use Remove from Vault to remove the Entry."
        )

    with _ledger_restored_on_failure(paths, the_ledger, entry_id):
        git(paths).run("rm", "-q", "--ignore-unmatch", "--", f"entries/{entry_id}/{member}")
        entry.files = [name for name in entry.files or () if name != member]
        entries.write(paths, entry)
        the_ledger.drop_member(entry_id, member)

        _stage(paths, entry_id)
        _publish_bindings(paths, config, description, the_ledger)
        _commit(
            paths,
            config,
            description,
            f"remove-file({entry.name}): {member} from {description.hostname}",
        )
    ledger.save(paths, the_ledger)

    log().info("Removed %s from %s. The file itself was not touched.", member, entry.name)


def rename_entry(
    paths: Paths,
    config: Config,
    description: MachineDescription,
    entry_id: str,
    name: str,
) -> Entry:
    """Rename an Entry. Moves no data, breaks no Binding, touches no Baseline: identity is the
    UUID, and the name is only ever a label (ADR-0004)."""
    vault.ensure_clean(paths)

    entry = entries.require(paths, entry_id)
    was = entry.name
    entry.name = name
    entries.write(paths, entry)

    if _stage(paths, entry_id):
        _commit(paths, config, description, f"rename({was} -> {name}): from {description.hostname}")

    return entry


def adopt_bindings(
    paths: Paths,
    the_ledger: Ledger,
    published: dict[str, str | dict[str, str]],
    pat: str | None = None,
) -> None:
    """Re-bind everything an adopted identity had published (see `github.bootstrap`).

    Local only: the published file already carries these Bindings, so nothing needs a
    commit. No Baselines are written - the content just arrived from the Cloud, and each
    Entry reads Vault Ahead (or In Sync, where the Live Save already matches) from there.
    """
    for entry_id, live in published.items():
        if isinstance(live, dict):
            the_ledger.bind_files(entry_id, live)
        else:
            the_ledger.bind(entry_id, Path(live))
    ledger.save(paths, the_ledger)
    vault.set_sparse(paths, the_ledger.bindings, pat=pat)
    log().info("Adopted %d published Binding(s).", len(published))


def bind_entry(
    paths: Paths,
    config: Config,
    description: MachineDescription,
    the_ledger: Ledger,
    entry_id: str,
    live_path: Path | Mapping[str, Path],
    pat: str | None = None,
) -> None:
    """Bind an Unlinked Entry to a path on this Machine, publish it, and widen the cone.

    A File Set is bound member by member: `live_path` maps member names to paths. For a File
    Set this Machine already binds - one that Needs Binding because another Machine added a
    file - only the given members are (re)bound and the Baseline is kept: it is still a true
    record of the last Sync, and nothing has moved.

    No Baseline is written: a Binding is not a Sync, and adopting one here would claim a
    Sync that never happened. The Entry surfaces as Vault Ahead or Local Ahead (or Conflict,
    when both sides hold different content), and the first data movement is the user's.
    """
    vault.ensure_clean(paths)
    entry = entries.require(paths, entry_id)

    if isinstance(live_path, Mapping):
        if not entry.is_file_set or set(live_path) - set(entry.files or ()):
            raise InvalidFileSet(f"Those are not the files of {entry.name}.")
        existing = the_ledger.get(entry_id)
        if existing is not None and existing.files is not None:
            _check_file_set([*{**ledger.live_members(entry, existing), **live_path}.items()])
            for name, path in live_path.items():
                the_ledger.set_member(entry_id, name, path)
        else:
            _check_file_set(list(live_path.items()))
            the_ledger.bind_files(entry_id, live_path)
        shown = ", ".join(map(str, live_path.values()))
    else:
        the_ledger.bind(entry_id, live_path)
        shown = str(live_path)
    ledger.save(paths, the_ledger)
    vault.set_sparse(paths, the_ledger.bindings, pat=pat)

    _publish_bindings(paths, config, description, the_ledger)
    # The published file may already say exactly this - binding back to the very paths this
    # Machine published before - and Git refuses an empty commit.
    if git(paths).run("diff", "--cached", "--name-only").strip():
        _commit(paths, config, description, f"bind({entry.name}): from {description.hostname}")
    log().info("Bound %s to %s", entry.name, shown)


def unbind_entry(
    paths: Paths,
    config: Config,
    description: MachineDescription,
    the_ledger: Ledger,
    entry_id: str,
) -> None:
    """Drop the Binding and Baseline on this Machine only. The Entry returns to Unlinked.

    The Vault keeps the Entry and its history, other Machines are unaffected, and the Live
    Save is not touched (Invariant 1). Fully reversible: binding it again is `bind_entry`.
    The one commit made here holds no save data - it retracts the Binding from this
    Machine's published file, because published means committed.
    """
    vault.ensure_clean(paths)
    entry = entries.require(paths, entry_id)

    the_ledger.unbind(entry_id)
    ledger.save(paths, the_ledger)
    vault.set_sparse(paths, the_ledger.bindings)

    _publish_bindings(paths, config, description, the_ledger)
    _commit(paths, config, description, f"unbind({entry.name}): from {description.hostname}")
    log().info("Unbound %s. The Live Save was not touched.", entry.name)


def remove_from_vault(
    paths: Paths,
    config: Config,
    description: MachineDescription,
    the_ledger: Ledger,
    entry_id: str,
) -> None:
    """Remove an Entry from the Vault, for every Machine, on their next Pull.

    A forward commit, so the content stays recoverable from history. **No Live Save anywhere
    is touched** - not this Machine's, and not any other's: the other Machines will simply see
    the Entry as Removed from Vault and be offered unbind or re-add.
    """
    vault.ensure_clean(paths)
    entry = entries.require(paths, entry_id)
    repo = git(paths)

    repo.run("rm", "-r", "-q", "--ignore-unmatch", "--", f"entries/{entry_id}")
    repo.run("rm", "-q", "--ignore-unmatch", "--", f"entries/{entry_id}.json")

    the_ledger.unbind(entry_id)
    ledger.save(paths, the_ledger)
    _publish_bindings(paths, config, description, the_ledger)

    _commit(paths, config, description, f"remove({entry.name}): from {description.hostname}")
    vault.set_sparse(paths, the_ledger.bindings)

    log().info("Removed %s from the Vault. No Live Save was touched.", entry.name)


# --- Sync: Live -> Vault ---------------------------------------------------------------------


@dataclass(frozen=True)
class Synced:
    entry_id: str
    baseline: str
    commit: str | None
    """None when the Vault already held exactly this content, so there was nothing to commit."""


def _copy_into_vault(live: Path, content_dir: Path) -> None:
    """Replace the Entry's Vault content with the Live Save, verbatim.

    `content_dir` (`entries/<id>`) is always a directory, and the bound item lands *inside* it
    under its own name: `entries/<id>/Skyrim/...` for a folder, `entries/<id>/settings.ini` for
    a file. So the Vault mirrors what the user pointed at, and the wrapper is a directory the
    sparse cone (ADR-0002) can select - a bare file at `entries/<id>` is one it would refuse.

    A symlinked save *folder* is followed - the user bound the folder it leads to - while
    symlinks *inside* the save are copied as links, exactly as the hashing scheme records
    them. Anything else would make the copy hash differently from its source, and the
    stable-read guard would reject a copy that is in fact perfectly faithful.
    """
    source = transaction.resolve_target(live)

    # Reset the Entry directory to hold exactly one child, the bound item under its own name.
    if content_dir.is_dir() and not content_dir.is_symlink():
        shutil.rmtree(content_dir)
    else:
        content_dir.unlink(missing_ok=True)
    content_dir.mkdir(parents=True, exist_ok=True)

    inner = content_dir / source.name
    if source.is_dir() and not source.is_symlink():
        shutil.copytree(source, inner, symlinks=True)
    else:
        shutil.copy2(source, inner, follow_symlinks=False)


def _content_name(live: Path) -> str | None:
    """The basename to store the bound item under, `<name>` in `entries/<id>/<name>` - the
    file's name, or the folder's. A Live Save that does not exist yet reads as `None` and is
    corrected at the first Sync, which is the moment its name is first knowable."""
    target = transaction.resolve_target(live)
    return target.name if target.exists() else None


def _content_source(paths: Paths, entry: Entry) -> TreeSource:
    """The Vault content a Restore writes back over the Live Save: the bound file or folder
    itself, at `entries/<id>/<content_name>` - never the sparse-cone wrapper around it. For a
    File Set, the members, each to be written to its own path."""
    if entry.is_file_set:
        return TreeSource(paths.entry_content_dir(entry.entry_id), tuple(entry.files or ()))
    return TreeSource(entries.content_path(paths, entry))


def _live(the_ledger: Ledger, entry: Entry) -> Path | dict[str, Path]:
    """Where the Entry's Live Save is on this Machine - refusing a File Set with a member this
    Machine has not bound, since moving the rest would treat the set as smaller than it is."""
    binding = the_ledger.require(entry.entry_id)
    unbound = ledger.unbound_members(entry, binding)
    if unbound:
        raise NeedsBinding(
            f"{entry.name} has {', '.join(unbound)}, which has no path on this Machine yet. "
            "Bind it first."
        )
    return ledger.live_layout(entry, binding)


def _copy_members_into_vault(members: Mapping[str, Path], content_dir: Path) -> None:
    """Replace a File Set's Vault content with its members, each stored flat under its member
    name. A symlinked member is followed - the hash reads the file it leads to - so the Vault
    holds that file's bytes, not a link that would dangle on every other Machine."""
    if content_dir.is_dir() and not content_dir.is_symlink():
        shutil.rmtree(content_dir)
    else:
        content_dir.unlink(missing_ok=True)
    content_dir.mkdir(parents=True, exist_ok=True)
    for name, live in members.items():
        shutil.copy2(transaction.resolve_target(live), content_dir / name)


def _sync_file_set(
    paths: Paths, the_ledger: Ledger, entry: Entry
) -> tuple[str, Callable[[], str | None], Callable[[], str | None]]:
    """The three pieces of the stable-read guard for a File Set: H1, then (copy, H3), then H2."""
    _live(the_ledger, entry)  # refuses a member with no path here
    members = ledger.live_members(entry, the_ledger.require(entry.entry_id))
    before = file_set_hash(members, complete=True)
    if before is None:
        missing = [
            str(path) for path in members.values() if not transaction.resolve_target(path).is_file()
        ]
        raise NothingToSync(
            f"{entry.name} cannot be Synced while a file is missing: {', '.join(missing)}. "
            "Syncing would erase it from the Vault. Restore the Entry to bring it back, or "
            "remove the file from the Entry."
        )

    def copy() -> str | None:
        _copy_members_into_vault(members, paths.entry_content_dir(entry.entry_id))
        return file_set_hash(entries.vault_members(paths, entry))

    return before, copy, lambda: file_set_hash(members, complete=True)


def sync_to_vault(
    paths: Paths,
    config: Config,
    description: MachineDescription,
    the_ledger: Ledger,
    entry_id: str,
    note: str | None = None,
) -> Synced:
    """Copy the Live Save into the Vault and commit it. One commit, one Entry, atomically.

    The only writer of a new Baseline, and it writes one only once the commit has landed.

    `note` is the user's optional one-line summary of what this Sync captures ("boss slain",
    "graphics tweaked"). It rides along as the commit body and surfaces in History and the log.
    """
    vault.ensure_clean(paths)

    binding = the_ledger.require(entry_id)
    entry = entries.require(paths, entry_id)

    # No cache anywhere below. The whole point of the guard is to see the bytes as they are.
    if entry.is_file_set:
        before, copy, reread = _sync_file_set(paths, the_ledger, entry)
        shown = entry.name
    else:
        live = binding.live
        shown = str(live)
        before = content_hash(live)
        if before is None:
            raise NothingToSync(
                f"{live} holds no save data. Syncing would commit its absence and empty the "
                "Entry in the Vault. If you meant to remove it, use Remove from Vault."
            )

        def copy() -> str | None:
            _copy_into_vault(live, paths.entry_content_dir(entry_id))
            # The Sync is the moment the Live Save's name and shape are authoritative: record
            # the basename so Restore, here or on any other Machine, rebuilds the right file or
            # folder. The faithfulness check hashes the stored item itself
            # (`entries/<id>/<name>`), which is what must equal the Live Save - never the
            # sparse-cone wrapper around it.
            entry.content_name = _content_name(live)
            return content_hash(entries.content_path(paths, entry))

        def reread() -> str | None:
            return content_hash(live)

    commit = None
    try:
        captured = copy()  # is the copy faithful?
        after = reread()  # did the source hold still while we read it?

        if not (before == after == captured):
            raise SyncAborted(
                f"{shown} changed while it was being copied, so nothing has been committed. "
                "Close the game and try again."
            )

        entries.write(paths, entry)  # persist content_name, staged and committed with the content

        if _stage(paths, entry_id):
            commit = _commit(
                paths,
                config,
                description,
                f"sync({entry.name}): from {description.hostname}",
                body=note,
            )
            log().info("Synced %s to the Vault (%s)", entry.name, commit[:8])
        else:
            log().info("%s already matched the Vault; nothing to commit.", entry.name)

    except BaseException:
        # Anything at all - a stalled game, a torn read, a full disk mid-commit - and the copy
        # goes back where it came from. Without this the Vault is left holding the new save
        # *uncommitted*, which breaches Invariant 2 and, worse, makes the next status refresh
        # read that uncommitted content as though it were the Vault's.
        vault.ensure_clean(paths)
        raise

    # And only now. The Baseline is written strictly *after* the commit has landed, because a
    # Baseline recorded first would survive a failed commit and claim a Sync that did not
    # happen: the Live Save would then match the Baseline while the Vault did not, the app
    # would report Vault Ahead, and it would offer to restore the *old* save over the new one.
    the_ledger.record_sync(entry_id, before, SyncDirection.TO_VAULT)
    ledger.save(paths, the_ledger)

    vault.assert_clean(paths)
    return Synced(entry_id=entry_id, baseline=before, commit=commit)


# --- Restore: Vault -> Live -------------------------------------------------------------------


def preview_restore(paths: Paths, the_ledger: Ledger, entry_id: str, reason: str = RESTORE):
    entry = entries.require(paths, entry_id)
    source = _content_source(paths, entry)
    return transaction.preview(paths, entry_id, _live(the_ledger, entry), source, reason)


def restore_to_live(
    paths: Paths,
    config: Config,
    the_ledger: Ledger,
    entry_id: str,
    approved: Preview | None = None,
    reason: str = RESTORE,
) -> Written:
    """Write the Vault's copy of an Entry over the Live Save.

    Does not touch a save file itself: `transaction.write_live` does, having first archived
    what it is about to destroy and journalled every step.
    """
    vault.ensure_clean(paths)

    entry = entries.require(paths, entry_id)
    live = _live(the_ledger, entry)
    source = _content_source(paths, entry)

    if source.digest() is None:
        raise transaction.LiveParentMissing(
            f"The Vault holds no content for {entry.name}, so there is nothing to restore. "
            "No Live Save has been touched."
        )

    written = transaction.write_live(
        paths, config, entry_id, live, source, reason, approved=approved
    )

    # Live and Vault now hold identical content, which is the definition of a completed Sync.
    the_ledger.record_sync(entry_id, source.digest(), SyncDirection.TO_LIVE)
    ledger.save(paths, the_ledger)

    return written


def resolve_conflict_toward_vault(
    paths: Paths, config: Config, the_ledger: Ledger, entry_id: str, approved: Preview | None = None
) -> Written:
    """Take the Vault's side, whole. Destructive to the Live Save, so it is backed up first."""
    return restore_to_live(paths, config, the_ledger, entry_id, approved, reason=CONFLICT_VAULT)


def resolve_conflict_toward_live(
    paths: Paths,
    config: Config,
    description: MachineDescription,
    the_ledger: Ledger,
    entry_id: str,
) -> Synced:
    """Take the Live Save's side, whole. Non-destructive: the Vault's version stays in history."""
    return sync_to_vault(paths, config, description, the_ledger, entry_id)


def restore_backup(
    paths: Paths,
    config: Config,
    the_ledger: Ledger,
    entry_id: str,
    backup: Backup,
    approved: Preview | None = None,
) -> Written:
    """Write an archived copy of the Live Save back over the Live Save.

    **The Baseline is deliberately not touched.** No data moved between the Live Save and the
    Vault - the content came from a zip - so the Entry correctly reads Local Ahead afterwards,
    and the user still has to decide whether to Sync it. Recording a Baseline here would claim
    a Sync that never happened and leave the app reporting In Sync while the two differ.
    """
    live = _live(the_ledger, entries.require(paths, entry_id))

    return transaction.write_live(
        paths,
        config,
        entry_id,
        live,
        BackupSource(backup),
        BACKUP_RESTORE,
        approved=approved,
    )


def preview_backup_restore(paths: Paths, the_ledger: Ledger, entry_id: str, backup: Backup):
    live = _live(the_ledger, entries.require(paths, entry_id))
    return transaction.preview(paths, entry_id, live, BackupSource(backup), BACKUP_RESTORE)


def list_backups(paths: Paths, entry_id: str) -> list[Backup]:
    return backups.list_for(paths, entry_id)


# --- history and rollback ------------------------------------------------------------------------


@dataclass(frozen=True)
class Commit:
    sha: str
    machine: str
    when: datetime
    subject: str
    body: str = ""
    """The author's own note, if any - a one-line summary of what the Sync captured."""

    @property
    def short(self) -> str:
        return self.sha[:8]


def history(paths: Paths, entry_id: str, limit: int = 100) -> list[Commit]:
    """Every commit that touched this Entry, newest first.

    Deliberately **not** `--follow`: `git log --help` states it "works only for a single
    file", and an Entry is usually a directory. Nothing is lost by its absence - identity is a
    UUID, so an Entry's path never moves and there is no rename to follow.

    Records are NUL-terminated (`-z`) rather than newline-split: the body carries the user's
    note, and a note is free to contain whatever a line-based parse would mistake for the next
    commit.
    """
    raw = git(paths).run(
        "log",
        f"-{limit}",
        "-z",
        "--format=%H%x1f%an%x1f%aI%x1f%s%x1f%b",
        "--",
        f"entries/{entry_id}",
        f"entries/{entry_id}.json",
    )

    found = []
    for record in raw.split("\0"):
        if not record.strip():
            continue
        sha, machine, when, subject, body = record.split("\x1f")
        found.append(
            Commit(
                sha=sha,
                machine=machine,
                when=datetime.fromisoformat(when),
                subject=subject,
                body=body.strip(),
            )
        )
    return found


def unpushed_commits(paths: Paths) -> set[str]:
    """The full SHAs on HEAD the Cloud Vault does not yet have - the local-ahead commits a Push
    would upload.

    `--not --remotes=origin` excludes everything already reachable from any `origin/*` ref, so a
    Vault that has never been pushed (no origin refs at all) correctly reports every commit as
    unpushed, with no special case for the missing tracking ref.
    """
    return set(git(paths).run("rev-list", "HEAD", "--not", "--remotes=origin").split())


@dataclass(frozen=True)
class RollbackChange:
    """One path a rollback would touch in the Vault. Sizes are deliberately omitted: the diff
    is against git history, not a working tree to be measured, and the file list is enough to
    decide."""

    path: str
    change: transaction.Change


# `git diff --name-status` reports the target relative to the *source*, which is exactly this
# rollback's direction (current HEAD -> the chosen commit): a path only in the commit is added,
# a path only in HEAD is deleted, a changed one is overwritten.
_STATUS_CHANGE = {
    "A": transaction.Change.ADD,
    "M": transaction.Change.REPLACE,
    "T": transaction.Change.REPLACE,  # a file became a symlink or vice-versa: still a rewrite
    "D": transaction.Change.REMOVE,
}


def preview_rollback(paths: Paths, entry_id: str, sha: str) -> list[RollbackChange]:
    """Exactly which files a rollback to `sha` would add, overwrite, or delete in the Vault.

    Reads only git history - it stages nothing and touches no working tree, so it is safe to
    call to fill a confirmation dialog. An empty list means the Vault already holds that
    version and the rollback would be a no-op (`rollback` itself makes the same check).
    """
    content = f"entries/{entry_id}"
    raw = git(paths).run("diff", "--no-renames", "--name-status", "HEAD", sha, "--", content)

    # Show paths as the user sees the save - relative to its root, `entries/<id>/<name>/` - so a
    # folder Entry's `saves/slot1.sav` reads as `slot1.sav`, and a file Entry simply as its name.
    # The save-root prefix is tried before the bare wrapper, so folders strip the deeper one.
    entry = entries.read(paths, entry_id)
    roots = [f"{content}/{entry.content_name}/"] if entry and entry.content_name else []
    roots.append(f"{content}/")

    found = []
    for line in raw.splitlines():
        if not line.strip():
            continue
        status, path = line.split("\t", 1)
        change = _STATUS_CHANGE.get(status[:1], transaction.Change.REPLACE)
        shown = next((path[len(root) :] for root in roots if path.startswith(root)), path)
        found.append(RollbackChange(path=shown, change=change))
    return sorted(found, key=lambda c: c.path)


def rollback(
    paths: Paths,
    config: Config,
    description: MachineDescription,
    entry_id: str,
    sha: str,
) -> str:
    """Return an Entry's Vault content to an earlier commit, as a **new commit**.

    Never a reset, never a force-push: the history is append-only (Invariant 3), so the version
    you rolled away from remains in the log and can be rolled back to in turn.

    It writes nothing to the Live Save. It changes the Vault, which lands the Entry in Vault
    Ahead, and restoring it is then the ordinary Restore - so exactly one piece of code in this
    application ever overwrites a save file. Rolling back with unsynced local changes lands in
    Conflict instead, which is correct, and needs no special case.
    """
    vault.ensure_clean(paths)
    entry = entries.require(paths, entry_id)
    repo = git(paths)

    content = f"entries/{entry_id}"
    repo.run("restore", "--source", sha, "--staged", "--worktree", "--", content)
    if entry.is_file_set:
        # The membership is today's, not the old commit's: a file since removed from the set
        # is not brought back by rolling its contents back.
        strays = [
            path
            for path in repo.run("ls-files", "--", content).splitlines()
            if path.removeprefix(f"{content}/") not in (entry.files or ())
        ]
        if strays:
            repo.run("rm", "-q", "-f", "--", *strays)

    if not repo.run("diff", "--cached", "--name-only").strip():
        log().info("%s already holds that version; nothing to roll back.", entry.name)
        return repo.run("rev-parse", "HEAD").strip()

    commit = _commit(
        paths,
        config,
        description,
        f"rollback({entry.name}): to {sha[:8]} from {description.hostname}",
    )
    log().info("Rolled %s back to %s. Restore it to apply the change.", entry.name, sha[:8])
    return commit
