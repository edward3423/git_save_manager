# Handoff - Git-Backed Save File Manager

**Date:** 2026-07-13
**Repo:** `/Users/edward/Projects/git_save_manager` (GitHub: `EdwardRusli/git_save_manager`)
**Branch:** `main` @ `a84732a`, clean, green
**State:** Phase 2 complete. The entire local half of the app is merged. Nothing is in flight.

---

## Read these first (do not re-derive them)

Everything about *what this app is* and *why it is shaped this way* is already written down. Read, don't reconstruct:

- `CONTEXT.md` - the domain vocabulary. Live Save, Vault, Cloud Vault, Entry, Binding, Baseline, Machine, the 7 Entry States.
- `plans/plan.md` - the plan. Section 3 is the state machine, Section 4 the invariants, Section 6 the explicit non-goals (rejected ideas - do not re-litigate them), Section 7 the slice table and workflow, Section 8 the scenarios the tests must encode.
- `docs/adr/0001-0006` - the six decisions with teeth. ADR-0006 (*absence never propagates*) is the newest and the least obvious.

The code is the other half of the documentation. Module docstrings carry the reasoning; `core/entry_state.py`, `core/transaction.py`, and `core/operations.py` in particular.

---

## Where the work stands

`main` now contains, all merged via PRs #1-#7:

| Layer | Modules |
|---|---|
| Foundations | `core/paths.py`, `jsonstore.py`, `logger.py`, `config.py`, `credentials.py` |
| Content | `core/hashing.py` |
| State | `core/entry_state.py`, `core/ledger.py` |
| Writes | `core/transaction.py`, `core/backups.py` |
| Git | `core/git.py`, `core/vault.py` |
| Verbs | `core/entries.py`, `core/operations.py` |

380 tests pass (`uv run pytest`), 46 mutations all caught (`uv run python scratch/mutate.py`), `ruff` clean.

## What is left

Per the slice table in `plans/plan.md` Section 7 - and note that **all remaining slices are marked `autonomous`**, meaning they merge on green without stopping for a human read:

1. **`core/cloud`** - Pull, Push, fetch status, Offline Mode. **This is next.**
2. **`core/github-bootstrap`** - the four bootstrap paths, the `vault.json` marker, PAT hygiene at repo-creation time.
3. **`ui/*`** - main window, dialogs, offline mode, Redo Initialization. Phase 4. PyQt6.

The three "human reads the diff" slices are all behind us. The user merged each one explicitly after reading it; do not squash-merge a PR yourself unless the user says so in the current session.

---

## Things the next session will get wrong if nobody says them

**The slice table is stale.** `plans/plan.md` lines 226-229 still show slices #4-#7 as `open`. They are all merged. Fix this in passing with the next slice's PR - it is a four-line edit, not a slice of its own.

**`core/cloud` has a hard constraint that is easy to violate by accident:** Push and Pull move commits between the Vault and the Cloud Vault, and **must never write a Baseline**. Only Sync writes a Baseline (Invariant 6). This is currently structural - `Ledger.record_sync` is the sole writer, and `core/operations.py` calls it from exactly one place - and it must stay that way. A Pull that advances the Baseline is the single worst bug this app can have: it makes a stale Live Save look In Sync, so the next Sync silently uploads old progress over new. `scratch/mutate.py` already carries a mutation for exactly this; add the Pull-side one when the code exists.

**Offline Mode is sticky and first-class.** Entering it is automatic on any network failure; the *only* exit is an explicit **Check Connection** that succeeds, probing with `git ls-remote`. It does not time out, retry, or heal itself. Section 5 of the plan.

**Pull merges. It never rebases.** Rewriting published history across Machines is how a Vault loses a commit.

**PAT hygiene is non-negotiable and already solved** - copy the pattern in `core/git.py`, do not invent a second one. The remote URL stays credential-free. The token is never written to a file, never put in a remote URL, never on a command line, never logged. It is read from the OS keyring at the moment of use and injected via a one-shot inline credential helper with the token in the subprocess environment only. GitHub REST calls send it per-request in an `Authorization` header. `core/git.py::_redact` scrubs it from error text.

**`scratch/mutate.py` is the review tool, and it is load-bearing.** It has found five real defects that the 380-test suite was blind to, including a Baseline written before the commit landed (survives every test, because in tests commits always succeed). Every new slice should add mutations for its own plausible-wrong-ways. Note the `PYTHONDONTWRITEBYTECODE=1` in it: without it, a mutation that replaces a line with one of identical byte length gets served stale `.pyc` bytecode and falsely reports as SURVIVED.

**Real Git, not assumptions.** Several designs here were settled by experiment against real Git and would have been wrong otherwise: cone-mode sparse-checkout's leading-directory rule, `git add`'s refusal of an unmatched pathspec, `branch --show-current` being the only way to read an unborn HEAD. When the next slice raises a "surely Git does X" question, go and check.

---

## Conventions in force

From the user's global instructions (`~/.claude/CLAUDE.md`):

- No em dashes. Plain `-` only.
- No emojis, anywhere.
- No agent co-author trailer in commit messages.
- Development cost is not a tiebreaker. Prefer quality, simplicity, robustness, long-term maintainability.
- Bug fixes start by reproducing the bug end-to-end, as the user would hit it.
- Lint failures, test failures, and flakiness get fixed on sight, even when unrelated to the task at hand.

Project conventions: `uv`, `pyproject.toml` (never `requirements.txt`), Python 3.12, `ruff` (line-length 100), tests in `scratch/tests`, Git driven by `subprocess` (never `gitpython`).

---

## Suggested skills

- **`/tdd`** for `core/cloud`. The scenarios in `plans/plan.md` Section 8 are already written as tests waiting to be typed - a Pull that must not move the Baseline; a stale Live plus a pulled Vault that must report Vault Ahead and not In Sync. Write those first, watch them fail, then build.
- **`/code-review`** before opening each PR, to check the diff against the plan rather than against itself.
- **`/prototype`** if the Offline Mode state model or the four GitHub bootstrap paths feel uncertain before committing to them.
- **`/run`** once `ui/*` starts, to actually launch the PyQt6 window rather than trusting the tests. The user is explicitly picky about UI and expects pixel perfection.
- **`/grilling`** if any new design question comes up that the plan does not already answer. The current plan is the output of one of these and is far better for it.

Do not reach for `/init` - `CLAUDE.md`, `CONTEXT.md`, and the ADRs already cover the ground.
