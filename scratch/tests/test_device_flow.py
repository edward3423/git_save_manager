"""Signing in with the OAuth device flow: GitHub faked as a script of answers, no clock.

The fake records every POST, so the tests can hold the flow to GitHub's polling contract -
the interval it was given, the longer one after `slow_down` - without sleeping a second.
"""

import urllib.error

import pytest

from core import device_flow
from core.device_flow import DeviceCode, SignInFailed
from core.github import GitHubUnreachable

CODE = DeviceCode(
    device_code="dc-123",
    user_code="WDJB-MJHT",
    verification_uri="https://github.com/login/device",
    expires_in=900,
    interval=5,
)


class ScriptedGitHub:
    """Answers each POST with the next scripted reply, and remembers what was asked."""

    def __init__(self, *answers: dict) -> None:
        self.answers = list(answers)
        self.posts: list[tuple[str, dict[str, str]]] = []

    def post(self, url: str, fields: dict[str, str]) -> dict:
        self.posts.append((url, fields))
        return self.answers.pop(0)


class Waits:
    """Records each interval waited, and cancels on the Nth wait when asked to."""

    def __init__(self, cancel_on: int | None = None) -> None:
        self.seen: list[float] = []
        self.cancel_on = cancel_on

    def __call__(self, seconds: float) -> bool:
        self.seen.append(seconds)
        return self.cancel_on is not None and len(self.seen) >= self.cancel_on


def test_starting_asks_for_a_repo_scoped_code_and_hands_back_what_the_user_needs():
    hub = ScriptedGitHub(
        {
            "device_code": "dc-123",
            "user_code": "WDJB-MJHT",
            "verification_uri": "https://github.com/login/device",
            "expires_in": 900,
            "interval": 5,
        }
    )

    assert device_flow.start(hub) == CODE
    assert hub.posts == [
        (device_flow.DEVICE_CODE_URL, {"client_id": device_flow.CLIENT_ID, "scope": "repo"})
    ]


def test_a_refused_start_says_why():
    hub = ScriptedGitHub({"error": "device_flow_disabled"})

    with pytest.raises(SignInFailed, match="device flow is not enabled"):
        device_flow.start(hub)


def test_polling_waits_out_pending_answers_and_returns_the_token():
    hub = ScriptedGitHub(
        {"error": "authorization_pending"},
        {"error": "authorization_pending"},
        {"access_token": "gho_abc", "token_type": "bearer", "scope": "repo"},
    )
    waits = Waits()

    assert device_flow.wait_for_token(hub, CODE, waits) == "gho_abc"
    assert waits.seen == [5, 5, 5]  # waits *before* every poll, never polling early
    url, fields = hub.posts[0]
    assert url == device_flow.TOKEN_URL
    assert fields == {
        "client_id": device_flow.CLIENT_ID,
        "device_code": "dc-123",
        "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
    }


def test_slow_down_keeps_the_longer_interval_github_names_from_then_on():
    hub = ScriptedGitHub(
        {"error": "slow_down", "interval": 10},
        {"error": "authorization_pending"},
        {"access_token": "gho_abc"},
    )
    waits = Waits()

    device_flow.wait_for_token(hub, CODE, waits)

    assert waits.seen == [5, 10, 10]


def test_slow_down_without_an_interval_adds_githubs_documented_five_seconds():
    hub = ScriptedGitHub({"error": "slow_down"}, {"access_token": "gho_abc"})
    waits = Waits()

    device_flow.wait_for_token(hub, CODE, waits)

    assert waits.seen == [5, 10]


@pytest.mark.parametrize(
    ("error", "says"),
    [
        ("access_denied", "declined"),
        ("expired_token", "expired"),
        ("incorrect_device_code", "incorrect_device_code"),
    ],
)
def test_a_sign_in_that_ends_without_a_token_says_why(error, says):
    hub = ScriptedGitHub({"error": "authorization_pending"}, {"error": error})

    with pytest.raises(SignInFailed, match=says):
        device_flow.wait_for_token(hub, CODE, Waits())


def test_a_cancel_stops_before_the_next_poll_and_yields_no_token():
    hub = ScriptedGitHub({"error": "authorization_pending"})
    waits = Waits(cancel_on=2)

    assert device_flow.wait_for_token(hub, CODE, waits) is None
    assert len(hub.posts) == 1  # the cancelled wait was not followed by a poll


def test_the_real_transport_turns_an_unreachable_github_into_the_setup_error(monkeypatch):
    def unreachable(*_args, **_kwargs):
        raise urllib.error.URLError("no route to host")

    monkeypatch.setattr(device_flow.urllib.request, "urlopen", unreachable)

    with pytest.raises(GitHubUnreachable):
        device_flow.FormTransport().post(device_flow.TOKEN_URL, {"client_id": "x"})
