# Handoff - Git-Backed Save File Manager

**Date:** 2026-07-15 (originally 2026-07-13; updated in place)
**Repo:** `D:\Projects\git_save_manager` (GitHub: `EdwardRusli/git_save_manager`)
**Branch:** `main` green, nothing in flight
**State:** **Every phase of plans/plan.md is built and merged** (PRs #1-#13). What remains is the *manual* half of Section 8: first-run against real GitHub, the GUI walked by a human, and the network-unplug scenarios - things only a person at the machine can verify.

Note the environment: development moved from macOS to **Windows 10** (PowerShell; `D:\` paths). The suite carries 11 platform skips there (symlinks need privileges, chmod does not restrict directories) and the mutation harness skips 4 symlink mutations to match; CI on ubuntu covers all of them.

---

## Read these first (do not re-derive them)

Everything about *what this app is* and *why it is shaped this way* is already written down. Read, don't reconstruct:

- `CONTEXT.md` - the domain vocabulary. Live Save, Vault, Cloud Vault, Entry, Binding, Baseline, Machine, the 7 Entry States.
- `plans/plan.md` - the plan. Section 3 is the state machine, Section 4 the invariants, Section 6 the explicit non-goals (rejected ideas - do not re-litigate them), Section 7 the slice table and workflow, Section 8 the scenarios the tests must encode.
- `docs/adr/0001-0006` - the six decisions with teeth. ADR-0006 (*absence never propagates*) is the newest and the least obvious.

The code is the other half of the documentation. Module docstrings carry the reasoning; `core/entry_state.py`, `core/transaction.py`, `core/operations.py`, `core/cloud.py`, and `core/github.py` in particular.

---

## Where the work stands

`main` contains, merged via PRs #1-#13:

| Layer | Modules |
|---|---|
| Foundations | `core/paths.py`, `jsonstore.py`, `logger.py`, `config.py`, `credentials.py` |
| Content | `core/hashing.py` |
| State | `core/entry_state.py`, `core/ledger.py` |
| Writes | `core/transaction.py`, `core/backups.py` |
| Git | `core/git.py`, `core/vault.py` (incl. published Bindings in machine files) |
| Verbs | `core/entries.py`, `core/operations.py` (incl. `bind_entry`/`unbind_entry`/`adopt_bindings`) |
| Cloud | `core/cloud.py` - Push, Pull, fetch status, sticky Offline Mode, Merge Conflict resolution |
| Bootstrap | `core/github.py` - four bootstrap paths, identity adoption, REST token hygiene |
| Startup | `core/lock.py` (single instance), `core/startup.py` (recovery, Invariant 2, `App.reset`) |
| Redo | `core/redo.py` - enumerated wipe, Vault-Ahead refusal, backups and Live Saves untouchable |
| UI | `main.py`, `ui/main_window.py`, `ui/dialogs.py` (one `PreviewDialog` for every destructive flow), `ui/presenter.py` (all deciding logic, headless-tested), `ui/style.qss` |

437 tests pass (`uv run pytest`; 11 skip on Windows), 76 mutations all caught (`uv run python scratch/mutate.py`; 4 skip on Windows; filter with `mutate.py <substring>` during development), `ruff check` and `ruff format --check` clean.

## What is left

The **manual** checklist in plan Section 8 - real GitHub auth, the four bootstrap paths first-hand, unplugging the network mid-operation, walking every dialog with a picky eye, Redo Initialization end-to-end including identity adoption on the next setup. The automated suite deliberately excludes the GUI and the network, so a human at the machine is the only honest verifier of those. `uv run python main.py` starts the app.

---

## Things the next session will get wrong if nobody says them

**CI runs `ruff format --check` as well as `ruff check` and pytest.** Running only the linter locally is exactly how PR #10 went red once. Run all three before pushing; the pre-push hook does not catch formatting either.

**The Baseline discipline is structural - keep it that way.** `core/cloud.py` and `core/github.py` do not import `core/ledger` at all; only a completed Sync writes a Baseline (Invariant 6), via `Ledger.record_sync` called from exactly two places in `core/operations.py` (sync and restore). A Pull that advances the Baseline is the single worst bug this app can have. `scratch/mutate.py` carries mutations for both sides of this; `test_a_pull_never_moves_the_baseline` is THE test.

**Offline Mode is sticky and lives in the one `Cloud` instance.** Entering it is automatic on any failed Cloud operation; the *only* exit is an explicit `check_connection` that succeeds (`git ls-remote origin HEAD`). One `Cloud` per running application - constructing a fresh one per operation silently disables the stickiness. A *rejected* push (`PushRejected`) is not a connectivity failure and does not enter Offline Mode.

**PAT hygiene is non-negotiable and already solved twice** - copy the existing patterns, do not invent a third. Git: one-shot inline credential helper, token in the subprocess environment only (`core/git.py`). REST: `Authorization` header per request, built by `core/github.py::rest_request`, which is public precisely so tests can hold it to that. `core/git.py::_redact` scrubs the token from error text.

**`scratch/mutate.py` is the review tool, and it is load-bearing.** Six-plus real defects found so far that the green suite was blind to. Every new slice adds mutations for its own plausible-wrong-ways. Two sharp edges inside it, both learned the hard way: `PYTHONDONTWRITEBYTECODE=1` (same-byte-length mutations otherwise run stale `.pyc` and falsely survive), and the pytest output parser counts `^ERROR` lines as catches, not only `^FAILED` - under `-x`, a mutation that breaks a *fixture* stops the run on an ERROR, and a FAILED-only regex reports it as a false SURVIVOR. Also: `ruff format` can rewrite a mutation's `before` string out of existence; the harness reports that as STALE, so run it (or at least a `before in source` check) after formatting.

**Real Git, not assumptions.** Several designs were settled by experiment against real Git and would have been wrong otherwise: cone-mode sparse-checkout's leading-directory rule, `git add`'s refusal of an unmatched pathspec, `branch --show-current` for an unborn HEAD, an empty repo answering `ls-remote --symref` with nothing (which is how bootstrap detects emptiness), and - Windows-specific - `CreateProcess` resolving a bare `git` to `.exe` only, which is why `core/git.py` resolves the executable itself via `shutil.which`. When a "surely Git does X" question comes up, go and check.

**Windows test specifics.** The git-spy tests write a `git.bat` fake (CRLF, `%*` capture); symlink and chmod tests skip via `skipif(os.name == "nt")`; pytest summary lines carry `\r`, so pipe through `tr '\r' '\n'` (or PowerShell equivalent) before grepping.

---

## Conventions in force

From the user's global instructions (org-managed `CLAUDE.md`):

- No em dashes. Plain `-` only.
- No emojis, anywhere.
- No agent co-author trailer in commit messages.
- Development cost is not a tiebreaker. Prefer quality, simplicity, robustness, long-term maintainability.
- Bug fixes start by reproducing the bug end-to-end, as the user would hit it.
- Lint failures, test failures, and flakiness get fixed on sight, even when unrelated to the task at hand.

Project conventions: `uv`, `pyproject.toml` (never `requirements.txt`), Python 3.12, `ruff` (line-length 100, plus `ruff format`), tests in `scratch/tests`, Git driven by `subprocess` (never `gitpython`). GitHub REST via stdlib `urllib` - no `requests` dependency. Commit messages are short declarative sentences; one branch per slice; squash-merge via PR once green.

---

## Suggested skills

- **`/run`** to launch the app (`uv run python main.py`) for the manual Section 8 checklist. The user is explicitly picky about UI and expects pixel perfection; the offscreen-screenshot trick used during development (`WA_DontShowOnScreen` + `grab()`) renders real fonts without flashing a window.
- **`/tdd`** for any new headless logic. The pattern held for every slice: the deciding half lives in `core/` or `ui/presenter.py` and is tested; the Qt half only renders.
- **`/code-review`** before opening each PR, to check the diff against the plan rather than against itself.
- **`/grilling`** if any new design question comes up that the plan does not already answer. The current plan is the output of one of these and is far better for it.

Do not reach for `/init` - `CLAUDE.md`, `CONTEXT.md`, and the ADRs already cover the ground.
