"""Driving Git, and keeping the token out of everything that persists.

The PAT tests do not mock `subprocess`. They put a **fake `git` on PATH** that writes down its
own `argv` and environment, and then assert against what a real Git would actually have
received. Mocking the call would only prove that we called our own mock the way we expected
to; this proves the token is not on the command line, where `ps` would show it to every other
process on the machine.

The fake is a shell script on POSIX and a batch file on Windows. The two record differently -
one argument per line versus the raw command-line tail, which is exactly what Task Manager
would show - so the assertions here are written to hold for both shapes.
"""

import os
import subprocess
from pathlib import Path

import pytest

from core.git import (
    PAT_ENV,
    Git,
    GitError,
    GitMissing,
    GitTimeout,
)

TOKEN = "ghp_a-real-looking-secret-token"

WINDOWS = os.name == "nt"


def fake_git(directory: Path, posix: str, windows: str) -> None:
    """Drop a fake `git` into `directory`, in whichever language this OS can execute."""
    if WINDOWS:
        (directory / "git.bat").write_text(windows, encoding="utf-8", newline="\r\n")
    else:
        script = directory / "git"
        script.write_text(posix, encoding="utf-8")
        script.chmod(0o755)


def fake_path(directory: Path) -> str:
    """A PATH that resolves `git` to the fake, and still lets the fake run its own tools."""
    if WINDOWS:
        system32 = Path(os.environ.get("SYSTEMROOT", r"C:\Windows")) / "System32"
        return f"{directory}{os.pathsep}{system32}"
    return f"{directory}:{Path('/usr/bin')}:{Path('/bin')}"


@pytest.fixture
def spy(tmp_path, monkeypatch):
    """A fake `git` that records how it was invoked, ahead of the real one on PATH."""
    recorded = tmp_path / "recorded"
    recorded.mkdir()

    binary = tmp_path / "bin"
    binary.mkdir()
    fake_git(
        binary,
        posix='#!/bin/sh\nprintf "%s\\n" "$@" > "$GSM_RECORD/argv"\nenv > "$GSM_RECORD/env"\n'
        "exit 0\n",
        windows='@echo off\n>"%GSM_RECORD%\\argv" echo %*\n>"%GSM_RECORD%\\env" set\nexit /b 0\n',
    )

    monkeypatch.setenv("PATH", fake_path(binary))
    monkeypatch.setenv("GSM_RECORD", str(recorded))

    class Spy:
        argv = recorded / "argv"
        env = recorded / "env"

        def argv_text(self) -> str:
            return self.argv.read_text(encoding="utf-8")

        def env_lines(self) -> dict[str, str]:
            pairs = {}
            for line in self.env.read_text(encoding="utf-8").splitlines():
                if "=" in line:
                    key, _, value = line.partition("=")
                    pairs[key] = value
            return pairs

    return Spy()


# --- the token ----------------------------------------------------------------------------


def test_the_token_never_appears_on_the_command_line(tmp_path, spy):
    """`ps` shows every process's argv to every user on the machine. A token there is a token
    published."""
    Git(tmp_path).run("push", "origin", "main", pat=TOKEN)

    assert TOKEN not in spy.argv_text()


def test_the_token_is_passed_in_the_environment_instead(tmp_path, spy):
    Git(tmp_path).run("push", "origin", "main", pat=TOKEN)

    assert spy.env_lines()[PAT_ENV] == TOKEN


def test_the_credential_helper_reads_the_token_from_the_environment(tmp_path, spy):
    """The helper text is a shell function that reads a variable. It holds no secret itself,
    so nothing sensitive is written to disk or shown in the process list."""
    Git(tmp_path).run("push", "origin", "main", pat=TOKEN)
    argv = spy.argv_text()

    assert f"password=${PAT_ENV}" in argv
    assert "username=token" in argv


def test_the_system_credential_helper_is_reset_first(tmp_path, spy):
    """Without the empty `credential.helper=`, the macOS Keychain (or Windows Credential
    Manager) answers first, with whatever stale credential it happens to be holding - and our
    helper, which has the right one, is never asked."""
    Git(tmp_path).run("push", "origin", "main", pat=TOKEN)
    argv = spy.argv_text()

    # The first `credential.helper=` in argv must be the bare reset - nothing glued onto it -
    # with our own helper somewhere after. Position, not line numbers: the two recording
    # formats agree on order but not on layout.
    first = argv.index("credential.helper=")
    following = argv[first + len("credential.helper=") :][:1]

    assert following in ("", " ", "\n")
    assert "credential.helper=!f()" in argv


def test_no_credential_helper_is_configured_for_local_commands(tmp_path, spy):
    """A commit does not touch the network, so it never sees the token at all."""
    Git(tmp_path).run("commit", "-m", "sync")

    assert "credential.helper" not in spy.argv_text()
    assert PAT_ENV not in spy.env_lines()


def test_a_token_in_the_parent_environment_is_not_inherited(tmp_path, spy, monkeypatch):
    """Whatever else is going on, the only token Git ever sees is the one we chose to give it."""
    monkeypatch.setenv(PAT_ENV, "a-token-from-somewhere-else")

    Git(tmp_path).run("status")

    assert PAT_ENV not in spy.env_lines()


def test_a_token_is_redacted_out_of_an_error(tmp_path, monkeypatch):
    """Git echoes the remote URL in its errors. If a token ever reaches one, it must not reach
    a log file, a crash report, or a screenshot."""
    binary = tmp_path / "bin"
    binary.mkdir()
    fake_git(
        binary,
        posix=f'#!/bin/sh\necho "fatal: authentication failed for {TOKEN}" >&2\nexit 128\n',
        windows=f"@echo off\necho fatal: authentication failed for {TOKEN} 1>&2\nexit /b 128\n",
    )
    monkeypatch.setenv("PATH", fake_path(binary))

    with pytest.raises(GitError) as raised:
        Git(tmp_path).run("push", pat=TOKEN)

    assert TOKEN not in str(raised.value)
    assert "***" in raised.value.stderr


# --- Git is never allowed to ask a question ---------------------------------------------------


def test_git_is_never_allowed_to_prompt(tmp_path, spy):
    """In a GUI application with no terminal attached, a password prompt is not a prompt. It is
    a hang, and the user sees an app that has simply frozen forever."""
    Git(tmp_path).run("fetch", pat=TOKEN)
    env = spy.env_lines()

    assert env["GIT_TERMINAL_PROMPT"] == "0"
    # Windows cannot represent an empty environment variable, so `set` records it as absent.
    # Absent and empty both mean the same thing here: no askpass program will ever run.
    assert env.get("GIT_ASKPASS", "") == ""


def test_the_users_global_config_cannot_reach_the_vault(tmp_path, spy):
    """A global core.autocrlf=true silently rewrites line endings in every file Git guesses is
    text - and it guesses wrong on save files often enough to corrupt them. A global
    core.hooksPath would run someone else's hooks against our commits."""
    Git(tmp_path).run("status")
    argv = spy.argv_text()

    assert "core.autocrlf=false" in argv
    assert "core.hooksPath=/dev/null" in argv
    assert "commit.gpgsign=false" in argv


# --- failures ------------------------------------------------------------------------------------


def test_a_failing_command_raises_with_its_stderr(tmp_path):
    with pytest.raises(GitError) as raised:
        Git(tmp_path).run("rev-parse", "--verify", "definitely-not-a-ref")

    assert raised.value.returncode != 0


def test_a_failing_command_can_be_asked_not_to_raise(tmp_path):
    Git(tmp_path).run("rev-parse", "--verify", "nope", check=False)  # must not raise


def test_a_missing_git_is_reported_clearly(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", str(tmp_path / "nothing-here"))

    with pytest.raises(GitMissing):
        Git(tmp_path).run("status")


def test_a_hanging_command_times_out_rather_than_wedging_the_app(tmp_path, monkeypatch):
    binary = tmp_path / "bin"
    binary.mkdir()
    fake_git(
        binary,
        posix="#!/bin/sh\nsleep 30\n",
        # `ping` is the batch idiom for sleeping (`timeout` refuses redirected stdin), and it
        # is kept short: it inherits the stdout pipe, and killing cmd.exe does not kill it,
        # so the test waits for it regardless of the 0.5s Git timeout.
        windows="@ping -n 4 127.0.0.1 > nul\n",
    )
    monkeypatch.setenv("PATH", fake_path(binary))

    with pytest.raises(GitTimeout):
        Git(tmp_path, timeout=0.5).run("fetch")


def test_stdout_is_returned(tmp_path):
    subprocess.run(["git", "init", "-q", "-b", "main", str(tmp_path)], check=True)

    assert Git(tmp_path).run("branch", "--show-current").strip() == "main"
