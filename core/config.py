"""This Machine's local configuration: `data/config.json`.

Holds the Machine ID and the Cloud Vault pointer. Never committed, never shared.

The Machine ID is a generated UUID (ADR-0003 depends on it: it is the filename of this
Machine's file in the Vault, and the one-writer-per-file invariant requires that identity
never silently change). Hostname and OS are *display attributes* - recomputed on every
launch, so a renamed machine simply shows its new name - and are deliberately not identity.
MAC addresses are not used at all: they change with docking, VPNs, and per-network
randomization, and are a stable hardware identifier we have no reason to commit to a repo.
"""

from __future__ import annotations

import platform
import socket
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any

from core.jsonstore import read_json, write_json
from core.paths import Paths

SCHEMA = 1

DEFAULT_BACKUP_RETENTION = 10
DEFAULT_VAULT_SIZE_WARN_BYTES = 1_073_741_824  # 1 GiB
MAX_SINGLE_FILE_BYTES = 94_371_840  # ~90 MiB; GitHub hard-rejects anything over 100 MB


@dataclass(frozen=True)
class MachineDescription:
    """How this Machine presents itself. Display only - never identity."""

    hostname: str
    os_name: str

    @classmethod
    def detect(cls) -> MachineDescription:
        return cls(hostname=socket.gethostname(), os_name=platform.system())


@dataclass
class Config:
    machine_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    repo: str | None = None  # "owner/name" of the Cloud Vault
    default_branch: str | None = None  # taken from the remote HEAD, never guessed
    backup_retention: int = DEFAULT_BACKUP_RETENTION
    vault_size_warn_bytes: int = DEFAULT_VAULT_SIZE_WARN_BYTES
    schema: int = SCHEMA

    @property
    def is_set_up(self) -> bool:
        """False until a Cloud Vault is known, which is what triggers first-run setup."""
        return self.repo is not None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Config:
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in data.items() if k in known})


class ConfigTooNew(Exception):
    """The config was written by a newer build. Refuse rather than misread it."""


def load(paths: Paths) -> Config:
    """Load the config, creating and persisting one (with a fresh Machine ID) if absent.

    Generating the Machine ID here, once, is what makes it stable: every later caller reads
    it rather than deriving it from anything about the hardware.
    """
    data = read_json(paths.config_file)
    if data is None:
        config = Config()
        save(paths, config)
        return config

    found = data.get("schema", 0)
    if found > SCHEMA:
        raise ConfigTooNew(
            f"{paths.config_file} has schema {found}; this build understands {SCHEMA}. "
            "Update the application."
        )
    return Config.from_dict(data)


def save(paths: Paths, config: Config) -> None:
    write_json(paths.config_file, config.to_dict())
