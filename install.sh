#!/bin/bash
#
# ship-labels installer for macOS.
#
#   curl -fsSL https://raw.githubusercontent.com/CamioCam/ship-labels/main/install.sh | bash
#
# What it touches:
#   ~/ship-labels        the program, its Python environment, and its output
#   ~/.local/bin/ups     a two-line wrapper so you can type "ups"
#   ~/.zshrc             one line, adding ~/.local/bin to your PATH
#
# What it will NEVER overwrite, on this run or any future one:
#   .env  shipper.json  boxes.json  labels/  previews/  shipments_log.csv
# Those names appear in exactly one place below -- the seeding step, which
# only writes them when they do not already exist. Grep for them and check.
#
# Safe to run again at any time; that is how you upgrade.

set -euo pipefail

REPO="CamioCam/ship-labels"
REF="${SHIP_LABELS_REF:-refs/heads/main}"
APP_DIR="${SHIP_LABELS_DIR:-$HOME/ship-labels}"
BIN_DIR="$HOME/.local/bin"

# Replaced on every run.
CODE_FILES="ups.py requirements.txt README.md FOR-LISA.md .env.example shipper.example.json boxes.example.json"
CODE_DIRS="tests shipments"

PYTHON=""
VPY=""
TMP=""
PATH_NOTE=0

say()  { printf '%s\n' "$*"; }
warn() { printf '\n%s\n' "$*" >&2; }
die()  { printf '\n%s\n\n' "$*" >&2; exit 1; }

# ---------------------------------------------------------------- preflight

reattach_terminal() {
    # Piped to bash, stdin is the script itself. Anything that reads would
    # swallow the rest of this file. Point stdin at the real terminal.
    if [ ! -t 0 ] && [ -r /dev/tty ]; then
        exec </dev/tty
    fi
}

require_macos() {
    [ "$(uname -s)" = "Darwin" ] || die "This installer only works on a Mac. Nothing was changed."
    case "$APP_DIR" in
        *[[:space:]]*) die "Please install somewhere without spaces in the folder name." ;;
    esac
}

find_python() {
    # Must RUN each candidate, not just look for it. On a Mac without the
    # Command Line Tools, /usr/bin/python3 exists and is executable but is a
    # stub that opens a GUI dialog and exits non-zero -- so "does the file
    # exist" tells you nothing. This probe also proves venv support and the
    # 3.8 floor in the same call.
    local candidate
    for candidate in \
        /opt/homebrew/bin/python3 \
        /usr/local/bin/python3 \
        "$(command -v python3 2>/dev/null || true)" \
        /usr/bin/python3
    do
        [ -n "$candidate" ] || continue
        [ -x "$candidate" ] || continue
        if "$candidate" -c 'import sys, venv, ensurepip; sys.exit(0 if sys.version_info >= (3, 8) else 1)' >/dev/null 2>&1; then
            PYTHON="$candidate"
            return 0
        fi
    done

    die "Your Mac does not have a usable Python yet.

  Copy this line into Terminal and press Return:

      xcode-select --install

  A window appears -- click Install and wait for it to finish
  (about ten minutes). Then run this installer again."
}

# ----------------------------------------------------------------- download

download() {
    TMP="$(mktemp -d "${TMPDIR:-/tmp}/ship-labels.XXXXXX")"
    trap 'rm -rf "$TMP"' EXIT INT TERM

    say "Downloading the program..."
    # Trust-on-first-use over TLS. Set SHIP_LABELS_REF to a tag to pin a
    # known version instead of tracking main.
    if ! curl --fail --location --silent --show-error \
              --proto '=https' --tlsv1.2 \
              --retry 3 --retry-delay 2 --connect-timeout 15 --max-time 120 \
              --output "$TMP/src.tar.gz" \
              "https://codeload.github.com/$REPO/tar.gz/$REF"
    then
        die "Could not download the program.

  Check that you are connected to the internet and try again.
  Nothing on your Mac was changed."
    fi

    mkdir -p "$TMP/src"
    if ! /usr/bin/tar -xz -f "$TMP/src.tar.gz" -C "$TMP/src" --strip-components 1; then
        die "The download arrived damaged -- usually a temporary network problem.

  Run this installer again. Nothing on your Mac was changed."
    fi
}

# ------------------------------------------------------------------ install

install_code() {
    mkdir -p "$APP_DIR" "$APP_DIR/labels" "$APP_DIR/previews"
    [ -w "$APP_DIR" ] || die "Cannot write to $APP_DIR. Close anything using it and try again."

    local f d
    for f in $CODE_FILES; do
        [ -e "$TMP/src/$f" ] && cp "$TMP/src/$f" "$APP_DIR/$f"
    done
    for d in $CODE_DIRS; do
        [ -e "$TMP/src/$d" ] || continue
        rm -rf "$APP_DIR/$d.new"
        cp -R "$TMP/src/$d" "$APP_DIR/$d.new"
        rm -rf "$APP_DIR/$d"
        mv "$APP_DIR/$d.new" "$APP_DIR/$d"
    done
    chmod +x "$APP_DIR/ups.py" 2>/dev/null || true
}

seed_config() {
    # The only place this script writes these three names, and only when
    # they are absent. Upgrades therefore never touch your settings.
    if [ ! -e "$APP_DIR/.env" ]; then
        cp "$TMP/src/.env.example" "$APP_DIR/.env"
        chmod 600 "$APP_DIR/.env"
    fi
    [ -e "$APP_DIR/shipper.json" ] || cp "$TMP/src/shipper.example.json" "$APP_DIR/shipper.json"
    [ -e "$APP_DIR/boxes.json" ]   || cp "$TMP/src/boxes.example.json"   "$APP_DIR/boxes.json"
}

build_venv() {
    VPY="$APP_DIR/.venv/bin/python"

    if [ -x "$VPY" ] && "$VPY" -c 'import sys' >/dev/null 2>&1; then
        say "Reusing the existing Python environment."
    else
        say "Setting up Python..."
        rm -rf "$APP_DIR/.venv"
        "$PYTHON" -m venv "$APP_DIR/.venv"
    fi

    # A virtual environment, always. Some Python installs refuse a plain
    # "pip install" outright (PEP 668); others accept it and put the package
    # somewhere the program cannot see. The venv avoids both.
    "$VPY" -m pip install --quiet --disable-pip-version-check --upgrade pip >/dev/null 2>&1 || true

    if ! "$VPY" -m pip install --quiet --disable-pip-version-check "requests>=2.31"; then
        die "Could not download the component that talks to UPS.

  If you are on a company or hotel network it may be blocking pypi.org.
  Try again on a different network."
    fi

    # Pillow only renders GIF/PNG previews, which the label workflow never
    # uses. Installed separately so a build failure cannot take requests
    # down with it.
    "$VPY" -m pip install --quiet --disable-pip-version-check "Pillow>=10.0" >/dev/null 2>&1 \
        || warn "Note: label image previews are unavailable. This does not affect printing."
}

install_command() {
    mkdir -p "$BIN_DIR"
    # A wrapper, not a symlink to ups.py. A symlink would be run through
    # ups.py's own "#!/usr/bin/env python3" line, which finds the system
    # Python -- the one without requests installed.
    cat > "$BIN_DIR/ups" <<WRAPPER
#!/bin/bash
exec "$APP_DIR/.venv/bin/python" "$APP_DIR/ups.py" "\$@"
WRAPPER
    chmod +x "$BIN_DIR/ups"

    local rcfile="$HOME/.zshrc"
    case "${SHELL:-}" in */bash) rcfile="$HOME/.bash_profile" ;; esac
    if ! grep -q 'ship-labels installer' "$rcfile" 2>/dev/null; then
        printf '\n# added by ship-labels installer\nexport PATH="$HOME/.local/bin:$PATH"\n' >> "$rcfile"
        PATH_NOTE=1
    fi
}

# ------------------------------------------------------------------- verify

verify() {
    if ! grep -qE '^UPS_CLIENT_ID=.+' "$APP_DIR/.env" 2>/dev/null; then
        # Expected on a first install. Not a failure.
        cat <<MSG

Installed. One thing left before you can make labels.

  Carter will send you three files: .env, shipper.json and boxes.json.
  Put all three in this folder, replacing what is already there:

      $APP_DIR

  Then run this installer again to check everything works.
MSG
        return 0
    fi

    say ""
    say "Checking your UPS connection..."
    local rc=0
    "$VPY" "$APP_DIR/ups.py" token || rc=$?

    if [ "$rc" -eq 0 ]; then
        cat <<MSG

Everything works.

  To make labels, type "ups ship-csv " then drag your order sheet from
  Finder into the Terminal window and press Return. Start with --dry-run:

      ups ship-csv <drag the file here> --dry-run

  FOR-LISA.md in $APP_DIR has the rest.
MSG
    else
        cat <<MSG

Installed, but UPS did not accept the credentials.

  The lines above beginning "Rating" or "Shipping" say why. Usually it is
  a typo in $APP_DIR/.env, or the UPS app is
  missing its Rating and Shipping products.

  Nothing was charged and no labels were created.
  Fix it and run this installer again -- re-running is always safe.
MSG
    fi
}

main() {
    reattach_terminal
    require_macos
    find_python
    download
    install_code
    seed_config
    build_venv
    install_command
    verify
    if [ "$PATH_NOTE" -eq 1 ]; then
        cat <<'MSG'

  One more thing: close this Terminal window and open a new one before
  typing "ups". The window you are in now has not learned the command yet.
MSG
    fi
}

# Called last on purpose. If the download of this script is cut short, bash
# reaches the end without a definition of main and does nothing at all.
main "$@"
