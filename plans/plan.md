# Implementation Plan: Git-Backed Save File Manager

A PyQt6 desktop app that manages game saves and application settings by versioning them in a Git repository backed by a private GitHub remote, so the same files can be carried between machines with history and rollback.

Vocabulary is defined in [`CONTEXT.md`](../CONTEXT.md) and is used precisely throughout this document. The load-bearing architectural decisions are recorded in [`docs/adr/`](../docs/adr/).

---

## 1. Invariants

These hold everywhere. They are the reason the design is safe, and no feature may break them.

1. **The application never deletes a Live Save.** Not on unbind, not on removal from the Vault, not on rollback. The only code permitted to overwrite Live data is the single restore path, which is preview-gated, backed up, and journaled.
2. **The Vault is always clean between operations.** A Sync to the Vault is atomically one commit. Any dirt found at startup is, by definition, an interrupted operation, and is discarded.
3. **The Vault is append-only.** Rollback and removal are forward commits. Nothing ever rewrites history, so no committed content can be lost.
4. **Every file in the Vault has exactly one writer** ([ADR-0003](../docs/adr/0003-single-writer-vault-layout.md)). Therefore every Merge Conflict that occurs is a real one.
5. **An Entry's content directory holds save data and nothing else** ([ADR-0004](../docs/adr/0004-entry-identity-is-a-uuid.md)). Restore copies it verbatim onto the Live Save, so metadata lives in a sibling sidecar.
6. **The Baseline is written only by a completed Sync** ([ADR-0001](../docs/adr/0001-baseline-ledger-three-way-state.md)). Never by a Push or a Pull.
7. **Every destructive operation previews its exact file operations first** - every path that will be written, overwritten, or deleted, and where the Backup lands.
8. **The Vault contains no ignored files.** Local state lives beside it, never inside it.

---

## 2. Directory Structure

```text
git_save_manager/
├── main.py                     # Entry point
├── pyproject.toml              # uv-managed deps; requires-python >=3.12
├── .gitignore                  # one line: /data/
├── CONTEXT.md                  # domain glossary
├── docs/adr/                   # architecture decisions
├── plans/plan.md               # this file
│
├── core/
│   ├── config.py               # config.json + OS keyring (PAT)
│   ├── ledger.py               # Bindings + Baselines (per-Machine, never committed)
│   ├── hashing.py              # SHA-256 file and directory content hashes
│   ├── entry_state.py          # the four-state machine
│   ├── vault.py                # Git operations, Cloud status, partial clone/sparse set
│   ├── transaction.py          # journaled Live-Save writes, backups, startup recovery
│   ├── github.py               # repo creation/validation via REST
│   └── logger.py               # logging -> GUI console via Qt signal
│
├── ui/
│   ├── main_window.py
│   ├── dialogs.py
│   └── style.qss
│
├── scratch/tests/              # automated tests (headless: no GUI, no network)
│
└── data/                       # ALL runtime state. Gitignored. Never committed.
    ├── config.json             # machine id (UUID), repo, default branch
    ├── ledger.json             # Bindings + Baselines for this Machine
    ├── journal.json            # active Live-Save transaction
    ├── app.lock                # single-instance PID lock
    ├── backups/                # restore-safety zips
    └── vault/                  # the Git repo. Committed content ONLY.
        ├── vault.json          # {"vault": true, "schema": 1, ...}
        ├── machines/
        │   └── <machine-uuid>.json     # ONLY its owner writes this
        └── entries/
            ├── <entry-uuid>/           # content: byte-for-byte the Live Save
            └── <entry-uuid>.json       # sidecar: display name, kind, created_by
```

The app must live somewhere writable. **Check at startup and refuse to run otherwise**, with a clear message - failing at the start of an operation is safe; failing in the middle is what the journal exists for.

---

## 3. The State Machine

The heart of the application ([ADR-0001](../docs/adr/0001-baseline-ledger-three-way-state.md)). An Entry's state is derived, never remembered.

```
if hash(Live) == hash(Vault):
    state = In Sync;  repair Baseline to that hash   # self-healing short-circuit
else:
    Live vs Baseline    Vault vs Baseline    State
    ----------------    -----------------    -----
    same                same                 In Sync
    CHANGED             same                 Local Ahead    -> offer Sync to Vault
    same                CHANGED              Vault Ahead    -> offer Restore to Live
    CHANGED             CHANGED              Conflict       -> ask the human
```

The equality short-circuit runs **first**. It heals a stale Baseline left by a crash between "files written" and "Baseline updated" (which would otherwise surface as a false Conflict), and it means binding an Entry whose live path already matches the Vault requires no prompt at all.

**Two distinct conflicts**, resolved the same way but triggered differently:

- **Sync Conflict** - Live and Vault both moved since the Baseline. Purely local; Git uninvolved.
- **Merge Conflict** - Vault and Cloud both have commits touching the same Entry. Raised by Git during a Pull.

**Resolution is always at Entry granularity, never per-file.** Resolving `slot1.sav` from one side and `slot3.sav` from the other yields a save that never existed on any machine. You take all of one side or all of the other. Resolving a Merge Conflict is non-destructive (both lineages remain in history and reachable via Rollback); resolving a Sync Conflict toward the Vault *is* destructive to the Live Save, which is why it takes a Backup first.

---

## 4. Implementation Roadmap

### Phase 1: Core utilities

1. **`logger.py`** - handler forwarding to stdout and emitting a Qt signal for the GUI console.
2. **`config.py`** - `data/config.json` (Machine ID = generated UUID, repo, default branch); PAT via `keyring`; validation against `https://api.github.com/user`.
   - **Machine ID is a generated UUID.** Hostname and OS are *display attributes*, refreshed every launch. **MAC addresses are not used** - they change with docking, VPNs, and per-network randomization, and are a stable hardware identifier we have no reason to commit to a repo.
3. **`hashing.py`** - SHA-256 per file; directory hash = recursive walk, sorted by relative path, composite over relative paths and contents. Cache per-file hashes keyed by `(size, mtime)` - as a cache key only, never as evidence of change.

### Phase 2: Safety - Live Save writes

1. **`transaction.py`**
   - **Transactional write.** Stage as a **sibling of the destination** (same directory ⇒ same volume). Atomic rename holds only within one filesystem; across volumes `os.rename`/`os.replace` raise `EXDEV` and `shutil.move` silently degrades to a non-atomic copy-then-delete. `flush()` + `os.fsync()` before the swap. Swap files with `os.replace()` (atomic same-volume; unlike `os.rename`, overwrites on Windows). Directories use the two-step swap (`dest`→`dest.old`, `dest.tmp`→`dest`, delete `dest.old`), journaling each step.
   - **Backups.** Zip the Live Save immediately before *any* write to it, and only then - Vault→Live restore, conflict resolution toward the Vault, Backup restore. A Sync *to* the Vault writes nothing Live and needs none. Skip the write and the zip entirely if the Live content already equals what we would write. Retention: keep the most recent **10 per Entry**, configurable, pruning oldest first and **only after a successful operation**.
   - **Backup restore is first class.** A per-Entry Backups view (timestamp, size, causing operation) with one-click restore through this same preview/backup/journal path, plus "Reveal in Finder" so the raw zip is reachable even if the app is broken. For Live progress overwritten before it was ever Synced, **the zip is the only copy in existence** - it cannot be a folder you are expected to unzip by hand.
   - **Journal + startup recovery** ([ADR-0005](../docs/adr/0005-git-journals-the-vault-we-journal-only-live-writes.md)). Journal *only* Live-Save writes: Entry, live path, backup location, stage (`backed_up` → `staged` → `swapped` → `done`). On startup: complete or roll back. The Vault needs no journal - assert Invariant 2 instead (`git merge --abort` if mid-merge, else `git reset --hard && git clean -fd`; **never `-x`**).
2. **`entry_state.py`** - the state machine above.
3. **`ledger.py`** - `data/ledger.json`: per bound Entry, its Binding (live path) and Baseline (content hash), plus last-sync time and direction for the UI.
4. **`app.lock`** - single-instance PID lock. Two instances running Git against one Vault and both writing the Ledger corrupts state; a lost Baseline means a wrong direction recommendation. Second instance refuses to start; a stale lock (dead PID) can be taken over.

### Phase 3: Git and Cloud

1. **`vault.py`**
   - **Clone**: `--filter=blob:none` + `sparse-checkout` driven by the bound-Entry set ([ADR-0002](../docs/adr/0002-selective-sync-partial-clone.md)). `vault.json`, `machines/`, and all Entry sidecars are pinned into the sparse set unconditionally.
   - **Sync (Live → Vault)** is atomically **one commit, one Entry**, with a **stable-read guard**:
     ```
     H1 = hash(Live)                    # already computed for status
     copy Live -> Vault working tree
     H3 = hash(the copy)                # streamed during the copy
     H2 = hash(Live)                    # one extra read
     require H1 == H2 == H3             # else: reset --hard && clean -fd, abort
     git add && git commit              # only now
     write Baseline
     ```
     `H1 == H2` proves the source was stable *for exactly the interval of the copy* - so **no artificial delay is used or wanted**; the copy *is* the window under test. `H3 == H1` proves the copy is faithful, catching torn copies from a full disk or an I/O error, not just a running game. On abort, nothing was committed and nothing pushed; tell the user to close the game and offer **Retry (never automatic** - a running game would just spin).
   - **Commit authorship carries the Machine**: author name = Machine display name, email = `<machine-uuid>@gsm.local`, so `git log --format=%an` yields the committing Machine directly. Message: `sync(<Display Name>): from <machine>`.
   - **Cloud status**: `git fetch` on startup, **asynchronous and non-blocking**. Compare HEAD to `origin/<branch>`: Up-to-date / Behind / Ahead / Diverged. Never auto-pull, never auto-push.
   - **Offline Mode is sticky and first-class.** *Any* failed Cloud operation drops the app into it immediately, with a warning. Push and Pull grey out; **everything else keeps working** - Sync, commit, history, rollback, restore, backups are all purely local. The **only** exit is an explicit **Check Connection** that succeeds, probing with `git ls-remote` (which tests network, credentials, *and* repo access in one shot - reaching github.com proves nothing if the PAT was revoked). Offline carries its reason: *"no network"* vs *"authentication failed"*, the latter offering to re-enter the PAT. The indicator keeps showing the last known Cloud state: *"Offline (last checked 2h ago: Behind)"*.
   - **Pull merges; it never rebases.** Thanks to Invariant 4, Entries no other Machine touched merge silently and automatically. Rebase would rewrite hashes and replay the same conflict once per commit for zero benefit on binary content. Contested Entries map back to Entry-granular choices, applied by taking one side's tree wholesale.
   - **History**: `git log -- entries/<entry-uuid>/`. **Not `--follow`** - it "works only for a single file" per `git log --help`, and an Entry is usually a directory. No rename-following is needed: identity is a UUID, so paths never move.
   - **Rollback**: Vault-only forward commit (`rollback(<Display Name>): to <hash> from <machine>`). It naturally lands the Entry in **Vault Ahead**, and restoring to Live is then the ordinary restore path - so exactly one piece of code in this application ever overwrites a Live Save. Rolling back with unsynced local changes correctly lands in **Conflict**; no special case, nothing at risk.
   - **Size guards** - fail early, not at push time. At Add Entry, refuse/warn above **~90 MB for any single file** (GitHub hard-rejects >100 MB, and discovering that at push leaves a wedged repo). Show Entry size in the details panel and total Vault size in the status area; warn once past a configurable threshold (default 1 GB). Git cannot delta compressed binaries, so the Cloud Vault grows by roughly *(save size) × (number of Syncs)*. The main defense is already free: **the Baseline means we only commit when content actually changed.** Git LFS and history rewriting are **explicitly deferred** - the Vault is an ordinary Git repo, so `git filter-repo` plus a re-clone remains the escape hatch if the wall is ever hit.
2. **`github.py`** - four bootstrap paths, all distinguished:
   | Situation | Behaviour |
   |---|---|
   | Repo does not exist | Create private via API, init Vault structure, push |
   | Repo exists, empty | Clone, init Vault structure, push |
   | Repo exists, is a Vault | Clone, register this Machine (**the second-machine path**) |
   | Repo exists, is something else | **Refuse.** Never commit saves into an unrelated project |
   - Validity is decided by the committed `vault.json` marker, which also carries **`schema`**. An old client meeting `schema: 2` refuses to touch the Vault and tells the user to update - one `if` that prevents the one failure mode capable of damaging every Entry at once.
   - PAT: classic `repo` scope. Validate against `/user` **and** verify access to the target repo before writing anything.
   - Default branch: take it from the remote HEAD; store it in config; name it explicitly in every operation.
   - **PAT hygiene**: the remote URL stays credential-free. Never embed the token - Git persists it in plaintext in `.git/config` and it leaks via `git remote -v`, logs, and backups. Read it from the keyring per invocation, pass it in the git subprocess *environment only*, and inject via a one-shot inline credential helper: `git -c credential.helper='!f() { echo username=token; echo "password=$GIT_VAULT_PAT"; }; f' ...`. The helper text holds no secret, so nothing sensitive touches disk or the process list. API calls send it per-request in the `Authorization` header.

### Phase 4: GUI

1. **`dialogs.py`** - Setup (four bootstrap paths; hostname-match offers **Machine identity adoption** after a Redo Initialization, reclaiming the UUID and published Bindings rather than leaving a ghost); Add Entry; **Bind** (Unlinked → bound, showing other Machines' paths as read-only hints); **Direction choice** on first bind onto a non-empty live path *whose content differs*; Conflict (two buttons, Entry-granular); Pull/Push preview; History & Rollback; Backups; Machines; Full Git Log.
   - Every destructive dialog renders the **operation preview** (Invariant 7). One preview component, used everywhere.
2. **`main_window.py`** - sidebar, details panel, toolbar, log console, dark QSS.
   - **Unlinked Entries are visible but unsyncable** - hiding them would conceal that Vault data exists.
   - **Removed from Vault**: an Entry you had bound that another Machine removed. Offer *unbind* (Live Save untouched) or *re-add* (an ordinary Sync). Never clean up silently.
   - **Staleness**: recompute an Entry's state **on window focus** and again **immediately before executing any operation**, refusing to proceed if it no longer matches what the preview promised. No filesystem watchers - they are OS-specific and fire constantly on directories games are actively writing.
3. **Redo Initialization** - the most destructive button in the app.
   - **Wipes**: keyring PAT, `config.json`, `ledger.json`, the `vault/` clone. All are reconstructible.
   - **Never wipes**: `backups/`. It is the safety net and the only copy of Live data overwritten but never Synced.
   - **Never touches**: any Live Save (Invariant 1).
   - **Refuses to run while the Vault is Ahead of the Cloud.** Unpushed commits exist on this Machine and nowhere else; deleting the clone destroys them permanently. This is the *only* place in the design where committed content can vanish. Not a warning - a refusal, with an explicit "discard N commits" as a separate, chosen act.
   - The confirmation **enumerates every path and keyring entry** it will delete.

---

## 5. Deletion

Two operations, never conflated:

- **Unbind on this Machine** - drops the Binding and Baseline from the Ledger and from this Machine's published file. Vault keeps the Entry and its history; other Machines unaffected; **Live Save untouched**. The Entry returns to Unlinked. Fully reversible, no commit.
- **Remove from Vault** - `git rm` content and sidecar, commit. Affects every Machine on their next Pull. A forward commit, so the content remains recoverable from history. **No Live Save anywhere is touched.**

---

## 6. Explicit Non-Goals

Considered and deliberately rejected. Recorded so they are not re-litigated.

- **Machine-private Entries.** No Entry will ever need to be confined to one Machine, so there is no `scope` field. **Every Entry is shared.** (Should this ever change, an absent field defaults to `shared`, so it is an afternoon's work rather than a migration.)
- **A "global vs local path" flag.** Bindings are per-Machine and always explicitly confirmed, so the flag could only ever pre-fill a suggestion - which is derivable from the other Machines' published Bindings.
- **Per-file conflict resolution.** Save data is a coherent unit; taking one file from each side yields a state that never existed on any Machine.
- **Git LFS and history rewriting.** Deferred, not designed against. The Vault is an ordinary Git repo, so `git filter-repo` plus a re-clone stays available if the size wall is ever hit.
- **Filesystem watchers.** OS-specific, and they fire constantly on directories games are actively writing. Focus-based refresh plus pre-operation re-validation covers the real cases.
- **Automatic pull, push, or retry.** Every Cloud operation and every retry after an aborted Sync is an explicit human act.
- **Deleting Live Saves.** Never, under any operation (Invariant 1).
- **Preserving empty directories and file modes.** The content hash may only describe what the Vault can actually carry. Git stores neither an empty directory nor a reliable permission bit across platforms, so if the hash counted them, a Live Save holding an empty folder would hash differently from its own faithful copy in the Vault - **forever**. The Entry would sit at Local Ahead for all time, every Sync would appear to do nothing, and **In Sync would be unreachable**. Both are therefore excluded from the hash, and the cost is accepted: an empty directory does not survive a round trip through the Vault. (Should a game ever need one, the fix is to record it in the metadata sidecar - never as a `.gitkeep` inside the content directory, which Invariant 5 forbids.)

---

## 7. Verification

### Automated (`scratch/tests/`) - headless: real Git in temp dirs, a second Machine simulated by cloning. No network, no GitHub, no PyQt, no games.

**The Live-Save-overwriting and Vault-destroying paths get tests before the GUI exists at all.** They are the paths that lose data, and they are perfectly testable headlessly.

- **State machine** - construct all four states from real files; assert the reported state and the offered action.
- **Baseline discipline** - assert a Pull does *not* move the Baseline; assert the stale-Live-plus-pulled-Vault case reports **Vault Ahead**, not In Sync. (This is the exact scenario in which a naive design overwrites another machine's progress and reports success.)
- **Self-healing** - a stale Baseline with Live == Vault reports In Sync and repairs itself, rather than a false Conflict.
- **Crash recovery** - kill mid-restore at each journal stage; assert the Live Save is fully old or fully new, **never torn**, and that the Backup exists.
- **Stable-read guard** - mutate the source mid-copy; assert the Sync aborts, the Vault is clean, and no commit was made.
- **Cross-volume writes** - assert staging is a sibling of the destination and the swap never crosses a filesystem.
- **Two-machine flows** - clone the Vault, Sync the same Entry from both, Pull; assert a Merge Conflict is raised at **Entry** granularity, and that the losing side remains reachable in history.
- **Single-writer layout** - concurrent binds on two Machines merge cleanly with no conflict.

### Manual - only what genuinely needs a human: the GUI, real GitHub auth, first-run.

1. First-run setup across all four bootstrap paths, including pointing at a non-Vault repo and being refused.
2. Add global and machine-specific Entries; bind an Unlinked Entry on a second Machine and confirm the details panel shows the first Machine's path.
3. Sync, and verify the commit appears with the correct Machine as author.
4. Restore, and verify a Backup zip is created - then restore *that* backup from the app.
5. Force a Sync Conflict and a Merge Conflict; verify both offer Entry-granular resolution only.
6. Modify the repo on GitHub directly, relaunch, and verify the status shows **Behind** without pulling.
7. Pull the plug on the network mid-operation; verify the app drops into Offline Mode with the right reason, that Push/Pull grey out, that everything else still works, and that only **Check Connection** restores it.
8. History & Rollback across Machines; verify rollback lands the Entry in Vault Ahead and restores through the normal path.
9. Redo Initialization: verify it refuses with unpushed commits, enumerates what it deletes, preserves `backups/`, and offers Machine identity adoption on the next setup.
10. View Git Log.
