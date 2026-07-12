# Selective sync via blobless partial clone and sparse checkout

The Cloud Vault is a mega-repo holding every Entry from every Machine, but any given Machine typically cares about a handful of them - and save files are incompressible binaries whose history only grows. A plain clone would make every Machine pay, in disk and bandwidth, for every Entry's entire history. We therefore clone with `--filter=blob:none` and drive `git sparse-checkout` from the set of **bound** Entries: commits and trees (which history, log, and rollback listings need) are fetched cheaply, while an Entry's file contents are downloaded only when it is bound on this Machine.

## Consequences

- **Binding is the selective-sync switch**, at both the semantic and the physical level. An Unlinked Entry costs essentially nothing on this Machine.
- `machines/` and every Entry metadata sidecar are **pinned into the sparse set unconditionally** - the app must see all Entries and all published Bindings even for Entries it has not bound. They are small JSON and effectively free.
- **Materializing content never before fetched requires network** - binding a new Entry, or rolling back to a revision whose blobs are not held locally. This is physics, not a limitation: you cannot restore data you never downloaded. Everything else (Sync, commit, history, restore of held blobs) works offline.
- This is **reversible**: the sparse set is exactly the set of bound Entries, so falling back to full clones is a configuration change and a re-clone, not a schema migration.
