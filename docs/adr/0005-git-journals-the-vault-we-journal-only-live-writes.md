# Git journals the Vault; we journal only writes to the Live Save

Every operation that changes the Vault - Sync to Vault, rollback, removal, pull, merge - is a Git operation, and Git's own crash safety is better than anything we would write. Combined with the invariant that a Sync is atomically one commit (so **the Vault is always clean between operations**), recovery of the Vault becomes trivially decidable: any dirt found at startup is by definition an interrupted operation and is discarded with `git reset --hard && git clean -fd` (never `-x`), or `git merge --abort` if a merge was in progress. Our own journal therefore covers only the one thing Git cannot protect because it lives outside the repository: **writes to the Live Save**.

## Consequences

- Only two operations write to a Live Save - a Vault-to-Live restore and a Backup restore - so only those are journaled, recording the Entry, the live path, the Backup zip's location, and the stage reached (`backed_up` → `staged` → `swapped` → `done`). On startup the app either completes the swap or rolls back using the Backup, whose existence is guaranteed because taking it is stage one.
- The safety story reduces to one sentence: **Git protects the Vault; the journal protects the Live Save; nothing else needs protecting.**
- This depends on the Vault containing **no ignored files**, which is why the Ledger, journal, and backups are siblings of the Vault rather than gitignored children. It makes `git clean` in the recovery path harmless rather than catastrophic, and it means deleting or re-cloning the Vault (the natural fix for a wedged repo, and what Redo Initialization does) cannot take the local Bindings, Baselines, or restore-safety zips with it.
