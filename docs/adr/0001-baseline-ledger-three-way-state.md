# Entry state is derived from a three-way comparison against a local Baseline

Comparing only the Live Save and the Vault tells us *that* they differ but not *which one moved* - and the two possibilities ("I played the game" vs "I pulled another Machine's save") demand opposite actions, each of which destroys the data the other preserves. We therefore store a **Baseline** - the content hash of the Entry at the last completed Sync - in a per-Machine [[Ledger]] that is never committed, and derive the Entry State from a three-way comparison of Live, Vault, and Baseline. This turns two ambiguous situations into automatic ones and leaves exactly one - both sides moved - that requires a human.

## Considered Options

- **Two-way comparison (Live vs Vault).** Rejected: cannot attribute a change, so the app must either guess (and lose saves) or prompt on every operation (making the human the sync algorithm).
- **Event tracking - remember "I pulled, so mark these Vault Ahead".** Rejected: the app is closed during the single most important mutation in the system - you playing the game. That change fires no event, so an app whose state is a memory of its own actions can never see it. Manual edits, out-of-band `git` commands, and crashes are invisible for the same reason.
- **Modification times.** Rejected: games rewrite saves without changing bytes (phantom changes), `git checkout` stamps mtimes to *now* (a Pull would look newer than a real local edit), and clocks drift between Machines.

## Consequences

The governing principle is **remember only what you did; observe everything else.** The Baseline is memory of the one thing the app performed itself. Live and Vault are re-read from disk every time, so whatever happened while the app was closed is observed directly and the state is self-healing.

Two rules follow and are load-bearing:

- The Baseline is written **only by a completed Sync**, never by a Push or Pull. A Pull changes the Vault but not the Live Save; moving the Baseline there would report **In Sync** while the Live Save is stale, and the next Sync would overwrite the pulled save with a stale one - the exact data loss this design exists to prevent.
- If Live and Vault hash **identically**, the Entry is In Sync and the Baseline is *repaired* to that hash, before the three-way table is consulted. This heals a stale Baseline left by a crash between "files written" and "Baseline updated", which would otherwise surface as a false Conflict.
