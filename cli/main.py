"""`gsm <command>`: parse, start the application, run one command, report, exit.

Output contract, for people and for scripts alike:

- **stdout** carries the result and nothing else: plain lines, or with `--json` exactly one
  JSON object, `{"ok": true, "command": ..., ...}` or `{"ok": false, "error": {"kind", "message"}}`.
- **stderr** carries progress, warnings and questions, so a pipe never mixes them in.
- **The exit code** says how it went: 0 done, 1 refused or failed, 2 the command line was
  wrong, 3 a destructive command stopped at its preview for want of confirmation.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import traceback
from collections.abc import Callable, Sequence
from typing import Any, TextIO

from cli import commands
from cli.support import (
    OK,
    USAGE,
    CliError,
    Context,
    DryRun,
    Report,
    counted,
)
from core import config as config_module
from core import device_flow, github, ledger, lock, startup, updater
from core import paths as paths_module
from core.config import MachineDescription
from core.credentials import CredentialStore, KeyringCredentialStore
from core.entries import UnknownEntry
from core.git import GitMissing, GitTimeout
from core.logger import LOGGER_NAME
from core.paths import Paths, WorkspaceNotWritable
from core.transaction import LiveParentMissing, NotAFile, StalePreview

DESCRIPTION = """\
Git Save Manager keeps game saves and settings files in a Git-backed Vault, carried between
Machines through a private GitHub repository (the Cloud Vault).

Two axes, never confused:
  sync / restore   move an Entry between its Live Save on disk and the Vault
  push / pull      move commits between the Vault and the Cloud Vault

Run `gsm` with no command to open the window. Run `gsm COMMAND --help` for one command.
"""

EPILOG = """\
An Entry is named by its Display Name (any case), its ID, or a unique ID prefix.

Destructive commands print exactly what they will do first, then ask. Without a terminal to
ask at, they stop with exit code 3 and do nothing unless given --yes - or, for a write over a
Live Save, --approve TOKEN, which proceeds only if nothing changed since the preview that
printed that token.

--json makes stdout a single JSON object. Exit codes: 0 done, 1 refused or failed, 2 bad
command line, 3 needs confirmation.

The window and gsm commands share one lock: while the window is open, commands are refused.
"""

KNOWN_FAILURES = (
    StalePreview,
    LiveParentMissing,
    NotAFile,
    GitMissing,
    GitTimeout,
    config_module.ConfigTooNew,
    ledger.LedgerTooNew,
)
"""Refusals core raises with a message written for the user: shown as they are."""


# --- the parser ---------------------------------------------------------------------------------


def _common() -> argparse.ArgumentParser:
    """Options every command accepts, in either position: `gsm --json status` and
    `gsm status --json`. SUPPRESS keeps a subcommand from overwriting the top-level value."""
    found = argparse.ArgumentParser(add_help=False)
    found.add_argument(
        "--json",
        action="store_true",
        default=argparse.SUPPRESS,
        help="print the result as one JSON object on stdout",
    )
    found.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        default=argparse.SUPPRESS,
        help="also print progress as it happens, on stderr",
    )
    return found


def _confirmation(parser: argparse.ArgumentParser, approve: bool = False) -> None:
    group = parser.add_mutually_exclusive_group()
    group.add_argument("-y", "--yes", action="store_true", help="go ahead without asking")
    group.add_argument(
        "--dry-run", action="store_true", help="only print what would happen; change nothing"
    )
    if approve:
        group.add_argument(
            "--approve",
            metavar="TOKEN",
            help="go ahead only if the preview still matches the one that printed TOKEN",
        )


def _sign_in_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--token-stdin",
        action="store_true",
        help="read a GitHub token (classic PAT, repo scope) from stdin instead of signing in "
        "in the browser",
    )
    parser.add_argument(
        "--no-browser",
        action="store_true",
        help="print the sign-in address without opening a browser",
    )


def build_parser() -> argparse.ArgumentParser:
    common = _common()
    parser = argparse.ArgumentParser(
        prog="gsm",
        description=DESCRIPTION,
        epilog=EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--json",
        action="store_true",
        default=False,
        help="print the result as one JSON object on stdout",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        default=False,
        help="also print progress as it happens, on stderr",
    )
    sub = parser.add_subparsers(dest="command", metavar="COMMAND", required=True)

    def command(name: str, handler: Callable, summary: str, **kwargs: Any):
        found = sub.add_parser(name, parents=[common], help=summary, description=summary, **kwargs)
        found.set_defaults(handler=handler)
        return found

    # Looking
    found = command("status", commands.status, "every Entry's state, and the Cloud Vault's")
    found.add_argument("entries", nargs="*", metavar="ENTRY", help="only these Entries")
    found.add_argument(
        "--fetch",
        action="store_true",
        help="ask the Cloud Vault first, rather than report the last fetch",
    )

    found = command("show", commands.show, "everything about one Entry")
    found.add_argument("entry", metavar="ENTRY")

    found = command("history", commands.history, "every commit that touched an Entry")
    found.add_argument("entry", metavar="ENTRY")
    found.add_argument("-n", "--limit", type=int, default=50, help="at most this many (50)")

    found = command("backups", commands.backups_list, "an Entry's Backups, newest first")
    found.add_argument("entry", metavar="ENTRY")

    command("machines", commands.machines, "every Machine registered in the Vault")

    found = command("log", commands.log, "the Vault's full Git log")
    found.add_argument("-n", "--limit", type=int, default=100, help="at most this many (100)")

    # Live Save <-> Vault
    found = command("sync", commands.sync, "copy Live Saves into the Vault (one commit each)")
    found.add_argument("entries", nargs="*", metavar="ENTRY", help="the Entries to Sync")
    found.add_argument("--all", action="store_true", help="every Entry that is Local Ahead")
    found.add_argument(
        "-m",
        "--message",
        metavar="NOTE",
        help="a one-line note on what this captures, kept in History",
    )

    found = command(
        "restore", commands.restore, "write the Vault's version (or a Backup) over Live Saves"
    )
    found.add_argument("entries", nargs="*", metavar="ENTRY", help="the Entries to Restore")
    found.add_argument("--all", action="store_true", help="every Entry that is Vault Ahead")
    found.add_argument(
        "--backup",
        metavar="N",
        help="restore Backup N (from `gsm backups`) instead of the Vault's version",
    )
    _confirmation(found, approve=True)

    found = command("resolve", commands.resolve, "decide a Conflict: keep one side, whole")
    found.add_argument("entry", metavar="ENTRY")
    found.add_argument(
        "--keep",
        choices=("live", "vault"),
        required=True,
        help="live: keep this Machine's save and Sync it; vault: take the "
        "Vault's version and write it over the Live Save",
    )
    _confirmation(found, approve=True)

    found = command(
        "rollback",
        commands.rollback,
        "roll an Entry's Vault copy back to an earlier commit",
    )
    found.add_argument("entry", metavar="ENTRY")
    found.add_argument("commit", metavar="COMMIT", help="a commit from `gsm history`")
    _confirmation(found)

    # Entries
    found = command("add", commands.add, "add an Entry: a folder, a file, or several files")
    found.add_argument("name", metavar="NAME", help="its Display Name")
    found.add_argument(
        "paths",
        nargs="+",
        metavar="PATH",
        help="one folder or file; several files make one File Set",
    )

    found = command("bind", commands.bind, "bind an Unlinked Entry to a path on this Machine")
    found.add_argument("entry", metavar="ENTRY")
    found.add_argument(
        "paths",
        nargs="+",
        metavar="PATH",
        help="the path here; for a File Set, MEMBER=PATH for each file",
    )

    found = command(
        "unbind",
        commands.unbind,
        "forget an Entry on this Machine (no file is touched)",
    )
    found.add_argument("entry", metavar="ENTRY")

    found = command("rename", commands.rename, "change an Entry's Display Name")
    found.add_argument("entry", metavar="ENTRY")
    found.add_argument("name", metavar="NEW_NAME")

    found = command(
        "remove",
        commands.remove,
        "remove an Entry from the Vault, for every Machine",
    )
    found.add_argument("entry", metavar="ENTRY")
    _confirmation(found)

    found = command("add-file", commands.add_file, "keep one more file in an Entry")
    found.add_argument("entry", metavar="ENTRY")
    found.add_argument("path", metavar="PATH")

    found = command(
        "remove-file",
        commands.remove_file,
        "take a file out of a File Set (the file stays where it is)",
    )
    found.add_argument("entry", metavar="ENTRY")
    found.add_argument("member", metavar="MEMBER", help="its name, as `gsm show` lists it")
    _confirmation(found)

    found = command(
        "download", commands.download, "save a plain copy of the Vault's version (binds nothing)"
    )
    found.add_argument("entry", metavar="ENTRY")
    found.add_argument("--to", metavar="DIR", help="where to put it (default: ~/Downloads)")

    # Vault <-> Cloud Vault
    command("fetch", commands.fetch, "check the Cloud Vault for new commits (changes nothing)")
    command("push", commands.push, "publish this Machine's commits to the Cloud Vault")
    found = command(
        "pull",
        commands.pull,
        "bring the Cloud Vault's commits into the Vault",
    )
    found.add_argument(
        "--take",
        action="append",
        metavar="ENTRY=SIDE",
        help="for an Entry Synced on two Machines: vault keeps this Machine's "
        "line, cloud takes the other's (repeatable)",
    )
    found.add_argument(
        "--take-all", choices=("vault", "cloud"), help="the same side for every contested Entry"
    )
    command("check", commands.check, "test the connection and sign-in to the Cloud Vault")

    # Setup and maintenance
    found = command("setup", commands.setup, "set this Machine up against a GitHub repository")
    found.add_argument(
        "repo",
        metavar="OWNER/REPOSITORY",
        help="created (private) if it does not exist; joined if it is a Vault",
    )
    _sign_in_options(found)
    adopt = found.add_mutually_exclusive_group()
    adopt.add_argument(
        "--adopt",
        dest="adopt",
        action="store_true",
        default=None,
        help="reclaim this Machine's old identity if the Vault lists one",
    )
    adopt.add_argument(
        "--no-adopt",
        dest="adopt",
        action="store_false",
        help="register as a new Machine even if an old identity is listed",
    )

    found = command("sign-in", commands.sign_in, "sign this Machine in to GitHub again")
    _sign_in_options(found)

    found = command(
        "redo",
        commands.redo_init,
        "start this Machine over: Redo Initialization (keeps Backups)",
    )
    found.add_argument(
        "--discard-unpushed",
        action="store_true",
        help="also destroy commits that were never pushed",
    )
    _confirmation(found)

    found = command("update", None, "update Git Save Manager itself to the latest version")
    found.add_argument(
        "--check", action="store_true", help="only say whether an update is available"
    )
    return parser


# --- output -----------------------------------------------------------------------------------


def _emit(ctx: Context, command: str, ok: bool, data: dict[str, Any], lines: list[str]) -> None:
    if ctx.json:
        body = {"ok": ok, "command": command, **data}
        print(json.dumps(body, indent=2, ensure_ascii=True, default=str), file=ctx.stdout)
    else:
        for line in lines:
            print(line, file=ctx.stdout)
    ctx.stdout.flush()


def _fail(ctx: Context, command: str, error: CliError) -> int:
    if ctx.json:
        data = {**error.data, "error": {"kind": error.kind, "message": str(error)}}
        _emit(ctx, command, False, data, [])
    else:
        if error.lines:
            _emit(ctx, command, False, {}, error.lines)
        print(f"gsm: {error}", file=ctx.stderr, flush=True)
    return error.code


class _Formatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        text = record.getMessage()
        return text if record.levelno < logging.WARNING else f"{record.levelname.lower()}: {text}"


def _configure_logging(stream: TextIO, verbose: bool) -> None:
    """Core's log on stderr: warnings always, progress with --verbose. Never on stdout, which
    belongs to the result. Replaces the window's handlers rather than adding to them."""
    logger = logging.getLogger(LOGGER_NAME)
    logger.handlers = []
    handler = logging.StreamHandler(stream)
    handler.setFormatter(_Formatter())
    logger.addHandler(handler)
    logger.setLevel(logging.INFO if verbose else logging.WARNING)
    logger.propagate = False


def _kind(error: Exception) -> str:
    name = type(error).__name__
    return "".join(f"_{c.lower()}" if c.isupper() else c for c in name).lstrip("_")


# --- running --------------------------------------------------------------------------------


def _update(ctx: Context, args: Any) -> Report:
    """`gsm update`, which runs without starting the application: it needs no Vault, only the
    lock - so the window is not running code, or loading libraries, being replaced under it."""
    root = ctx.paths.root
    try:
        if args.check:
            standing = updater.check(root)
            return _update_report(standing, None)
        paths_module.check_writable(ctx.paths)
        held = lock.acquire(ctx.paths)
        try:
            ctx.say("Checking for an update...")
            done = updater.update(root)
        finally:
            held.release()
    except (updater.UpdateRefused, updater.UpdateFailed) as error:
        raise CliError(str(error), kind=_kind(error)) from error
    return _update_report(done.before, done.now)


def _update_report(standing: updater.Standing, now: str | None) -> Report:
    data = {
        "branch": standing.branch,
        "from": standing.current,
        "latest": standing.latest,
        "available": standing.behind,
        "incoming": list(standing.incoming),
    }
    if now is None:  # --check
        if standing.up_to_date:
            lines = [f"Up to date ({standing.current[:7]} on {standing.branch})."]
        else:
            lines = [
                f"{counted(standing.behind, 'update')} available on {standing.branch} "
                f"({standing.current[:7]} -> {standing.latest[:7]}):"
            ]
            lines += [f"  {line}" for line in standing.incoming[:20]]
            if len(standing.incoming) > 20:
                lines.append(f"  ... and {len(standing.incoming) - 20} more")
            lines.append(f"`gsm update` installs {'it' if standing.behind == 1 else 'them'}.")
        return Report(data=data, lines=lines)

    data["now"] = now
    if now == standing.current:
        return Report(
            data={**data, "updated": False},
            lines=[f"Already up to date ({now[:7]} on {standing.branch})."],
        )
    lines = [
        f"Updated {standing.branch} from {standing.current[:7]} to {now[:7]} "
        f"({counted(standing.behind, 'commit')}):"
    ]
    lines += [f"  {line}" for line in standing.incoming[:20]]
    if len(standing.incoming) > 20:
        lines.append(f"  ... and {len(standing.incoming) - 20} more")
    return Report(data={**data, "updated": True}, lines=lines)


def main(
    argv: Sequence[str],
    *,
    paths: Paths | None = None,
    store: CredentialStore | None = None,
    description: MachineDescription | None = None,
    stdin: TextIO | None = None,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
    api: github.Api | None = None,
    transport: device_flow.Transport | None = None,
    wait: Callable[[float], bool] | None = None,
    open_url: Callable[[str], object] | None = None,
) -> int:
    """Run one command. Everything after `argv` is for tests; production passes argv alone."""
    stdout = stdout or sys.stdout
    stderr = stderr or sys.stderr
    for stream in (stdout, stderr):
        # A Display Name the console's code page cannot show must not crash the command.
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")

    parser = build_parser()
    try:
        args = parser.parse_args(list(argv))
    except SystemExit as exit_:
        return int(exit_.code or 0) if exit_.code in (0, None) else USAGE

    ctx = Context(
        paths=paths or Paths.default(),
        store=store or KeyringCredentialStore(),
        stdin=stdin or sys.stdin,
        stdout=stdout,
        stderr=stderr,
        json=args.json,
        **({"api": api} if api else {}),
        **({"transport": transport} if transport else {}),
        **({"wait": wait} if wait else {}),
        open_url=open_url,
    )
    _configure_logging(stderr, args.verbose)
    name = args.command

    try:
        if name == "update":
            report = _update(ctx, args)
        else:
            ctx.app = startup.start(ctx.paths, description)
            try:
                report = args.handler(ctx, args)
            finally:
                ctx.app.shutdown()
    except DryRun as dry:
        _emit(ctx, name, True, dry.data, dry.lines)
        return OK
    except CliError as error:
        return _fail(ctx, name, error)
    except (lock.AlreadyRunning, WorkspaceNotWritable) as refusal:
        return _fail(ctx, name, CliError(str(refusal), kind=_kind(refusal)))
    except KNOWN_FAILURES as error:
        return _fail(ctx, name, CliError(str(error), kind=_kind(error)))
    except UnknownEntry as error:
        return _fail(ctx, name, CliError(f"No Entry {error.args[0]}.", kind="unknown_entry"))
    except KeyboardInterrupt:
        print("gsm: interrupted.", file=stderr, flush=True)
        return 130
    except Exception as error:  # noqa: BLE001 - reported, never swallowed
        if args.verbose:
            traceback.print_exc(file=stderr)
        message = f"unexpected {type(error).__name__}: {error}"
        if not args.verbose:
            message += " (run again with --verbose for the details)"
        return _fail(ctx, name, CliError(message, kind="unexpected"))

    _emit(ctx, name, report.code == OK, report.data, report.lines)
    return report.code


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
