import uuid

import pytest

from core.config import SCHEMA, Config, ConfigTooNew, MachineDescription, load, save
from core.jsonstore import read_json, write_json
from core.paths import Paths


def test_first_load_generates_and_persists_a_machine_id(tmp_path):
    paths = Paths(root=tmp_path)
    config = load(paths)

    uuid.UUID(config.machine_id)  # raises if it is not a real UUID
    assert paths.config_file.exists()
    assert read_json(paths.config_file)["machine_id"] == config.machine_id


def test_the_machine_id_is_stable_across_loads(tmp_path):
    """Identity must never silently change: it is the filename of this Machine's file in
    the Vault, and the one-writer-per-file invariant (ADR-0003) rests on it."""
    paths = Paths(root=tmp_path)
    first = load(paths).machine_id
    second = load(paths).machine_id
    assert first == second


def test_two_machines_get_different_ids(tmp_path):
    a = load(Paths(root=tmp_path / "a")).machine_id
    b = load(Paths(root=tmp_path / "b")).machine_id
    assert a != b


def test_a_fresh_config_is_not_set_up(tmp_path):
    assert load(Paths(root=tmp_path)).is_set_up is False


def test_config_is_set_up_once_a_cloud_vault_is_known(tmp_path):
    paths = Paths(root=tmp_path)
    config = load(paths)
    config.repo = "EdwardRusli/save-vault"
    save(paths, config)

    assert load(paths).is_set_up is True


def test_settings_round_trip(tmp_path):
    paths = Paths(root=tmp_path)
    config = load(paths)
    config.repo = "owner/name"
    config.default_branch = "main"
    config.backup_retention = 3
    save(paths, config)

    reloaded = load(paths)
    assert reloaded.repo == "owner/name"
    assert reloaded.default_branch == "main"
    assert reloaded.backup_retention == 3


def test_a_newer_schema_is_refused_rather_than_misread(tmp_path):
    """An old build meeting a newer config must stop, not silently drop fields it cannot see."""
    paths = Paths(root=tmp_path)
    write_json(paths.config_file, {"machine_id": "x", "schema": SCHEMA + 1})

    with pytest.raises(ConfigTooNew):
        load(paths)


def test_unknown_fields_are_ignored_rather_than_crashing(tmp_path):
    paths = Paths(root=tmp_path)
    write_json(paths.config_file, {"machine_id": "x", "schema": SCHEMA, "from_the_future": 1})

    assert load(paths).machine_id == "x"


def test_identity_is_a_uuid_and_nothing_else(tmp_path):
    """Hostname and OS are display attributes, recomputed each launch - not identity. MAC
    addresses are not used at all: they change with docking, VPNs, and randomization."""
    fields = set(Config.__dataclass_fields__)
    assert "machine_id" in fields
    assert fields.isdisjoint({"hostname", "os_name", "mac", "mac_address"})


def test_machine_description_is_detected_at_runtime():
    described = MachineDescription.detect()
    assert described.hostname
    assert described.os_name
