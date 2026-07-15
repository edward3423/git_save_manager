"""First-run setup: the four bootstrap paths, distinguished and refused correctly.

GitHub's REST surface is faked in-process, but it is not a stub: creating a repository
through the fake really creates a bare repository on disk, exactly as GitHub would, so the
Git half of every bootstrap runs against a real remote. No network, no GitHub.
"""

import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest

from core import config as config_module
from core import github, vault
from core.config import Config, MachineDescription
from core.github import Answer, BootstrapOutcome
from core.paths import Paths

MACHINE_A = "9c8b7a65-0000-4000-8000-00000000000a"
MACHINE_B = "9c8b7a65-0000-4000-8000-00000000000b"

TOKEN = "ghp_secret-under-test"


@dataclass(frozen=True)
class Call:
    method: str
    path: str
    body: dict | None


class FakeGitHub:
    """GitHub as bootstrap sees it: a REST answer for every call, and real bare repositories.

    `create` genuinely creates the remote on disk, so a push after a create lands somewhere
    real rather than being asserted into existence.
    """

    def __init__(self, hosting: Path) -> None:
        self.hosting = hosting
        self.login = "edward"
        self.user_status = 200
        self.repo_status: int | None = None
        self.default_branch = "main"
        self.repos: dict[str, dict] = {}
        self.calls: list[Call] = []
        self.tokens_seen: set[str] = set()

    def bare_path(self, repo: str) -> Path:
        return self.hosting / f"{repo}.git"

    def host(self, repo: str, empty: bool = True) -> Path:
        """Bring a repository into existence server-side, as if created on github.com."""
        bare = self.bare_path(repo)
        subprocess.run(
            ["git", "init", "-q", "--bare", "-b", self.default_branch, str(bare)], check=True
        )
        # Partial clone must be opted into by the server, exactly as GitHub does.
        subprocess.run(
            ["git", "-C", str(bare), "config", "uploadpack.allowFilter", "true"], check=True
        )
        self.repos[repo] = {"full_name": repo, "default_branch": self.default_branch}
        return bare

    def request(self, method: str, path: str, token: str, body: dict | None = None) -> Answer:
        self.calls.append(Call(method, path, body))
        self.tokens_seen.add(token)

        if path == "/user":
            if self.user_status != 200:
                return Answer(self.user_status, {"message": "Bad credentials"})
            return Answer(200, {"login": self.login})
        if method == "GET" and path.startswith("/repos/"):
            if self.repo_status is not None:
                return Answer(self.repo_status, {"message": "Forbidden"})
            repo = path.removeprefix("/repos/")
            if repo in self.repos:
                return Answer(200, self.repos[repo])
            return Answer(404, {"message": "Not Found"})
        if method == "POST" and path == "/user/repos":
            assert body is not None
            self.host(f"{self.login}/{body['name']}")
            return Answer(201, self.repos[f"{self.login}/{body['name']}"])
        if method == "POST" and path.startswith("/orgs/") and path.endswith("/repos"):
            assert body is not None
            org = path.removeprefix("/orgs/").removesuffix("/repos")
            self.host(f"{org}/{body['name']}")
            return Answer(201, self.repos[f"{org}/{body['name']}"])
        raise AssertionError(f"bootstrap called an endpoint the fake does not know: {path}")


@pytest.fixture
def hub(tmp_path, monkeypatch):
    """The fake GitHub, with `vault.remote_url` pointed at its on-disk hosting."""
    fake = FakeGitHub(hosting=tmp_path / "github")
    fake.hosting.mkdir()
    monkeypatch.setattr(vault, "remote_url", lambda repo: str(fake.bare_path(repo)))
    return fake


@pytest.fixture
def machine(tmp_path):
    """A Machine that has not been set up yet: no repo in its config, nothing on disk."""
    paths = Paths(root=tmp_path / "laptop")
    paths.data_dir.mkdir(parents=True)
    return paths, Config(machine_id=MACHINE_A), MachineDescription(hostname="laptop", os_name="X")


def served(hub: FakeGitHub, repo: str, path: str) -> str:
    """What the Cloud Vault holds at HEAD for one path, read server-side."""
    return subprocess.run(
        ["git", "-C", str(hub.bare_path(repo)), "show", f"HEAD:{path}"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout


# --- path one: the repository does not exist ---------------------------------------------------


def test_a_missing_repo_is_created_private_and_the_new_vault_is_pushed(hub, machine):
    paths, config, description = machine

    done = github.bootstrap(paths, config, description, api=hub, token=TOKEN, repo="edward/saves")

    assert done.outcome is BootstrapOutcome.CREATED
    create = next(c for c in hub.calls if c.method == "POST")
    assert create.body["private"] is True

    # The Cloud Vault now really holds the marker and this Machine's file.
    assert '"vault": true' in served(hub, "edward/saves", "vault.json")
    assert MACHINE_A in served(hub, "edward/saves", f"machines/{MACHINE_A}.json")


def test_a_missing_repo_under_an_organization_is_created_there_not_under_the_user(hub, machine):
    """`POST /user/repos` always creates under the caller. Pointed at an organization's
    namespace, bootstrap must use the org endpoint - or the Vault lands under the wrong
    owner and every later operation pushes somewhere the user did not name."""
    paths, config, description = machine

    done = github.bootstrap(
        paths, config, description, api=hub, token=TOKEN, repo="our-family/saves"
    )

    assert done.outcome is BootstrapOutcome.CREATED
    create = next(c for c in hub.calls if c.method == "POST")
    assert create.path == "/orgs/our-family/repos"
    assert '"vault": true' in served(hub, "our-family/saves", "vault.json")


# --- path two: the repository exists, but holds nothing ----------------------------------------


def test_an_existing_empty_repo_is_adopted_on_its_own_default_branch(hub, machine):
    """The user made the repo on github.com first ("trunk" as its default branch proves we
    honour GitHub's answer rather than assuming "main"). Nothing is created; the new Vault
    is simply pushed into it."""
    paths, config, description = machine
    hub.default_branch = "trunk"
    hub.host("edward/saves")

    done = github.bootstrap(paths, config, description, api=hub, token=TOKEN, repo="edward/saves")

    assert done.outcome is BootstrapOutcome.ADOPTED_EMPTY
    assert done.branch == "trunk"
    assert not any(c.method == "POST" for c in hub.calls)
    assert '"vault": true' in served(hub, "edward/saves", "vault.json")


# --- path three: the repository is already a Vault - the second-machine path -------------------


def test_a_repo_that_is_a_vault_is_joined_and_this_machine_registers_itself(hub, machine, tmp_path):
    """The branch comes from the remote HEAD - "trunk", so that an implementation quietly
    assuming "main" cannot pass."""
    paths_a, config_a, description_a = machine
    hub.default_branch = "trunk"
    github.bootstrap(paths_a, config_a, description_a, api=hub, token=TOKEN, repo="edward/saves")

    paths_b = Paths(root=tmp_path / "desktop")
    paths_b.data_dir.mkdir(parents=True)
    config_b = Config(machine_id=MACHINE_B)
    description_b = MachineDescription(hostname="desktop", os_name="Y")

    done = github.bootstrap(
        paths_b, config_b, description_b, api=hub, token=TOKEN, repo="edward/saves"
    )

    assert done.outcome is BootstrapOutcome.JOINED
    assert done.branch == "trunk"

    # Registered: this Machine's file is committed and published, and only added to - the
    # first Machine's file is still there, on the Cloud and in the local clone alike.
    assert MACHINE_B in served(hub, "edward/saves", f"machines/{MACHINE_B}.json")
    assert MACHINE_A in served(hub, "edward/saves", f"machines/{MACHINE_A}.json")
    assert paths_b.machine_file(MACHINE_A).exists()
    assert vault.is_clean(paths_b)


def test_a_machine_that_already_registered_can_join_again(hub, machine, tmp_path):
    """Setup re-run after a reinstall: the clone is fresh but the Vault already holds this
    Machine's file, byte for byte. There is nothing to commit, and that is success - not a
    `git commit` failure mid-setup."""
    paths_a, config_a, description_a = machine
    github.bootstrap(paths_a, config_a, description_a, api=hub, token=TOKEN, repo="edward/saves")

    again = Paths(root=tmp_path / "reinstalled")
    again.data_dir.mkdir(parents=True)

    done = github.bootstrap(
        again, config_a, description_a, api=hub, token=TOKEN, repo="edward/saves"
    )

    assert done.outcome is BootstrapOutcome.JOINED
    assert vault.is_clean(again)
    assert config_module.load(again).repo == "edward/saves"


# --- path four: the repository is somebody's project -------------------------------------------


def seed(hub: FakeGitHub, repo: str, files: dict[str, str], tmp_path: Path) -> str:
    """Host a repository already holding commits that did not come from this application."""
    hub.host(repo)
    wt = tmp_path / "seed"
    wt.mkdir()
    run = lambda *args: subprocess.run(["git", "-C", str(wt), *args], check=True)  # noqa: E731
    run("init", "-q", "-b", "main")
    for name, text in files.items():
        (wt / name).write_text(text, encoding="utf-8")
    run("add", "-A")
    run("-c", "user.name=t", "-c", "user.email=t@x", "commit", "-q", "-m", "theirs")
    run("push", "-q", str(hub.bare_path(repo)), "main")
    return subprocess.run(
        ["git", "-C", str(wt), "rev-parse", "HEAD"], capture_output=True, text=True, check=True
    ).stdout.strip()


def test_a_repo_that_is_someone_elses_project_is_refused_untouched(hub, machine, tmp_path):
    """The user typo'd their dotfiles repo. Nothing may be committed into it, nothing pushed
    to it, and the aborted clone must not be left behind to wedge the next attempt."""
    paths, config, description = machine
    theirs = seed(hub, "edward/dotfiles", {"README.md": "my dotfiles"}, tmp_path)

    with pytest.raises(vault.NotAVault):
        github.bootstrap(paths, config, description, api=hub, token=TOKEN, repo="edward/dotfiles")

    served_head = subprocess.run(
        ["git", "-C", str(hub.bare_path("edward/dotfiles")), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    assert served_head == theirs  # nothing was pushed
    assert not paths.vault_dir.exists()  # the aborted clone was cleaned away
    assert config_module.load(paths).is_set_up is False  # setup did not claim to succeed


def test_a_vault_from_a_newer_build_is_refused_the_same_way(hub, machine, tmp_path):
    """A marker with a schema we do not understand is a Vault we might damage. Same refusal,
    same cleanup - and the message tells the user to update, not to retry."""
    paths, config, description = machine
    seed(hub, "edward/saves", {"vault.json": '{"vault": true, "schema": 99}'}, tmp_path)

    with pytest.raises(vault.VaultTooNew):
        github.bootstrap(paths, config, description, api=hub, token=TOKEN, repo="edward/saves")

    assert not paths.vault_dir.exists()
    assert config_module.load(paths).is_set_up is False


# --- the PAT is validated before anything is written anywhere ----------------------------------


def test_a_bad_token_is_refused_before_anything_is_written(hub, machine):
    """Validation comes first: no repository is created, no clone is made, no config saved.
    GitHub answers /user with 401 for a revoked or mistyped PAT."""
    paths, config, description = machine
    hub.user_status = 401

    with pytest.raises(github.BadToken):
        github.bootstrap(paths, config, description, api=hub, token=TOKEN, repo="edward/saves")

    assert [c.path for c in hub.calls] == ["/user"]  # it never got further
    assert not paths.vault_dir.exists()
    assert not paths.config_file.exists()


# --- what a completed setup leaves behind -------------------------------------------------------


def test_a_completed_setup_records_the_repo_and_branch_in_config(hub, machine):
    """Every later operation names the branch explicitly and finds the repo in config -
    neither is ever guessed again after setup."""
    paths, config, description = machine
    hub.default_branch = "trunk"

    github.bootstrap(paths, config, description, api=hub, token=TOKEN, repo="edward/saves")

    saved = config_module.load(paths)
    assert saved.is_set_up
    assert saved.repo == "edward/saves"
    assert saved.default_branch == "trunk"
    assert saved.machine_id == MACHINE_A  # the identity written at first launch, kept


def test_an_api_answer_we_do_not_understand_stops_the_bootstrap_cold(hub, machine):
    """403 on the repo lookup (rate limit, SAML enforcement) is neither "absent" nor
    "present". Guessing either way risks creating a duplicate or pushing into the dark, so
    the only safe move is to stop before anything is written."""
    paths, config, description = machine
    hub.repo_status = 403

    with pytest.raises(github.GitHubError):
        github.bootstrap(paths, config, description, api=hub, token=TOKEN, repo="edward/saves")

    assert not any(c.method == "POST" for c in hub.calls)
    assert not paths.vault_dir.exists()
    assert not paths.config_file.exists()


# --- the real transport: the token rides in the header, and nowhere else -----------------------


def test_the_rest_transport_carries_the_token_in_the_authorization_header_only():
    """A token in a URL lands in proxy logs, browser histories, and exception messages.
    In the header it is seen only by GitHub."""
    request = github.rest_request("POST", "/user/repos", TOKEN, {"name": "saves"})

    assert request.full_url == "https://api.github.com/user/repos"
    assert TOKEN not in request.full_url
    assert request.get_header("Authorization") == f"Bearer {TOKEN}"
    assert request.get_header("Accept") == "application/vnd.github+json"
    assert TOKEN not in (request.data or b"").decode()
