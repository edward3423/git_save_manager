"""Every `gsm` command. Each one takes the context and the parsed arguments and returns a
Report, or raises a CliError saying what was not done and why.

The commands decide nothing the window does not. States, what each state offers, and every
preview come from `core` and `ui.presenter`; a command's job is to name the Entry, re-check its
state at the last moment, ask where the window asks, and say what happened.
"""

from __future__ import annotations

import contextlib
import webbrowser
from datetime import datetime
from pathlib import Path
from typing import Any

from cli.support import (
    CliError,
    Context,
    Report,
    approval_of,
    ask,
    confirm,
    counted,
    find_entries,
    find_entry,
    preview_data,
    quoted,
)
from core import backups, cloud, device_flow, entries, github, operations, redo, vault
from core.backups import Backup
from core.cloud import CloudOffline, CloudState, ForeignConflict, PushRejected, Side
from core.credentials import Credential
from core.entries import Entry
from core.entry_state import Action, EntryState
from core.git import GitError, GitTimeout
from core.transaction import Preview
from ui import presenter

VERBS = {
    Action.SYNC_TO_VAULT: "sync",
    Action.RESTORE_TO_LIVE: "restore",
    Action.RESOLVE: "resolve",
    Action.UNBIND: "unbind",
}
"""What each offered action is called on the command line: the command that performs it."""


# --- shared ----------------------------------------------------------------------------------


def _require_set_up(ctx: Context) -> None:
    if not ctx.the_app.config.is_set_up:
        raise CliError(
            "This Machine is not set up yet. Run `gsm setup OWNER/REPOSITORY` first.",
            kind="not_set_up",
        )


def _rows(ctx: Context) -> dict[str, presenter.Row]:
    """Every Entry's state, derived from disk right now. Called again immediately before each
    operation - the staleness rule - never cached across one."""
    app = ctx.the_app
    return {row.entry_id: row for row in presenter.rows(app.paths, app.the_ledger)}


def _state_key(row: presenter.Row) -> str:
    if row.status is not None:
        return row.status.state.value
    return "needs_binding" if row.bound else "unlinked"


def _actions(row: presenter.Row) -> list[str]:
    """The commands this Entry's state offers right now, most relevant first."""
    if row.status is None:
        return ["bind", "unbind"] if row.bound else ["bind"]
    return [VERBS[action] for action in row.status.offered]


def _recommended(row: presenter.Row) -> str | None:
    if row.status is None:
        return "bind"
    return VERBS[row.status.recommended] if row.status.recommended else None


def _next(row: presenter.Row) -> str:
    """The NEXT column: the safe default, or every choice when the human must pick one."""
    recommended = _recommended(row)
    if recommended is not None:
        return recommended
    return " or ".join(_actions(row)) or "-"


def _row_data(row: presenter.Row) -> dict[str, Any]:
    return {
        "id": row.entry_id,
        "name": row.name,
        "state": _state_key(row),
        "caption": row.caption,
        "bound": row.bound,
        "file_set": row.is_file_set,
        "actions": _actions(row),
        "recommended": _recommended(row),
    }


def _table(headers: list[str], rows: list[list[str]]) -> list[str]:
    widths = [max(len(cell) for cell in column) for column in zip(headers, *rows, strict=True)]
    return [
        "  ".join(cell.ljust(width) for cell, width in zip(line, widths, strict=True)).rstrip()
        for line in [headers, *rows]
    ]


def _offered(ctx: Context, entry: Entry, action: Action, hint: str = "") -> presenter.Row:
    """The staleness rule: re-derive the state now, and refuse unless it still offers this."""
    row = _rows(ctx)[entry.entry_id]
    if row.status is None or action not in row.status.offered:
        choices = _actions(row)
        raise CliError(
            f"{entry.name} is {row.caption}, which does not offer {VERBS[action]}. "
            + (f"It offers: {', '.join(choices)}." if choices else "There is nothing to do.")
            + (f" {hint}" if hint else ""),
            kind="not_offered",
            data={"entry": _row_data(row)},
        )
    return row


def _live_path(text: str) -> Path:
    """A path as typed at a terminal: `~` is home, and a relative path is relative to here.

    Made absolute because a Binding outlives this shell's working directory, but never
    resolved: a symlinked save folder stays bound as the link (see `ledger.normalize_live_path`).
    """
    return Path(text).expanduser().absolute()


def _when(moment: datetime) -> str:
    return f"{moment.astimezone():%Y-%m-%d %H:%M}"


def _cloud_words(state: CloudState | None, ahead: int, behind: int) -> str:
    if state is None:
        return "not compared yet (nothing pushed or fetched)"
    return {
        CloudState.UP_TO_DATE: "up to date",
        CloudState.AHEAD: f"{counted(ahead, 'commit')} to push",
        CloudState.BEHIND: f"{counted(behind, 'commit')} to pull",
        CloudState.DIVERGED: f"{counted(ahead, 'commit')} to push and {behind} to pull",
    }[state]


def _cloud(ctx: Context, fetch: bool) -> tuple[dict[str, Any], str]:
    """Where the Vault stands against the Cloud Vault: fetched now, or as of the last fetch."""
    app = ctx.the_app
    fresh = False
    if fetch:
        try:
            app.cloud.fetch_status(ctx.token())
            fresh = True
        except CloudOffline as error:
            ctx.say(f"warning: could not reach the Cloud Vault. {error}")

    try:
        state, ahead, behind = app.cloud.known_state()
    except GitError:
        state, ahead, behind = None, 0, 0

    offline = app.cloud.offline.reason.value if app.cloud.offline else None
    data = {
        "repo": app.config.repo,
        "branch": app.config.default_branch,
        "state": state.value if state else None,
        "ahead": ahead,
        "behind": behind,
        "fresh": fresh,
        "offline": offline,
    }
    words = _cloud_words(state, ahead, behind)
    if not fresh:
        words += " - as of the last fetch (`gsm status --fetch` checks now)"
    return data, f"Cloud Vault {app.config.repo} ({app.config.default_branch}): {words}"


# --- looking ----------------------------------------------------------------------------------


def status(ctx: Context, args: Any) -> Report:
    _require_set_up(ctx)
    cloud_data, cloud_line = _cloud(ctx, fetch=args.fetch)
    rows = list(_rows(ctx).values())
    if args.entries:
        wanted = {entry.entry_id for entry in find_entries(ctx.paths, args.entries)}
        rows = [row for row in rows if row.entry_id in wanted]

    lines = [cloud_line, ""]
    if rows:
        lines += _table(
            ["NAME", "STATE", "NEXT"], [[row.name, row.caption, _next(row)] for row in rows]
        )
    else:
        lines.append("No Entries yet. `gsm add NAME PATH` adds one.")
    return Report(data={"cloud": cloud_data, "entries": [_row_data(r) for r in rows]}, lines=lines)


def show(ctx: Context, args: Any) -> Report:
    _require_set_up(ctx)
    app = ctx.the_app
    entry = find_entry(ctx.paths, args.entry)
    row = _rows(ctx)[entry.entry_id]
    binding = app.the_ledger.get(entry.entry_id)

    if entry.is_file_set:
        kind = f"file set ({len(entry.files or ())} files)"
    elif entry.content_name is None:
        kind = "not known until the first Sync"
    elif vault.content_is_file(ctx.paths, entry.entry_id, entry.content_name):
        kind = "file"
    else:
        kind = "folder"

    live = presenter.live_lines(ctx.paths, app.the_ledger, entry.entry_id) if binding else []
    hints = presenter.bind_hints(ctx.paths, app.config, entry.entry_id)
    found_backups = backups.list_for(ctx.paths, entry.entry_id)
    last_sync = None
    if binding is not None and binding.last_sync_at:
        direction = {"to_vault": "to the Vault", "to_live": "to the Live Save"}.get(
            binding.last_sync_direction or "", ""
        )
        last_sync = f"{_when(datetime.fromisoformat(binding.last_sync_at))} {direction}".strip()

    data = {
        **_row_data(row),
        "kind": kind,
        "live": live,
        "last_sync": binding.last_sync_at if binding else None,
        "last_sync_direction": binding.last_sync_direction if binding else None,
        "backups": len(found_backups),
        "other_machines": [
            {"hostname": hint.hostname, "os": hint.os_name, "path": hint.path, "files": hint.files}
            for hint in hints
        ],
    }
    lines = [
        entry.name,
        f"  ID          {entry.entry_id}",
        f"  Kind        {kind}",
        f"  State       {row.caption}",
        f"  Next        {_next(row)}",
    ]
    if live:
        lines.append(f"  Live Save   {live[0]}")
        lines += [f"              {line}" for line in live[1:]]
    else:
        lines.append("  Live Save   not bound on this Machine")
    lines.append(f"  Last Sync   {last_sync or 'never on this Machine'}")
    lines.append(f"  Backups     {len(found_backups)}")
    if hints:
        lines.append("  Elsewhere")
        for hint in hints:
            first, *rest = hint.path.splitlines()
            lines.append(f"    {hint.machine}: {first}")
            lines += [f"      {line}" for line in rest]
    if row.status is not None and row.status.state is EntryState.REMOVED_FROM_VAULT:
        lines += [
            "",
            "Another Machine removed this Entry from the Vault. Unbind it here, or Sync "
            "to re-add it.",
        ]
    return Report(data=data, lines=lines)


def history(ctx: Context, args: Any) -> Report:
    _require_set_up(ctx)
    entry = find_entry(ctx.paths, args.entry)
    commits = operations.history(ctx.paths, entry.entry_id, limit=args.limit)
    unpushed = operations.unpushed_commits(ctx.paths)

    data = [
        {
            "sha": commit.sha,
            "short": commit.short,
            "when": commit.when.isoformat(),
            "machine": commit.machine,
            "subject": commit.subject,
            "note": commit.body,
            "current": index == 0,
            "unpushed": commit.sha in unpushed,
        }
        for index, commit in enumerate(commits)
    ]
    lines = []
    for index, commit in enumerate(commits):
        tags = ("[current] " if index == 0 else "") + (
            "[unpushed] " if commit.sha in unpushed else ""
        )
        note = f"  - {commit.body}" if commit.body else ""
        lines.append(
            f"{commit.short}  {_when(commit.when)}  {commit.machine}  {tags}{commit.subject}{note}"
        )
    if not lines:
        lines = [f"{entry.name} has no history yet."]
    return Report(data={"entry": entry.entry_id, "commits": data}, lines=lines)


def backups_list(ctx: Context, args: Any) -> Report:
    _require_set_up(ctx)
    entry = find_entry(ctx.paths, args.entry)
    found = backups.list_for(ctx.paths, entry.entry_id)
    data = [
        {
            "number": number,
            "file": backup.path.name,
            "path": str(backup.path),
            "taken_at": backup.taken_at.isoformat(),
            "reason": backup.reason,
            "size_bytes": backup.size_bytes,
        }
        for number, backup in enumerate(found, start=1)
    ]
    if not found:
        return Report(
            data={"entry": entry.entry_id, "backups": []}, lines=[f"{entry.name} has no Backups."]
        )
    lines = _table(
        ["#", "TAKEN", "REASON", "SIZE", "FILE"],
        [
            [str(n), _when(b.taken_at), b.reason, presenter.size_text(b.size_bytes), b.path.name]
            for n, b in enumerate(found, start=1)
        ],
    )
    lines += ["", f"`gsm restore {quoted(entry.name)} --backup N` restores one."]
    return Report(data={"entry": entry.entry_id, "backups": data}, lines=lines)


def machines(ctx: Context, args: Any) -> Report:
    _require_set_up(ctx)
    found = vault.list_machines(ctx.paths)
    data = [
        {
            "machine_id": machine.get("machine_id"),
            "hostname": machine.get("hostname"),
            "os": machine.get("os"),
            "bound": len(machine.get("bindings", {})),
            "this_machine": machine.get("machine_id") == ctx.the_app.config.machine_id,
        }
        for machine in found
    ]
    lines = _table(
        ["HOSTNAME", "OS", "BOUND", "MACHINE ID"],
        [
            [
                str(m["hostname"]) + (" (this Machine)" if m["this_machine"] else ""),
                str(m["os"]),
                str(m["bound"]),
                str(m["machine_id"]),
            ]
            for m in data
        ],
    )
    return Report(data={"machines": data}, lines=lines)


def log(ctx: Context, args: Any) -> Report:
    _require_set_up(ctx)
    raw = vault.git(ctx.paths).run(
        "log", "--graph", "--date=short", "--format=%h %ad %an  %s  %b", f"-{args.limit}"
    )
    lines = [line.rstrip() for line in raw.splitlines()]
    return Report(data={"log": lines}, lines=lines)


# --- Live Save <-> Vault ----------------------------------------------------------------------


def sync(ctx: Context, args: Any) -> Report:
    """Live Save -> Vault, one commit per Entry. The only writer of a new Baseline."""
    _require_set_up(ctx)
    app = ctx.the_app
    if args.all == bool(args.entries):
        raise CliError("Name the Entries to Sync, or use --all (not both).", kind="usage")

    if args.all:
        targets = [
            entries.require(ctx.paths, row.entry_id)
            for row in presenter.bulk_targets(
                presenter.visible_rows(list(_rows(ctx).values()), {EntryState.LOCAL_AHEAD}),
                Action.SYNC_TO_VAULT,
            )
        ]
        if not targets:
            return Report(data={"entries": []}, lines=["No Entry is Local Ahead; nothing to Sync."])
    else:
        targets = find_entries(ctx.paths, args.entries)
        for entry in targets:  # all checked before any is moved
            _offered(ctx, entry, Action.SYNC_TO_VAULT, _resolve_hint(ctx, entry))

    results, lines, failed = [], [], 0
    for entry in targets:
        try:
            _offered(ctx, entry, Action.SYNC_TO_VAULT)  # and again, at the moment it runs
            done = operations.sync_to_vault(
                ctx.paths,
                app.config,
                app.description,
                app.the_ledger,
                entry.entry_id,
                note=args.message or None,
            )
        except (
            CliError,
            operations.SyncAborted,
            operations.NothingToSync,
            operations.NeedsBinding,
        ) as error:
            failed += 1
            results.append(
                {
                    "id": entry.entry_id,
                    "name": entry.name,
                    "result": "failed",
                    "message": str(error),
                }
            )
            lines.append(f"{entry.name}: not Synced. {error}")
            continue
        results.append(
            {
                "id": entry.entry_id,
                "name": entry.name,
                "result": "synced" if done.commit else "unchanged",
                "commit": done.commit,
            }
        )
        lines.append(
            f"{entry.name}: Synced to the Vault ({done.commit[:8]})."
            if done.commit
            else f"{entry.name}: already matched the Vault; nothing to commit."
        )

    if len(targets) - failed:
        lines += ["", "`gsm push` publishes it to the Cloud Vault."]
    return Report(data={"entries": results}, lines=lines, code=1 if failed else 0)


def _resolve_hint(ctx: Context, entry: Entry) -> str:
    row = _rows(ctx)[entry.entry_id]
    if row.status is not None and row.status.state is EntryState.CONFLICT:
        return (
            f"Both sides changed: `gsm resolve {quoted(entry.name)} --keep live` keeps this "
            "Machine's save, `--keep vault` takes the Vault's."
        )
    if row.status is None:
        return f"`gsm bind {quoted(entry.name)} PATH` binds it here first."
    return ""


def _restore_lines(previews: list[tuple[Entry, Preview]]) -> list[str]:
    if len(previews) == 1:
        return presenter.preview_lines(previews[0][1])
    return presenter.bulk_preview_lines([(entry.name, preview) for entry, preview in previews])


def _write_result(entry: Entry, written: Any) -> tuple[dict[str, Any], str]:
    backup = str(written.backup.path) if written.backup else None
    data = {
        "id": entry.entry_id,
        "name": entry.name,
        "result": "unchanged" if written.skipped else "written",
        "backup": backup,
    }
    if written.skipped:
        return data, f"{entry.name}: the Live Save already held this; nothing was written."
    kept = f" The previous Live Save is in {backup}." if backup else ""
    return data, f"{entry.name}: written.{kept}"


def restore(ctx: Context, args: Any) -> Report:
    """Vault -> Live Save (or a Backup -> Live Save), behind the preview every time."""
    _require_set_up(ctx)
    if args.backup is not None:
        if args.all or len(args.entries) != 1:
            raise CliError("--backup restores one Entry: name exactly one.", kind="usage")
        return _restore_backup(ctx, args, find_entry(ctx.paths, args.entries[0]))
    if args.all == bool(args.entries):
        raise CliError("Name the Entries to Restore, or use --all (not both).", kind="usage")

    app = ctx.the_app
    if args.all:
        targets = [
            entries.require(ctx.paths, row.entry_id)
            for row in presenter.bulk_targets(
                presenter.visible_rows(list(_rows(ctx).values()), {EntryState.VAULT_AHEAD}),
                Action.RESTORE_TO_LIVE,
            )
        ]
        if not targets:
            return Report(
                data={"entries": []}, lines=["No Entry is Vault Ahead; nothing to Restore."]
            )
    else:
        targets = find_entries(ctx.paths, args.entries)
        for entry in targets:
            _offered(ctx, entry, Action.RESTORE_TO_LIVE, _resolve_hint(ctx, entry))

    previews = [
        (entry, operations.preview_restore(ctx.paths, app.the_ledger, entry.entry_id))
        for entry in targets
    ]
    plural = "these Entries" if len(previews) > 1 else previews[0][0].name
    confirm(
        ctx,
        args,
        _restore_lines(previews),
        f"Write the Vault's version over {plural}?",
        data={"writes": [preview_data(preview) for _, preview in previews]},
        approval=approval_of(preview for _, preview in previews),
    )

    results, lines, failed = [], [], 0
    for entry, preview in previews:
        try:
            written = operations.restore_to_live(
                ctx.paths, app.config, app.the_ledger, entry.entry_id, approved=preview
            )
        except Exception as error:  # noqa: BLE001 - surfaced per Entry, never hidden
            failed += 1
            results.append(
                {
                    "id": entry.entry_id,
                    "name": entry.name,
                    "result": "failed",
                    "message": str(error),
                }
            )
            lines.append(f"{entry.name}: not Restored. {error}")
            continue
        data, line = _write_result(entry, written)
        results.append(data)
        lines.append(line)
    return Report(data={"entries": results}, lines=lines, code=1 if failed else 0)


def _backup(found: list[Backup], token: str, entry: Entry) -> Backup:
    if token.isdigit() and 1 <= int(token) <= len(found):
        return found[int(token) - 1]
    for backup in found:
        if backup.path.name == token:
            return backup
    raise CliError(
        f"{entry.name} has no Backup {quoted(token)}. `gsm backups {quoted(entry.name)}` "
        "lists them by number.",
        kind="unknown_backup",
    )


def _restore_backup(ctx: Context, args: Any, entry: Entry) -> Report:
    """A Backup -> Live Save. Leaves the Baseline alone: no data moved between the Live Save
    and the Vault, so the Entry correctly reads Local Ahead afterwards."""
    app = ctx.the_app
    if not app.the_ledger.is_bound(entry.entry_id):
        raise CliError(
            f"{entry.name} is not bound on this Machine, so there is no Live Save to restore over.",
            kind="not_bound",
        )
    backup = _backup(backups.list_for(ctx.paths, entry.entry_id), args.backup, entry)
    preview = operations.preview_backup_restore(ctx.paths, app.the_ledger, entry.entry_id, backup)
    confirm(
        ctx,
        args,
        presenter.preview_lines(preview),
        f"Write the Backup from {_when(backup.taken_at)} over {entry.name}?",
        data={"writes": [preview_data(preview)], "backup_restored": str(backup.path)},
        approval=approval_of([preview]),
    )
    written = operations.restore_backup(
        ctx.paths, app.config, app.the_ledger, entry.entry_id, backup, approved=preview
    )
    data, line = _write_result(entry, written)
    return Report(
        data={"entries": [data]},
        lines=[
            line,
            "",
            "The Entry now reads Local Ahead: `gsm sync` captures it if it is the version to keep.",
        ],
    )


def resolve(ctx: Context, args: Any) -> Report:
    """A Sync Conflict, decided at Entry granularity: one side whole, never a mixture."""
    _require_set_up(ctx)
    app = ctx.the_app
    entry = find_entry(ctx.paths, args.entry)
    _offered(ctx, entry, Action.RESOLVE)

    if args.keep == "live":
        done = operations.resolve_conflict_toward_live(
            ctx.paths, app.config, app.description, app.the_ledger, entry.entry_id
        )
        return Report(
            data={"id": entry.entry_id, "kept": "live", "commit": done.commit},
            lines=[
                f"{entry.name}: kept this Machine's save and Synced it to the Vault"
                + (f" ({done.commit[:8]})." if done.commit else ".")
                + " The Vault's version stays in history."
            ],
        )

    preview = operations.preview_restore(
        ctx.paths, app.the_ledger, entry.entry_id, reason=operations.CONFLICT_VAULT
    )
    confirm(
        ctx,
        args,
        presenter.preview_lines(preview),
        f"Take the Vault's version of {entry.name}, writing it over this Machine's save?",
        data={"writes": [preview_data(preview)]},
        approval=approval_of([preview]),
    )
    written = operations.resolve_conflict_toward_vault(
        ctx.paths, app.config, app.the_ledger, entry.entry_id, approved=preview
    )
    data, line = _write_result(entry, written)
    return Report(data={**data, "kept": "vault"}, lines=[line])


def rollback(ctx: Context, args: Any) -> Report:
    """The Vault copy back to an earlier commit, as a forward commit. Touches no Live Save."""
    _require_set_up(ctx)
    app = ctx.the_app
    entry = find_entry(ctx.paths, args.entry)
    commits = operations.history(ctx.paths, entry.entry_id, limit=10_000)
    token = args.commit.lower()
    matches = [c for c in commits if len(token) >= 4 and c.sha.startswith(token)]
    if len(matches) != 1:
        raise CliError(
            f"{quoted(args.commit)} names "
            + ("more than one commit" if matches else "no commit")
            + f" in the history of {entry.name}. `gsm history {quoted(entry.name)}` lists them.",
            kind="unknown_commit",
        )
    target = matches[0]
    changes = operations.preview_rollback(ctx.paths, entry.entry_id, target.sha)
    if not changes:
        return Report(
            data={"id": entry.entry_id, "commit": None},
            lines=[f"{entry.name} already holds {target.short}; nothing to roll back."],
        )

    confirm(
        ctx,
        args,
        presenter.rollback_lines(entry.name, target.short, changes),
        f"Roll the Vault copy of {entry.name} back to {target.short}?",
        data={"changes": [{"path": c.path, "change": c.change.value} for c in changes]},
    )
    commit = operations.rollback(ctx.paths, app.config, app.description, entry.entry_id, target.sha)
    return Report(
        data={"id": entry.entry_id, "commit": commit, "to": target.sha},
        lines=[
            f"{entry.name}: the Vault copy is back at {target.short} ({commit[:8]}).",
            f"It reads Vault Ahead: `gsm restore {quoted(entry.name)}` applies it to the "
            "Live Save.",
        ],
    )


# --- Entries ------------------------------------------------------------------------------------


def add(ctx: Context, args: Any) -> Report:
    _require_set_up(ctx)
    app = ctx.the_app
    paths = [_live_path(text) for text in args.paths]
    if any(entry.name.casefold() == args.name.casefold() for entry in entries.list_all(ctx.paths)):
        ctx.say(
            f"warning: another Entry is already named {args.name}. Commands will need this "
            "one's ID to tell them apart."
        )
    try:
        if len(paths) == 1:
            entry = operations.add_entry(
                ctx.paths, app.config, app.description, app.the_ledger, args.name, paths[0]
            )
        else:
            entry = operations.add_file_set(
                ctx.paths, app.config, app.description, app.the_ledger, args.name, paths
            )
    except (vault.FileTooLarge, operations.GitRepoNotAllowed, operations.InvalidFileSet) as error:
        raise CliError(str(error), kind="refused") from error
    return Report(
        data={"id": entry.entry_id, "name": entry.name, "live": [str(path) for path in paths]},
        lines=[
            f"Added {entry.name} ({entry.entry_id}), bound to "
            + ", ".join(str(path) for path in paths)
            + ".",
            "Nothing has been copied into the Vault yet: adding only records where the save "
            f"lives. `gsm sync {quoted(entry.name)}` captures it.",
        ],
    )


def bind(ctx: Context, args: Any) -> Report:
    """Unlinked (or Needs Binding) -> bound on this Machine. Moves no data, writes no Baseline."""
    _require_set_up(ctx)
    app = ctx.the_app
    entry = find_entry(ctx.paths, args.entry)
    row = _rows(ctx)[entry.entry_id]
    if row.status is not None:
        raise CliError(
            f"{entry.name} is already bound on this Machine ({row.caption}). "
            f"`gsm unbind {quoted(entry.name)}` first to bind it somewhere else.",
            kind="already_bound",
        )

    target: Path | dict[str, Path]
    if entry.is_file_set:
        target = {}
        for pair in args.paths:
            member, sep, text = pair.partition("=")
            if not sep or member not in (entry.files or ()):
                raise CliError(
                    f"{entry.name} keeps several files: bind each as MEMBER=PATH, where MEMBER "
                    f"is one of {', '.join(entry.files or ())}.",
                    kind="usage",
                )
            target[member] = _live_path(text)
        shown = [str(path) for path in target.values()]
    else:
        if len(args.paths) != 1:
            raise CliError(f"{entry.name} is bound to one path.", kind="usage")
        target = _live_path(args.paths[0])
        file_name = presenter.bind_file_name(ctx.paths, entry.entry_id)
        if file_name and target.is_dir() and target.name != file_name:
            # A single-file save, pointed at the folder it belongs in: every other Machine
            # keeps it under this name, so that is the file the game here will read.
            target = target / file_name
            ctx.say(f"{entry.name} is a single file, so it is bound as {target}.")
        shown = [str(target)]

    existing = [p for p in shown if Path(p).exists()]
    try:
        operations.bind_entry(
            ctx.paths,
            app.config,
            app.description,
            app.the_ledger,
            entry.entry_id,
            target,
            pat=ctx.session.token(),
        )
    except (operations.InvalidFileSet, vault.FileTooLarge) as error:
        raise CliError(str(error), kind="refused") from error

    now = _rows(ctx)[entry.entry_id]
    lines = [f"Bound {entry.name} to {', '.join(shown)}. It reads {now.caption}."]
    if existing:
        lines.append(
            "Binding moved no data. Something is already there: Sync uploads it to the "
            "Vault, Restore overwrites it with the Vault's copy."
        )
    if now.status is None:
        lines.append("Some of its files still have no path here: bind them too to sync it.")
    elif _recommended(now):
        lines.append(f"Next: `gsm {_recommended(now)} {quoted(entry.name)}`.")
    return Report(data={"entry": _row_data(now), "live": shown}, lines=lines)


def unbind(ctx: Context, args: Any) -> Report:
    _require_set_up(ctx)
    app = ctx.the_app
    entry = find_entry(ctx.paths, args.entry)
    if not app.the_ledger.is_bound(entry.entry_id):
        raise CliError(f"{entry.name} is not bound on this Machine.", kind="not_bound")
    operations.unbind_entry(ctx.paths, app.config, app.description, app.the_ledger, entry.entry_id)
    return Report(
        data={"id": entry.entry_id},
        lines=[
            f"Unbound {entry.name} on this Machine. The Live Save was not touched, and the "
            "Vault keeps the Entry and its history."
        ],
    )


def rename(ctx: Context, args: Any) -> Report:
    _require_set_up(ctx)
    app = ctx.the_app
    entry = find_entry(ctx.paths, args.entry)
    was = entry.name
    operations.rename_entry(ctx.paths, app.config, app.description, entry.entry_id, args.name)
    return Report(
        data={"id": entry.entry_id, "was": was, "name": args.name},
        lines=[f"Renamed {was} to {args.name}."],
    )


def remove(ctx: Context, args: Any) -> Report:
    _require_set_up(ctx)
    app = ctx.the_app
    entry = find_entry(ctx.paths, args.entry)
    confirm(
        ctx,
        args,
        [
            f"Remove {entry.name} from the Vault, for every Machine.",
            "A forward commit: the content stays recoverable from history, and no Live Save "
            "anywhere is touched.",
        ],
        f"Remove {entry.name} from the Vault?",
    )
    operations.remove_from_vault(
        ctx.paths, app.config, app.description, app.the_ledger, entry.entry_id
    )
    return Report(
        data={"id": entry.entry_id},
        lines=[f"Removed {entry.name} from the Vault. `gsm push` publishes the removal."],
    )


def add_file(ctx: Context, args: Any) -> Report:
    _require_set_up(ctx)
    app = ctx.the_app
    entry = find_entry(ctx.paths, args.entry)
    if not app.the_ledger.is_bound(entry.entry_id):
        raise CliError(f"{entry.name} is not bound on this Machine.", kind="not_bound")
    path = _live_path(args.path)
    try:
        member = operations.add_file(
            ctx.paths, app.config, app.description, app.the_ledger, entry.entry_id, path
        )
    except (operations.InvalidFileSet, vault.FileTooLarge) as error:
        raise CliError(str(error), kind="refused") from error
    return Report(
        data={"id": entry.entry_id, "member": member, "live": str(path)},
        lines=[
            f"{entry.name} now keeps {path} too, as {member}. "
            f"`gsm sync {quoted(entry.name)}` captures it."
        ],
    )


def remove_file(ctx: Context, args: Any) -> Report:
    _require_set_up(ctx)
    app = ctx.the_app
    entry = find_entry(ctx.paths, args.entry)
    confirm(
        ctx,
        args,
        [
            f"Take {args.member} out of {entry.name}, for every Machine.",
            "The file itself is not touched, here or anywhere. Its copy leaves the Vault and stays "
            "recoverable from History.",
        ],
        f"Take {args.member} out of {entry.name}?",
    )
    try:
        operations.remove_file(
            ctx.paths, app.config, app.description, app.the_ledger, entry.entry_id, args.member
        )
    except operations.InvalidFileSet as error:
        raise CliError(str(error), kind="refused") from error
    return Report(
        data={"id": entry.entry_id, "member": args.member},
        lines=[f"Took {args.member} out of {entry.name}. The file was not touched."],
    )


def download(ctx: Context, args: Any) -> Report:
    """A plain copy of the Vault's version, outside the app's care. Binds nothing."""
    _require_set_up(ctx)
    entry = find_entry(ctx.paths, args.entry)
    destination = _live_path(args.to) if args.to else Path.home() / "Downloads"
    from core import download as download_module

    try:
        landed = download_module.download(
            ctx.paths, entry.entry_id, destination, pat=ctx.session.token()
        )
    except (
        download_module.NothingToDownload,
        download_module.NeedsSignIn,
        download_module.FetchFailed,
    ) as error:
        raise CliError(str(error), kind="refused") from error
    return Report(
        data={"id": entry.entry_id, "path": str(landed)},
        lines=[
            f"Downloaded {entry.name} to {landed}.",
            "It is a plain copy: not bound, and never touched by Sync or Restore.",
        ],
    )


# --- Vault <-> Cloud Vault -------------------------------------------------------------------


def fetch(ctx: Context, args: Any) -> Report:
    _require_set_up(ctx)
    try:
        ctx.the_app.cloud.fetch_status(ctx.token())
    except CloudOffline as error:
        raise CliError(str(error), kind="offline") from error
    data, line = _cloud(ctx, fetch=False)
    data["fresh"] = True
    return Report(data={"cloud": data}, lines=[line.split(" - as of")[0]])


def push(ctx: Context, args: Any) -> Report:
    _require_set_up(ctx)
    app = ctx.the_app
    token = ctx.token()
    with contextlib.suppress(GitError):  # never pushed: no tracking ref to compare against yet
        _, ahead, _ = app.cloud.known_state()
        if ahead == 0:
            return Report(
                data={"pushed": 0}, lines=["Nothing to push: the Cloud Vault has every commit."]
            )
    try:
        app.cloud.push(token)
    except PushRejected as error:
        raise CliError(str(error), kind="push_rejected") from error
    except CloudOffline as error:
        raise CliError(str(error), kind="offline") from error
    return Report(data={"pushed": True}, lines=[f"Pushed to {app.config.repo}."])


def _conflict_names(ctx: Context, conflicts: tuple[str, ...]) -> dict[str, str]:
    """A readable name for each contested Entry. The sidecar itself may be what conflicts, so
    an unreadable one falls back to the ID rather than failing the Pull."""
    names = {}
    for entry_id in conflicts:
        found = None
        with contextlib.suppress(Exception):
            found = entries.read(ctx.paths, entry_id)
        names[entry_id] = found.name if found else entry_id
    return names


def _choices(ctx: Context, args: Any, names: dict[str, str]) -> dict[str, Side]:
    """Which side to take for each contested Entry, from --take / --take-all, then by asking."""
    chosen: dict[str, Side] = {}
    if args.take_all:
        chosen = dict.fromkeys(names, Side(args.take_all))
    for pair in args.take or ():
        token, sep, side = pair.rpartition("=")
        if not sep or side not in ("vault", "cloud"):
            raise CliError(
                f"--take wants ENTRY=vault or ENTRY=cloud, not {quoted(pair)}.", kind="usage"
            )
        matched = [
            entry_id
            for entry_id, name in names.items()
            if token in (entry_id, name)
            or name.casefold() == token.casefold()
            or (len(token) >= 4 and entry_id.startswith(token.lower()))
        ]
        if len(matched) != 1:
            raise CliError(
                f"{quoted(token)} is not one of the contested Entries: "
                f"{', '.join(names.values())}.",
                kind="usage",
            )
        chosen[matched[0]] = Side(side)

    for entry_id, name in names.items():
        if entry_id in chosen or not ctx.interactive:
            continue
        ctx.say(
            f"{name} was Synced on another Machine while this one Synced it too. Take one "
            "side, whole; both lines of progress stay in history."
        )
        answer = ask(
            ctx, "  [v]ault: keep this Machine's line   [c]loud: take the other's   [a]bort:"
        )
        if answer in ("v", "vault"):
            chosen[entry_id] = Side.VAULT
        elif answer in ("c", "cloud"):
            chosen[entry_id] = Side.CLOUD
        else:
            break
    return chosen


def pull(ctx: Context, args: Any) -> Report:
    """Cloud Vault -> Vault, by merge. Touches no Live Save and writes no Baseline: what it
    brings down surfaces as Vault Ahead, and Restore is the explicit next step."""
    _require_set_up(ctx)
    app = ctx.the_app
    try:
        pulled = app.cloud.pull(ctx.token(), app.description)
    except CloudOffline as error:
        raise CliError(str(error), kind="offline") from error
    except ForeignConflict as error:
        raise CliError(str(error), kind="foreign_conflict") from error

    resolved: dict[str, str] = {}
    if pulled.conflicts:
        names = _conflict_names(ctx, pulled.conflicts)
        finished = False
        try:
            chosen = _choices(ctx, args, names)
            if set(chosen) == set(names):
                for entry_id, side in chosen.items():
                    cloud.resolve_merge(ctx.paths, entry_id, side)
                cloud.finish_merge(ctx.paths, app.config, app.description)
                finished = True
                resolved = {names[entry_id]: side.value for entry_id, side in chosen.items()}
        finally:
            if not finished:
                # Never leave a half-decided merge behind: abort it whole. The Pull can simply
                # be run again, this time with a side for each Entry.
                cloud.abort_merge(ctx.paths)
        if not finished:
            example = " ".join(f"--take {quoted(name)}=vault|cloud" for name in names.values())
            raise CliError(
                f"Pull stopped: {len(names)} Entr{'y was' if len(names) == 1 else 'ies were'} "
                f"Synced both here and on another Machine: {', '.join(names.values())}. Nothing "
                f"has changed. Pull again choosing a side for each: `gsm pull {example}` "
                "(vault keeps this Machine's line, cloud takes the other's).",
                kind="merge_conflict",
                data={"conflicts": [{"id": i, "name": n} for i, n in names.items()]},
            )

    rows = list(_rows(ctx).values())
    waiting = [row for row in rows if row.status and row.status.state is EntryState.VAULT_AHEAD]
    contested = [row for row in rows if row.status and row.status.state is EntryState.CONFLICT]
    lines = [
        f"Pulled {counted(pulled.commits, 'commit')}." if pulled.commits else "Already up to date."
    ]
    kept = {"vault": "kept this Machine's line", "cloud": "took the other Machine's line"}
    lines += [f"{name}: {kept[side]}." for name, side in resolved.items()]
    if waiting:
        command = (
            f"gsm restore {quoted(waiting[0].name)}"
            if len(waiting) == 1
            else ("gsm restore NAME`, or `gsm restore --all")
        )
        lines.append(
            "Vault Ahead, ready to Restore: "
            + ", ".join(r.name for r in waiting)
            + f" (`{command}`)."
        )
    if contested:
        command = f"gsm resolve {quoted(contested[0].name) if len(contested) == 1 else 'NAME'}"
        lines.append(
            "Conflict, needing a decision: "
            + ", ".join(r.name for r in contested)
            + f" (`{command} --keep live|vault`)."
        )
    if resolved:
        lines.append("`gsm push` publishes the merge.")
    return Report(
        data={
            "commits": pulled.commits,
            "merged": resolved,
            "vault_ahead": [_row_data(r) for r in waiting],
            "conflict": [_row_data(r) for r in contested],
        },
        lines=lines,
    )


def check(ctx: Context, args: Any) -> Report:
    _require_set_up(ctx)
    app = ctx.the_app
    if app.cloud.check_connection(ctx.token()):
        return Report(
            data={"connected": True},
            lines=[f"Connected: {app.config.repo} is reachable with this sign-in."],
        )
    reason = app.cloud.offline.reason.value if app.cloud.offline else "no_network"
    raise CliError(
        str(CloudOffline(app.cloud.offline)) if app.cloud.offline else "Offline.",
        kind="offline",
        data={"connected": False, "reason": reason},
    )


# --- setup, sign-in, starting over -------------------------------------------------------------


def _credential(ctx: Context, args: Any) -> tuple[Credential, str | None]:
    """A sign-in: a token piped on stdin, or GitHub's device flow in the terminal.

    A token is never taken as an argument - it would sit in the shell history and in every
    other process's view of the command line (see `core.git`).
    """
    if args.token_stdin:
        text = ctx.stdin.readline().strip()
        if not text:
            raise CliError("--token-stdin was given, but nothing arrived on stdin.", kind="usage")
        return Credential(access_token=text), None

    try:
        code = device_flow.start(ctx.transport)
    except (device_flow.SignInFailed, github.GitHubUnreachable) as error:
        raise CliError(str(error), kind="sign_in_failed") from error
    ctx.say(f"To sign in, open {code.verification_uri} and enter the code: {code.user_code}")
    if not args.no_browser:
        opener = ctx.open_url or webbrowser.open
        with contextlib.suppress(Exception):
            opener(code.verification_uri)
    ctx.say("Waiting for you to approve it on GitHub... (Ctrl+C to cancel)")
    try:
        found = device_flow.wait_for_token(ctx.transport, code, ctx.wait)
        if found is None:
            raise CliError("Sign-in cancelled.", kind="cancelled")
        return found, github.viewer(ctx.api, found.access_token)
    except (device_flow.SignInFailed, github.GitHubUnreachable, github.BadToken) as error:
        raise CliError(str(error), kind="sign_in_failed") from error


def setup(ctx: Context, args: Any) -> Report:
    app = ctx.the_app
    if app.config.is_set_up:
        raise CliError(
            f"This Machine is already set up against {app.config.repo}. `gsm sign-in` signs in "
            "again; `gsm redo` starts over.",
            kind="already_set_up",
        )
    owner, _, name = args.repo.partition("/")
    if not owner or not name or "/" in name:
        raise CliError(
            f"Name the repository as OWNER/REPOSITORY, not {quoted(args.repo)}.", kind="usage"
        )

    credential, _ = _credential(ctx, args)

    def adopt(ghost: dict) -> bool:
        bound = len(ghost.get("bindings", {}))
        if args.adopt is not None:
            return args.adopt
        if not ctx.interactive:
            ctx.say(
                f"warning: this Vault already lists a Machine named {ghost.get('hostname')} with "
                f"{counted(bound, 'Binding')} - probably this one, before a Redo. It was not "
                "reclaimed: pass --adopt to reclaim it instead of registering beside it."
            )
            return False
        ctx.say(
            f"This Vault already lists a Machine named {ghost.get('hostname')} with "
            f"{counted(bound, 'published Binding')}. It is probably this Machine, before a Redo "
            "Initialization."
        )
        answer = ask(
            ctx,
            "Reclaim that identity and its Bindings, instead of registering a new Machine "
            "beside it? [Y/n]",
        )
        return answer not in ("n", "no")

    try:
        done = github.bootstrap(
            ctx.paths,
            app.config,
            app.description,
            api=ctx.api,
            token=credential.access_token,
            repo=args.repo,
            adopt=adopt,
        )
    except (
        github.BadToken,
        github.GitHubError,
        github.GitHubUnreachable,
        vault.NotAVault,
        vault.VaultTooNew,
        GitError,
        GitTimeout,
    ) as error:
        raise CliError(str(error), kind="setup_failed") from error

    ctx.store.save(credential)
    if done.adopted is not None:
        operations.adopt_bindings(
            ctx.paths,
            app.the_ledger,
            done.adopted.get("bindings", {}),
            pat=credential.access_token,
        )
    outcome = {
        github.BootstrapOutcome.CREATED: "Created a new private Cloud Vault.",
        github.BootstrapOutcome.ADOPTED_EMPTY: "Adopted the empty repository as the Vault.",
        github.BootstrapOutcome.JOINED: "Joined the existing Vault as another Machine.",
    }[done.outcome]
    return Report(
        data={
            "outcome": done.outcome.value,
            "repo": args.repo,
            "branch": done.branch,
            "adopted": done.adopted is not None,
        },
        lines=[
            f"{outcome} ({args.repo}, branch {done.branch})",
            "`gsm status` lists the Entries; `gsm add NAME PATH` adds one.",
        ],
    )


def sign_in(ctx: Context, args: Any) -> Report:
    """A new sign-in for this set-up Machine, kept only once it has reached the Cloud Vault."""
    _require_set_up(ctx)
    app = ctx.the_app
    credential, login = _credential(ctx, args)
    if not app.cloud.check_connection(credential.access_token):
        who = f"Signed in as {login}, but" if login else "This token"
        raise CliError(
            f"{who} could not reach {app.config.repo}. The previous sign-in was kept. Sign in "
            "with the account that owns the repository, or has access to it.",
            kind="sign_in_failed",
        )
    ctx.store.save(credential)
    return Report(
        data={"login": login, "repo": app.config.repo},
        lines=[f"Signed in to GitHub{f' as {login}' if login else ''}."],
    )


def redo_init(ctx: Context, args: Any) -> Report:
    """Redo Initialization: delete what setup can rebuild. Never a Backup, never a Live Save."""
    app = ctx.the_app
    ahead = redo.unpushed_commits(ctx.paths)
    if ahead and not args.discard_unpushed:
        raise CliError(
            f"The Vault holds {counted(ahead, 'commit')} that exist on this Machine and nowhere "
            "else. `gsm push` them first - or discard them with --discard-unpushed, as an "
            "explicit, separate choice. Nothing has been done.",
            kind="vault_ahead",
        )
    the_plan = redo.plan(ctx.paths)
    lines = presenter.redo_lines(the_plan)
    if ahead:
        lines += ["", f"And {counted(ahead, 'unpushed commit')}, which exist nowhere else."]
    confirm(
        ctx,
        args,
        lines,
        "Delete all of this and start over?",
        data={"deletes": [str(path) for path in the_plan.deletions]},
    )
    redo.execute(ctx.paths, ctx.store, discard_unpushed=args.discard_unpushed)
    app.reset()
    return Report(
        data={"deleted": [str(path) for path in the_plan.deletions]},
        lines=[
            "Done. Every Backup and every Live Save was kept. "
            "`gsm setup OWNER/REPOSITORY` sets this Machine up again."
        ],
    )
