# gtk-a11y-mcp

MCP server that lets an AI assistant drive GTK4/libadwaita apps through the GNOME accessibility bus (AT-SPI).

## Why

GUI testing/development of GTK apps from an AI client is hard:

- Keystroke injection (`xdotool` / `ydotool`) is fragile — breaks the moment focus or layout shifts, can't read offscreen text, and doesn't work uniformly across X11/Wayland.
- Screenshot-only loops can't read text the LLM can't see, can't invoke widgets directly, and burn tokens.
- The GNOME accessibility bus already exposes the full widget tree to screen readers; this server just plugs that into MCP — read the tree, click named buttons, type into named entries.

## How AT-SPI export is enabled (the safe way)

Stock GNOME ships with `org.gnome.desktop.interface toolkit-accessibility = false`, so GTK apps don't expose their widget tree to AT-SPI by default.

**This server never touches that gsetting.** Hot-flipping it on a live GNOME Wayland session can take `gnome-shell` (the compositor) down with it — the entire desktop session ends and unsaved work is lost.

Instead, expose **only the app you're testing**, per-process, by setting `GTK_A11Y=atspi` on its launch:

```bash
GTK_A11Y=atspi python3 -m src.main             # your app
GTK_A11Y=atspi gnome-calculator                 # any GTK4 app
```

That env var wins over the global gsetting. Only that one process appears on the bus. Everything else in your session is unaffected. Quit the app and it's gone from the bus.

If you really want export *globally* (because you're going to be testing all the time): set the gsetting once, **then log out and back in** so `gnome-shell` starts with it. Don't toggle it inside a running session.

## Tools

### Read

| Tool | What it does |
|---|---|
| `list_apps` | Apps currently on the AT-SPI bus |
| `dump_tree(app, max_depth=8)` | Pretty-print an app's widget tree. Lines show `role 'name' #accessible-id [states]`; the curated state set surfaces `focused / checked / selected / expanded / pressed` and a pseudo-state `disabled` when `sensitive` is absent |
| `find_widgets(app, role?, name?, state?, accessible_id?, description?)` | Search the tree. `state` is a single AT-SPI state name (prefix with `!` to require absence, e.g. `state="!sensitive"` for disabled). `accessible_id` is exact-match (locale-independent). Each result carries the node's full `states`, `attributes`, `description` |
| `get_text(app, role, name)` | Read the text content of a widget |
| `get_attributes(app, role, name)` | Full AT-SPI Attributes dict (level, container, id, app hints) |
| `get_value(app, role, name)` | Read AT-SPI Value: `{value, minimum, maximum, minimum_increment}` (sliders, spin buttons, progress) |
| `audit(app, rules?, max_issues=200)` | Run accessibility rules over the tree: empty-name, name-equals-role, name-only-whitespace, clickable-without-action, actionable-not-focusable, editable-without-label, image-without-description, duplicate-accessible-id |

### Interact

| Tool | What it does |
|---|---|
| `click(app, name, role="push button")` | Invoke the default Action on a widget |
| `click_by_path(app, path)` | Click by exact tree path (bypasses ambiguity errors) |
| `type_text(app, role, name, text)` | Set the contents of an editable widget |
| `set_value(app, role, name, value)` | Set a slider / spin button / progress to a numeric value |
| `select_item(app, parent_path, item_name)` | Select a child by name within a Selection-capable container (combo boxes, list views) |
| `set_checked(app, role, name, checked)` | Set a check box / toggle to a state; no-op when already correct |
| `focus(app, role, name)` | Move keyboard focus to a widget (`Component.grabFocus`) |
| `press_keys(keys)` | Synthesize key events at the session level. Spec: `"Tab"`, `"Ctrl+S"`, `"Ctrl+Shift+T"`, sequence `"Ctrl+a Delete Tab"`. Tries AT-SPI first, falls back to `ydotool` (needs `ydotoold` running) |

### Lifecycle

| Tool | What it does |
|---|---|
| `launch_app(command, args?, wait_seconds?, app_name_match?, extra_env?)` | Spawn a subprocess with `GTK_A11Y=atspi` pre-set; optionally poll the bus until the named app appears |
| `activate_app(app_name)` | Bring an app's main window to the front via AT-SPI `grabFocus` (best-effort; Wayland compositors may refuse cross-process activation) |
| `wait_for_app(app, timeout_s=10)` | Block until an app appears on the bus |
| `wait_for_widget(app, role?, name?, timeout_s=10)` | Block until a widget appears |
| `wait_for_state(app, role, name, state, present=true, timeout_s=10)` | Block until a widget's state matches (e.g. wait for `sensitive` to become true after async validation) |

### Capture

| Tool | What it does |
|---|---|
| `screenshot(window_only=true)` | `gnome-screenshot` of active window or full screen |
| `screenshot_widget(app, role, name, padding_px=4, activate_first=true)` | Single widget cropped to AT-SPI extents (10–100× cheaper in tokens than a full window). By default tries to raise the widget's window before capturing so an occluded window doesn't produce a screenshot of what's behind it — best-effort on Wayland |

### Diagnostic

| Tool | What it does |
|---|---|
| `status` | `toolkit-accessibility` value + apps-on-bus count + launch hint |
| `screen_status` | Pre-flight for `screenshot`: session type, lock state, active app |

### Batch

| Tool | What it does |
|---|---|
| `script(steps)` | Run a sequence of tool calls in one MCP round-trip. Each step: `{"tool": "...", "args": {...}, "wait_ms_after": 200, "continue_on_error": false}`. Stops at first failure unless flagged otherwise. `screenshot*` and `audit` are excluded — call them individually |

The server **does not write any system settings**. `status` reads the gsetting purely to surface it; it never sets it. Every AT-SPI-touching tool retries once on a DBus bus error (registryd restart, session reset) before propagating the failure.

## Install

One-liner (auto-fetches the latest release):

```bash
curl -fsSL https://raw.githubusercontent.com/hawwwran/gtk-a11y-mcp/main/install.sh | bash
```

Or clone the repo and run the same script — it auto-detects whether it's running inside a checkout (dev mode, installs editable from your working tree) or standalone (downloads the latest release zip):

```bash
git clone https://github.com/hawwwran/gtk-a11y-mcp.git
cd gtk-a11y-mcp
./install.sh
```

`install.sh` is idempotent and does everything end-to-end:

1. **System deps via apt** — `python3-venv`, `python3-pyatspi`, `at-spi2-core`, `gnome-screenshot`, `python3-pil` (Pillow, powers `screenshot_widget` cropping), `ydotool` (Wayland keyboard-injection fallback for `press_keys`), `unzip`. Runs `sudo apt-get install` only if anything is missing. Pass `--skip-apt` to manage them yourself.
2. **Source detection** — local clone or fetched release zip (or `git clone` of `main` as a fallback if no release zip is found). Pin a specific release with `--version vX.Y.Z`.
3. **Venv** at `~/.local/share/gtk-a11y-mcp/.venv/` with `--system-site-packages` so it can see the apt-installed `python3-pyatspi` (not on PyPI).
4. **Editable install** of the project into the venv. Console script lands at `~/.local/share/gtk-a11y-mcp/.venv/bin/gtk-a11y-mcp`.
5. **AI client registration** — adds the server to Claude Code (`claude mcp add --scope user gtk-a11y -- <path>`) and Codex CLI (`codex mcp add gtk-a11y -- <path>`) when those CLIs are on PATH and a `gtk-a11y` entry doesn't already exist.
6. **Runtime verification** — at the end of the install, every tool the server can call (`pyatspi`, `gnome-screenshot`, Pillow, `ydotool` + `ydotoold` daemon, `gdbus`) is probed. Each missing piece is warned about with a hint about which MCP tools will fail.

Re-running is a no-op when nothing has changed.

`ydotool` needs the `ydotoold` daemon running for `press_keys` to work as a Wayland fallback. Most distros ship a user-mode systemd unit:

```bash
systemctl --user enable --now ydotoold
```

## Uninstall

```bash
./uninstall.sh
# or, after install via curl:
curl -fsSL https://raw.githubusercontent.com/hawwwran/gtk-a11y-mcp/main/uninstall.sh | bash
```

Removes the venv at `~/.local/share/gtk-a11y-mcp/` and unregisters the `gtk-a11y` server from Claude Code and Codex CLI. **Does not touch apt packages** (`python3-pyatspi`, `at-spi2-core`, `gnome-screenshot`, `python3-pil`, `ydotool`, …) — those may be used by other things on your system. Remove them manually if you want:

```bash
sudo apt-get remove --purge python3-pyatspi at-spi2-core gnome-screenshot python3-pil ydotool
```

Flags: `-y/--yes` (skip the confirmation prompt), `-n/--dry-run` (print what would happen without doing it).

## Manual registration (if you skipped step 4)

For Claude Code:

```bash
claude mcp add --scope user gtk-a11y -- "$HOME/.local/share/gtk-a11y-mcp/.venv/bin/gtk-a11y-mcp"
```

For Codex CLI:

```bash
codex mcp add gtk-a11y -- "$HOME/.local/share/gtk-a11y-mcp/.venv/bin/gtk-a11y-mcp"
```

Or edit `~/.claude.json` directly:

```json
{
  "mcpServers": {
    "gtk-a11y": {
      "command": "/home/<you>/.local/share/gtk-a11y-mcp/.venv/bin/gtk-a11y-mcp"
    }
  }
}
```

## Tests

```bash
~/.local/share/gtk-a11y-mcp/.venv/bin/pip install pytest
cd <repo>
~/.local/share/gtk-a11y-mcp/.venv/bin/pytest
```

Tests use mock AT-SPI nodes so they don't require a live bus.

## Caveats

- **GNOME-flavored.** AT-SPI works on KDE/sway too, but `GTK_A11Y=atspi` is GTK-specific (Qt apps need their own AT-SPI bridge). The server itself is desktop-environment-agnostic — anything on the AT-SPI bus is fair game.
- **Tray icons (StatusIcon / pystray) are not AT-SPI-exposed.** Drive the windows the tray spawns instead, or invoke window subprocesses directly.
- **Apps register with AT-SPI at startup.** An already-running app can't be retroactively added to the bus — relaunch it with `GTK_A11Y=atspi`.
- `python3-pyatspi` emits `SyntaxWarning` on Python 3.12+ — cosmetic, upstream issue.

## License

MIT — see [LICENSE](LICENSE).
