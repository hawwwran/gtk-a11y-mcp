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

| Tool | What it does |
|---|---|
| `list_apps` | Apps currently on the AT-SPI bus |
| `dump_tree(app, max_depth=8)` | Pretty-print an app's widget tree |
| `find_widgets(app, role?, name?)` | Search the tree by role and/or name (substring) |
| `click(app, name, role="push button")` | Invoke the default Action on a widget |
| `get_text(app, role, name)` | Read the text content of a widget |
| `type_text(app, role, name, text)` | Set the contents of an editable widget |
| `screenshot(window_only=true)` | `gnome-screenshot` of active window or full screen |
| `status` | Diagnostic snapshot (read-only): `toolkit-accessibility` value + apps-on-bus count + launch hint |

The server **does not write any system settings**. `status` reads the gsetting purely to surface it; it never sets it.

## Install

```bash
git clone https://github.com/hawwwran/gtk-a11y-mcp.git
cd gtk-a11y-mcp
./scripts/install.sh
```

`scripts/install.sh` is idempotent and does everything end-to-end:

1. **System deps via apt** — `python3-venv`, `python3-pyatspi`, `at-spi2-core`, `gnome-screenshot`. Runs `sudo apt-get install` only if anything is missing. Pass `--skip-apt` to manage them yourself.
2. **Venv** at `~/.local/share/gtk-a11y-mcp/.venv/` with `--system-site-packages` so it can see the apt-installed `python3-pyatspi` (not on PyPI).
3. **Editable install** of the project into the venv. Console script lands at `~/.local/share/gtk-a11y-mcp/.venv/bin/gtk-a11y-mcp`.
4. **AI client registration** — adds the server to Claude Code (`claude mcp add --scope user gtk-a11y -- <path>`) and Codex CLI (`codex mcp add gtk-a11y -- <path>`) when those CLIs are on PATH and a `gtk-a11y` entry doesn't already exist.

Re-running is a no-op when nothing has changed.

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
