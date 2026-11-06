"""The session that keeps the GitHub sign-in alive: renewed before it lapses, stored the
moment it is renewed, and never turned into an error the caller has to know about.

GitHub is a script of answers, the clock is a number, and the keyring is a dict - so each test
pins exactly when renewal happens and what reaches the store.
"""

import threading

import pytest

from core import device_flow
from core.credentials import Credential
from core.github import GitHubUnreachable
from core.session import RENEWAL_MARGIN, GitHubSession

NOW = 1_000_000.0
EIGHT_HOURS = 28800
SIX_MONTHS = 15897600


def expiring(access: str, refresh: str, issued: float) -> Credential:
    return Credential(
        access_token=access,
        refresh_token=refresh,
        expires_at=issued + EIGHT_HOURS,
        refresh_expires_at=issued + SIX_MONTHS,
    )


def renewal_answer(access: str, refresh: str) -> dict:
    return {
        "access_token": access,
        "expires_in": EIGHT_HOURS,
        "refresh_token": refresh,
        "refresh_token_expires_in": SIX_MONTHS,
    }


class Store:
    def __init__(self, credential: Credential | None) -> None:
        self.credential = credential
        self.saved: list[Credential] = []

    def load(self) -> Credential | None:
        return self.credential

    def save(self, credential: Credential) -> None:
        self.credential = credential
        self.saved.append(credential)

    def delete(self) -> None:
        self.credential = None


class GitHub:
    """Answers each POST with the next scripted reply (or raises it), and counts the asks."""

    def __init__(self, *answers) -> None:
        self.answers = list(answers)
        self.posts: list[dict[str, str]] = []

    def post(self, url: str, fields: dict[str, str]) -> dict:
        assert url == device_flow.TOKEN_URL
        self.posts.append(fields)
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer


def session(store: Store, hub: GitHub, now: float) -> GitHubSession:
    return GitHubSession(store, transport=hub, now=lambda: now)


def test_not_signed_in_is_none():
    assert session(Store(None), GitHub(), NOW).token() is None


def test_a_fresh_token_is_handed_back_without_asking_github():
    store, hub = Store(expiring("gho_a", "ghr_a", issued=NOW)), GitHub()

    assert session(store, hub, NOW + 60).token() == "gho_a"
    assert hub.posts == []


def test_a_token_about_to_expire_is_renewed_and_stored_before_it_is_used():
    """The bug: eight hours after sign-in, every push and fetch failed as signed out."""
    store = Store(expiring("gho_a", "ghr_a", issued=NOW))
    hub = GitHub(renewal_answer("gho_b", "ghr_b"))
    later = NOW + EIGHT_HOURS - RENEWAL_MARGIN + 1

    assert session(store, hub, later).token() == "gho_b"
    assert hub.posts[0]["refresh_token"] == "ghr_a"
    assert store.saved == [expiring("gho_b", "ghr_b", issued=later)]


def test_a_token_that_already_expired_days_ago_is_renewed_too():
    """The app was closed overnight: the refresh token outlives the access token by months."""
    store = Store(expiring("gho_a", "ghr_a", issued=NOW))
    hub = GitHub(renewal_answer("gho_b", "ghr_b"))

    assert session(store, hub, NOW + 3 * 86400).token() == "gho_b"


def test_once_renewed_the_next_request_uses_the_new_token_without_renewing_again():
    store = Store(expiring("gho_a", "ghr_a", issued=NOW))
    hub = GitHub(renewal_answer("gho_b", "ghr_b"))
    the_session = session(store, hub, NOW + EIGHT_HOURS)

    assert the_session.token() == "gho_b"
    assert the_session.token() == "gho_b"
    assert len(hub.posts) == 1


def test_a_pat_or_an_older_bare_token_is_never_renewed():
    store, hub = Store(Credential(access_token="ghp_pasted")), GitHub()

    assert session(store, hub, NOW * 100).token() == "ghp_pasted"
    assert hub.posts == []


def test_a_refresh_token_past_its_own_expiry_is_not_spent():
    """Six months unused: only a new sign-in helps, and GitHub is not asked to say so."""
    store, hub = Store(expiring("gho_a", "ghr_a", issued=NOW)), GitHub()

    assert session(store, hub, NOW + SIX_MONTHS).token() == "gho_a"
    assert hub.posts == []


def test_no_network_hands_back_the_old_token_and_tries_again_next_time():
    store = Store(expiring("gho_a", "ghr_a", issued=NOW))
    hub = GitHub(GitHubUnreachable("offline"), renewal_answer("gho_b", "ghr_b"))
    the_session = session(store, hub, NOW + EIGHT_HOURS)

    assert the_session.token() == "gho_a"  # Git then reports no network, as it always has
    assert store.saved == []
    assert the_session.token() == "gho_b"


def test_a_rejected_renewal_hands_back_the_old_token_and_says_to_sign_in(caplog):
    store = Store(expiring("gho_a", "ghr_a", issued=NOW))
    hub = GitHub(
        {
            "error": "bad_refresh_token",
            "error_description": "The refresh token passed is incorrect or expired.",
        }
    )

    assert session(store, hub, NOW + EIGHT_HOURS).token() == "gho_a"
    assert store.saved == []
    assert "incorrect or expired. Use Sign In" in caplog.text


def test_losing_the_race_to_another_copy_of_the_app_takes_the_pair_it_stored():
    """An installed copy and a dev checkout share the keyring slot. Both read the old pair;
    the other one spent the refresh token first, so GitHub refuses this one."""
    old = expiring("gho_a", "ghr_a", issued=NOW)
    winner = expiring("gho_w", "ghr_w", issued=NOW + EIGHT_HOURS)

    class RacedStore(Store):
        reads = 0

        def load(self):
            self.reads += 1
            return old if self.reads == 1 else winner

    raced = RacedStore(old)
    hub = GitHub({"error": "bad_refresh_token"})

    assert session(raced, hub, NOW + EIGHT_HOURS).token() == "gho_w"
    assert raced.saved == []  # the winner already stored its pair; nothing to overwrite


def test_two_threads_due_for_renewal_spend_the_refresh_token_once():
    """The startup fetch's worker and a click on the UI thread can ask at the same moment.
    A refresh token is single-use, so a second spend would void the first's new pair."""
    store = Store(expiring("gho_a", "ghr_a", issued=NOW))
    inside = threading.Event()
    release = threading.Event()

    class SlowGitHub(GitHub):
        def post(self, url, fields):
            inside.set()
            assert release.wait(5)
            return super().post(url, fields)

    hub = SlowGitHub(renewal_answer("gho_b", "ghr_b"))
    the_session = session(store, hub, NOW + EIGHT_HOURS)
    got: list[str | None] = []

    first = threading.Thread(target=lambda: got.append(the_session.token()))
    first.start()
    assert inside.wait(5)
    second = threading.Thread(target=lambda: got.append(the_session.token()))
    second.start()
    release.set()
    first.join(5)
    second.join(5)

    assert got == ["gho_b", "gho_b"]
    assert len(hub.posts) == 1


@pytest.mark.parametrize("margin_left", [RENEWAL_MARGIN + 1, RENEWAL_MARGIN * 3])
def test_renewal_waits_until_the_margin(margin_left):
    store, hub = Store(expiring("gho_a", "ghr_a", issued=NOW)), GitHub()

    assert session(store, hub, NOW + EIGHT_HOURS - margin_left).token() == "gho_a"
    assert hub.posts == []
