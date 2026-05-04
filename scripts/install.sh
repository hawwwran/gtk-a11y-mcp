#!/usr/bin/env bash
# Install gtk-a11y-mcp into a venv at $HOME/.local/share/gtk-a11y-mcp/.venv.
# The venv inherits system site-packages (--system-site-packages) so it can
# see python3-pyatspi (apt-only, not on PyPI).
#
# This script does NOT touch apt. Install system deps first:
#   sudo apt install python3-venv python3-pyatspi at-spi2-core gnome-screenshot
#
# After this script, register with Claude Code:
#   claude mcp add --scope user gtk-a11y -- "$HOME/.local/share/gtk-a11y-mcp/.venv/bin/gtk-a11y-mcp"

set -euo pipefail

HERE="$(cd "$(dirname "$0")/.." && pwd)"
VENV_DIR="${VENV_DIR:-$HOME/.local/share/gtk-a11y-mcp/.venv}"

echo "Project root : $HERE"
echo "Venv         : $VENV_DIR"

mkdir -p "$(dirname "$VENV_DIR")"

if [[ ! -x "$VENV_DIR/bin/python" ]]; then
    rm -rf "$VENV_DIR"
    python3 -m venv --system-site-packages "$VENV_DIR"
fi

"$VENV_DIR/bin/pip" install --quiet --upgrade pip
"$VENV_DIR/bin/pip" install --quiet -e "$HERE"

echo
echo "Installed. Console script:"
echo "  $VENV_DIR/bin/gtk-a11y-mcp"
echo
echo "Smoke check:"
"$VENV_DIR/bin/python" -c "from gtk_a11y_mcp import server; print('  imports OK')"
