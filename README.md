# gtk-a11y-mcp

MCP server that lets an AI assistant drive GTK4/libadwaita apps through the GNOME accessibility bus (AT-SPI).

## Why

GUI testing/development of GTK apps from an AI client is hard:

- Keystroke injection (`xdotool` / `ydotool`) is fragile — breaks the moment focus or layout shifts, can't read offscreen text, and doesn't work uniformly across X11/Wayland.
- Screenshot-only loops can't read text the LLM can't see, can't invoke widgets directly, and burn tokens.
- The GNOME accessibility bus already exposes the full widget tree to screen readers; this server just plugs that into MCP — read the tree, click named buttons, type into named entries.

There's one catch on stock GNOME: `org.gnome.desktop.interface toolkit-accessibility` defaults to `false`, so GTK apps don't actually export widgets to AT-SPI until that's flipped. This server flips it on first use and restores the prior value at shutdown — including a watchdog that catches orphaned-on state from a hard kill.

## Tools

| Tool | What it does |
|---|---|
| `list_apps` | Apps visible on the AT-SPI bus |
| `dump_tree(app, max_depth=8)` | Pretty-print an app's widget tree |
| `find_widgets(app, role?, name?)` | Search the tree by role and/or name (substring) |
| `click(app, name, role="push button")` | Invoke the default Action on a widget |
| `get_text(app, role, name)` | Read the text content of a widget |
| `type_text(app, role, name, text)` | Set the contents of an editable widget |
| `screenshot(window_only=true)` | `gnome-screenshot` of active window or full screen |
| `release_a11y` | Disable toolkit-accessibility now (next call re-enables) |
| `status` | Current lifecycle state |

## Lifecycle

```
acquire (first widget call)  →  read current toolkit-accessibility, flip to true if needed,
                                 record prior value + PID in $XDG_STATE_HOME/gtk-a11y-mcp/state.json

release (clean shutdown)     →  restore prior value, delete state file

cleanup_orphans (next start) →  if state file exists and recorded PID is dead,
                                 restore prior value and remove the file
```

Worst case (server killed `-9` *and* nothing relaunches it): `toolkit-accessibility` stays `true`. Cosmetic perf cost; nothing breaks. Flip it back manually with `gsettings set org.gnome.desktop.interface toolkit-accessibility false` if you care.

## Install

System dependencies (Ubuntu / Zorin / Debian):

```bash
sudo apt install python3-pyatspi python3-dogtail at-spi2-core gnome-screenshot
```

Then either install the package:

```bash
git clone https://github.com/<you>/gtk-a11y-mcp.git
cd gtk-a11y-mcp
pip install --user .
```

…or run from a checkout without installing:

```bash
pip install --user mcp
python -m gtk_a11y_mcp
```

`python3-pyatspi` is a system package and is **not** available on PyPI — keep it apt-installed; pip won't help.

## Register with Claude Code

Globally (user-scope):

```bash
claude mcp add --scope user gtk-a11y -- gtk-a11y-mcp
```

Or edit `~/.claude.json` directly:

```json
{
  "mcpServers": {
    "gtk-a11y": {
      "command": "gtk-a11y-mcp"
    }
  }
}
```

If you didn't `pip install` and are running from a checkout:

```json
{
  "mcpServers": {
    "gtk-a11y": {
      "command": "python3",
      "args": ["-m", "gtk_a11y_mcp"],
      "env": {
        "PYTHONPATH": "/path/to/gtk-a11y-mcp/src"
      }
    }
  }
}
```

## Tests

```bash
pip install --user pytest
pytest
```

Unit tests cover lifecycle (acquire / release / orphan cleanup) with `gsettings` mocked so they're hermetic.

## Caveats

- **GNOME-flavored.** AT-SPI works on KDE/sway too, but the `toolkit-accessibility` gsettings key is GNOME-specific. On other DEs the toggle is a no-op; this server still works if AT-SPI is already exposing widgets.
- **Tray icons (StatusIcon / pystray) are not AT-SPI-exposed.** Drive the windows the tray spawns instead, or invoke window subprocesses directly.
- **GTK apps register at startup.** Apps already running when the gate flips need a restart before they appear on the bus.
- `python3-pyatspi` emits `SyntaxWarning` on Python 3.12+ — cosmetic, upstream issue.

## License

MIT — see [LICENSE](LICENSE).
