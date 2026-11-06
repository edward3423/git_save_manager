"""The one way the app reads its GitHub token: renewed in place, before it expires.

An expiring sign-in (`core.credentials`) is only as good as its renewal. Every caller that
needs a token - Git's fetch and push, the Bind and Download that fetch lazily - asks this
session, and the session hands back an access token that is good for at least
`RENEWAL_MARGIN` more seconds whenever it can. Callers never see a refresh token, never
decide when to renew, and never learn that renewal exists.

## When renewal cannot happen

A renewal that fails never fails the caller. It hands back the token it has, and Git's own
answer then says what is wrong through the paths that already exist: no network enters
Offline Mode with Check Connection, and a token GitHub no longer takes enters it with Sign
In. Renewal is tried again on the next request, so a dropped connection costs nothing once
it is back.

## Renewing exactly once

A refresh token is single-use - spending it voids it and the old access token together - so
two renewals racing with the same refresh token leave one of them holding nothing. Within the
app a lock serializes them (the startup fetch runs on a worker thread while the window takes
clicks). Across processes - an installed copy and a dev checkout share this Machine's keyring
slot - the loser of the race finds GitHub rejecting a refresh token that the winner already
replaced, so a rejection re-reads the keyring and takes the winner's pair if it is there.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable

from core import device_flow
from core.credentials import CredentialStore
from core.github import GitHubUnreachable
from core.logger import log

RENEWAL_MARGIN = 600.0
"""Renew this many seconds before the access token expires. Comfortably more than the
longest network operation, so a token cannot lapse halfway through a push."""


class GitHubSession:
    def __init__(
        self,
        store: CredentialStore,
        transport: device_flow.Transport | None = None,
        now: Callable[[], float] = time.time,
    ) -> None:
        self.store = store
        self.transport = transport or device_flow.FormTransport()
        self.now = now
        self._lock = threading.Lock()

    def token(self) -> str | None:
        """An access token for Git and the REST API, or None if this Machine is not signed in.

        May make one network call, to renew a token about to expire - so, like every other
        GitHub call, it belongs wherever the caller already accepts waiting on the network.
        """
        with self._lock:
            current = self.store.load()
            if current is None:
                return None
            now = self.now()
            if not current.due_for_renewal(now, RENEWAL_MARGIN) or not current.renewable(now):
                return current.access_token
            try:
                renewed = device_flow.refresh(self.transport, current, now=self.now)
            except GitHubUnreachable:
                return current.access_token  # Git will say "no network"; retried next time
            except device_flow.RefreshRejected as error:
                latest = self.store.load()
                if latest is not None and latest != current:
                    return latest.access_token  # another copy of the app renewed it first
                said = str(error).rstrip(".")  # GitHub's description may bring its own
                log().warning("%s. Use Sign In to sign in to GitHub again.", said)
                return current.access_token
            # Stored before it is used: the old pair is void the moment GitHub answered.
            self.store.save(renewed)
            return renewed.access_token
