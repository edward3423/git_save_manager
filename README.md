# Git Save Manager

A desktop app that manages game saves and application settings by versioning them in a Git
repository backed by a private GitHub remote, so the same files can be carried between
machines with history and rollback.

## Install

Linux and macOS:

```bash
curl -fsSL https://raw.githubusercontent.com/edward3423/git_save_manager/main/install.sh | bash
```

Windows (PowerShell):

```powershell
irm https://raw.githubusercontent.com/edward3423/git_save_manager/main/install.ps1 | iex
```

Then open a new terminal and run:

```
gsm
```

Either script installs uv if needed, clones the app, syncs its dependencies, puts a `gsm`
launcher on your PATH, and adds a menu entry - a `.desktop` file on Linux, an app bundle in
`~/Applications` on macOS, a Start Menu shortcut on Windows. Neither installs system packages
for you: they detect what is missing and print the exact command to run. On Linux that is the
Qt xcb library and a keyring, listed under [Troubleshooting](#troubleshooting-linux); on macOS
the Xcode command line tools; on Windows only Git, since PyQt6 ships its own Qt plugin and the
token goes to the Windows Credential Manager.

Default locations:

| | Checkout | Launcher |
| --- | --- | --- |
| Linux, macOS | `~/.local/share/git-save-manager` | `~/.local/bin/gsm` |
| Windows | `%LOCALAPPDATA%\Programs\git-save-manager` | `%LOCALAPPDATA%\Programs\bin\gsm.cmd` |

If the launcher directory is not already on your PATH, the installer adds it: an `export PATH`
line in your shell profile (`.zshrc`, `.bashrc`, or `.profile`, matching `$SHELL`) on Linux and
macOS, the user PATH environment variable on Windows. Either way `gsm` works in a **new**
terminal. Set `GSM_NO_PATH=1` to be left alone.

To **update**, run `gsm update` (or `gsm update --check` to only see what is new). It
fast-forwards the checkout to the latest `main` and re-syncs dependencies in place; your
`data/` directory, including the Vault, is untouched. It refuses, changing nothing, if the
checkout has local edits or commits - running the install command again replaces the
checkout outright and also refreshes the launcher and menu entry.

Overrides: `PREFIX` (`GSM_PREFIX` on Windows) to install somewhere else, `GSM_REF` to track a
different branch or tag, `GSM_NO_DESKTOP=1` (`GSM_NO_SHORTCUT=1` on Windows) to skip the menu
entry, `GSM_NO_PATH=1` to skip the PATH change.

## Command line

`gsm` on its own opens the window. `gsm COMMAND` runs one command and exits, without loading
the GUI toolkit - for a terminal, a script, or an agent. `gsm --help` lists every command, and
`gsm COMMAND --help` describes one.

```
gsm status                       # every Entry's state, and the Cloud Vault's
gsm sync Hades -m "boss slain"   # Live Save -> Vault, one commit
gsm push                         # Vault -> Cloud Vault
gsm pull                         # Cloud Vault -> Vault (no Live Save is touched)
gsm restore Hades                # Vault -> Live Save, previewed and backed up
gsm sync --all                   # every Entry that is Local Ahead
gsm restore --all                # every Entry that is Vault Ahead
```

The rest: `show`, `history`, `backups`, `machines`, `log`, `resolve`, `rollback`, `add`,
`bind`, `unbind`, `rename`, `remove`, `add-file`, `remove-file`, `download`, `fetch`, `check`,
`setup`, `sign-in`, `redo`, `update`. An Entry is named by its Display Name (any case), its
ID, or a unique ID prefix.

The commands keep the window's rules. Each re-checks the Entry's state immediately before
acting, and refuses what the state does not offer. Every destructive command prints exactly
what it will do first and then asks; with no terminal to ask at, it stops with exit code 3
having done nothing, unless given `--yes`. For a write over a Live Save, `--dry-run` also
prints an approval token, and `--approve TOKEN` proceeds only if nothing has changed since
that preview - a game that saved in between is never overwritten.

For scripts and agents:

- `--json` makes stdout exactly one JSON object: `{"ok": true, "command": ..., ...}`, or
  `{"ok": false, "error": {"kind": ..., "message": ...}}`. Progress and warnings go to stderr.
- Exit codes: 0 done, 1 refused or failed, 2 bad command line, 3 needs confirmation.
- A Pull that two Machines contest stops and changes nothing until each Entry is given a side:
  `gsm pull --take Hades=vault` keeps this Machine's line, `=cloud` takes the other's.
- Sign in without a browser by piping a token: `gsm setup OWNER/REPO --token-stdin`. A token
  is never accepted as an argument, where it would land in shell history.
- The window and every command share one lock, so commands are refused while the window is
  open, and two commands started at once never both run.

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
