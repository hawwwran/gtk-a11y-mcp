"""GTK AT-SPI MCP server.

Tools to drive GTK4/libadwaita apps via the GNOME accessibility bus.

The server NEVER touches `org.gnome.desktop.interface toolkit-accessibility`.
Hot-flipping that key on a live GNOME Wayland session can take gnome-shell
(the compositor) down with it. Per-process AT-SPI export is achieved by
launching the target app with `GTK_A11Y=atspi` instead -- env var wins
over the global gsetting and only affects that one process.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
from typing import Any

from mcp.server.fastmcp import FastMCP, Image

mcp = FastMCP("gtk-a11y")

LAUNCH_HINT = (
    "Launch your app with GTK_A11Y=atspi to expose it on the bus, e.g. "
    "`GTK_A11Y=atspi python3 -m src.main`."
)


def _import_pyatspi():
    try:
        import pyatspi
    except ImportError as e:
        raise RuntimeError(
            "python3-pyatspi missing. Install: sudo apt install python3-pyatspi"
        ) from e
    return pyatspi


def _find_app(name: str):
    pyatspi = _import_pyatspi()
    desktop = pyatspi.Registry.getDesktop(0)
    needle = name.lower()
    for i in range(desktop.childCount):
        try:
            app = desktop.getChildAtIndex(i)
            if app.name and needle in app.name.lower():
                return app
        except Exception:
            continue
    return None


def _walk_tree(node, depth: int, max_depth: int, lines: list[str], indent: int = 0) -> None:
    if depth > max_depth:
        return
    try:
        role = node.getRoleName()
        nm = node.name or ""
    except Exception as e:
        lines.append("  " * indent + f"<error: {e}>")
        return
    line = "  " * indent + role
    if nm:
        line += f" '{nm}'"
    lines.append(line)
    try:
        count = node.childCount
    except Exception:
        return
    for i in range(count):
        try:
            child = node.getChildAtIndex(i)
        except Exception:
            continue
        _walk_tree(child, depth + 1, max_depth, lines, indent + 1)


def _walk_match(
    node,
    role: str | None,
    name: str | None,
    results: list,
    max_results: int = 50,
    path: str = "",
) -> None:
    if len(results) >= max_results:
        return
    try:
        node_role = node.getRoleName()
        node_name = node.name or ""
    except Exception:
        return
    here = f"{path}/{node_role}[{node_name}]" if path else f"{node_role}[{node_name}]"
    role_ok = role is None or role == node_role
    name_ok = name is None or (name.lower() in node_name.lower())
    if role_ok and name_ok:
        results.append({"path": here, "role": node_role, "name": node_name})
    try:
        count = node.childCount
    except Exception:
        return
    for i in range(count):
        try:
            child = node.getChildAtIndex(i)
        except Exception:
            continue
        _walk_match(child, role, name, results, max_results, here)


def _resolve_path(root, target: str):
    found: list = []

    def visit(node, current: str) -> None:
        if found:
            return
        try:
            r = node.getRoleName()
            n = node.name or ""
        except Exception:
            return
        here = f"{current}/{r}[{n}]" if current else f"{r}[{n}]"
        if here == target:
            found.append(node)
            return
        try:
            count = node.childCount
        except Exception:
            return
        for i in range(count):
            try:
                child = node.getChildAtIndex(i)
            except Exception:
                continue
            visit(child, here)
            if found:
                return

    visit(root, "")
    return found[0] if found else None


@mcp.tool()
def list_apps() -> dict[str, Any]:
    """List applications visible on the AT-SPI bus.

    If empty, the target app probably wasn't launched with GTK_A11Y=atspi.
    """
    pyatspi = _import_pyatspi()
    desktop = pyatspi.Registry.getDesktop(0)
    apps: list[dict[str, Any]] = []
    for i in range(desktop.childCount):
        try:
            app = desktop.getChildAtIndex(i)
            apps.append({"name": app.name or "", "child_count": app.childCount})
        except Exception:
            continue
    out: dict[str, Any] = {"apps": apps, "count": len(apps)}
    if not apps:
        out["hint"] = LAUNCH_HINT
    return out


@mcp.tool()
def dump_tree(app_name: str, max_depth: int = 8) -> str:
    """Pretty-print the AT-SPI widget tree for an app (substring match on name)."""
    app = _find_app(app_name)
    if app is None:
        return f"No app matching '{app_name}' on AT-SPI bus. {LAUNCH_HINT}"
    lines: list[str] = []
    _walk_tree(app, 0, max_depth, lines)
    return "\n".join(lines)


@mcp.tool()
def find_widgets(
    app_name: str,
    role: str | None = None,
    name: str | None = None,
) -> list[dict[str, Any]]:
    """Find widgets in an app by role and/or name (substring match on name)."""
    app = _find_app(app_name)
    if app is None:
        return []
    results: list[dict[str, Any]] = []
    _walk_match(app, role, name, results)
    return results


@mcp.tool()
def click(app_name: str, name: str, role: str = "push button") -> str:
    """Invoke the default Action ('click') on a widget identified by role + name."""
    app = _find_app(app_name)
    if app is None:
        return f"No app '{app_name}' on AT-SPI bus. {LAUNCH_HINT}"
    results: list[dict[str, Any]] = []
    _walk_match(app, role, name, results, max_results=2)
    if not results:
        return f"No {role}='{name}' in {app_name}."
    if len(results) > 1:
        return (
            f"Ambiguous: {len(results)} matches for {role}='{name}'. "
            "Narrow with find_widgets() and use exact name."
        )
    node = _resolve_path(app, results[0]["path"])
    if node is None:
        return f"Couldn't resolve {results[0]['path']}."
    try:
        action = node.queryAction()
    except NotImplementedError:
        return f"Widget at {results[0]['path']} has no Action interface."
    try:
        action.doAction(0)
    except Exception as e:
        return f"doAction failed: {e}"
    return f"Clicked: {results[0]['path']}"


@mcp.tool()
def get_text(app_name: str, role: str, name: str) -> str:
    """Read the text content of a widget."""
    app = _find_app(app_name)
    if app is None:
        return f"No app '{app_name}' on AT-SPI bus. {LAUNCH_HINT}"
    results: list[dict[str, Any]] = []
    _walk_match(app, role, name, results, max_results=1)
    if not results:
        return f"No {role}='{name}' in {app_name}."
    node = _resolve_path(app, results[0]["path"])
    if node is None:
        return ""
    try:
        text = node.queryText()
        return text.getText(0, text.characterCount)
    except NotImplementedError:
        return node.name or ""


@mcp.tool()
def type_text(app_name: str, role: str, name: str, text: str) -> str:
    """Set the contents of an editable widget (entry / text field)."""
    app = _find_app(app_name)
    if app is None:
        return f"No app '{app_name}' on AT-SPI bus. {LAUNCH_HINT}"
    results: list[dict[str, Any]] = []
    _walk_match(app, role, name, results, max_results=1)
    if not results:
        return f"No {role}='{name}' in {app_name}."
    node = _resolve_path(app, results[0]["path"])
    if node is None:
        return "Couldn't resolve widget."
    try:
        editable = node.queryEditableText()
    except NotImplementedError:
        return "Widget has no EditableText interface."
    try:
        editable.setTextContents(text)
    except Exception as e:
        return f"setTextContents failed: {e}"
    return f"Set text on {results[0]['path']}."


@mcp.tool()
def screenshot(window_only: bool = True) -> Image:
    """Capture a screenshot via gnome-screenshot.

    Args:
        window_only: if true, capture only the active window; else full screen.
    """
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
        path = f.name
    try:
        cmd = ["gnome-screenshot", "-f", path]
        if window_only:
            cmd.insert(1, "-w")
        subprocess.check_call(cmd)
        with open(path, "rb") as f:
            data = f.read()
        return Image(data=data, format="png")
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


@mcp.tool()
def status() -> dict[str, Any]:
    """Diagnostic snapshot: gsetting value (read-only) + apps on bus.

    Never sets the gsetting -- flipping it on a live Wayland session can
    crash gnome-shell. If apps are missing, launch them with GTK_A11Y=atspi.
    """
    out: dict[str, Any] = {"hint": LAUNCH_HINT}
    try:
        gs = subprocess.check_output(
            ["gsettings", "get", "org.gnome.desktop.interface", "toolkit-accessibility"],
            text=True,
        ).strip()
        out["toolkit_accessibility"] = gs
    except (subprocess.CalledProcessError, FileNotFoundError) as e:
        out["toolkit_accessibility"] = f"<read failed: {e}>"
    try:
        pyatspi = _import_pyatspi()
        desktop = pyatspi.Registry.getDesktop(0)
        out["apps_on_bus"] = desktop.childCount
    except Exception as e:
        out["apps_on_bus"] = f"<error: {e}>"
    return out


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
