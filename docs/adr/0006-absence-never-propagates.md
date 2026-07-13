# ADR-0006: Absence is content-free, and it never propagates

## Status

Accepted.

## Context

[ADR-0001](0001-baseline-ledger-three-way-state.md) derives an Entry's state by comparing the Live Save and the Vault against a Baseline. The table it gives treats "this side has no content" as simply another value to compare - absence differs from a hash, so the side reads as *changed*.

Building the state machine against real files showed that this is safe only while there is no Baseline, and dangerous the moment there is one. Once a Baseline exists, absence does not mean "changed". It means **something was destroyed or disconnected** - and the plain three-way answer is to propagate that destruction to the other copy:

| What actually happened | What the three-way table says | What the app would offer | Result |
|---|---|---|---|
| The external drive holding the saves is unplugged | Live "changed", Vault did not: **Local Ahead** | Sync to Vault | Commits the absence. **Erases the Entry's content in the Vault.** |
| Another Machine removed the Entry from the Vault | Vault "changed", Live did not: **Vault Ahead** | Restore to Live | Writes nothing over the save. **Deletes the Live Save.** |

Each violates an invariant outright - the second is Invariant 1, the one the whole application exists to uphold. Neither is exotic: the first is an unplugged drive or an uninstalled game, and the second is a documented, supported operation on another Machine.

A second, subtler form of the same bug hides in the word *absence*. A game that is uninstalled often removes its save files but leaves the folder behind. That folder is **empty, not absent** - so a naive `path.exists()` check reports content where there is none, and the disaster above proceeds exactly as if the guard were not there.

## Decision

**A side has no content when its path does not exist, or when it is a directory containing no files.** These are one state, not two, because Git stores no empty directories: an Entry whose content directory is empty is simply *not there* after a clone. `core.hashing.content_hash` returns `None` for both.

This is forced, not chosen. It is the same rule the hashing scheme already follows - *the hash may only describe what the Vault can represent* - applied one level up. Were empty and absent to hash differently, an Entry with no saves in it could never be In Sync with its own faithful, and necessarily absent, copy in the Vault.

**Where a Baseline exists, an absent side is its own state, and never evidence to change the other side.** Three states, none of which the plain table produces:

- **Live Save Missing** - offer Restore or Unbind. Never Sync.
- **Removed from Vault** - offer Sync (re-add) or Unbind. Never Restore.
- **No Content** - nothing anywhere; offer Unbind only. Notably *not* In Sync: there is no content to be in sync about, and a green tick over an Entry with no data is a lie.

Neither of the first two recommends a default. An unplugged drive and a deliberate deletion are indistinguishable from inside the app, and the right answer differs completely between them.

**Two rules make this structural rather than a property of six separate branches.** They are applied last, to every state, without exception:

```
Sync to Vault    is never offered when the Live Save has no content.
Restore to Live  is never offered when the Vault has no content.
```

Content is destroyed only by an explicit act - Remove from Vault, or the user's own hand - never as a *derived recommendation*.

## Consequences

The state machine has seven states rather than four. That is a real cost in surface area, paid for by two disasters that the four-state version reaches through entirely ordinary use.

The state space stays finite and tiny: only the *equality pattern* of three values matters, so it is exactly 3x3x3 = 27 cases. The tests enumerate all of them, assert the expected state for each, and assert the two rules above across the whole space at once. There is nowhere for a case to hide.

An Entry that legitimately becomes empty - every save deleted, deliberately - cannot be Synced. Removing its content from the Vault requires the explicit Remove from Vault operation. This is the intended trade: the app cannot tell that deletion apart from an unplugged drive, and it must not guess.

## Alternatives considered

**Guard at Sync time instead of in the state machine.** Refuse the commit rather than the recommendation. Rejected: the state machine's whole job is to produce the right offered action, and a UI that presents a button its executor will refuse is a UI that lied. The guard belongs where the recommendation is made - though a Sync-time assertion remains cheap defence in depth, and is left for `vault.py`.

**Treat an empty directory as content distinct from absence.** Rejected: it makes In Sync unreachable for an Entry that round-trips through the Vault, which is the exact failure mode `hashing.py` was written to avoid.
