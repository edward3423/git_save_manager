"""Exercises the real KeyringCredentialStore against an in-memory keyring backend, so the
code under test is the production class rather than a stand-in - without touching the
developer's actual OS keychain.
"""

import keyring
import pytest
from keyring.backend import KeyringBackend

from core.credentials import ACCOUNT, SERVICE, Credential, KeyringCredentialStore


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


EXPIRING = Credential(
    access_token="gho_access",
    refresh_token="ghr_refresh",
    expires_at=1_000_000.0,
    refresh_expires_at=2_000_000.0,
)


def test_absent_token_reads_as_none(store):
    assert store.load() is None


def test_an_expiring_sign_in_round_trips_with_everything_that_renews_it(store):
    """Dropping the refresh token at this point is what made sign-ins die after 8 hours."""
    store.save(EXPIRING)
    assert store.load() == EXPIRING


def test_a_pasted_pat_round_trips_as_a_token_that_never_expires(store):
    store.save(Credential(access_token="ghp_example"))
    found = store.load()
    assert found == Credential(access_token="ghp_example")
    assert not found.due_for_renewal(now=10**12, margin=600)


def test_the_sign_in_is_stored_under_the_expected_service_and_account(store):
    store.save(EXPIRING)
    assert Credential.parsed(keyring.get_password(SERVICE, ACCOUNT)) == EXPIRING


def test_a_bare_token_written_by_an_older_version_still_reads(store):
    """Upgrading must not sign anyone out: the slot used to hold just the token string."""
    keyring.set_password(SERVICE, ACCOUNT, "gho_from_before")
    assert store.load() == Credential(access_token="gho_from_before")


@pytest.mark.parametrize(
    "raw",
    [
        "{not json",
        '{"schema": 99, "access_token": "gho_x"}',  # a newer version's format
        '{"schema": 1}',
        '{"schema": 1, "access_token": ""}',
        "[]",
        "   ",
    ],
)
def test_a_slot_that_cannot_be_read_is_no_sign_in_rather_than_a_guess(store, raw):
    keyring.set_password(SERVICE, ACCOUNT, raw)
    assert store.load() is None


def test_the_stored_form_fits_the_windows_credential_manager(store):
    """Windows caps a credential blob at 2560 bytes, which keyring fills with UTF-16 - 1280
    characters. GitHub documents tokens growing up to 255 characters each."""
    longest = Credential(
        access_token="a" * 255,
        refresh_token="r" * 255,
        expires_at=99_999_999_999.123456,
        refresh_expires_at=99_999_999_999.123456,
    )
    assert len(longest.serialized()) < 1280


def test_saving_replaces_a_previous_sign_in(store):
    store.save(Credential(access_token="old"))
    store.save(Credential(access_token="new"))
    assert store.load() == Credential(access_token="new")


def test_an_empty_token_is_refused(store):
    with pytest.raises(ValueError):
        store.save(Credential(access_token="   "))
    assert store.load() is None


def test_delete_removes_the_sign_in(store):
    store.save(EXPIRING)
    store.delete()
    assert store.load() is None


def test_deleting_an_absent_token_is_not_an_error(store):
    """Redo Initialization must run cleanly whether or not a token was ever stored."""
    store.delete()
    store.delete()


@pytest.mark.parametrize(
    ("now", "due"),
    [
        (EXPIRING.expires_at - 601, False),
        (EXPIRING.expires_at - 600, True),  # inside the margin: renew before it lapses
        (EXPIRING.expires_at + 5, True),
    ],
)
def test_renewal_falls_due_a_margin_before_the_access_token_expires(now, due):
    assert EXPIRING.due_for_renewal(now, margin=600) is due


def test_only_a_live_refresh_token_can_renew():
    assert EXPIRING.renewable(now=EXPIRING.refresh_expires_at - 1)
    assert not EXPIRING.renewable(now=EXPIRING.refresh_expires_at)
    assert not Credential(access_token="ghp_example").renewable(now=0)
