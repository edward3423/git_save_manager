"""Break the code on purpose, and see whether the tests notice.

The three slices that can lose save data (`plans/plan.md` section 7) share a failure mode
that no amount of green CI catches: the same author writes the code, writes the tests, and
judges whether they pass. Tests written from the same wrong mental model as the code agree
with it enthusiastically.

Reading the tests is the first defence. This is the second. Each mutation below is a way the
code could plausibly be wrong - usually a way it *nearly was*. A mutation that SURVIVES is a
claim the test suite does not actually check, and it is worth more attention than a hundred
passing assertions.

    uv run python scratch/mutate.py

It edits a throwaway copy of the tree under `data/`, never the working tree.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class Mutation:
    """A specific, plausible way the code could be wrong."""

    describes: str
    file: str
    before: str
    after: str

    requires_symlinks: bool = False
    """The tests that would catch this are skipped on Windows (symlinks need privileges), so
    running it there reports a hole in the tests that is really a hole in the platform. It is
    skipped under the same condition the tests use, and still runs everywhere CI does."""


MUTATIONS = [
    Mutation(
        "Drop the absence guard, so every state offers every action",
        "core/entry_state.py",
        "    return tuple(action for action in offered if action not in forbidden)",
        "    return offered",
    ),
    Mutation(
        "Advance the Baseline on Pull - the disaster ADR-0001 exists to prevent",
        "core/ledger.py",
        "    if status.baseline_repair is not None:",
        "    binding.baseline = status.vault\n    if status.baseline_repair is not None:",
    ),
    Mutation(
        "Swap Local Ahead and Vault Ahead, so every Sync runs backwards",
        "core/entry_state.py",
        "    if live_moved and not vault_moved:",
        "    if vault_moved and not live_moved:",
    ),
    Mutation(
        "Never run the equality short-circuit, so a stale Baseline is a permanent Conflict",
        "core/entry_state.py",
        "    if live is not None and live == vault:",
        "    if False and live is not None and live == vault:",
    ),
    Mutation(
        "Treat an empty directory as content rather than as absence",
        "core/hashing.py",
        "        if not files:\n            return None",
        "        if not files:\n            return _hash_files(files, cache)",
    ),
    Mutation(
        "Let bind() adopt a Baseline, claiming a Sync that never happened",
        "core/ledger.py",
        "        binding = Binding(entry_id=entry_id, live_path=normalize_live_path(live_path))",
        "        binding = Binding(entry_id=entry_id, live_path=normalize_live_path(live_path),"
        ' baseline="never-synced")',
    ),
    Mutation(
        "Call it In Sync when neither side holds anything",
        "core/entry_state.py",
        "        return _status(EntryState.NO_CONTENT, live, vault, baseline, offered=EVERYTHING)",
        "        return _status(EntryState.IN_SYNC, live, vault, baseline, offered=EVERYTHING)",
    ),
    Mutation(
        "Repair the Baseline even where the two sides disagree - inventing one, not healing it",
        "core/entry_state.py",
        "            baseline_repair=None if baseline == live else live,",
        "            baseline_repair=live,",
    ),
    Mutation(
        "Hash a file's path but not its bytes, so no save ever looks changed",
        "core/hashing.py",
        "        digest.update(bytes.fromhex(file_hash))\n\n    return digest.hexdigest()",
        "        pass\n\n    return digest.hexdigest()",
    ),
    Mutation(
        "Hash a symlinked Entry root as a link, so a save folder on a second drive never syncs",
        "core/hashing.py",
        "    if path.is_symlink():\n        path = path.resolve()",
        "    if False:\n        path = path.resolve()",
        requires_symlinks=True,
    ),
    Mutation(
        "Follow symlinks *inside* an Entry, pulling content from outside it into the Vault",
        "core/hashing.py",
        "    if path.is_symlink():\n        # Recorded, not followed",
        "    if False:\n        # Recorded, not followed",
        requires_symlinks=True,
    ),
    # --- transaction: the only code that overwrites a Live Save ---------------------------
    Mutation(
        "Write into the Live Save directly instead of staging and swapping - a torn save",
        "core/transaction.py",
        "    source.materialize(staged)",
        "    _remove(target)\n    source.materialize(target)\n    source.materialize(staged)",
    ),
    Mutation(
        "Take the backup *after* the write, when the save it was meant to rescue is gone",
        "core/transaction.py",
        "    backup = None\n    if current.live_hash is not None:",
        "    backup = None\n    if False:",
    ),
    Mutation(
        "Trust the backup archive instead of verifying it hashes back to the save",
        "core/backups.py",
        "        if found != expected_hash:",
        "        if False:",
    ),
    Mutation(
        "Stage somewhere other than a sibling, so the swap crosses a filesystem",
        "core/transaction.py",
        '        target.parent / f".{target.name}.gsm-new",',
        '        Path(tempfile.gettempdir()) / f"{target.name}.gsm-new",',
    ),
    Mutation(
        "Swap the symlink itself rather than the folder it points at",
        "core/transaction.py",
        "    return live.resolve() if live.is_symlink() else live",
        "    return live",
        requires_symlinks=True,
    ),
    Mutation(
        "Roll forward onto a half-written staged copy after a crash mid-staging",
        "core/transaction.py",
        "    if journal.stage == Stage.BACKED_UP:",
        "    if False:",
    ),
    Mutation(
        "Abandon the Live Save when a crash lands between the two renames",
        "core/transaction.py",
        "    elif staged.exists() or staged.is_symlink():\n        # Caught inside the swap",
        "    elif False:\n        # Caught inside the swap",
    ),
    Mutation(
        "Never prune, so the retention setting silently does nothing",
        "core/transaction.py",
        "    pruned = backups.prune(paths, entry_id, config.backup_retention)",
        "    pruned = []",
    ),
    Mutation(
        "Prune to make room *before* the write, destroying a backup for a write that may fail",
        "core/transaction.py",
        "    backup = None\n    if current.live_hash is not None:",
        "    backups.prune(paths, entry_id, config.backup_retention)\n"
        "    backup = None\n    if current.live_hash is not None:",
    ),
    Mutation(
        "Execute a write even though the Live Save moved since the user approved the preview",
        "core/transaction.py",
        "    if approved is not None and (",
        "    if False and (",
    ),
    Mutation(
        "Create the missing parent folder instead of refusing - writing under a mount point",
        "core/transaction.py",
        "    if not target.parent.is_dir():",
        "    if False:",
    ),
    Mutation(
        "Follow symlinks when archiving, baking a link's target into the backup",
        "core/backups.py",
        "    if source.is_symlink():",
        "    if False:",
        requires_symlinks=True,
    ),
    # --- git: the token, and the user's global config ---------------------------------------
    Mutation(
        "Leak the token onto the command line, where `ps` shows it to every process",
        "core/git.py",
        "        env = {PAT_ENV: pat} if pat is not None else {}",
        "        argv += [str(pat)]\n        env = {PAT_ENV: pat} if pat is not None else {}",
    ),
    Mutation(
        "Let the system keychain answer for credentials before our helper does",
        "core/git.py",
        '            argv += ["-c", "credential.helper="]',
        "            pass",
    ),
    Mutation(
        "Let Git prompt for a password - which, in a GUI with no terminal, is a hang",
        "core/git.py",
        '    env["GIT_TERMINAL_PROMPT"] = "0"',
        '    env["GIT_TERMINAL_PROMPT"] = "1"',
    ),
    Mutation(
        "Print the token in an error message, and thus into every log and crash report",
        "core/git.py",
        '    return text.replace(pat, "***")',
        "    return text",
    ),
    Mutation(
        "Inherit a stray token from the parent environment",
        "core/git.py",
        "    env.pop(PAT_ENV, None)",
        "    pass",
    ),
    Mutation(
        "Let a global core.autocrlf rewrite the line endings inside binary save files",
        "core/git.py",
        '    "core.autocrlf=false",',
        "",
    ),
    # --- vault: cleanliness and selective sync -----------------------------------------------
    Mutation(
        "Use `git clean -fdx`, which deletes ignored files",
        "core/vault.py",
        '    repo.run("clean", "-fd")',
        '    repo.run("clean", "-fdx")',
    ),
    Mutation(
        "Drop the sidecar pin, so a Machine with nothing bound can see no Entry to bind",
        "core/vault.py",
        '    return ["machines", SIDECAR_PIN, *bound]',
        '    return ["machines", *bound]',
    ),
    Mutation(
        "Check out every Entry in the Vault, not just the bound ones",
        "core/vault.py",
        '    return ["machines", SIDECAR_PIN, *bound]',
        '    return ["machines", "entries"]',
    ),
    Mutation(
        "Clone every blob in the Vault's history rather than only what is bound",
        "core/vault.py",
        '        "--filter=blob:none",',
        "",
    ),
    Mutation(
        "Touch a Vault written by a newer build of the app",
        "core/vault.py",
        "    if schema > SCHEMA:",
        "    if False:",
    ),
    Mutation(
        "Commit save files into whatever repository the user pointed at",
        "core/vault.py",
        '    if data is None or not data.get("vault"):',
        "    if False:",
    ),
    Mutation(
        "Let Git attempt a textual three-way merge inside a binary save file",
        "core/vault.py",
        "/entries/** binary",
        "",
    ),
    # --- operations: the stable-read guard, and who may write a Baseline ---------------------
    Mutation(
        "Drop the stable-read guard, committing a save the game was writing as we copied it",
        "core/operations.py",
        "    if not (before == after == captured):",
        "    if False:",
    ),
    Mutation(
        "Check only that the source held still, never that the copy came out faithful",
        "core/operations.py",
        "    if not (before == after == captured):",
        "    if not (before == after):",
    ),
    Mutation(
        "Check only that the copy is faithful, never that the game held still",
        "core/operations.py",
        "    if not (before == after == captured):",
        "    if not (before == captured):",
    ),
    Mutation(
        "Record the Baseline before the commit lands, so a failed commit leaves one that lies",
        "core/operations.py",
        "    commit = None\n    try:",
        "    the_ledger.record_sync(entry_id, before, SyncDirection.TO_VAULT)\n"
        "    commit = None\n    try:",
    ),
    Mutation(
        "Leave the Vault dirty after an aborted Sync, so the next status refresh reads wreckage",
        "core/operations.py",
        "        vault.ensure_clean(paths)\n        raise",
        "        raise",
    ),
    Mutation(
        "Sync an empty Live Save, committing its absence and emptying the Entry",
        "core/operations.py",
        "    if before is None:",
        "    if False:",
    ),
    Mutation(
        "Move the Baseline when a Backup is restored, claiming a Sync that never happened",
        "core/operations.py",
        "    binding = the_ledger.require(entry_id)\n\n    return transaction.write_live(",
        "    binding = the_ledger.require(entry_id)\n"
        "    the_ledger.record_sync(\n"
        "        entry_id, BackupSource(backup).digest(), SyncDirection.TO_LIVE\n"
        "    )\n"
        "    return transaction.write_live(",
    ),
    Mutation(
        "Roll back with a hard reset, destroying the history you rolled away from",
        "core/operations.py",
        '    repo.run("restore", "--source", sha, "--staged", "--worktree", "--", content)',
        '    repo.run("reset", "--hard", sha)',
    ),
    Mutation(
        "Author every commit as the same machine, so `git log` cannot say who synced what",
        "core/operations.py",
        '        f"user.name={description.hostname}",',
        '        "user.name=gsm",',
    ),
    Mutation(
        "Delete the Live Save when the Entry is removed from the Vault",
        "core/operations.py",
        "    the_ledger.unbind(entry_id)\n    ledger.save(paths, the_ledger)\n"
        "    vault.set_sparse(paths, the_ledger.bindings)\n\n"
        '    log().info("Removed %s from the Vault. No Live Save was touched.", entry.name)',
        "    shutil.rmtree(the_ledger.require(entry_id).live, ignore_errors=True)\n"
        "    the_ledger.unbind(entry_id)\n    ledger.save(paths, the_ledger)\n"
        "    vault.set_sparse(paths, the_ledger.bindings)",
    ),
]


def failures_under(tree: Path) -> list[str]:
    """Run the suite against the mutated tree.

    `PYTHONDONTWRITEBYTECODE` is not optional here. A `.pyc` is invalidated by the source's
    (mtime, size), with the mtime stored to a one-second granularity - so a mutation that
    replaces a line with one of the *same byte length*, written within a second of the last
    compile, leaves the cached bytecode looking current. Python then runs the unmutated code
    and the mutation appears to survive. Swapping `live_moved` for `vault_moved` is exactly
    that: same length, same second. The harness reported a hole in the tests that did not
    exist, which is a nicely humbling instance of the very thing it is built to catch.
    """
    # `--color=no` because the output is parsed, not read: under a color-forcing environment
    # (FORCE_COLOR is set in some agent terminals) every FAILED line arrives wrapped in ANSI
    # escapes, the regex below matches nothing, and all 50+ mutations report as SURVIVED at
    # once - a false alarm distinguishable from a real one only by its implausible size.
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "--tb=no", "-x", "--no-header", "--color=no"],
        cwd=tree,
        capture_output=True,
        text=True,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
    )
    # ERRORs count as much as FAILEDs. `-x` stops the run at the first of either, and a run
    # that stopped on an error would otherwise report zero failures - making the mutation a
    # SURVIVOR when in truth the suite went red before its catching test got a chance to.
    return re.findall(r"^(?:FAILED|ERROR) (\S+)", proc.stdout, re.M)


def main() -> int:
    survivors = 0
    skipped = 0

    with tempfile.TemporaryDirectory() as workspace:
        tree = Path(workspace) / "tree"
        shutil.copytree(
            PROJECT_ROOT,
            tree,
            ignore=shutil.ignore_patterns(".git", "data", "__pycache__", ".venv", ".pytest_cache"),
        )

        for mutation in MUTATIONS:
            if mutation.requires_symlinks and os.name == "nt":
                skipped += 1
                print(f"skipped   {mutation.describes}")
                print("          Its tests skip on Windows; run this on CI or a POSIX box.\n")
                continue

            target = tree / mutation.file
            original = target.read_text(encoding="utf-8")

            if mutation.before not in original:
                print(f"STALE     {mutation.describes}")
                print("          The code it patches has moved. Fix the mutation.\n")
                survivors += 1
                continue

            target.write_text(
                original.replace(mutation.before, mutation.after, 1), encoding="utf-8"
            )
            failed = failures_under(tree)
            target.write_text(original, encoding="utf-8")

            if not failed:
                survivors += 1
                print(f"SURVIVED  {mutation.describes}")
                print("          Nothing caught this. The suite does not check that claim.\n")
                continue

            caught_by = failed[0].split("::")[-1].split("[")[0]
            print(f"caught    {mutation.describes}")
            print(f"          {caught_by}\n")

    if survivors:
        print(f"{survivors} mutation(s) survived. Each one is a hole in the tests.")
        return 1

    ran = len(MUTATIONS) - skipped
    trailer = f" ({skipped} skipped on this platform.)" if skipped else ""
    print(f"All {ran} mutations were caught.{trailer}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
