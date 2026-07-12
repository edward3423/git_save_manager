import os

import pytest

from core.hashing import (
    HashCache,
    hash_directory,
    hash_entry,
    hash_entry_if_exists,
    hash_file,
)


def make_tree(root, files: dict[str, str]):
    """Create a directory tree from {relative path: contents}."""
    root.mkdir(parents=True, exist_ok=True)
    for relative, contents in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(contents, encoding="utf-8")
    return root


# --- the property everything else depends on: same data, same hash --------------------


def test_identical_trees_in_different_locations_hash_the_same(tmp_path):
    """The Live Save and its copy in the Vault live at different paths. If this were false,
    an Entry could never reach In Sync."""
    files = {"slot1.sav": "boss killed", "meta/profile.json": "{}"}
    a = make_tree(tmp_path / "live", files)
    b = make_tree(tmp_path / "vault", files)

    assert hash_entry(a) == hash_entry(b)


def test_changed_content_changes_the_hash(tmp_path):
    a = make_tree(tmp_path / "a", {"slot1.sav": "before"})
    b = make_tree(tmp_path / "b", {"slot1.sav": "after"})

    assert hash_entry(a) != hash_entry(b)


def test_a_renamed_file_changes_the_hash(tmp_path):
    """Paths are part of the digest: moving a save between slots is a change."""
    a = make_tree(tmp_path / "a", {"slot1.sav": "data"})
    b = make_tree(tmp_path / "b", {"slot2.sav": "data"})

    assert hash_entry(a) != hash_entry(b)


def test_an_added_file_changes_the_hash(tmp_path):
    a = make_tree(tmp_path / "a", {"slot1.sav": "data"})
    b = make_tree(tmp_path / "b", {"slot1.sav": "data", "slot2.sav": "more"})

    assert hash_entry(a) != hash_entry(b)


def test_hashing_is_stable_across_repeated_calls(tmp_path):
    tree = make_tree(tmp_path / "a", {"slot1.sav": "data", "b/c.sav": "more"})
    assert hash_entry(tree) == hash_entry(tree)


def test_content_moved_between_files_does_not_collide(tmp_path):
    """Length-prefixing the names stops two different trees concatenating to the same bytes."""
    a = make_tree(tmp_path / "a", {"ab": "1", "c": "23"})
    b = make_tree(tmp_path / "b", {"a": "1", "bc": "23"})

    assert hash_entry(a) != hash_entry(b)


# --- the hash may only describe what the Vault can carry -------------------------------


def test_empty_directories_are_ignored(tmp_path):
    """Git cannot store an empty directory. If the hash counted it, a Live Save holding one
    would differ from its own faithful copy in the Vault forever, and In Sync would be
    unreachable. The documented cost: an empty directory does not survive the round trip."""
    without = make_tree(tmp_path / "without", {"slot1.sav": "data"})
    with_empty = make_tree(tmp_path / "with", {"slot1.sav": "data"})
    (with_empty / "screenshots").mkdir()

    assert hash_entry(without) == hash_entry(with_empty)


@pytest.mark.skipif(os.name == "nt", reason="permission bits are not meaningful on Windows")
def test_the_executable_bit_is_ignored(tmp_path):
    """Git does not reliably carry file modes across platforms; counting them would strand
    an Entry as permanently changed after a clone on another OS."""
    a = make_tree(tmp_path / "a", {"run.sh": "#!/bin/sh"})
    b = make_tree(tmp_path / "b", {"run.sh": "#!/bin/sh"})
    (b / "run.sh").chmod(0o755)

    assert hash_entry(a) == hash_entry(b)


# --- symlinks --------------------------------------------------------------------------


@pytest.mark.skipif(os.name == "nt", reason="symlinks need privileges on Windows")
def test_a_symlink_hashes_as_its_target_and_is_not_followed(tmp_path):
    outside = tmp_path / "outside.sav"
    outside.write_text("data outside the entry", encoding="utf-8")

    tree = make_tree(tmp_path / "entry", {"real.sav": "data"})
    (tree / "link.sav").symlink_to(outside)

    digest = hash_entry(tree)  # must not read `outside`, must not raise

    outside.write_text("changed outside the entry", encoding="utf-8")
    assert hash_entry(tree) == digest  # following the link would have changed this


@pytest.mark.skipif(os.name == "nt", reason="symlinks need privileges on Windows")
def test_repointing_a_symlink_changes_the_hash(tmp_path):
    tree = make_tree(tmp_path / "entry", {"a.sav": "1", "b.sav": "2"})
    link = tree / "current.sav"

    link.symlink_to(tree / "a.sav")
    pointing_at_a = hash_entry(tree)

    link.unlink()
    link.symlink_to(tree / "b.sav")

    assert hash_entry(tree) != pointing_at_a


# --- files, directories, and absence ---------------------------------------------------


def test_a_file_and_a_directory_never_collide(tmp_path):
    single = tmp_path / "solo.sav"
    single.write_text("data", encoding="utf-8")
    tree = make_tree(tmp_path / "tree", {"solo.sav": "data"})

    assert hash_entry(single) != hash_entry(tree)


def test_a_single_file_entry_hashes(tmp_path):
    path = tmp_path / "settings.ini"
    path.write_text("volume=11", encoding="utf-8")

    assert hash_entry(path) == hash_entry(path)


def test_an_empty_directory_has_a_stable_hash(tmp_path):
    a = tmp_path / "a"
    b = tmp_path / "b"
    a.mkdir()
    b.mkdir()

    assert hash_directory(a) == hash_directory(b)


def test_an_absent_path_hashes_to_none(tmp_path):
    """Absence is a real state - a bound Entry whose Live Save does not exist yet - not an
    error to be raised at the state machine."""
    assert hash_entry_if_exists(tmp_path / "not-here") is None


def test_a_present_path_hashes_to_a_digest(tmp_path):
    tree = make_tree(tmp_path / "a", {"slot1.sav": "data"})
    assert hash_entry_if_exists(tree) == hash_entry(tree)


# --- the cache -------------------------------------------------------------------------


def test_the_cache_returns_the_same_hash_as_an_uncached_read(tmp_path):
    tree = make_tree(tmp_path / "a", {"slot1.sav": "data", "b/c.sav": "more"})
    assert hash_entry(tree, HashCache()) == hash_entry(tree)


def test_the_cache_is_invalidated_when_a_file_changes(tmp_path):
    cache = HashCache()
    path = tmp_path / "slot1.sav"
    path.write_text("before", encoding="utf-8")
    before = hash_file(path, cache)

    path.write_text("after and longer", encoding="utf-8")

    assert hash_file(path, cache) != before


def test_the_cache_actually_avoids_re_reading(tmp_path):
    """A cache that never hits is just slow. Proven by mutating content behind its back:
    same size, same mtime, so the key is unchanged and the stale digest is returned."""
    cache = HashCache()
    path = tmp_path / "slot1.sav"
    path.write_text("aaaa", encoding="utf-8")
    first = hash_file(path, cache)

    stat = path.stat()
    path.write_text("bbbb", encoding="utf-8")  # identical size
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns))  # identical mtime

    assert hash_file(path, cache) == first  # served from cache
    assert hash_file(path) != first  # the truth, read fresh


def test_an_uncached_read_is_always_fresh(tmp_path):
    """The Sync path passes no cache, so its stable-read guard observes the bytes as they
    are right now. If this were false, the guard could not detect a save being written."""
    path = tmp_path / "slot1.sav"
    path.write_text("aaaa", encoding="utf-8")
    first = hash_file(path)

    stat = path.stat()
    path.write_text("bbbb", encoding="utf-8")
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns))

    assert hash_file(path) != first


def test_clearing_the_cache_forces_a_re_read(tmp_path):
    cache = HashCache()
    path = tmp_path / "slot1.sav"
    path.write_text("aaaa", encoding="utf-8")
    first = hash_file(path, cache)

    stat = path.stat()
    path.write_text("bbbb", encoding="utf-8")
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns))
    cache.clear()

    assert hash_file(path, cache) != first
