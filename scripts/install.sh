#!/usr/bin/env bash
# Install gtk-a11y-mcp into a venv at $HOME/.local/share/gtk-a11y-mcp/.venv
# and register it with any AI clients we find on PATH (Claude Code, Codex CLI).
#
# Idempotent: re-running is safe.
# - Apt packages already installed are skipped.
# - The venv is reused when valid.
# - AI client registrations are skipped when an entry named "gtk-a11y" exists.
#
# The venv inherits system site-packages so it can see python3-pyatspi
# (apt-only, not on PyPI).
#
# Flags:
#   --skip-apt   don't touch apt; assume system deps are already there
#   -h, --help   print this help and exit

set -euo pipefail

usage() {
    sed -n '2,/^$/p' "$0" | sed 's/^# \{0,1\}//'
    exit 0
}

SKIP_APT=0
for arg in "$@"; do
    case "$arg" in
        --skip-apt) SKIP_APT=1 ;;
        -h|--help) usage ;;
        *) echo "Unknown flag: $arg" >&2; exit 2 ;;
    esac
done

HERE="$(cd "$(dirname "$0")/.." && pwd)"
VENV_DIR="${VENV_DIR:-$HOME/.local/share/gtk-a11y-mcp/.venv}"
SERVER_NAME="${SERVER_NAME:-gtk-a11y}"
APT_PACKAGES=(python3-venv python3-pyatspi at-spi2-core gnome-screenshot)

echo "Project root : $HERE"
echo "Venv         : $VENV_DIR"
echo "Server name  : $SERVER_NAME"
echo

# --- apt system dependencies ------------------------------------------------

ensure_apt_deps() {
    if [[ "$SKIP_APT" -eq 1 ]]; then
        echo "  [apt] --skip-apt -- skipping system dependency check"
        return 0
    fi

    if ! command -v apt-get >/dev/null 2>&1; then
        cat >&2 <<EOF
This installer assumes apt (Debian / Ubuntu / Zorin). On other distros,
install equivalents manually and re-run with --skip-apt:
  python3-venv         (stdlib venv module)
  python3-pyatspi      (AT-SPI Python bindings)
  at-spi2-core         (AT-SPI bus / registry)
  gnome-screenshot     (gnome-screenshot CLI)
EOF
        exit 1
    fi

    local missing=()
    for pkg in "${APT_PACKAGES[@]}"; do
        if ! dpkg -s "$pkg" >/dev/null 2>&1; then
            missing+=("$pkg")
        fi
    done

    if [[ ${#missing[@]} -eq 0 ]]; then
        echo "  [apt] all dependencies already installed"
        return 0
    fi

    echo "  [apt] missing: ${missing[*]}"
    echo "  [apt] sudo apt-get update && sudo apt-get install -y ${missing[*]}"
    sudo apt-get update
    sudo apt-get install -y "${missing[@]}"
}

echo "Checking system dependencies:"
ensure_apt_deps
echo

# --- venv + editable install -------------------------------------------------

mkdir -p "$(dirname "$VENV_DIR")"

if [[ ! -x "$VENV_DIR/bin/python" ]]; then
    rm -rf "$VENV_DIR"
    python3 -m venv --system-site-packages "$VENV_DIR"
fi

"$VENV_DIR/bin/pip" install --quiet --upgrade pip
"$VENV_DIR/bin/pip" install --quiet -e "$HERE"

CMD_PATH="$VENV_DIR/bin/gtk-a11y-mcp"
if [[ ! -x "$CMD_PATH" ]]; then
    echo "FAIL: console script $CMD_PATH not produced." >&2
    exit 1
fi

"$VENV_DIR/bin/python" -c "from gtk_a11y_mcp import server" \
    || { echo "FAIL: server module did not import." >&2; exit 1; }

echo "Installed. Console script: $CMD_PATH"
echo

# --- AI client registration --------------------------------------------------

register_with() {
    # $1 = CLI name on PATH (claude | codex)
    # $2 = extra `mcp add` args before `-- cmd` (e.g. --scope user). May be empty.
    local cli="$1"
    local extra_args="${2:-}"

    if ! command -v "$cli" >/dev/null 2>&1; then
        echo "  [$cli] not on PATH -- skipping"
        return 0
    fi

    if "$cli" mcp get "$SERVER_NAME" >/dev/null 2>&1; then
        echo "  [$cli] '$SERVER_NAME' already registered -- skipping"
        return 0
    fi

    # shellcheck disable=SC2086  # word splitting on $extra_args is intentional
    if "$cli" mcp add $extra_args "$SERVER_NAME" -- "$CMD_PATH"; then
        echo "  [$cli] '$SERVER_NAME' registered -> $CMD_PATH"
    else
        echo "  [$cli] registration failed (continuing)" >&2
    fi
}

echo "Registering with AI clients (idempotent):"
register_with claude "--scope user"
register_with codex ""
echo

echo "Done. To use:"
echo "  Launch your GTK app with the AT-SPI bridge:"
echo "    GTK_A11Y=atspi <command-to-run-your-app>"
echo "  Then talk to the MCP from your AI client."
