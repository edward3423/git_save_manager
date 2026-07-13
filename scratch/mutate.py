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
        "        digest.update(bytes.fromhex(hash_file(absolute, cache)))",
        "        pass",
    ),
    Mutation(
        "Hash a symlinked Entry root as a link, so a save folder on a second drive never syncs",
        "core/hashing.py",
        "    if path.is_symlink():\n        path = path.resolve()",
        "    if False:\n        path = path.resolve()",
    ),
    Mutation(
        "Follow symlinks *inside* an Entry, pulling content from outside it into the Vault",
        "core/hashing.py",
        "    if path.is_symlink():\n        # Recorded, not followed",
        "    if False:\n        # Recorded, not followed",
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
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "--tb=no", "-x", "--no-header"],
        cwd=tree,
        capture_output=True,
        text=True,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
    )
    return re.findall(r"^FAILED (\S+)", proc.stdout, re.M)


def main() -> int:
    survivors = 0

    with tempfile.TemporaryDirectory() as workspace:
        tree = Path(workspace) / "tree"
        shutil.copytree(
            PROJECT_ROOT,
            tree,
            ignore=shutil.ignore_patterns(".git", "data", "__pycache__", ".venv", ".pytest_cache"),
        )

        for mutation in MUTATIONS:
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

    print(f"All {len(MUTATIONS)} mutations were caught.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
