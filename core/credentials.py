"""The GitHub PAT, held in the OS keyring and nowhere else.

The token is never written to a file, never placed in a remote URL, and never passed on a
command line. It is read from the keyring at the moment of use and handed to Git through the
subprocess environment, so it appears in no `.git/config`, no process list, and no log.
"""

from __future__ import annotations

import contextlib
from typing import Protocol

import keyring

SERVICE = "git-save-manager"
ACCOUNT = "github-pat"


class CredentialStore(Protocol):
    """Somewhere a PAT can be kept. Implemented by the OS keyring in production."""

    def get_pat(self) -> str | None: ...

    def set_pat(self, token: str) -> None: ...

    def delete_pat(self) -> None: ...


class KeyringCredentialStore:
    """The real store: whatever secure backend the OS provides."""

    def get_pat(self) -> str | None:
        return keyring.get_password(SERVICE, ACCOUNT)

    def set_pat(self, token: str) -> None:
        if not token.strip():
            raise ValueError("Refusing to store an empty token")
        keyring.set_password(SERVICE, ACCOUNT, token)

    def delete_pat(self) -> None:
        # Deleting is idempotent by design: Redo Initialization must run cleanly whether or
        # not a token was ever stored.
        with contextlib.suppress(keyring.errors.PasswordDeleteError):
            keyring.delete_password(SERVICE, ACCOUNT)
