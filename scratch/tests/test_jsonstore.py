import json

import pytest

from core.jsonstore import CorruptJson, read_json, staging_path, write_json


def test_staging_path_is_a_sibling_of_the_destination(tmp_path):
    """An atomic rename only holds within one filesystem, so staging must not leave the dir."""
    target = tmp_path / "nested" / "ledger.json"
    assert staging_path(target).parent == target.parent


def test_write_then_read_round_trips(tmp_path):
    target = tmp_path / "config.json"
    write_json(target, {"machine_id": "abc", "repo": None})
    assert read_json(target) == {"machine_id": "abc", "repo": None}


def test_write_creates_missing_parent_directories(tmp_path):
    target = tmp_path / "data" / "config.json"
    write_json(target, {"a": 1})
    assert target.exists()


def test_write_leaves_no_staging_file_behind(tmp_path):
    target = tmp_path / "config.json"
    write_json(target, {"a": 1})
    assert not staging_path(target).exists()
    assert list(tmp_path.iterdir()) == [target]


def test_overwrite_replaces_content_entirely(tmp_path):
    target = tmp_path / "config.json"
    write_json(target, {"old": True, "stale": "gone"})
    write_json(target, {"new": True})
    assert read_json(target) == {"new": True}


def test_a_failed_write_leaves_the_previous_file_intact(tmp_path):
    """The point of staging: a reader sees the old file or the new one, never a torn one."""
    target = tmp_path / "config.json"
    write_json(target, {"good": True})

    with pytest.raises(TypeError):
        write_json(target, {"bad": object()})  # not JSON-serializable

    assert read_json(target) == {"good": True}


def test_read_missing_file_returns_none(tmp_path):
    assert read_json(tmp_path / "absent.json") is None


def test_read_corrupt_file_raises_rather_than_returning_garbage(tmp_path):
    target = tmp_path / "config.json"
    target.write_text("{not json", encoding="utf-8")
    with pytest.raises(CorruptJson):
        read_json(target)


def test_written_file_is_valid_json_on_disk(tmp_path):
    target = tmp_path / "config.json"
    write_json(target, {"b": 2, "a": 1})
    assert json.loads(target.read_text(encoding="utf-8")) == {"a": 1, "b": 2}
