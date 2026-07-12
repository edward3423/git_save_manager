# Every file in the Vault has exactly one writer

A single committed `machines.json` would be the one file every Machine writes to, changing on every bind on every Machine - guaranteeing recurring text conflicts inside a structured file, and doing so in the file that describes where all the other files live. We instead give every Machine its own `machines/<machine-id>.json`, holding its identity and its published Bindings, and derive the shared view as the union of those files.

## Consequences

- **No two Machines ever write the same bytes.** Entry content is written by whichever Machine last Synced it; a machine file is written only by its owner. Concurrent binds on two Machines now merge cleanly and automatically.
- A whole category of Git conflict becomes **structurally impossible** rather than merely handled. When a Merge Conflict does occur it is therefore always a real one: two Machines genuinely changed the same Entry's save data - which is exactly the case the Conflict resolution flow exists for, and nothing else ever lands there.
- The Ledger and the published machine file hold overlapping Binding data. This duplication is deliberate: the Ledger is local truth (and additionally holds Baselines, which are never shared), while the published file is a projection other Machines read as a hint.
