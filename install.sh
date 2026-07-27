#!/usr/bin/env bash
#
# Git Save Manager - installer and updater for Linux and macOS.
# Windows has its own script: install.ps1.
#
#   curl -fsSL https://raw.githubusercontent.com/EdwardRusli/git_save_manager/main/install.sh | bash
#
# Running it again updates an existing install in place. It is safe to pipe from curl:
# nothing is read from stdin and nothing prompts.
#
# Environment overrides (all optional):
#   PREFIX      install root, default $HOME/.local
#               -> checkout at $PREFIX/share/git-save-manager
#               -> launcher at $PREFIX/bin/git-save-manager
#               -> desktop entry at $PREFIX/share/applications (Linux)
#   GSM_HOME    override the checkout location on its own
#   GSM_REPO    clone URL, default the public GitHub repository
#   GSM_REF     branch or tag to track, default main
#   GSM_APP_DIR macOS app bundle location, default $HOME/Applications
#   GSM_NO_DESKTOP=1   skip the .desktop entry / .app bundle

set -euo pipefail

REPO_DEFAULT="https://github.com/EdwardRusli/git_save_manager.git"

PREFIX="${PREFIX:-$HOME/.local}"
GSM_HOME="${GSM_HOME:-$PREFIX/share/git-save-manager}"
GSM_REPO="${GSM_REPO:-$REPO_DEFAULT}"
GSM_REF="${GSM_REF:-main}"
BIN_DIR="$PREFIX/bin"
DESKTOP_DIR="$PREFIX/share/applications"
LAUNCHER="$BIN_DIR/git-save-manager"

say()  { printf '==> %s\n' "$*"; }
warn() { printf 'warning: %s\n' "$*" >&2; }
die()  { printf 'error: %s\n' "$*" >&2; exit 1; }

# --- platform ---------------------------------------------------------------

case "$(uname -s)" in
    Linux)  OS=linux ;;
    Darwin) OS=macos ;;
    *) die "unsupported platform: $(uname -s). Use install.ps1 on Windows; Linux and macOS use this script." ;;
esac
say "Platform: $OS"

# --- prerequisites ----------------------------------------------------------

if ! command -v git >/dev/null 2>&1; then
    if [ "$OS" = macos ]; then
        die "git was not found. Install the Xcode command line tools: xcode-select --install"
    fi
    die "git was not found. Install it with your package manager and re-run."
fi

if ! command -v curl >/dev/null 2>&1 && ! command -v wget >/dev/null 2>&1; then
    die "either curl or wget is required."
fi

# --- system libraries the app needs at runtime ------------------------------
#
# We never run sudo on the user's behalf. We detect what is missing and print the
# exact command to fix it. See the Troubleshooting section of the README.

pkg_hint() {
    # $1: what is missing, named the way apt names it. Only two things are ever checked,
    # so the translation is a small explicit table rather than a package-search.
    if command -v apt-get >/dev/null 2>&1; then
        printf 'sudo apt install -y %s\n' "$1"
    elif command -v dnf >/dev/null 2>&1; then
        case "$1" in
            libxcb-cursor0) printf 'sudo dnf install -y xcb-util-cursor\n' ;;
            *)              printf 'sudo dnf install -y gnome-keyring\n' ;;
        esac
    elif command -v pacman >/dev/null 2>&1; then
        case "$1" in
            libxcb-cursor0) printf 'sudo pacman -S --needed xcb-util-cursor\n' ;;
            *)              printf 'sudo pacman -S --needed gnome-keyring\n' ;;
        esac
    elif command -v zypper >/dev/null 2>&1; then
        printf 'sudo zypper install %s\n' "$1"
    else
        printf 'install the equivalent of: %s\n' "$1"
    fi
}

fix_hint() {
    printf '    fix: %s\n' "$(pkg_hint "$1")" >&2
}

have_lib() {
    if command -v ldconfig >/dev/null 2>&1; then
        ldconfig -p 2>/dev/null | grep -q "$1"
    else
        return 0  # cannot tell; do not cry wolf
    fi
}

say "Checking system dependencies"
missing=0

if [ "$OS" = linux ]; then
    # Qt 6.5+ refuses to start without the X11 cursor library, even on Wayland sessions
    # that fall back to xcb.
    if ! have_lib 'libxcb-cursor\.so\.0'; then
        missing=1
        warn "libxcb-cursor is missing - Qt will fail with 'Could not load the Qt platform plugin \"xcb\"'."
        fix_hint libxcb-cursor0
    fi

    # The GitHub PAT is stored only in the OS keyring, so a Secret Service provider must exist.
    if ! command -v gnome-keyring-daemon >/dev/null 2>&1 \
       && ! command -v kwalletd6 >/dev/null 2>&1 \
       && ! command -v kwalletd5 >/dev/null 2>&1; then
        missing=1
        warn "no Secret Service keyring found - storing the GitHub token will fail."
        fix_hint gnome-keyring
    fi

    if [ "$missing" -ne 0 ]; then
        warn "install the packages above before launching, or the app will not start."
        warn "if the keyring still errors at startup, see the Troubleshooting section of the README:"
        warn "  a default 'Login' keyring with an empty password is required."
    fi
else
    # macOS ships the Cocoa Qt platform plugin and the login Keychain, so there is no
    # equivalent of the xcb and Secret Service problems. Only the Xcode command line
    # tools matter, because that is where git comes from.
    if ! xcode-select -p >/dev/null 2>&1; then
        missing=1
        warn "the Xcode command line tools are not installed - git will not work."
        printf '    fix: xcode-select --install\n' >&2
    fi
fi

if [ "$missing" -eq 0 ]; then
    say "System dependencies look fine"
fi

# --- uv ---------------------------------------------------------------------

ensure_uv() {
    if command -v uv >/dev/null 2>&1; then
        UV="$(command -v uv)"
        say "Found uv at $UV"
        return
    fi
    for candidate in "$HOME/.local/bin/uv" "$HOME/.cargo/bin/uv"; do
        if [ -x "$candidate" ]; then
            UV="$candidate"
            say "Found uv at $UV"
            return
        fi
    done

    say "Installing uv (no uv found on PATH)"
    if command -v curl >/dev/null 2>&1; then
        curl -LsSf https://astral.sh/uv/install.sh | sh
    else
        wget -qO- https://astral.sh/uv/install.sh | sh
    fi

    for candidate in "$HOME/.local/bin/uv" "$HOME/.cargo/bin/uv"; do
        if [ -x "$candidate" ]; then
            UV="$candidate"
            say "Installed uv at $UV"
            return
        fi
    done
    die "uv installation finished but the uv binary was not found."
}
ensure_uv

# --- checkout ---------------------------------------------------------------
#
# All runtime state lives in the gitignored data/ directory inside the checkout, so an
# update must never re-clone over an existing install. Fetch and hard-reset instead:
# data/ is untracked and is left alone.

if [ -d "$GSM_HOME/.git" ]; then
    say "Updating existing install at $GSM_HOME"
    git -C "$GSM_HOME" remote set-url origin "$GSM_REPO"
    git -C "$GSM_HOME" fetch --quiet origin "$GSM_REF"
    git -C "$GSM_HOME" checkout --quiet -B "$GSM_REF" "origin/$GSM_REF"
    git -C "$GSM_HOME" reset --quiet --hard "origin/$GSM_REF"
elif [ -e "$GSM_HOME" ] && [ -n "$(ls -A "$GSM_HOME" 2>/dev/null)" ]; then
    die "$GSM_HOME exists and is not a Git checkout. Move it aside and re-run."
else
    say "Cloning $GSM_REPO into $GSM_HOME"
    mkdir -p "$(dirname "$GSM_HOME")"
    git clone --quiet --branch "$GSM_REF" "$GSM_REPO" "$GSM_HOME"
fi

say "Installed version: $(git -C "$GSM_HOME" rev-parse --short HEAD)"

# --- dependencies -----------------------------------------------------------

say "Syncing Python dependencies with uv"
"$UV" sync --project "$GSM_HOME" --quiet

# --- launcher ---------------------------------------------------------------

say "Writing launcher $LAUNCHER"
mkdir -p "$BIN_DIR"
cat > "$LAUNCHER" <<EOF
#!/usr/bin/env bash
# Generated by the Git Save Manager installer. Re-run install.sh to regenerate.
set -euo pipefail
UV="$UV"
[ -x "\$UV" ] || UV="\$(command -v uv)" || {
    echo "git-save-manager: uv not found. Re-run the installer." >&2
    exit 1
}
exec "\$UV" run --project "$GSM_HOME" --directory "$GSM_HOME" python main.py "\$@"
EOF
chmod +x "$LAUNCHER"

case ":$PATH:" in
    *":$BIN_DIR:"*) ;;
    *)
        warn "$BIN_DIR is not on your PATH. Add this to your shell profile:"
        warn "  export PATH=\"$BIN_DIR:\$PATH\""
        ;;
esac

# --- desktop entry ----------------------------------------------------------

if [ "${GSM_NO_DESKTOP:-0}" != "1" ] && [ "$OS" = macos ]; then
    # A minimal .app bundle so the app is launchable from Finder and Spotlight. The
    # executable is a two-line shim onto the same launcher the terminal uses.
    APP_DIR="${GSM_APP_DIR:-$HOME/Applications}/Git Save Manager.app"
    say "Writing app bundle $APP_DIR"
    mkdir -p "$APP_DIR/Contents/MacOS"
    cat > "$APP_DIR/Contents/Info.plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleName</key><string>Git Save Manager</string>
    <key>CFBundleDisplayName</key><string>Git Save Manager</string>
    <key>CFBundleIdentifier</key><string>com.github.edwardrusli.git-save-manager</string>
    <key>CFBundleExecutable</key><string>git-save-manager</string>
    <key>CFBundlePackageType</key><string>APPL</string>
    <key>CFBundleVersion</key><string>0.1.0</string>
    <key>NSHighResolutionCapable</key><true/>
</dict>
</plist>
EOF
    cat > "$APP_DIR/Contents/MacOS/git-save-manager" <<EOF
#!/usr/bin/env bash
exec "$LAUNCHER" "\$@"
EOF
    chmod +x "$APP_DIR/Contents/MacOS/git-save-manager"

elif [ "${GSM_NO_DESKTOP:-0}" != "1" ]; then
    say "Writing desktop entry $DESKTOP_DIR/git-save-manager.desktop"
    mkdir -p "$DESKTOP_DIR"
    cat > "$DESKTOP_DIR/git-save-manager.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=Git Save Manager
Comment=Version game saves and application settings in a private Git repository
Exec=$LAUNCHER
Terminal=false
Categories=Utility;
StartupNotify=true
EOF
    if command -v update-desktop-database >/dev/null 2>&1; then
        update-desktop-database "$DESKTOP_DIR" >/dev/null 2>&1 || true
    fi
fi

# --- done -------------------------------------------------------------------

say "Done."
say "Launch with: git-save-manager"
say "Update later by re-running the same install command."
