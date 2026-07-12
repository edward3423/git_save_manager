# An Entry's identity is a UUID, not its name

An Entry's on-disk name in the Vault must be legal on every Machine that clones it. Human names are not: Windows rejects `: * ? " < > |`, reserves `CON`/`NUL`/`COM1`, and forbids trailing dots - so an Entry created on macOS as `Save: Elden Ring (2024)` makes the Windows checkout *fail outright*. Worse, macOS and Windows filesystems are case-insensitive while Git is not, so `Elden Ring` and `elden ring` are two paths to Git and one directory to the OS, and the working tree silently becomes incoherent. We therefore name every Entry directory (and its metadata sidecar) with a generated UUID, and keep the freely-renamable **Display Name** in the sidecar.

## Consequences

- **Renaming an Entry is free** - it rewrites one JSON field, touches no paths, and cannot break per-Entry history. There are no renames to follow, which is why history lookups use plain `git log -- entries/<id>/` and not `--follow` (which in any case "works only for a single file" and would not work on an Entry directory).
- **Display-name uniqueness becomes a soft UI policy, not a correctness requirement.** Duplicate names are merely confusing, never corrupting.
- **The raw Vault is not human-browsable.** Mitigated by putting the Display Name in every commit message (so `git log` reads naturally), keeping the sidecar beside the directory (so `grep -l Elden entries/*.json` finds it), and offering "Reveal in Finder" in the app.
- The metadata sidecar is a **sibling** of the content directory, never inside it. A Vault-to-Live restore copies that directory verbatim onto the real save location, so anything stored inside it would be injected into the game's save folder on every restore.
