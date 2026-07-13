"""Content hashes for Entries. Every state decision in the app rests on these.

An Entry's hash answers one question: *is this the same data?* It is compared against the
Baseline to decide whether the Live Save or the Vault has moved, so a hash that is unstable
across machines, or that reports a difference the Vault cannot actually carry, produces a
wrong answer - which means either a phantom Conflict or, worse, a wrong direction.

Two rules follow, and they are why this module deliberately ignores things:

**The hash may only describe what the Vault can represent.** Git does not store empty
directories, and it does not reliably carry permission bits across Windows and macOS. If
the hash counted either, a Live Save holding an empty folder would hash differently from
its own faithful copy in the Vault - forever. The Entry would sit at "Local Ahead" for all
time, every Sync would appear to do nothing, and In Sync would be unreachable. So empty
directories and file modes are excluded, and the honest cost is documented: an empty
directory does not survive a round trip through the Vault.

**The scheme is versioned.** Baselines are hashes. Change how they are computed and every
stored Baseline silently becomes a lie, so the version is folded into the digest: a scheme
change produces visibly different hashes rather than quietly wrong comparisons.
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass, field
from pathlib import Path

SCHEME = b"gsm-hash-v1"
_CHUNK = 1024 * 1024


@dataclass
class HashCache:
    """Caches file hashes by `(size, mtime_ns)`.

    Used as a *cache key only*, never as evidence of change - the content hash remains the
    single source of truth about whether anything moved.

    Deliberately opt-in, and deliberately not used on the Sync path. Syncing runs a
    stable-read guard (hash source, copy, hash copy, hash source again) whose entire purpose
    is to observe the bytes as they are *right now*, so serving it a cached hash would defeat
    the check it exists to perform. Caching belongs to the status refresh, where the cost is
    re-reading every managed file on every window focus.
    """

    _entries: dict[Path, tuple[int, int, str]] = field(default_factory=dict)

    def get(self, path: Path, stat: os.stat_result) -> str | None:
        cached = self._entries.get(path)
        if cached is None:
            return None
        size, mtime_ns, digest = cached
        if size == stat.st_size and mtime_ns == stat.st_mtime_ns:
            return digest
        return None

    def put(self, path: Path, stat: os.stat_result, digest: str) -> None:
        self._entries[path] = (stat.st_size, stat.st_mtime_ns, digest)

    def clear(self) -> None:
        self._entries.clear()


def hash_file(path: Path, cache: HashCache | None = None) -> str:
    """SHA-256 of one file's bytes. A symlink hashes as its target, and is never followed."""
    stat = path.lstat()

    if cache is not None:
        cached = cache.get(path, stat)
        if cached is not None:
            return cached

    digest = hashlib.sha256()
    if path.is_symlink():
        # Recorded, not followed: following one could walk outside the Entry entirely, and
        # ignoring it would make a changed link invisible to the state machine.
        digest.update(b"symlink:")
        digest.update(os.readlink(path).encode("utf-8"))
    else:
        with path.open("rb") as handle:
            while chunk := handle.read(_CHUNK):
                digest.update(chunk)

    result = digest.hexdigest()
    if cache is not None:
        cache.put(path, stat, result)
    return result


def _walk_files(root: Path) -> list[tuple[str, Path]]:
    """Every file under `root`, as (relative posix path, absolute path), sorted by that path.

    Sorted so the digest does not depend on filesystem walk order, and posix-style so a
    Vault written on Windows hashes identically on macOS.
    """
    found: list[tuple[str, Path]] = []
    for current, dirs, files in os.walk(root, followlinks=False):
        dirs.sort()
        for name in sorted(files):
            absolute = Path(current) / name
            relative = absolute.relative_to(root).as_posix()
            found.append((relative, absolute))
    return sorted(found, key=lambda pair: pair[0])


def _hash_files(files: list[tuple[str, Path]], cache: HashCache | None) -> str:
    digest = hashlib.sha256()
    digest.update(SCHEME)
    digest.update(b"dir")

    for relative, absolute in files:
        encoded = relative.encode("utf-8")
        # Length-prefixed so that no rearrangement of names and contents can collide with a
        # different tree that happens to concatenate to the same bytes.
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)
        digest.update(bytes.fromhex(hash_file(absolute, cache)))

    return digest.hexdigest()


def hash_directory(path: Path, cache: HashCache | None = None) -> str:
    """Composite hash over every file's relative path and content.

    Paths are part of the digest, so moving a save between slots registers as a change.
    Empty directories are not: see the module docstring.
    """
    return _hash_files(_walk_files(path), cache)


def hash_entry(path: Path, cache: HashCache | None = None) -> str:
    """Content hash of an Entry, whether it is a single file or a directory.

    A file and a directory never collide, even with identical bytes: the kind is folded in.
    """
    if path.is_dir() and not path.is_symlink():
        return hash_directory(path, cache)

    digest = hashlib.sha256()
    digest.update(SCHEME)
    digest.update(b"file")
    digest.update(bytes.fromhex(hash_file(path, cache)))
    return digest.hexdigest()


def content_hash(path: Path, cache: HashCache | None = None) -> str | None:
    """The Entry's content hash, or `None` if it holds no content the Vault can represent.

    This is the function the state machine uses, and `None` is a first-class answer, not an
    error: a bound Entry whose Live Save has not been created yet, or an Entry another
    Machine removed from the Vault.

    `None` deliberately covers *two* situations, because the Vault cannot tell them apart:

    1. The path does not exist.
    2. The path is a directory containing no files (however many empty subdirectories).

    Git stores no empty directories, so an Entry whose content directory is empty is simply
    *not there* after a clone. Were these to hash differently, an Entry with no saves in it
    could never be In Sync with its own faithful - and necessarily absent - copy in the
    Vault. They are the same content: none. This is the same rule as the module docstring's,
    applied at the top: the hash may only describe what the Vault can represent.

    The distinction is load-bearing for *safety*, not merely for correctness. `entry_state`
    refuses to Sync a contentless Live Save into the Vault, since that would erase the
    Entry's content there. An uninstalled game that leaves an empty save folder behind must
    not be able to trigger that, and it is empty rather than absent.

    **The Entry's root is followed if it is a symlink; symlinks inside it are not.** These
    look contradictory and are not, because they answer different questions. A symlinked root
    is how the *user* named the Entry - `~/Library/.../Game` pointing at a second drive - and
    they bound the folder it leads to, not the link itself. A symlink *inside* the Entry is
    part of the save data, is stored by Git as a link, and must never be followed: doing so
    would pull content from outside the Entry into its hash and, on Sync, into the Vault.
    (`ledger.normalize_live_path` refuses to resolve the Binding for the same reason from the
    other side: re-pointing the link later must move the Entry with it.)

    A broken root symlink therefore has no content - which is exactly right, and is how the
    unmounted external drive arrives at Live Save Missing rather than at a crash.
    """
    if path.is_symlink():
        path = path.resolve()
    if not path.exists():
        return None
    if path.is_dir():
        files = _walk_files(path)
        if not files:
            return None
        return _hash_files(files, cache)
    return hash_entry(path, cache)
