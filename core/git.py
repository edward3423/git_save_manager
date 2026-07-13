"""Driving Git as a subprocess, and keeping the PAT out of everything that persists.

Git is invoked directly rather than through a binding, because the two things that matter
most here - exactly what lands in `argv`, and exactly what lands in the environment - are
precisely what a binding hides.

## The token never comes to rest

The remote URL stays credential-free. A token embedded in a remote is written to
`.git/config` in plaintext, and from there it leaks into `git remote -v`, into every log and
crash report, into backups, and into any screenshot of a terminal. It is never passed on a
command line either, where it would be visible in `ps` to every other process on the machine.

Instead it is read from the OS keyring at the moment of use, placed in the subprocess
*environment only*, and consumed by a one-shot inline credential helper:

    git -c credential.helper= \\
        -c credential.helper='!f() { echo username=token; echo "password=$GIT_VAULT_PAT"; }; f'

The helper text itself holds no secret - it is a shell function that reads an environment
variable - so nothing sensitive touches disk or the process list. The empty
`-c credential.helper=` first is not decoration: it *resets* the helper chain, so a
system-level helper (macOS Keychain, Windows Credential Manager) cannot answer first with a
stale or wrong credential.

## Git never gets to ask a question

`GIT_TERMINAL_PROMPT=0` and an empty `GIT_ASKPASS`. Without them, a revoked PAT does not
fail - it *hangs*, forever, on a password prompt that no one will ever see, inside a GUI
application with no terminal attached.

## The user's global config cannot reach the Vault

Hooks are disabled, commit signing is off, and CRLF translation is forced off. A global
`core.hooksPath`, a global `commit.gpgsign`, or a global `core.autocrlf=true` would otherwise
apply to the Vault - and the last of those silently corrupts every binary save file Git's
heuristics happen to guess is text.
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

CREDENTIAL_HELPER = '!f() { echo username=token; echo "password=$GIT_VAULT_PAT"; }; f'
PAT_ENV = "GIT_VAULT_PAT"

DEFAULT_TIMEOUT = 60.0
NETWORK_TIMEOUT = 300.0
"""Network operations get longer, but never *forever*: a hung clone must not wedge the app."""

SAFE_CONFIG = (
    # A global core.autocrlf=true rewrites line endings in any file Git's heuristics guess is
    # text. Save files are binary, and the guess is wrong often enough to corrupt them.
    "core.autocrlf=false",
    "core.eol=lf",
    # The Vault is our repository, not the user's project. Their global hooks and signing
    # config have no business running against it - and a failing hook would block a Sync.
    "core.hooksPath=/dev/null",
    "commit.gpgsign=false",
)


class GitMissing(Exception):
    """Git is not installed, or not on PATH."""


class GitError(Exception):
    """A Git command failed. Carries the redacted command and its stderr."""

    def __init__(self, argv: list[str], returncode: int, stderr: str) -> None:
        self.argv = argv
        self.returncode = returncode
        self.stderr = stderr.strip()
        super().__init__(f"`{' '.join(argv)}` exited {returncode}: {self.stderr}")


class GitTimeout(Exception):
    """A Git command ran too long. Almost always a network operation against the Cloud Vault."""


@dataclass(frozen=True)
class Git:
    """Runs Git against one working tree."""

    work_tree: Path
    timeout: float = field(default=DEFAULT_TIMEOUT)

    def run(
        self,
        *args: str,
        pat: str | None = None,
        config: tuple[str, ...] = (),
        check: bool = True,
        timeout: float | None = None,
    ) -> str:
        """Run a Git command and return its stdout.

        `pat` is injected via the environment and an inline credential helper, never into
        `argv` and never into `.git/config`. Pass it only to commands that reach the network.
        """
        argv = ["git", "-C", str(self.work_tree)]

        for setting in SAFE_CONFIG + config:
            argv += ["-c", setting]

        if pat is not None:
            argv += ["-c", "credential.helper="]  # reset: no system helper answers first
            argv += ["-c", f"credential.helper={CREDENTIAL_HELPER}"]

        argv += list(args)

        env = {PAT_ENV: pat} if pat is not None else {}

        try:
            completed = subprocess.run(
                argv,
                capture_output=True,
                text=True,
                timeout=timeout or self.timeout,
                env={**_base_env(), **env},
                check=False,
            )
        except FileNotFoundError as exc:
            raise GitMissing("Git is not installed, or is not on PATH.") from exc
        except subprocess.TimeoutExpired as exc:
            raise GitTimeout(f"`{' '.join(args)}` did not finish in time.") from exc

        if check and completed.returncode != 0:
            raise GitError(argv, completed.returncode, _redact(completed.stderr, pat))

        return completed.stdout


def _base_env() -> dict[str, str]:
    env = dict(os.environ)
    env.pop(PAT_ENV, None)  # never inherit a token from the parent process

    # Git must never stop to ask a human anything. In a GUI app with no terminal, a prompt is
    # not a prompt - it is a hang, and the user sees an application that has simply frozen.
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GIT_ASKPASS"] = ""
    env["SSH_ASKPASS"] = ""
    env["GCM_INTERACTIVE"] = "never"

    # Deterministic output, whatever the user's locale.
    env["LC_ALL"] = "C"
    return env


def _redact(text: str, pat: str | None) -> str:
    """Belt and braces: the token is never in `argv`, but never let one out of here either."""
    if not pat:
        return text
    return text.replace(pat, "***")
