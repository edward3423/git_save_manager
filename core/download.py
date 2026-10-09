"""Download: a plain copy of an Entry's Vault content, dropped into an ordinary folder.

The copy is the Vault's version at HEAD - the same bytes whether or not this Machine has
bound the Entry - and once written it is outside the application's care: no Binding, no
Baseline, no Ledger row, nothing that Sync or Restore will ever look at again.

## Without binding (ADR-0002)

An Unlinked Entry's file contents are usually not on this Machine at all: the clone is
blobless, and only bound Entries are checked out. Binding would fetch them by widening the
sparse cone, but that *is* binding. So instead the missing blobs are listed straight from
the trees (`rev-list --missing=print`, which never fetches) and fetched in one batch - the
very command Git's own lazy fetch runs, minus its one-round-trip-per-file habit. The sparse
cone, the working tree and the index are never touched. A bound Entry's blobs are already
held, so its Download needs no network at all.

## Never overwrite

Nothing that already exists at the destination is replaced: a taken name gets a numbered
sibling (`Hades (1)`), as a browser does. The content is unpacked into a hidden staging
directory beside the destination first and renamed into place only when complete, so an
interrupted Download never leaves a half-written folder under the real name.
"""

from __future__ import annotations

import shutil
import tarfile
import tempfile
from pathlib import Path

from core import entries, vault
from core.git import NETWORK_TIMEOUT, Git, GitError, GitTimeout
from core.paths import Paths

FETCH_BATCH = 400
"""Object ids per fetch: well inside Windows' 32K command-line limit at 41 bytes apiece."""

STAGING_PREFIX = ".gsm-download-"


class NothingToDownload(Exception):
    """The Vault holds no content for this Entry: never Synced, or removed from the Vault."""


class FetchFailed(Exception):
    """The content had to come from GitHub, and could not: no network, or a refused token."""


class NeedsSignIn(Exception):
    """Some of the content is not on this Machine yet, and there is no token to fetch it with."""


def download(paths: Paths, entry_id: str, destination_dir: Path, pat: str | None) -> Path:
    """Copy the Entry's Vault content into `destination_dir`, and return where it landed."""
    entry = entries.require(paths, entry_id)
    repo = vault.git(paths)
    if (
        entry.content_name is None
        or not repo.run("ls-tree", "HEAD", "--", f"entries/{entry_id}/{entry.content_name}").strip()
    ):
        raise NothingToDownload(
            f"The Vault holds nothing for {entry.name} yet: no Machine has Synced it."
        )

    missing = missing_blobs(repo, entry_id)
    if missing:
        if pat is None:
            raise NeedsSignIn(
                f"{entry.name} has not been downloaded to this Machine before, so it has to "
                "come from GitHub. Sign in first."
            )
        try:
            fetch_blobs(repo, missing, pat)
        except (GitError, GitTimeout) as error:
            raise FetchFailed(
                f"Could not get {entry.name} from GitHub. Check the connection, or use Sign In "
                "if this Machine's sign-in has been revoked."
            ) from error

    destination_dir.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=STAGING_PREFIX, dir=destination_dir))
    try:
        archive = staging / "content.tar"
        repo.run(
            "archive",
            "--format=tar",
            f"--output={archive}",
            f"HEAD:entries/{entry_id}",
            "--",
            entry.content_name,
        )
        unpacked = staging / "content"
        with tarfile.open(archive) as tar:
            # `data` refuses absolute paths, links out of the tree, and device files.
            tar.extractall(unpacked, filter="data")
        target = free_name(destination_dir, entry.content_name)
        (unpacked / entry.content_name).rename(target)
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    return target


def missing_blobs(repo: Git, entry_id: str) -> list[str]:
    """The Entry's blobs this clone does not hold. Reads trees only; fetches nothing."""
    listed = repo.run("rev-list", "--objects", "--missing=print", f"HEAD:entries/{entry_id}")
    return [line[1:] for line in listed.splitlines() if line.startswith("?")]


def fetch_blobs(repo: Git, oids: list[str], pat: str) -> None:
    """Fetch exactly these objects from the Cloud Vault into the promisor pack.

    The same invocation Git's lazy fetch makes internally (`promisor-remote.c`), batched:
    `noop` negotiation because we want named objects, not history, and the filter so the
    pack stays a promisor pack like everything else the blobless clone holds.
    """
    for start in range(0, len(oids), FETCH_BATCH):
        repo.run(
            "fetch",
            "-q",
            "origin",
            "--no-tags",
            "--no-write-fetch-head",
            "--recurse-submodules=no",
            "--filter=blob:none",
            *oids[start : start + FETCH_BATCH],
            config=("fetch.negotiationAlgorithm=noop",),
            pat=pat,
            timeout=NETWORK_TIMEOUT,
        )


def free_name(directory: Path, name: str) -> Path:
    """`directory/name`, or the first `name (n)` not yet taken - a file keeps its suffix."""
    candidate = directory / name
    stem, suffix = (Path(name).stem, Path(name).suffix) if "." in name[1:] else (name, "")
    number = 1
    while candidate.exists() or candidate.is_symlink():
        candidate = directory / f"{stem} ({number}){suffix}"
        number += 1
    return candidate
