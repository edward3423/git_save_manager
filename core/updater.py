"""Updating the application itself: `gsm update`.

The installers put the application on disk as a Git checkout of its own source, with every
byte of runtime state in the gitignored `data/` beside it. So an update is a Git operation on
that checkout - bring the branch it tracks up to date - followed by `uv sync` for the
dependencies. This is the same pair of steps the installers run when re-run, and `data/`,
the Vault and the Backups are untouched by either.

## Fast-forward only

Re-running an installer hard-resets the checkout, which suits an install script: the
checkout is its own. A command can be run anywhere, though, including in a developer's
checkout, where a hard reset would destroy local edits and commits without a word. So this
only ever fast-forwards. A checkout with local changes, or local commits upstream does not
have, is refused with the reason, and nothing is touched; re-running the installer remains the
deliberate way to throw local changes away.

## Dependencies are synced every time

Even when the code is already current. An update whose `uv sync` failed (no network halfway,
say) leaves new code on old dependencies, and a second `gsm update` that answered "already up
to date" without syncing would leave it there for good. A sync with nothing to do is quick.

## This is not the Vault

The checkout is the user's own repository, so their Git config applies to it as it did when
the installer cloned it (`Git(isolated=False)`). Forcing the Vault's `core.autocrlf=false`
here would make a Windows checkout cloned with `autocrlf=true` read as modified throughout.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

from core.git import NETWORK_TIMEOUT, Git, GitError, GitMissing, GitTimeout

SYNC_TIMEOUT = 600.0
"""`uv sync` may download PyQt6, which is large; ten minutes, never forever."""


class UpdateRefused(Exception):
    """The checkout cannot be updated safely as it stands. Nothing was changed."""


class UpdateFailed(Exception):
    """The update was attempted and did not complete; the message says how far it got."""


@dataclass(frozen=True)
class Standing:
    """Where this checkout stands against the branch it tracks, as of a fresh fetch."""

    branch: str
    upstream: str
    current: str
    latest: str
    behind: int
    """Commits upstream has that this checkout lacks: what an update would bring in."""
    ahead: int
    """Local commits upstream lacks. Any at all and a fast-forward is impossible."""
    incoming: tuple[str, ...] = field(default=())
    """One line per incoming commit, newest first: `<short sha> <subject>`."""

    @property
    def up_to_date(self) -> bool:
        return self.behind == 0


@dataclass(frozen=True)
class Updated:
    before: Standing
    now: str
    """The commit the checkout is on after the update."""


def _git(root: Path) -> Git:
    return Git(work_tree=root, isolated=False)


def check(root: Path) -> Standing:
    """Fetch, and say whether an update is available. Changes nothing but the fetched refs."""
    if not (root / ".git").exists():
        raise UpdateRefused(
            f"{root} is not a Git checkout, so it cannot update itself. Install it with the "
            "installer (see the README) and it will be."
        )
    repo = _git(root)
    try:
        branch = repo.run("symbolic-ref", "--short", "-q", "HEAD").strip()
    except GitError:
        branch = ""
    if not branch:
        raise UpdateRefused(
            "The checkout is not on a branch (detached HEAD), so there is no branch to bring "
            "up to date. Re-run the installer to switch it back to one."
        )

    remote = _config(repo, f"branch.{branch}.remote") or "origin"
    merge = _config(repo, f"branch.{branch}.merge") or f"refs/heads/{branch}"
    upstream = f"{remote}/{merge.removeprefix('refs/heads/')}"

    try:
        repo.run("fetch", "--quiet", remote, timeout=NETWORK_TIMEOUT)
    except (GitError, GitTimeout) as error:
        raise UpdateFailed(
            f"Could not fetch from {remote}. Check the connection and try again. ({error})"
        ) from error

    try:
        latest = repo.run("rev-parse", "--verify", "-q", f"{upstream}^{{commit}}").strip()
    except GitError:
        latest = ""
    if not latest:
        raise UpdateRefused(f"{remote} has no branch {upstream.split('/', 1)[1]} to update from.")

    current = repo.run("rev-parse", "HEAD").strip()
    behind, ahead = (
        int(part)
        for part in repo.run("rev-list", "--left-right", "--count", f"{upstream}...HEAD").split()
    )
    incoming = repo.run("log", "--format=%h %s", f"HEAD..{upstream}").splitlines()
    return Standing(
        branch=branch,
        upstream=upstream,
        current=current,
        latest=latest,
        behind=behind,
        ahead=ahead,
        incoming=tuple(incoming),
    )


def update(root: Path, uv: Sequence[str] | None = None) -> Updated:
    """Fast-forward the checkout to its upstream, then sync the dependencies.

    `uv` is the command that runs uv; by default the one running this process (`uv run` sets
    `$UV`), else the first on PATH.
    """
    standing = check(root)
    repo = _git(root)

    if not standing.up_to_date:
        changed = repo.run("status", "--porcelain", "--untracked-files=no").splitlines()
        if changed:
            shown = ", ".join(line[3:] for line in changed[:5])
            more = f" and {len(changed) - 5} more" if len(changed) > 5 else ""
            raise UpdateRefused(
                f"The checkout at {root} has local changes ({shown}{more}). Updating could "
                "overwrite them, so nothing was changed. Commit or discard them first, or "
                "re-run the installer to replace the checkout outright."
            )
        if standing.ahead:
            raise UpdateRefused(
                f"{standing.branch} has {standing.ahead} local commit(s) that "
                f"{standing.upstream} does not, so it cannot simply move forward. Nothing was "
                "changed. Merge or rebase them yourself, or re-run the installer to replace "
                "the checkout outright."
            )
        repo.run("merge", "--ff-only", "--quiet", standing.upstream)

    now = repo.run("rev-parse", "HEAD").strip()
    _sync_dependencies(root, uv, moved=now != standing.current)
    return Updated(before=standing, now=now)


def _sync_dependencies(root: Path, uv: Sequence[str] | None, moved: bool) -> None:
    found = (
        list(uv) if uv else [exe] if (exe := os.environ.get("UV") or shutil.which("uv")) else None
    )
    code = "The code was updated, but its" if moved else "The code is current, but its"
    if found is None:
        raise UpdateFailed(
            f"{code} dependencies could not be synced: uv was not found. Re-run the installer."
        )
    try:
        completed = subprocess.run(
            [*found, "sync", "--project", str(root), "--quiet"],
            capture_output=True,
            text=True,
            timeout=SYNC_TIMEOUT,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise UpdateFailed(f"{code} dependencies could not be synced: {error}") from error
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout).strip()
        raise UpdateFailed(
            f"{code} dependencies could not be synced (uv sync exited "
            f"{completed.returncode}): {detail}. Run `gsm update` again, or re-run the installer."
        )


def _config(repo: Git, key: str) -> str | None:
    try:
        return repo.run("config", "--get", key).strip() or None
    except (GitError, GitMissing):
        return None
