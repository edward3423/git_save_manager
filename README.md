# Git Save Manager

A desktop app that manages game saves and application settings by versioning them in a Git
repository backed by a private GitHub remote, so the same files can be carried between
machines with history and rollback.

## Design

Read these before changing anything:

- [`CONTEXT.md`](CONTEXT.md) - the domain glossary. Terms are used precisely. In particular:
  **Sync** moves data between the Live Save and the Vault; **Push** and **Pull** move it
  between the Vault and the Cloud Vault. They are never both called "sync".
- [`plans/plan.md`](plans/plan.md) - the invariants, the state machine, and the roadmap.
- [`docs/adr/`](docs/adr/) - the decisions that are expensive to reverse.

The invariant that matters most: **the application never deletes a Live Save**, and exactly
one code path is permitted to overwrite one. That path is preview-gated, backed up, and
journaled. Rollback, conflict resolution, and backup restore all route through it rather
than writing files themselves.

## Two repositories, nested

- `git_save_manager/` - this repository. The app's source code.
- `git_save_manager/data/vault/` - the **Vault**: a *separate* Git repository holding your
  save files, whose remote is your private Cloud Vault. The app drives it programmatically.

They are kept apart by the single `/data/` line in `.gitignore`, so the outer repository
cannot see the inner one. All runtime state lives under `data/` and is disposable:
`rm -rf data/` is always a valid recovery, because nothing there cannot be reconstructed.

## Development

```bash
uv sync                              # install dependencies
uv run pytest                        # run the tests
uv run ruff check . && uv run ruff format .
uv run python main.py                # launch the app

git config core.hooksPath .githooks  # once: run lint and tests before every push
```

`main` is always green. Work happens on a branch, arrives via a pull request, and is
squash-merged once lint and tests pass.

The tests are headless: real Git repositories in temp directories, a second Machine
simulated by cloning. No network, no GitHub, no PyQt, no games.
