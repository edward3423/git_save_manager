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

## Troubleshooting (Linux)

### `Could not load the Qt platform plugin "xcb"`

Qt 6.5+ needs the X11 cursor library, which many distros don't ship by default:

```bash
sudo apt install -y libxcb-cursor0
```

If it still fails, install the wider xcb set:

```bash
sudo apt install -y libxcb-cursor0 libxcb-xinerama0 libxcb-icccm4 \
                    libxcb-image0 libxcb-keysyms1 libxcb-render-util0
```

On Wayland you can skip X11 entirely: `QT_QPA_PLATFORM=wayland uv run main.py`.

### The app hangs at startup, or crashes with a `secretstorage` / D-Bus error

The PAT is stored in the OS keyring (see [`core/credentials.py`](core/credentials.py) - it is
kept nowhere else). If your login keyring was never unlocked, there is no default Secret
Service collection, and the keyring library blocks trying to create one behind a D-Bus prompt.
The traceback ends in `secretstorage` /`jeepney` with
`Object does not exist at path .../collection/login`.

Create a default keyring with an **empty password** so it auto-unlocks at every login:

1. Open **Passwords and Keys** (`seahorse`).
2. If a stale keyring file exists but no keyring is listed, move it aside first:
   `mv ~/.local/share/keyrings/login.keyring{,.bak}`
3. Click **`+`** → **Password Keyring**, name it **`Login`**, and leave the password **blank**
   (confirm *Use Unsafe Storage*).
4. Right-click the new **Login** keyring → **Set as default**.

Restart the app. An empty-password keyring unlocks automatically, so there are no further
prompts.
