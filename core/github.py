"""First-run setup against GitHub: the four bootstrap paths, distinguished.

| Situation                      | Behaviour                                              |
|--------------------------------|--------------------------------------------------------|
| Repo does not exist            | Create private via API, init Vault structure, push    |
| Repo exists, empty             | Init Vault structure, push                             |
| Repo exists, is a Vault        | Clone, register this Machine (the second-machine path) |
| Repo exists, is something else | Refuse. Never commit saves into an unrelated project   |

Whether a repository is a Vault is decided by its committed `vault.json` marker, and the
marker's `schema` lets an old build refuse a Vault written by a newer one - the one failure
mode capable of damaging every Entry at once, prevented by one `if` (`vault.read_marker`).

The PAT is validated against `/user` and the target repository is looked up **before
anything is written anywhere** - locally or remotely. API calls carry the token per-request
in the `Authorization` header only: never in a URL, never in a file, never on a command line.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import urllib.error
import urllib.request
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol

from core import config as config_module
from core import vault
from core.config import Config, MachineDescription
from core.git import NETWORK_TIMEOUT, Git
from core.paths import Paths


@dataclass(frozen=True)
class Answer:
    """One REST response: the status code, and the parsed JSON body."""

    status: int
    data: dict[str, Any]


class Api(Protocol):
    """GitHub's REST surface, as narrow as bootstrap needs it. Faked in tests."""

    def request(self, method: str, path: str, token: str, body: dict | None = None) -> Answer: ...


class GitHubUnreachable(Exception):
    """The API could not be reached at all. At setup time there is no Offline Mode yet to
    enter; the user simply cannot set up until the network is back."""


API_ROOT = "https://api.github.com"
API_TIMEOUT = 30.0


def rest_request(method: str, path: str, token: str, body: dict | None = None):
    """Build one API request. Public because it *is* the hygiene guarantee: the token rides
    in the `Authorization` header - never in the URL, where it would land in proxy logs and
    exception messages - and the tests hold this function to that."""
    data = None if body is None else json.dumps(body).encode("utf-8")
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if data is not None:
        headers["Content-Type"] = "application/json"
    return urllib.request.Request(API_ROOT + path, data=data, method=method, headers=headers)


class RestApi:
    """The real GitHub API, over stdlib urllib. Everything network-shaped lives here, which
    is exactly why the tests hand `bootstrap` a fake instead."""

    def request(self, method: str, path: str, token: str, body: dict | None = None) -> Answer:
        request = rest_request(method, path, token, body)
        try:
            with urllib.request.urlopen(request, timeout=API_TIMEOUT) as response:
                return Answer(response.status, _parsed(response.read()))
        except urllib.error.HTTPError as error:
            # 401, 404, 403... are answers, not transport failures: the caller decides.
            return Answer(error.code, _parsed(error.read()))
        except urllib.error.URLError as error:
            raise GitHubUnreachable(
                "Could not reach api.github.com. Check the connection and try again."
            ) from error


def _parsed(raw: bytes) -> dict[str, Any]:
    if not raw:
        return {}
    found = json.loads(raw)
    return found if isinstance(found, dict) else {"data": found}


class BadToken(Exception):
    """GitHub did not accept the PAT. The user must issue a new one and try again."""


class GitHubError(Exception):
    """GitHub answered something bootstrap cannot act on safely.

    A 403 on the repo lookup (rate limit, SAML enforcement) is neither "absent" nor
    "present": guessing either way risks creating a duplicate repository or pushing into
    the dark, so the bootstrap stops before anything is written.
    """

    def __init__(self, status: int, message: str) -> None:
        self.status = status
        super().__init__(f"GitHub answered {status}: {message}")


def _expect(answer: Answer, ok: int) -> Answer:
    if answer.status != ok:
        raise GitHubError(answer.status, str(answer.data.get("message", answer.data)))
    return answer


class BootstrapOutcome(StrEnum):
    """Which of the bootstrap paths ran. The refusals raise instead."""

    CREATED = "created"  # the repository did not exist; a private one was created and pushed
    ADOPTED_EMPTY = "adopted_empty"  # it existed with no commits; the new Vault was pushed
    JOINED = "joined"  # it is already a Vault; cloned, and this Machine registered itself


@dataclass(frozen=True)
class Bootstrapped:
    outcome: BootstrapOutcome
    branch: str


def _viewer(api: Api, token: str) -> str:
    """Validate the PAT against `/user` and name its owner. The first call, always."""
    answer = api.request("GET", "/user", token)
    if answer.status != 200:
        raise BadToken(
            "GitHub did not accept this token. Issue a classic PAT with the `repo` scope "
            "and try again."
        )
    return answer.data["login"]


def _remote_head(paths: Paths, token: str, repo: str) -> str | None:
    """The branch the remote HEAD names, or None when the repository has no commits at all.

    An empty repository answers `ls-remote --symref` with nothing - not even its unborn
    HEAD - which is exactly how emptiness is detected. A non-empty one names its default
    branch: `ref: refs/heads/<branch>  HEAD`.
    """
    raw = Git(work_tree=paths.data_dir).run(
        "ls-remote", "--symref", vault.remote_url(repo), "HEAD", pat=token, timeout=NETWORK_TIMEOUT
    )
    for line in raw.splitlines():
        if line.startswith("ref:"):
            return line.split()[1].removeprefix("refs/heads/")
    return None


def _publish_new_vault(
    paths: Paths,
    config: Config,
    description: MachineDescription,
    token: str,
    repo: str,
    branch: str,
) -> None:
    """Turn nothing into a Vault: init locally, commit the structure, push it."""
    vault.initialize(paths, config, description, branch=branch)
    repo_git = vault.git(paths)
    repo_git.run("add", "-A")
    repo_git.run(
        "commit",
        "-q",
        "-m",
        f"init(vault): from {description.hostname}",
        config=vault.commit_identity(config, description),
    )
    repo_git.run("remote", "add", "origin", vault.remote_url(repo))
    repo_git.run("push", "-q", "-u", "origin", branch, pat=token, timeout=NETWORK_TIMEOUT)


def _complete(paths: Paths, config: Config, repo: str, branch: str) -> None:
    """Record what setup established: which Cloud Vault, and its branch, named explicitly
    in every operation from here on."""
    config.repo = repo
    config.default_branch = branch
    config_module.save(paths, config)


def bootstrap(
    paths: Paths,
    config: Config,
    description: MachineDescription,
    api: Api,
    token: str,
    repo: str,
) -> Bootstrapped:
    """Set this Machine up against `owner/name`, whatever state that repository is in."""
    login = _viewer(api, token)

    found = api.request("GET", f"/repos/{repo}", token)
    if found.status == 404:
        owner, _, name = repo.partition("/")
        # `POST /user/repos` always creates under the caller, whatever owner was named - so
        # any other owner must be an organization, addressed through the org endpoint.
        endpoint = "/user/repos" if owner == login else f"/orgs/{owner}/repos"
        body = {"name": name, "private": True, "auto_init": False}
        created = _expect(api.request("POST", endpoint, token, body=body), ok=201)
        branch = created.data["default_branch"]
        _publish_new_vault(paths, config, description, token, repo, branch)
        _complete(paths, config, repo, branch)
        return Bootstrapped(outcome=BootstrapOutcome.CREATED, branch=branch)

    _expect(found, ok=200)
    branch = _remote_head(paths, token, repo)
    if branch is None:
        # The repository exists but holds no commits: the user made it on github.com first.
        # Its default branch has no commit to be read from, so GitHub's API answers for it.
        branch = found.data["default_branch"]
        _publish_new_vault(paths, config, description, token, repo, branch)
        _complete(paths, config, repo, branch)
        return Bootstrapped(outcome=BootstrapOutcome.ADOPTED_EMPTY, branch=branch)

    # The repository has history. Clone it - which refuses, before anything is written into
    # it, unless its committed marker says it is a Vault this build understands - and then
    # publish this Machine's file: the second-machine path.
    try:
        vault.clone(paths, repo, pat=token, entry_ids=())
    except (vault.NotAVault, vault.VaultTooNew):
        _discard_clone(paths)
        raise
    vault.register_machine(paths, config, description)
    repo_git = vault.git(paths)
    repo_git.run("add", "-A")
    # A setup re-run after a reinstall finds its own file already in the Vault, byte for
    # byte. Nothing to commit is success, not a `git commit` failure mid-setup.
    if repo_git.run("diff", "--cached", "--name-only").strip():
        repo_git.run(
            "commit",
            "-q",
            "-m",
            f"register({description.hostname})",
            config=vault.commit_identity(config, description),
        )
        repo_git.run("push", "-q", "origin", branch, pat=token, timeout=NETWORK_TIMEOUT)
    _complete(paths, config, repo, branch)
    return Bootstrapped(outcome=BootstrapOutcome.JOINED, branch=branch)


def _discard_clone(paths: Paths) -> None:
    """Remove a refused clone, so the refusal leaves nothing behind to wedge the next attempt.

    This deletes only what the aborted clone itself just wrote - the refusal fired before
    anything else touched it. Git marks its object files read-only, which on Windows makes a
    plain rmtree fail partway; clearing the bit first is the whole trick.
    """

    def force(func, path, _exc):
        os.chmod(path, stat.S_IWRITE)
        func(path)

    shutil.rmtree(paths.vault_dir, onexc=force)


