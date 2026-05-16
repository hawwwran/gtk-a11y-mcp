#!/usr/bin/env bash
# gtk-a11y-mcp uninstaller.
#
# Removes everything install.sh put in place EXCEPT third-party tools:
#   * Deletes the venv at $HOME/.local/share/gtk-a11y-mcp/.venv
#   * Removes the empty parent dir if nothing else lives there
#   * Unregisters the gtk-a11y server from Claude Code (claude mcp remove)
#   * Unregisters the gtk-a11y server from Codex CLI  (codex mcp remove)
#
# It does NOT touch apt packages (python3-pyatspi, at-spi2-core, gnome-
# screenshot, python3-pil, ydotool, python3-venv, unzip). Those may be
# used by other things on your system. Remove them yourself if you want:
#
#   sudo apt-get remove --purge \
#     python3-pyatspi at-spi2-core gnome-screenshot python3-pil ydotool
#
# Flags:
#   -y, --yes    don't prompt; remove everything immediately
#   -n, --dry-run    print what would be removed, don't actually remove
#   -h, --help   show this help

set -euo pipefail

VENV_DIR="${VENV_DIR:-$HOME/.local/share/gtk-a11y-mcp/.venv}"
PARENT_DIR="$(dirname "$VENV_DIR")"
SERVER_NAME="${SERVER_NAME:-gtk-a11y}"

ASSUME_YES=0
DRY_RUN=0

if [[ -t 1 ]]; then
    C_DIM=$'\033[2m'; C_GREEN=$'\033[0;32m'; C_CYAN=$'\033[0;36m'
    C_YELLOW=$'\033[1;33m'; C_RED=$'\033[0;31m'; C_BOLD=$'\033[1m'; NC=$'\033[0m'
else
    C_DIM=""; C_GREEN=""; C_CYAN=""; C_YELLOW=""; C_RED=""; C_BOLD=""; NC=""
fi

step() { printf '\n%s==>%s %s\n' "$C_CYAN" "$NC" "$*"; }
log()  { printf '    %s\n' "$*"; }
ok()   { printf '    %s%s%s\n' "$C_GREEN" "$*" "$NC"; }
warn() { printf '    %s%s%s\n' "$C_YELLOW" "$*" "$NC" >&2; }
die()  { printf '\n%sFAIL:%s %s\n' "$C_RED" "$NC" "$*" >&2; exit 1; }

show_help() {
    sed -n '2,/^$/p' "${BASH_SOURCE[0]}" 2>/dev/null \
        | sed 's/^# \{0,1\}//' \
        || cat <<EOF
gtk-a11y-mcp uninstaller. Flags: -y/--yes, -n/--dry-run, -h.
EOF
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        -y|--yes) ASSUME_YES=1; shift ;;
        -n|--dry-run) DRY_RUN=1; shift ;;
        -h|--help) show_help; exit 0 ;;
        *) die "unknown flag: $1" ;;
    esac
done

run() {
    # Echo + execute (or just echo in --dry-run mode).
    if [[ "$DRY_RUN" -eq 1 ]]; then
        log "${C_DIM}would run:${NC} $*"
    else
        "$@"
    fi
}

# ---- discover what's actually present ---------------------------------------

VENV_PRESENT=0
CLAUDE_REGISTERED=0
CODEX_REGISTERED=0

[[ -d "$VENV_DIR" ]] && VENV_PRESENT=1

if command -v claude >/dev/null 2>&1; then
    claude mcp get "$SERVER_NAME" >/dev/null 2>&1 && CLAUDE_REGISTERED=1
fi
if command -v codex >/dev/null 2>&1; then
    codex mcp get "$SERVER_NAME" >/dev/null 2>&1 && CODEX_REGISTERED=1
fi

# ---- show what we plan to do ------------------------------------------------

printf '\n%s%s gtk-a11y-mcp uninstaller %s\n' "$C_BOLD" "==>" "$NC"
step "What will be removed"

ANY=0
if [[ $VENV_PRESENT -eq 1 ]]; then
    log "venv directory:  $VENV_DIR"
    ANY=1
fi
if [[ $CLAUDE_REGISTERED -eq 1 ]]; then
    log "Claude Code:     mcp registration '$SERVER_NAME' (user scope)"
    ANY=1
fi
if [[ $CODEX_REGISTERED -eq 1 ]]; then
    log "Codex CLI:       mcp registration '$SERVER_NAME'"
    ANY=1
fi
if [[ $ANY -eq 0 ]]; then
    ok "nothing to remove; system is already clean"
    exit 0
fi

log ""
log "Will keep (third-party tools installed via apt):"
log "  python3-pyatspi, at-spi2-core, gnome-screenshot, python3-pil,"
log "  ydotool, python3-venv, unzip"
log "Remove them yourself with apt-get if you want."

if [[ "$ASSUME_YES" -ne 1 && "$DRY_RUN" -ne 1 ]]; then
    printf '\n    Proceed? [y/N] '
    read -r reply
    case "$reply" in
        y|Y|yes|YES) : ;;
        *) log "aborted"; exit 0 ;;
    esac
fi

# ---- do it ------------------------------------------------------------------

if [[ $CLAUDE_REGISTERED -eq 1 ]]; then
    step "Unregistering from Claude Code"
    # `claude mcp remove --scope user` matches what install.sh used.
    if run claude mcp remove --scope user "$SERVER_NAME"; then
        ok "removed"
    else
        warn "Claude removal failed (continuing)"
    fi
fi

if [[ $CODEX_REGISTERED -eq 1 ]]; then
    step "Unregistering from Codex CLI"
    if run codex mcp remove "$SERVER_NAME"; then
        ok "removed"
    else
        warn "Codex removal failed (continuing)"
    fi
fi

if [[ $VENV_PRESENT -eq 1 ]]; then
    step "Removing venv"
    run rm -rf "$VENV_DIR"
    ok "removed: $VENV_DIR"
    # Tidy: drop the parent dir if it's now empty (created solely by install.sh).
    if [[ "$DRY_RUN" -ne 1 && -d "$PARENT_DIR" ]] && ! ls -A "$PARENT_DIR" >/dev/null 2>&1; then
        rmdir "$PARENT_DIR" 2>/dev/null && ok "cleaned empty parent: $PARENT_DIR" || true
    fi
fi

step "Done"
log "gtk-a11y-mcp has been removed."
log "Third-party apt packages were left in place (see help for cleanup)."
echo
