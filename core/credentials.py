"""The GitHub sign-in, held in the OS keyring and nowhere else.

The token is never written to a file, never placed in a remote URL, and never passed on a
command line. It is read from the keyring at the moment of use and handed to Git through the
subprocess environment, so it appears in no `.git/config`, no process list, and no log.

## What is stored

A `Credential`: the access token, plus - when GitHub issued an expiring one - the refresh
token that renews it and both expiry times. A device-flow sign-in to an OAuth App with
token expiration turned on yields an access token that lives eight hours and a refresh
token that lives six months unused; a pasted classic PAT yields just the token, forever.
Keeping only the access token, as this module once did, is exactly what made the sign-in
"expire" after a few hours: the means to renew it was thrown away at sign-in.

The keyring slot holds the Credential as one JSON object. A slot written before that holds
the bare token string, and still reads as a Credential that never expires - nobody has to
sign in again because the storage format moved on. Renewal itself is `core.session`'s job;
this module only keeps what it is handed.
"""

from __future__ import annotations

import contextlib
import json
import re
from dataclasses import asdict, dataclass
from typing import Protocol

import keyring

SERVICE = "git-save-manager"
ACCOUNT = "github-pat"  # the slot's historical name, kept so existing sign-ins carry over
SCHEMA = 1
BARE_TOKEN = re.compile(r"[A-Za-z0-9_]+")
"""Every GitHub token format - classic `ghp_`, OAuth `gho_`, fine-grained `github_pat_`, and the
older 40-hex kind - is drawn from this alphabet."""


@dataclass(frozen=True)
class Credential:
    """One GitHub sign-in. Times are epoch seconds on this Machine's clock; None means
    never: a token with no `expires_at` is valid until revoked."""

    access_token: str
    refresh_token: str | None = None
    expires_at: float | None = None
    refresh_expires_at: float | None = None

    def due_for_renewal(self, now: float, margin: float) -> bool:
        """The access token expires within `margin` seconds (or already has)."""
        return self.expires_at is not None and now >= self.expires_at - margin

    def renewable(self, now: float) -> bool:
        """There is a refresh token, and it has not itself expired."""
        if self.refresh_token is None:
            return False
        return self.refresh_expires_at is None or now < self.refresh_expires_at

    def serialized(self) -> str:
        return json.dumps({"schema": SCHEMA, **asdict(self)}, separators=(",", ":"))

    @classmethod
    def parsed(cls, raw: str) -> Credential | None:
        """Read a keyring slot: today's JSON object, or a bare token from before it.

        A GitHub token is letters, digits and underscores, so the two cannot be mistaken for
        each other. A slot that is neither - damaged, or written by a newer version - reads as
        no sign-in, which the app already answers with Sign In; guessing at it could hand Git
        garbage.
        """
        text = raw.strip()
        if BARE_TOKEN.fullmatch(text):
            return cls(access_token=text)
        if not text.startswith("{"):
            return None
        try:
            found = json.loads(text)
        except json.JSONDecodeError:
            return None
        if not isinstance(found, dict) or found.get("schema") != SCHEMA:
            return None
        token = found.get("access_token")
        if not isinstance(token, str) or not token:
            return None
        return cls(
            access_token=token,
            refresh_token=found.get("refresh_token") or None,
            expires_at=_seconds(found.get("expires_at")),
            refresh_expires_at=_seconds(found.get("refresh_expires_at")),
        )


def _seconds(value: object) -> float | None:
    return float(value) if isinstance(value, int | float) else None


class CredentialStore(Protocol):
    """Somewhere a Credential can be kept. Implemented by the OS keyring in production."""

    def load(self) -> Credential | None: ...

    def save(self, credential: Credential) -> None: ...

    def delete(self) -> None: ...


class KeyringCredentialStore:
    """The real store: whatever secure backend the OS provides."""

    def load(self) -> Credential | None:
        raw = keyring.get_password(SERVICE, ACCOUNT)
        return None if raw is None else Credential.parsed(raw)

    def save(self, credential: Credential) -> None:
        if not credential.access_token.strip():
            raise ValueError("Refusing to store an empty token")
        keyring.set_password(SERVICE, ACCOUNT, credential.serialized())

    def delete(self) -> None:
        # Deleting is idempotent by design: Redo Initialization must run cleanly whether or
        # not a token was ever stored.
        with contextlib.suppress(keyring.errors.PasswordDeleteError):
            keyring.delete_password(SERVICE, ACCOUNT)
