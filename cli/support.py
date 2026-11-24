"""What every command shares: the context it runs in, how it names an Entry, how it reports,
and how it asks before doing anything destructive."""

from __future__ import annotations

import hashlib
import sys
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any, TextIO

from core import device_flow, entries, github
from core.credentials import CredentialStore
from core.entries import Entry
from core.paths import Paths
from core.session import GitHubSession
from core.startup import App
from core.transaction import Preview

# --- exit codes -------------------------------------------------------------------------------

OK = 0
FAILED = 1
"""Refused, failed, or cancelled. The message says which, and what to do."""
USAGE = 2
"""The command line itself was wrong (argparse's own code)."""
NEEDS_CONFIRMATION = 3
"""A destructive command printed its preview and stopped: nothing was done. Re-run it with
`--yes`, or with the `--approve` token the preview carried."""


# --- what a command produces -------------------------------------------------------------------


@dataclass
class Report:
    """A command's result: `data` for `--json`, `lines` for a person. Both say the same thing."""

    data: dict[str, Any] = field(default_factory=dict)
    lines: list[str] = field(default_factory=list)
    code: int = OK


class CliError(Exception):
    """A command could not do what was asked. Nothing beyond what the message says was done."""

    def __init__(
        self,
        message: str,
        kind: str = "error",
        code: int = FAILED,
        data: dict[str, Any] | None = None,
        lines: list[str] | None = None,
    ) -> None:
        super().__init__(message)
        self.kind = kind
        self.code = code
        self.data = data or {}
        self.lines = lines or []
        """Printed before the message: a preview that was not approved, say."""


class DryRun(Exception):
    """`--dry-run`: the preview is the whole result, and nothing was done."""

    def __init__(self, data: dict[str, Any], lines: list[str]) -> None:
        super().__init__("dry run")
        self.data = data
        self.lines = lines


# --- the context --------------------------------------------------------------------------------


def _sleep(seconds: float) -> bool:
    """The device-flow poll's wait: sleep, and never report a cancel (Ctrl+C raises instead)."""
    time.sleep(seconds)
    return False


@dataclass
class Context:
    """Everything a command may touch. Tests build one around a temp `Paths` and fakes."""

    paths: Paths
    store: CredentialStore
    stdin: TextIO
    stdout: TextIO
    stderr: TextIO
    json: bool = False
    app: App | None = None
    api: github.Api = field(default_factory=github.RestApi)
    transport: device_flow.Transport = field(default_factory=device_flow.FormTransport)
    wait: Callable[[float], bool] = _sleep
    open_url: Callable[[str], object] | None = None
    session: GitHubSession = field(init=False)

    def __post_init__(self) -> None:
        self.session = GitHubSession(self.store, transport=self.transport)

    @property
    def interactive(self) -> bool:
        """A person is at the keyboard to answer a question. Never in `--json` mode: a
        machine reading JSON is not going to type `y`."""
        if self.json:
            return False
        return is_terminal(self.stdin)

    @property
    def the_app(self) -> App:
        if self.app is None:
            raise AssertionError("this command runs without the application started")
        return self.app

    def say(self, line: str = "") -> None:
        """Progress meant for a person, on stderr so stdout stays the result alone."""
        print(line, file=self.stderr, flush=True)

    def token(self) -> str:
        """The GitHub token, or a refusal naming the way to get one."""
        found = self.session.token()
        if found is None:
            raise CliError(
                "This Machine is not signed in to GitHub. Run `gsm sign-in` first.",
                kind="not_signed_in",
            )
        return found


def is_terminal(stream: TextIO) -> bool:
    """Whether a person can type into this stream.

    `isatty` alone is not enough on Windows, where it answers True for the NUL device - which
    is exactly what a script, a scheduler or an agent attaches as stdin. A question asked
    there reads end-of-file at once, so a command meant to stop with "needs confirmation"
    would instead report a cancel. On Windows a terminal is a console, so ask for the console
    mode, which only a console has.
    """
    try:
        if not stream.isatty():
            return False
    except (AttributeError, ValueError):
        return False
    if sys.platform != "win32":
        return True
    try:
        fd = stream.fileno()
    except (AttributeError, OSError, ValueError):
        return True  # not backed by a file at all: an in-process stand-in that says it is a tty
    import ctypes
    import msvcrt

    mode = ctypes.c_ulong()
    handle = msvcrt.get_osfhandle(fd)
    return bool(ctypes.windll.kernel32.GetConsoleMode(handle, ctypes.byref(mode)))


def ask(ctx: Context, question: str) -> str | None:
    """Ask a person at the terminal; their answer, casefolded - or None if the input ended
    without one, which is no answer at all rather than a no."""
    print(question, end=" ", file=ctx.stderr, flush=True)
    line = ctx.stdin.readline()
    if not line:
        print(file=ctx.stderr, flush=True)
        return None
    return line.strip().casefold()


def counted(count: int, word: str, plural: str | None = None) -> str:
    """`1 commit`, `2 commits`."""
    return f"{count} {word if count == 1 else plural or word + 's'}"


# --- naming Entries ---------------------------------------------------------------------------


def quoted(text: str) -> str:
    """An argument as it would have to be typed: quoted if it holds a space."""
    return f'"{text}"' if any(c.isspace() for c in text) or not text else text


def find_entry(paths: Paths, token: str) -> Entry:
    """The Entry `token` names: its ID, its Display Name (any case), or a unique ID prefix.

    Names are labels, not identities (ADR-0004), so two Entries may share one. That is
    refused as ambiguous rather than guessed at - acting on the wrong Entry is how a save is
    overwritten - and the IDs are listed so the next attempt can be exact.
    """
    found = entries.list_all(paths)
    for entry in found:
        if entry.entry_id == token:
            return entry

    named = [entry for entry in found if entry.name.casefold() == token.casefold()]
    if not named and len(token) >= 4:
        named = [entry for entry in found if entry.entry_id.startswith(token.lower())]
    if len(named) == 1:
        return named[0]
    if named:
        listed = ", ".join(f"{entry.name} ({entry.entry_id})" for entry in named)
        raise CliError(
            f"{quoted(token)} matches {len(named)} Entries: {listed}. Name one by its ID.",
            kind="ambiguous_entry",
        )
    raise CliError(
        f"No Entry is named {quoted(token)}. `gsm status` lists every Entry.",
        kind="unknown_entry",
    )


def find_entries(paths: Paths, tokens: Iterable[str]) -> list[Entry]:
    """Several Entries, each named once, in the order given."""
    found: dict[str, Entry] = {}
    for token in tokens:
        entry = find_entry(paths, token)
        found.setdefault(entry.entry_id, entry)
    return list(found.values())


# --- confirmation (Invariant 7) -------------------------------------------------------------------


def approval_of(previews: Iterable[Preview]) -> str:
    """A short token naming exactly these previews: which Entries, what each Live Save holds
    now, and what would be written over it.

    It is the command line's form of handing the approved `Preview` back to the executor. A
    `--dry-run` prints it; `--approve <token>` proceeds only if a fresh preview yields the same
    token - so a game that saved after the preview was read is refused, not overwritten.
    """
    digest = hashlib.sha256()
    for preview in sorted(previews, key=lambda p: p.entry_id):
        digest.update(f"{preview.entry_id}\0{preview.live_hash}\0{preview.source_hash}\n".encode())
    return digest.hexdigest()[:12]


def preview_data(preview: Preview) -> dict[str, Any]:
    """A Live Save write, structured: every path it touches, and whether a Backup is taken."""
    return {
        "entry_id": preview.entry_id,
        "nothing_to_write": preview.is_noop,
        "backup_first": preview.will_back_up and not preview.is_noop,
        "writes": [
            {
                "live_path": str(part.live_path),
                "path": change.path,
                "change": change.change.value,
                "size_bytes": change.size_bytes,
            }
            for part in preview.parts
            for change in part.changes
        ],
    }


def confirm(
    ctx: Context,
    args: Any,
    lines: list[str],
    question: str,
    data: dict[str, Any] | None = None,
    approval: str | None = None,
) -> None:
    """Show what is about to happen, then return to go ahead - or raise to stop.

    In order: `--dry-run` stops with the preview as the result. `--approve` goes ahead only if
    its token is this preview's. `--yes` goes ahead. A person at a terminal is asked. Anything
    else - a script, an agent - stops with the preview and exit code 3, having done nothing:
    a destructive command never proceeds on silence.
    """
    data = {"preview": lines, **(data or {})}
    if approval is not None:
        data["approval"] = approval
    hint = (
        [f"Approval: {approval}  (--approve {approval} proceeds only if nothing changes first)"]
        if approval is not None
        else []
    )

    if getattr(args, "dry_run", False):
        raise DryRun(data={"dry_run": True, **data}, lines=[*lines, "", *hint] if hint else lines)

    approve = getattr(args, "approve", None)
    if approve is not None:
        if approval is None or approve != approval:
            raise CliError(
                "The preview no longer matches the approval: something changed since it was "
                f"shown (approved {approve}, now {approval}). Nothing has been done. Look at "
                "the new preview and approve that instead.",
                kind="stale_preview",
                data=data,
                lines=[*lines, "", *hint],
            )
        _show(ctx, lines)
        return

    if getattr(args, "yes", False):
        _show(ctx, lines)
        return

    answer = None
    if ctx.interactive:
        _show(ctx, lines)
        answer = ask(ctx, f"{question} [y/N]")
        if answer in ("y", "yes"):
            return
        if answer is not None:
            raise CliError("Cancelled. Nothing has been done.", kind="cancelled")

    raise CliError(
        "Nothing has been done: this needs confirmation. Re-run with --yes"
        + (" or --approve" if approval is not None else "")
        + " to go ahead, or --dry-run to only preview.",
        kind="confirmation_required",
        code=NEEDS_CONFIRMATION,
        data=data,
        lines=[] if answer is None and ctx.interactive else [*lines, "", *hint] if hint else lines,
    )


def _show(ctx: Context, lines: list[str]) -> None:
    """The preview, ahead of the result, for a person; JSON carries it in the result instead."""
    if not ctx.json:
        for line in lines:
            print(line, file=ctx.stdout)
        print(file=ctx.stdout, flush=True)
