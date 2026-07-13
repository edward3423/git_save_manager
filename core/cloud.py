"""The Cloud Vault: Push, Pull, fetch status, and Offline Mode.

Push and Pull move commits between the Vault and the Cloud Vault. Neither ever touches a
Live Save, and **neither ever writes a Baseline** (Invariant 6): the Baseline records what
last moved between the Live Save and the Vault, and nothing here moves data on that axis.
This module therefore does not import the Ledger at all - the constraint is structural, not
a comment. A Pull that advanced the Baseline would make a stale Live Save read as In Sync,
and the next Sync would upload old progress over another Machine's new progress and report
success. It is the single worst bug this application can have.

Status is observed, never acted on: `fetch_status` fetches and compares, and nothing is ever
auto-pulled or auto-pushed.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum

from core import vault
from core.config import Config, MachineDescription
from core.git import NETWORK_TIMEOUT, GitError, GitTimeout
from core.paths import Paths
from core.vault import git, merging


class CloudState(StrEnum):
    """Where HEAD stands relative to the Cloud Vault, as of the last fetch."""

    UP_TO_DATE = "up_to_date"
    AHEAD = "ahead"  # commits exist here and nowhere else; a Push would publish them
    BEHIND = "behind"  # the Cloud has commits we lack; a Pull would bring them down
    DIVERGED = "diverged"  # both; a Pull merges, and only then can a Push succeed


@dataclass(frozen=True)
class CloudStatus:
    state: CloudState
    ahead: int
    behind: int
    checked_at: datetime


class OfflineReason(StrEnum):
    """What the user can do about it: check their connection, or re-enter their PAT."""

    NO_NETWORK = "no_network"
    AUTH_FAILED = "auth_failed"


@dataclass(frozen=True)
class OfflineMode:
    """The degraded mode entered the moment any Cloud operation fails.

    Sticky by design: it does not time out, does not retry, and never heals itself. The only
    exit is an explicit Check Connection that succeeds. An automatic retry loop would hammer
    a captive portal, and an operation that quietly healed the state would contradict the
    indicator the user is looking at.
    """

    reason: OfflineReason
    since: datetime
    """For the indicator, alongside `Cloud.last_status` - which going offline deliberately
    does not clear: "Offline (last checked 2h ago: Behind)"."""


class CloudOffline(Exception):
    """The Cloud Vault is unreachable, so the operation was not attempted (or just failed).

    Everything purely local - Sync, Restore, history, rollback, backups - keeps working.
    """

    def __init__(self, mode: OfflineMode) -> None:
        self.mode = mode
        hint = (
            "Re-enter your GitHub token, then use Check Connection."
            if mode.reason is OfflineReason.AUTH_FAILED
            else "Use Check Connection when the network is back."
        )
        super().__init__(f"Offline: {hint}")


AUTH_SIGNS = (
    "authentication failed",
    "invalid username or password",
    "could not read username",  # prompts are disabled, so no credential reached Git at all
    "repository not found",  # GitHub's answer for a private repo and a bad token alike
    # Anchored to curl's exact phrasing, not bare "403"/"permission denied": those substrings
    # also appear in local-filesystem errors (an antivirus holding a pack file is "Permission
    # denied"), and telling that user to re-enter a perfectly good PAT sends them in circles.
    "the requested url returned error: 403",
    "the requested url returned error: 401",
)


def classify(error: GitError | GitTimeout) -> OfflineReason:
    """Auth failure or no network - decided from stderr, because Git's exit codes cannot."""
    if isinstance(error, GitTimeout):
        return OfflineReason.NO_NETWORK
    text = error.stderr.lower()
    if any(sign in text for sign in AUTH_SIGNS):
        return OfflineReason.AUTH_FAILED
    return OfflineReason.NO_NETWORK


@dataclass(frozen=True)
class Pulled:
    """What a Pull brought down, and whether any of it is contested."""

    commits: int
    """How many Cloud commits came in. Zero means there was nothing to pull."""

    conflicts: tuple[str, ...] = ()
    """Entry IDs both sides changed - a Merge Conflict, awaiting an Entry-granular choice.
    Empty means the merge completed."""


class PushRejected(Exception):
    """The Cloud Vault has commits this Machine lacks, so the push did not land.

    Not a connectivity failure - the Cloud answered, and answered no - so it does not enter
    Offline Mode. The remedy is a Pull, then pushing the merge.
    """


class ForeignConflict(Exception):
    """A merge conflict on a path outside `entries/`, which ADR-0003 makes impossible.

    Every file outside the Entries has exactly one writer - a Machine file is written only by
    its Machine, the marker only at creation - so a conflict there means the Vault was edited
    by hand or by another tool. We abort the merge and refuse to guess.
    """


def _is_rejection(error: GitError) -> bool:
    """A non-fast-forward refusal, as distinct from failing to reach the Cloud at all."""
    text = error.stderr.lower()
    return "[rejected]" in text or "non-fast-forward" in text


# --- Merge Conflicts, resolved at Entry granularity -------------------------------------------


def _entry_of(path: str) -> str | None:
    """The Entry a Vault path belongs to: its content directory or its sidecar."""
    prefix, _, rest = path.partition("/")
    if prefix != "entries" or not rest:
        return None
    entry_id = rest.split("/", 1)[0]
    return entry_id.removesuffix(".json")


def _unmerged_paths(paths: Paths) -> list[str]:
    """Every path the merge could not decide, from `ls-files -u`: mode sha stage\\tpath."""
    raw = git(paths).run("ls-files", "-u")
    return sorted({line.split("\t", 1)[1] for line in raw.splitlines() if "\t" in line})


def _entries_of(unmerged: list[str]) -> tuple[str, ...]:
    found = {_entry_of(path) for path in unmerged}
    return tuple(sorted(entry_id for entry_id in found if entry_id is not None))


def conflicted_entries(paths: Paths) -> tuple[str, ...]:
    """The Entries awaiting a choice, each to be taken whole from one side or the other."""
    return _entries_of(_unmerged_paths(paths))


class Side(StrEnum):
    """Whose line of progress to keep, whole. There is no per-file middle ground: a save
    assembled from both sides is a save that never existed on any Machine."""

    VAULT = "vault"  # this Machine's commits: HEAD
    CLOUD = "cloud"  # the other line: MERGE_HEAD


class MergeUnfinished(Exception):
    """Entries are still awaiting a choice, so there is no merge to commit yet."""


def resolve_merge(paths: Paths, entry_id: str, side: Side) -> None:
    """Resolve one contested Entry by taking one side's tree for it, whole.

    Wholesale replacement rather than `checkout --ours/--theirs`: the chosen side may have
    *deleted* the Entry (a remove contested by a sync), and taking that side must stage the
    deletion, which per-path checkout of conflict stages cannot express.
    """
    repo = git(paths)
    source = "HEAD" if side is Side.VAULT else "MERGE_HEAD"
    specs = [f"entries/{entry_id}", f"entries/{entry_id}.json"]

    repo.run("rm", "-r", "-f", "-q", "--ignore-unmatch", "--", *specs)
    held = set(repo.run("ls-tree", "--name-only", source, "--", *specs).splitlines())
    present = [spec for spec in specs if spec in held]
    if present:
        repo.run("restore", "--source", source, "--staged", "--worktree", "--", *present)


def finish_merge(paths: Paths, config: Config, description: MachineDescription) -> str:
    """Commit the merge once every contested Entry has been resolved."""
    unresolved = conflicted_entries(paths)
    if unresolved:
        raise MergeUnfinished(
            f"{len(unresolved)} Entr{'y' if len(unresolved) == 1 else 'ies'} still awaiting "
            "a choice. Resolve each one, or abort the merge."
        )

    repo = git(paths)
    repo.run("commit", "--no-edit", config=vault.commit_identity(config, description))
    return repo.run("rev-parse", "HEAD").strip()


def abort_merge(paths: Paths) -> None:
    """Walk away from the merge entirely. The Pull can simply be run again later."""
    if merging(paths):
        git(paths).run("merge", "--abort")


@dataclass
class Cloud:
    """This Machine's view of the Cloud Vault, including whether it is reachable at all.

    **One instance per running application**, created at startup and shared by every
    operation. Offline Mode's stickiness lives in this object and nowhere else, so a caller
    constructing a fresh Cloud per operation would silently disable it.
    """

    paths: Paths
    config: Config
    offline: OfflineMode | None = None
    last_status: CloudStatus | None = None

    @property
    def branch(self) -> str:
        found = self.config.default_branch
        if found is None:
            raise ValueError("No default branch is configured; setup has not completed.")
        return found

    @property
    def upstream(self) -> str:
        return f"origin/{self.branch}"

    def _require_online(self) -> None:
        if self.offline is not None:
            raise CloudOffline(self.offline)

    def _lost(self, error: GitError | GitTimeout) -> CloudOffline:
        """Enter Offline Mode, and hand back the exception to raise in the operation's place."""
        self.offline = OfflineMode(reason=classify(error), since=datetime.now(UTC))
        return CloudOffline(self.offline)

    def _fetch(self, pat: str) -> None:
        try:
            git(self.paths).run("fetch", "origin", pat=pat, timeout=NETWORK_TIMEOUT)
        except (GitError, GitTimeout) as error:
            raise self._lost(error) from error

    def check_connection(self, pat: str) -> bool:
        """Probe the Cloud Vault, and on success leave Offline Mode. **The only exit.**

        `ls-remote` proves the network, the credential, and access to this repository in one
        shot - merely reaching github.com proves nothing if the PAT was revoked. Explicitly
        allowed to run while offline, being the way back.
        """
        try:
            git(self.paths).run("ls-remote", "origin", "HEAD", pat=pat, timeout=NETWORK_TIMEOUT)
        except (GitError, GitTimeout) as error:
            was = self.offline
            self.offline = OfflineMode(
                reason=classify(error),  # refreshed: "no network" may have become "bad PAT"
                since=was.since if was else datetime.now(UTC),
            )
            return False

        self.offline = None
        return True

    def _ahead_behind(self) -> tuple[int, int]:
        """Both counts in one process: left of `...` is the upstream's own commits (behind),
        right is ours (ahead)."""
        raw = git(self.paths).run("rev-list", "--left-right", "--count", f"{self.upstream}...HEAD")
        behind, ahead = (int(part) for part in raw.split())
        return ahead, behind

    def fetch_status(self, pat: str) -> CloudStatus:
        """Fetch, then say where HEAD stands. Observes only: never pulls, never pushes."""
        self._require_online()
        self._fetch(pat)
        ahead, behind = self._ahead_behind()

        if ahead and behind:
            state = CloudState.DIVERGED
        elif ahead:
            state = CloudState.AHEAD
        elif behind:
            state = CloudState.BEHIND
        else:
            state = CloudState.UP_TO_DATE

        status = CloudStatus(state=state, ahead=ahead, behind=behind, checked_at=datetime.now(UTC))
        self.last_status = status
        return status

    def push(self, pat: str) -> None:
        """Publish this Machine's commits to the Cloud Vault. Never forced: the Cloud Vault's
        history is as append-only as the Vault's (Invariant 3)."""
        self._require_online()
        try:
            git(self.paths).run("push", "origin", self.branch, pat=pat, timeout=NETWORK_TIMEOUT)
        except GitTimeout as error:
            raise self._lost(error) from error
        except GitError as error:
            if _is_rejection(error):
                # The Cloud answered; the answer was no. Not a connectivity failure, so not
                # Offline Mode - going offline here would grey out the Pull that fixes it.
                raise PushRejected(
                    "The Cloud Vault has commits this Machine does not. "
                    "Pull first, then push the merge."
                ) from error
            raise self._lost(error) from error

    def pull(self, pat: str, description: MachineDescription) -> Pulled:
        """Bring the Cloud Vault's commits into the Vault, by merge and never by rebase.

        Rebasing would rewrite this Machine's published hashes and replay the same conflict
        once per commit, for zero benefit on binary content - and rewriting history shared
        across Machines is how a Vault loses a commit.

        Touches no Live Save and writes no Baseline: a pulled Entry surfaces as Vault Ahead,
        and moving it into the Live Save is the user's explicit Restore.
        """
        self._require_online()
        vault.ensure_clean(self.paths)
        repo = git(self.paths)

        self._fetch(pat)
        _, behind = self._ahead_behind()
        if behind == 0:
            return Pulled(commits=0)

        # The pat is passed because a partial clone fetches file contents lazily: the merge
        # itself may have to go back to the Cloud for the blobs it is about to check out.
        try:
            repo.run(
                "merge",
                "-m",
                f"pull({self.branch}): from {description.hostname}",
                self.upstream,
                pat=pat,
                timeout=NETWORK_TIMEOUT,
                config=vault.commit_identity(self.config, description),
            )
        except GitTimeout as error:
            vault.ensure_clean(self.paths)
            raise self._lost(error) from error
        except GitError as error:
            if not merging(self.paths):
                # The merge never started - a lazy blob fetch failed, or something stranger.
                # Nothing to resolve, so put the Vault back the way it was and go offline.
                vault.ensure_clean(self.paths)
                raise self._lost(error) from error

            unmerged = _unmerged_paths(self.paths)
            foreign = [path for path in unmerged if _entry_of(path) is None]
            if foreign:
                abort_merge(self.paths)
                raise ForeignConflict(
                    f"Merge conflict outside entries/: {', '.join(sorted(foreign))}. "
                    "Every such file has a single writer, so the Vault has been edited by "
                    "hand. The merge was aborted; nothing has changed."
                ) from None

            return Pulled(commits=behind, conflicts=_entries_of(unmerged))

        return Pulled(commits=behind)
