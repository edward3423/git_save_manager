# Git Save Manager

A desktop tool that manages game save files and application settings by versioning them in a Git repository backed by a private remote, so the same files can be carried between machines with history and rollback.

## Language

### The three copies

**Live Save**:
The real files on disk that the game or application actually reads and writes, at a location owned by that program.
_Avoid_: local save, original, source

**Vault**:
The local Git repository holding a managed copy of every Entry's files, plus the metadata that describes them. Not the same repository as the app's own source.
_Avoid_: repo, local repo, store

**Cloud Vault**:
The private remote repository the Vault pushes to and pulls from.
_Avoid_: origin, GitHub repo, server

### Entries

**Entry**:
A unit of managed content - one file or one directory - that is versioned in the Vault and can exist on many Machines. Its identity is its **Entry ID**, never its name and never its path.
_Avoid_: item, save, game, profile, target

**Entry ID**:
The immutable, generated identifier of an Entry. It is the only name the Vault uses on disk, so an Entry's location never changes and its display name can be edited freely.
_Avoid_: slug, key, folder name

**Display Name**:
The human-readable label of an Entry, held in its metadata and freely renamable. Carries no identity and appears in no path.
_Avoid_: title, entry name, label

**Binding**:
The association between an Entry and the Live Save path it occupies on one specific Machine. Bindings are per-Machine; the same Entry has a different Binding on each Machine.
_Avoid_: mapping, link, path config

**Unlinked**:
The state of an Entry that exists in the Vault but has no Binding on this Machine. Unlinked Entries are visible but cannot be synced until a Binding is created, which the user must confirm explicitly.
_Avoid_: unbound, orphaned, inactive

### Machines

**Machine**:
One computer registered in the Vault, holding its own Bindings. The single canonical term for a participating computer.
_Avoid_: device, host, node, client

### Movement

The two axes of movement are never both called "sync". Sync moves data between Live Save and Vault; Push and Pull move it between Vault and Cloud Vault. Nothing moves between Live Save and Cloud Vault directly.

**Sync**:
Copying an Entry's content between the Live Save and the Vault, in one named direction. The only operation that makes the two copies equal, and therefore the only one that may write a Baseline.
_Avoid_: backup, save, upload (when Live to Vault); restore, download (when Vault to Live)

**Push** / **Pull**:
Transferring commits between the Vault and the Cloud Vault. Neither ever touches a Live Save, and neither ever writes a Baseline.
_Avoid_: sync, upload, download

**Backup**:
A zip archive of a Live Save taken immediately before an operation would overwrite it. The undo for a destructive Sync.
_Avoid_: snapshot, copy, restore point

### State

**Baseline**:
The content hash of an Entry as of the last completed Sync on this Machine - a memory of the last moment the Live Save and the Vault were known to be identical. Written only by a completed Sync; never by a Push or Pull.
_Avoid_: last known state, snapshot hash, checkpoint

**Ledger**:
The per-Machine, never-committed record holding each bound Entry's Binding and Baseline. Local truth about this Machine, deliberately not shared with others.
_Avoid_: state file, cache, index

**Offline Mode**:
The degraded mode the app enters the moment any operation against the Cloud Vault fails, and stays in until an explicit connection check succeeds. Push and Pull are unavailable; every purely local capability continues to work. Carries the reason it was entered (no network, or authentication failure).
_Avoid_: disconnected, error state, no internet

**Entry State**:
The result of comparing an Entry's Live Save and Vault content against its Baseline. Exactly one of:
- **In Sync** - neither side has moved.
- **Local Ahead** - the Live Save changed; the Vault has not.
- **Vault Ahead** - the Vault changed (typically by a Pull); the Live Save has not.
- **Conflict** - both sides changed since the Baseline. The only state requiring a human decision.
- **Live Save Missing** - the Live Save is gone, though a Baseline says it was there. An unplugged drive, or an uninstalled game.
- **Removed from Vault** - another Machine removed the Entry from the Vault, though a Baseline says it was there.
- **No Content** - neither side holds anything. Usually a Binding pointing at a path the game has not created yet.
_Avoid_: dirty, modified, out of date, stale

**No Content**:
An Entry side holds no content when its path does not exist *or* when it is a directory containing no files. Git stores no empty directories, so the Vault cannot tell those two apart - and neither, therefore, may we. See [ADR-0006](docs/adr/0006-absence-never-propagates.md).
_Avoid_: empty (ambiguous: an empty *file* is content)
