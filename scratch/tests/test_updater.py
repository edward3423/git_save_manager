"""`gsm update`: the application's own checkout, brought up to date - and never at the cost of
anything local.

Real Git: an upstream repository standing in for GitHub, and an installed checkout cloned from
it the way the installers clone. `uv` is a stand-in script that records how it was called.
"""

from __future__ import annotations

import io
import json
import subprocess
import sys
from pathlib import Path

import pytest

from cli.main import main
from core import lock, updater
from core.paths import Paths

IDENTITY = ("-c", "user.name=t", "-c", "user.email=t@example.com")


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *IDENTITY, *args], capture_output=True, text=True, check=True
    ).stdout.strip()


def commit(repo: Path, name: str, text: str, message: str) -> str:
    (repo / name).write_text(text, encoding="utf-8")
    git(repo, "add", name)
    git(repo, "commit", "-q", "-m", message)
    return git(repo, "rev-parse", "HEAD")


@pytest.fixture
def upstream(tmp_path):
    """Where releases are published: a working repo whose commits are pushed to a bare one."""
    bare = tmp_path / "upstream.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(bare)], check=True)
    work = tmp_path / "publisher"
    subprocess.run(["git", "clone", "-q", str(bare), str(work)], check=True)
    git(work, "checkout", "-q", "-b", "main")
    commit(work, "main.py", "print('v1')\n", "v1")
    git(work, "push", "-q", "origin", "main")
    return work


def release(upstream: Path, text: str, message: str) -> str:
    sha = commit(upstream, "main.py", text, message)
    git(upstream, "push", "-q", "origin", "main")
    return sha


@pytest.fixture
def installed(tmp_path, upstream):
    """An install, as the installer leaves one: a clone on the branch, with live data/."""
    root = tmp_path / "install"
    subprocess.run(
        ["git", "clone", "-q", "--branch", "main", str(tmp_path / "upstream.git"), str(root)],
        check=True,
    )
    (root / ".gitignore").write_text("/data/\n", encoding="utf-8")
    git(root, "add", ".gitignore")
    git(root, "commit", "-q", "-m", "ignore data")
    git(root, "push", "-q", "origin", "main")
    git(upstream, "pull", "-q", "origin", "main")
    (root / "data").mkdir()
    (root / "data" / "ledger.json").write_text("{}", encoding="utf-8")
    return root


@pytest.fixture
def uv(tmp_path):
    """A stand-in for uv: records each call, and fails when asked to."""
    record = tmp_path / "uv-calls.jsonl"
    script = tmp_path / "fake_uv.py"
    script.write_text(
        "import json, os, sys\n"
        f"open({str(record)!r}, 'a').write(json.dumps(sys.argv[1:]) + '\\n')\n"
        "sys.exit(int(os.environ.get('FAKE_UV_EXIT', '0')))\n",
        encoding="utf-8",
    )
    command = [sys.executable, str(script)]

    def calls() -> list[list[str]]:
        if not record.exists():
            return []
        return [json.loads(line) for line in record.read_text(encoding="utf-8").splitlines()]

    return command, calls


def test_an_installed_checkout_moves_forward_and_keeps_its_data(installed, upstream, uv):
    command, calls = uv
    latest = release(upstream, "print('v2')\n", "v2: something better")

    done = updater.update(installed, uv=command)

    assert done.now == latest and done.before.behind == 1
    assert done.before.incoming[0].endswith("v2: something better")
    assert (installed / "main.py").read_text(encoding="utf-8") == "print('v2')\n"
    assert (installed / "data" / "ledger.json").read_text(encoding="utf-8") == "{}"
    assert calls() == [["sync", "--project", str(installed), "--quiet"]]


def test_check_reports_an_update_without_applying_it(installed, upstream):
    release(upstream, "print('v2')\n", "v2")

    standing = updater.check(installed)

    assert standing.behind == 1 and not standing.up_to_date
    assert (installed / "main.py").read_text(encoding="utf-8") == "print('v1')\n"


def test_an_up_to_date_checkout_still_syncs_its_dependencies(installed, uv):
    """A previous update may have moved the code and then failed its sync."""
    command, calls = uv

    done = updater.update(installed, uv=command)

    assert done.now == done.before.current
    assert len(calls()) == 1


def test_local_edits_are_refused_not_overwritten(installed, upstream, uv):
    command, calls = uv
    release(upstream, "print('v2')\n", "v2")
    (installed / "main.py").write_text("print('my fix')\n", encoding="utf-8")

    with pytest.raises(updater.UpdateRefused, match="local changes"):
        updater.update(installed, uv=command)

    assert (installed / "main.py").read_text(encoding="utf-8") == "print('my fix')\n"
    assert calls() == []


def test_local_commits_are_refused_not_discarded(installed, upstream, uv):
    command, _ = uv
    release(upstream, "print('v2')\n", "v2")
    mine = commit(installed, "notes.txt", "mine", "a local commit")

    with pytest.raises(updater.UpdateRefused, match="1 local commit"):
        updater.update(installed, uv=command)

    assert git(installed, "rev-parse", "HEAD") == mine


def test_a_detached_checkout_is_refused(installed, uv):
    command, _ = uv
    git(installed, "checkout", "-q", "--detach")

    with pytest.raises(updater.UpdateRefused, match="detached"):
        updater.update(installed, uv=command)


def test_something_that_is_not_a_checkout_is_refused(tmp_path, uv):
    command, _ = uv

    with pytest.raises(updater.UpdateRefused, match="not a Git checkout"):
        updater.update(tmp_path, uv=command)


def test_a_failed_dependency_sync_says_the_code_already_moved(installed, upstream, uv, monkeypatch):
    command, _ = uv
    release(upstream, "print('v2')\n", "v2")
    monkeypatch.setenv("FAKE_UV_EXIT", "1")

    with pytest.raises(updater.UpdateFailed, match="code was updated, but its dependencies"):
        updater.update(installed, uv=command)


def test_gsm_update_check_reports_as_json(installed, upstream):
    release(upstream, "print('v2')\n", "v2: something better")
    out, err = io.StringIO(), io.StringIO()

    code = main(
        ["update", "--check", "--json"], paths=Paths(root=installed), stdout=out, stderr=err
    )

    assert code == 0, err.getvalue()
    body = json.loads(out.getvalue())
    assert body["available"] == 1 and body["incoming"][0].endswith("v2: something better")


def test_gsm_update_is_refused_while_the_window_is_open(installed, upstream):
    release(upstream, "print('v2')\n", "v2")
    held = lock.acquire(Paths(root=installed))
    out, err = io.StringIO(), io.StringIO()
    try:
        code = main(["update"], paths=Paths(root=installed), stdout=out, stderr=err)
    finally:
        held.release()

    assert code == 1 and "already running" in err.getvalue()
    assert (installed / "main.py").read_text(encoding="utf-8") == "print('v1')\n"
