"""Exercises the real KeyringCredentialStore against an in-memory keyring backend, so the
code under test is the production class rather than a stand-in - without touching the
developer's actual OS keychain.
"""

import keyring
import pytest
from keyring.backend import KeyringBackend

from core.credentials import ACCOUNT, SERVICE, KeyringCredentialStore


class InMemoryKeyring(KeyringBackend):
    priority = 1  # type: ignore[assignment]

    def __init__(self) -> None:
        super().__init__()
        self._store: dict[tuple[str, str], str] = {}

    def get_password(self, service: str, username: str) -> str | None:
        return self._store.get((service, username))

    def set_password(self, service: str, username: str, password: str) -> None:
        self._store[(service, username)] = password

    def delete_password(self, service: str, username: str) -> None:
        try:
            del self._store[(service, username)]
        except KeyError as exc:
            raise keyring.errors.PasswordDeleteError(service) from exc


@pytest.fixture
def store():
    previous = keyring.get_keyring()
    keyring.set_keyring(InMemoryKeyring())
    try:
        yield KeyringCredentialStore()
    finally:
        keyring.set_keyring(previous)


def test_absent_token_reads_as_none(store):
    assert store.get_pat() is None


def test_token_round_trips(store):
    store.set_pat("ghp_example")
    assert store.get_pat() == "ghp_example"


def test_token_is_stored_under_the_expected_service_and_account(store):
    store.set_pat("ghp_example")
    assert keyring.get_password(SERVICE, ACCOUNT) == "ghp_example"


def test_setting_replaces_a_previous_token(store):
    store.set_pat("old")
    store.set_pat("new")
    assert store.get_pat() == "new"


def test_an_empty_token_is_refused(store):
    with pytest.raises(ValueError):
        store.set_pat("   ")
    assert store.get_pat() is None


def test_delete_removes_the_token(store):
    store.set_pat("ghp_example")
    store.delete_pat()
    assert store.get_pat() is None


def test_deleting_an_absent_token_is_not_an_error(store):
    """Redo Initialization must run cleanly whether or not a token was ever stored."""
    store.delete_pat()
    store.delete_pat()
