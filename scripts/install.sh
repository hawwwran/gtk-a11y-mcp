#!/usr/bin/env bash
# Install gtk-a11y-mcp into a venv at $HOME/.local/share/gtk-a11y-mcp/.venv
# and register it with any AI clients we find on PATH (Claude Code, Codex CLI).
#
# Idempotent: re-running is safe. The venv is reused when valid; AI client
# registrations are skipped when an entry named "gtk-a11y" already exists.
#
# System deps (apt; install once outside this script):
#   sudo apt install python3-venv python3-pyatspi at-spi2-core gnome-screenshot
#
# The venv inherits system site-packages so it can see python3-pyatspi
# (apt-only, not on PyPI).

set -euo pipefail

HERE="$(cd "$(dirname "$0")/.." && pwd)"
VENV_DIR="${VENV_DIR:-$HOME/.local/share/gtk-a11y-mcp/.venv}"
SERVER_NAME="${SERVER_NAME:-gtk-a11y}"

echo "Project root : $HERE"
echo "Venv         : $VENV_DIR"
echo "Server name  : $SERVER_NAME"
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
