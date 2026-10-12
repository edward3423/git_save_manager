# ADR-0007: An Entry may be a set of files

## Status

Accepted.

## Context

An Entry was one path: a file or a folder. Some things worth keeping are several files in unrelated folders that mean nothing apart - a tiling window manager's `~/.yabairc` and the hotkey daemon's `~/.skhdrc` that drives it. Their common folder is the home directory, which cannot be an Entry, and two separate Entries can be restored, rolled back and conflicted independently, producing a configuration that never existed on any Machine.

## Decision

**An Entry may be a File Set: several individual files, each a member, kept as one unit.**

- **Layout.** Members are stored flat, `entries/<id>/<member>`, beside a single-file Entry's own layout. The sidecar lists them in `files` (entries schema 3); `content_name` stays for a file or folder Entry. Members are files only: a folder is its own Entry.
- **Identity.** A member is its member name - its basename when it was added - not its path. Each Machine binds each member to its own path (`Binding.files`, ledger schema 2; published as a member -> path map), so the same member can be `~/.skhdrc` on one Machine and `~/.config/skhd/skhdrc` on another. Names must differ case-insensitively (ADR-0004).
- **One hash.** A File Set hashes as `directory_digest` over (member name, file hash) - exactly the hash of its folder in the Vault, and for one member exactly the single-file hash. So a single-file Entry becomes a File Set by gaining a second member, keeping its layout, history and Baseline.
- **Absence (ADR-0006).** One missing member makes the whole Live side absent, never a smaller set: otherwise a Sync would commit the absence and erase that file from the Vault. A member with no path on this Machine (added elsewhere) makes the Entry **Needs Binding**, which has no state until it is bound.
- **One write.** Restore swaps every member as one write: one Backup holding every member about to be overwritten, every member staged and fsynced before any swap, one journal. Recovery rolls every member forward once any swap has begun, and abandons every member if none has - never a mixture.
- **Membership changes are commits.** Adding a file records it (the next Sync captures it). Removing one deletes its stored copy for every Machine, touches no file on any disk, and never removes the last member - that is Remove from Vault. Rollback restores members' content, never membership: a file since removed is not brought back.

## Consequences

- Older builds understand neither `files` nor a published map, so every Machine must be updated before the first File Set is created.
- A symlinked member is followed: the Vault stores the file it leads to, and Restore writes through the link. Dotfiles are routinely links into a dotfiles repository.
- A file a Machine has bound that another Machine has not yet Synced reads, on the first Machine, as Local Ahead; on a Machine where it has never existed, it reads Live Save Missing with Restore offered but not recommended, since one missing file cannot be told from a deleted one.
