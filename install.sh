#!/usr/bin/env bash
# gtk-a11y-mcp installer.
#
# Auto-detects mode:
#   * LOCAL    -- run from a checkout of the gtk-a11y-mcp repo (./install.sh).
#                 Installs from that checkout.
#   * REMOTE   -- piped through curl (curl ... | bash) or copied to a fresh
#                 machine. Downloads the latest release zip from GitHub and
#                 installs from that. Falls back to `git clone main` if no
#                 release zip is found.
#
# Both modes do the same thing afterward:
#   1. apt deps (python3-venv python3-pyatspi at-spi2-core gnome-screenshot
#      python3-pil ydotool unzip) -- only the missing ones, only if any are
#      missing. python3-pil powers screenshot_widget cropping; ydotool is
#      the Wayland keyboard-injection fallback for press_keys.
#   2. venv at $HOME/.local/share/gtk-a11y-mcp/.venv with --system-site-packages
#   3. editable pip install of the project into the venv
#   4. Register with Claude Code (--scope user) and Codex CLI when those CLIs
#      are on PATH and a "gtk-a11y" entry doesn't already exist.
#
# Idempotent: re-running is a no-op when nothing has changed.
#
# Flags:
#   --skip-apt          don't touch apt; assume system deps are present
#   --version vX.Y.Z    pin remote-mode fetch to this release tag
#   -h, --help          show this help and exit

set -euo pipefail

# ---- constants --------------------------------------------------------------

GH_REPO="hawwwran/gtk-a11y-mcp"
ASSET_NAME="gtk-a11y-mcp.zip"
VENV_DIR="${VENV_DIR:-$HOME/.local/share/gtk-a11y-mcp/.venv}"
SERVER_NAME="${SERVER_NAME:-gtk-a11y}"
APT_PACKAGES=(python3-venv python3-pyatspi at-spi2-core gnome-screenshot python3-pil ydotool unzip)

SKIP_APT=0
VERSION_PIN=""
SOURCE_DIR=""
TEMP_DIR=""
EDITABLE_INSTALL=0  # 1 only when SOURCE_DIR is a persistent local checkout

# ---- ansi colours (only when stdout is a tty) -------------------------------

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

cleanup() {
    # Always return 0 -- this runs from the EXIT trap and its exit status
    # otherwise leaks back as the script's exit code (e.g. when TEMP_DIR is
    # empty in LOCAL mode, the test `[[ -n "" ]]` evaluates to false).
    [[ -n "$TEMP_DIR" && -d "$TEMP_DIR" ]] && rm -rf "$TEMP_DIR"
    return 0
}
trap cleanup EXIT

# ---- arg parsing ------------------------------------------------------------

show_help() {
    sed -n '2,/^$/p' "${BASH_SOURCE[0]}" 2>/dev/null \
        | sed 's/^# \{0,1\}//' \
        || cat <<EOF
gtk-a11y-mcp installer. Flags: --skip-apt, --version vX.Y.Z, -h.
EOF
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --skip-apt) SKIP_APT=1; shift ;;
        --version)  VERSION_PIN="$2"; shift 2 ;;
        -h|--help)  show_help; exit 0 ;;
        *) die "unknown flag: $1" ;;
    esac
done

# ---- mode detection ---------------------------------------------------------

detect_local_source() {
    # Returns the repo root if the script is sitting inside one; empty otherwise.
    local script="${BASH_SOURCE[0]:-}"
    [[ -n "$script" && -f "$script" ]] || { echo ""; return; }
    local d
    d="$(cd "$(dirname "$script")" 2>/dev/null && pwd)" || { echo ""; return; }
    while [[ "$d" != "/" && -n "$d" ]]; do
        if [[ -f "$d/pyproject.toml" ]] \
            && grep -q '^name = "gtk-a11y-mcp"' "$d/pyproject.toml" 2>/dev/null; then
            echo "$d"
            return
        fi
        d="$(dirname "$d")"
    done
    echo ""
}

fetch_remote_source() {
    # Populates SOURCE_DIR. Tries the release zip first, falls back to git clone.
    TEMP_DIR=$(mktemp -d -t gtk-a11y-mcp-install.XXXXXXXX)

    local url
    if [[ -n "$VERSION_PIN" ]]; then
        local v="${VERSION_PIN#v}"
        url="https://github.com/$GH_REPO/releases/download/v$v/$ASSET_NAME"
    else
        url="https://github.com/$GH_REPO/releases/latest/download/$ASSET_NAME"
    fi

    log "trying release zip: $url"
    if curl -fsSL "$url" -o "$TEMP_DIR/release.zip" 2>/dev/null; then
        if ! command -v unzip >/dev/null 2>&1; then
            log "unzip missing -- installing first"
            sudo apt-get update -qq
            sudo apt-get install -y -qq unzip
        fi
        unzip -q "$TEMP_DIR/release.zip" -d "$TEMP_DIR/extracted"
        local root
        root=$(find "$TEMP_DIR/extracted" -maxdepth 2 -name pyproject.toml -type f | head -n1)
        [[ -n "$root" ]] || die "release zip did not contain pyproject.toml"
        SOURCE_DIR="$(dirname "$root")"
        ok "fetched release zip"
        return
    fi

    warn "release zip not found -- falling back to git clone of main"
    command -v git >/dev/null 2>&1 || die "git not available; can't fall back"
    git clone --quiet --depth 1 "https://github.com/$GH_REPO.git" "$TEMP_DIR/clone"
    SOURCE_DIR="$TEMP_DIR/clone"
    ok "cloned main from $GH_REPO"
}

# ---- apt deps ---------------------------------------------------------------

ensure_apt_deps() {
    if [[ "$SKIP_APT" -eq 1 ]]; then
        log "[apt] --skip-apt: skipped"
        return
    fi
    if ! command -v apt-get >/dev/null 2>&1; then
        cat >&2 <<EOF
This installer assumes apt (Debian / Ubuntu / Zorin). On other distros, install
the equivalents manually and re-run with --skip-apt:
  python3-venv, python3-pyatspi, at-spi2-core, gnome-screenshot,
  python3-pil, ydotool, unzip
EOF
        exit 1
    fi
    local missing=()
    for pkg in "${APT_PACKAGES[@]}"; do
        dpkg -s "$pkg" >/dev/null 2>&1 || missing+=("$pkg")
    done
    if [[ ${#missing[@]} -eq 0 ]]; then
        ok "all dependencies installed"
        return
    fi
    log "missing: ${missing[*]}"
    log "running: sudo apt-get update && sudo apt-get install -y ${missing[*]}"
    sudo apt-get update
    sudo apt-get install -y "${missing[@]}"
    ok "installed"
}

# ---- venv + pip install -----------------------------------------------------

ensure_venv_install() {
    mkdir -p "$(dirname "$VENV_DIR")"
    if [[ ! -x "$VENV_DIR/bin/python" ]]; then
        rm -rf "$VENV_DIR"
        log "creating venv (--system-site-packages)"
        python3 -m venv --system-site-packages "$VENV_DIR"
    else
        log "reusing existing venv"
    fi
    "$VENV_DIR/bin/pip" install --quiet --upgrade pip
    # Always uninstall first so a prior install (editable from a different path,
    # or non-editable from a previous release) doesn't shadow this one.
    "$VENV_DIR/bin/pip" uninstall --quiet -y gtk-a11y-mcp >/dev/null 2>&1 || true
    if [[ "$EDITABLE_INSTALL" -eq 1 ]]; then
        log "pip install -e (editable from local checkout)"
        "$VENV_DIR/bin/pip" install --quiet -e "$SOURCE_DIR"
    else
        log "pip install (non-editable; source is ephemeral)"
        "$VENV_DIR/bin/pip" install --quiet "$SOURCE_DIR"
    fi

    CMD_PATH="$VENV_DIR/bin/gtk-a11y-mcp"
    [[ -x "$CMD_PATH" ]] || die "console script $CMD_PATH not produced"
    "$VENV_DIR/bin/python" -c "from gtk_a11y_mcp import server" >/dev/null 2>&1 \
        || die "server module did not import"
    ok "venv: $VENV_DIR"
    ok "console script: $CMD_PATH"
}

# ---- AI client registration -------------------------------------------------

register_with() {
    # $1 = CLI name, $2 = extra `mcp add` args (may be empty)
    local cli="$1"
    local extra_args="${2:-}"

    if ! command -v "$cli" >/dev/null 2>&1; then
        log "[$cli] not on PATH -- skipping"
        return 0
    fi
    if "$cli" mcp get "$SERVER_NAME" >/dev/null 2>&1; then
        log "[$cli] '$SERVER_NAME' already registered -- skipping"
        return 0
    fi
    # shellcheck disable=SC2086
    if "$cli" mcp add $extra_args "$SERVER_NAME" -- "$CMD_PATH"; then
        ok "[$cli] '$SERVER_NAME' registered"
    else
        warn "[$cli] registration failed (continuing)"
    fi
}

# =============================================================================

printf '\n%s%s gtk-a11y-mcp installer %s\n' "$C_BOLD" "==>" "$NC"

step "Detecting source"
SOURCE_DIR=$(detect_local_source)
if [[ -n "$SOURCE_DIR" ]]; then
    EDITABLE_INSTALL=1
    log "mode: ${C_GREEN}LOCAL${NC} (running from a checkout)"
    ok "source: $SOURCE_DIR"
else
    EDITABLE_INSTALL=0
    log "mode: ${C_GREEN}REMOTE${NC} (running standalone)"
    fetch_remote_source
    log "source: $SOURCE_DIR (ephemeral; will install non-editable)"
fi

step "System dependencies (apt)"
ensure_apt_deps

step "Venv + editable pip install"
ensure_venv_install

step "AI client registration"
register_with claude "--scope user"
register_with codex ""

step "Verifying runtime tools"
# Each check warns rather than dies so a partial install still completes.
# Severity = (whether the server can do anything useful without it).
verify_runtime() {
    local fatal=0
    if "$VENV_DIR/bin/python" -c "import pyatspi" >/dev/null 2>&1; then
        ok "python3-pyatspi: OK (AT-SPI bridge)"
    else
        warn "python3-pyatspi MISSING -- server can't talk to AT-SPI at all"
        fatal=1
    fi
    if command -v gnome-screenshot >/dev/null 2>&1; then
        ok "gnome-screenshot: OK (screenshot / screenshot_widget)"
    else
        warn "gnome-screenshot missing -- screenshot tools will fail"
    fi
    if "$VENV_DIR/bin/python" -c "import PIL" >/dev/null 2>&1; then
        ok "Pillow: OK (screenshot_widget cropping)"
    else
        warn "Pillow (python3-pil) missing -- screenshot_widget will fail"
    fi
    if command -v ydotool >/dev/null 2>&1; then
        ok "ydotool: OK (press_keys Wayland fallback)"
        if ! pgrep -x ydotoold >/dev/null 2>&1; then
            warn "ydotoold daemon not running -- press_keys ydotool fallback will fail"
            log "  start it (one-off):  sudo ydotoold &"
            log "  or enable systemd unit per your distro's ydotool docs"
        fi
    else
        warn "ydotool missing -- press_keys has no Wayland fallback"
    fi
    if command -v gdbus >/dev/null 2>&1; then
        ok "gdbus: OK (screen_status lock detection)"
    else
        warn "gdbus missing -- screen_status will report locked=null"
    fi
    [[ $fatal -eq 0 ]] || die "core deps missing; install can't proceed"
}
verify_runtime

step "Done"
log "Launch any GTK4 app with the AT-SPI bridge to expose it to the MCP:"
log "    ${C_BOLD}GTK_A11Y=atspi <command-to-run-your-app>${NC}"
log "Then talk to the gtk-a11y MCP from Claude Code or Codex CLI."
echo
