"""Signing in to GitHub without a hand-made token: the OAuth device flow.

The app asks GitHub for a short user code, the user approves it at github.com/login/device
in a browser where they are usually already logged in, and the app collects a token by
polling. Nothing is copied by hand, and every Machine gets its own token, revocable on its
own from GitHub's "Authorized OAuth Apps" page.

The client ID below is public by design: the device flow needs no client secret, so nothing
sensitive ships with the application. The token that comes back is an ordinary OAuth token
with the `repo` scope - Git and the REST API take it exactly where they took a classic PAT,
and it is stored in the same keyring slot (`core.credentials`).

## Expiring tokens

When the OAuth App has token expiration turned on, GitHub answers the sign-in with an access
token that lives eight hours *and* a refresh token, with both lifetimes. The answer becomes a
`Credential` that keeps all of it, and `refresh` trades the refresh token for a fresh pair -
without a client secret, which GitHub waives for tokens that came from the device flow. Each
refresh token is single-use: once spent, it and the old access token both stop working, so
the new pair must be stored before anything else happens (`core.session`).

Like every other GitHub call, the transport is a narrow protocol faked in tests; the waiting
between polls is injected too, so a test never sleeps and the UI can cancel instantly.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

from core.credentials import Credential
from core.github import GitHubUnreachable

CLIENT_ID = "Ov23li2BxcADatIHg22w"
SCOPE = "repo"  # private repositories: create the Cloud Vault, push, pull

DEVICE_CODE_URL = "https://github.com/login/device/code"
TOKEN_URL = "https://github.com/login/oauth/access_token"
GRANT_TYPE = "urn:ietf:params:oauth:grant-type:device_code"
REFRESH_GRANT_TYPE = "refresh_token"
TIMEOUT = 30.0

SLOW_DOWN_PENALTY = 5
"""Seconds GitHub adds to the polling interval on `slow_down`, when it does not say."""


class Transport(Protocol):
    """A form POST to github.com answered with JSON. Faked in tests."""

    def post(self, url: str, fields: dict[str, str]) -> dict[str, Any]: ...


class FormTransport:
    """The real one, over stdlib urllib.

    GitHub answers these endpoints with HTTP 200 even for errors like `authorization_pending`,
    so the error lives in the body; a 4xx body is parsed the same way rather than raised.
    """

    def post(self, url: str, fields: dict[str, str]) -> dict[str, Any]:
        request = urllib.request.Request(
            url,
            data=urllib.parse.urlencode(fields).encode("ascii"),
            method="POST",
            headers={"Accept": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
                return _parsed(response.read())
        except urllib.error.HTTPError as error:
            return _parsed(error.read())
        except urllib.error.URLError as error:
            raise GitHubUnreachable(
                "Could not reach github.com. Check the connection and try again."
            ) from error


def _parsed(raw: bytes) -> dict[str, Any]:
    try:
        found = json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        return {"error": "unreadable_answer"}
    return found if isinstance(found, dict) else {"error": "unreadable_answer"}


@dataclass(frozen=True)
class DeviceCode:
    """What GitHub hands out to start a sign-in. Only `user_code` is shown to the user."""

    device_code: str
    user_code: str
    verification_uri: str
    expires_in: int
    interval: int


class SignInFailed(Exception):
    """The sign-in ended without a token: denied, expired, or refused by GitHub."""


class RefreshRejected(Exception):
    """GitHub would not renew the sign-in: the refresh token is spent, expired, or revoked.

    Only a new sign-in recovers from this - unless another copy of the app on this Machine
    spent the refresh token first, and the keyring already holds the pair it got back.
    """


_EXPLAINED = {
    "access_denied": "The sign-in was declined on GitHub. Nothing was changed.",
    "expired_token": "The code expired before it was approved. Start the sign-in again.",
    "device_flow_disabled": (
        "GitHub refused the sign-in: device flow is not enabled for this app. Enable it in "
        "the OAuth App's settings on GitHub, or use a personal access token instead."
    ),
    "incorrect_client_credentials": (
        "GitHub does not recognize this app's client ID. Use a personal access token instead."
    ),
}


def _failure(answer: dict[str, Any]) -> SignInFailed:
    code = str(answer.get("error", "unknown"))
    if code in _EXPLAINED:
        return SignInFailed(_EXPLAINED[code])
    detail = answer.get("error_description") or code
    return SignInFailed(f"GitHub refused the sign-in: {detail}")


def start(transport: Transport, client_id: str = CLIENT_ID) -> DeviceCode:
    """Ask GitHub for a user code. The first step; nothing is stored anywhere."""
    answer = transport.post(DEVICE_CODE_URL, {"client_id": client_id, "scope": SCOPE})
    if "error" in answer or "device_code" not in answer:
        raise _failure(answer)
    return DeviceCode(
        device_code=answer["device_code"],
        user_code=answer["user_code"],
        verification_uri=answer["verification_uri"],
        expires_in=int(answer["expires_in"]),
        interval=int(answer["interval"]),
    )


def _granted(answer: dict[str, Any], now: float) -> Credential:
    """A token answer as a Credential. The lifetimes are relative ("expires in 28800 seconds"),
    so they are pinned to this Machine's clock at the moment they arrived - which makes the
    expiry immune to any skew between this clock and GitHub's."""

    def deadline(field: str) -> float | None:
        seconds = answer.get(field)
        return now + float(seconds) if isinstance(seconds, int | float) else None

    return Credential(
        access_token=str(answer["access_token"]),
        refresh_token=str(answer["refresh_token"]) if answer.get("refresh_token") else None,
        expires_at=deadline("expires_in"),
        refresh_expires_at=deadline("refresh_token_expires_in"),
    )


def wait_for_token(
    transport: Transport,
    code: DeviceCode,
    wait: Callable[[float], bool],
    client_id: str = CLIENT_ID,
    now: Callable[[], float] = time.time,
) -> Credential | None:
    """Poll until the user approves, and return the sign-in - or None if `wait` reports a cancel.

    `wait(seconds)` blocks for the polling interval and answers True when the caller has
    cancelled (in the app, `threading.Event.wait`). GitHub's own `expired_token` ends a code
    that was never approved, so this loop needs no clock of its own.
    """
    interval = code.interval
    fields = {"client_id": client_id, "device_code": code.device_code, "grant_type": GRANT_TYPE}
    while True:
        if wait(interval):
            return None
        answer = transport.post(TOKEN_URL, fields)
        if answer.get("access_token"):
            return _granted(answer, now())
        error = answer.get("error")
        if error == "authorization_pending":
            continue
        if error == "slow_down":
            # Polling too fast is answered with a new, longer interval that must be kept.
            interval = int(answer.get("interval", interval + SLOW_DOWN_PENALTY))
            continue
        raise _failure(answer)


def refresh(
    transport: Transport,
    credential: Credential,
    client_id: str = CLIENT_ID,
    now: Callable[[], float] = time.time,
) -> Credential:
    """Trade the refresh token for a new access token and refresh token.

    Raises RefreshRejected when GitHub says no, and GitHubUnreachable when it cannot be asked.
    """
    if credential.refresh_token is None:
        raise RefreshRejected("This sign-in cannot be renewed: it came with no refresh token.")
    answer = transport.post(
        TOKEN_URL,
        {
            "client_id": client_id,
            "grant_type": REFRESH_GRANT_TYPE,
            "refresh_token": credential.refresh_token,
        },
    )
    if answer.get("access_token"):
        return _granted(answer, now())
    detail = answer.get("error_description") or answer.get("error", "unknown")
    raise RefreshRejected(f"GitHub would not renew the sign-in: {detail}")
