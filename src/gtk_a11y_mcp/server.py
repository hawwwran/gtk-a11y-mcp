"""GTK AT-SPI MCP server.

Tools to drive GTK4/libadwaita apps via the GNOME accessibility bus.

The server NEVER touches `org.gnome.desktop.interface toolkit-accessibility`.
Hot-flipping that key on a live GNOME Wayland session can take gnome-shell
(the compositor) down with it. Per-process AT-SPI export is achieved by
launching the target app with `GTK_A11Y=atspi` instead -- env var wins
over the global gsetting and only affects that one process.
"""

from __future__ import annotations

import functools
import io
import os
import subprocess
import sys
import tempfile
import time
from typing import Any, Callable

from mcp.server.fastmcp import FastMCP, Image

mcp = FastMCP("gtk-a11y")

LAUNCH_HINT = (
    "Launch your app with GTK_A11Y=atspi to expose it on the bus, e.g. "
    "`GTK_A11Y=atspi python3 -m src.main`."
)


def _import_pyatspi():
    """Import and return the pyatspi module.

    IMPORTANT: every consumer must call this helper and resolve attributes
    *off the returned module*, never via `from pyatspi import X` at module
    top -- `_reconnect_bus()` may `importlib.reload(pyatspi)` to recover
    from a bus restart, and any cached top-level binding would still point
    at the dead pre-reload module. Resolve lazily, on each call.
    """
    try:
        import pyatspi
    except ImportError as e:
        raise RuntimeError(
            "python3-pyatspi missing. Install: sudo apt install python3-pyatspi"
        ) from e
    return pyatspi


# ---------------------------------------------------------------------------
# Bus reconnect resilience
#
# AT-SPI bus restarts are rare but happen (registryd crash, session restart,
# long-lived MCP server outliving its first login). One DBusException
# currently propagates straight to the MCP client and stays unrecoverable
# for the rest of the session. _with_bus_retry catches DBus-flavored errors
# once, forces pyatspi to reconnect, and re-invokes the wrapped tool. Any
# other exception propagates immediately.
# ---------------------------------------------------------------------------

def _is_dbus_exception(exc: BaseException) -> bool:
    """Best-effort detection of bus-failure exceptions.

    We avoid hard-importing `dbus` because dbus-python is optional in some
    installs. Pyatspi may surface bus errors as `dbus.DBusException`,
    `GLib.GError`, or plain `OSError` from libatspi -- match on type
    name + module rather than `isinstance`.
    """
    name = type(exc).__name__
    module = type(exc).__module__ or ""
    if module.startswith("dbus."):
        return True
    if "DBusException" in name:
        return True
    if "GError" in name and ("gi." in module or "GLib" in module):
        return True
    # libatspi often returns "Object does not exist at path" via a stub
    # accessible -- those manifest as AttributeError / TypeError at the
    # caller, NOT as DBusException. We don't retry those (it's the agent's
    # bug to ask for a stale widget).
    return False


def _reconnect_bus() -> None:
    """Best-effort: force pyatspi to drop its cached registry.

    `importlib.reload` is heavy but bulletproof -- the next `_import_pyatspi`
    call rebuilds the Registry singleton and re-opens the bus connection.
    Silently no-ops if pyatspi isn't imported yet.
    """
    if "pyatspi" not in sys.modules:
        return
    try:
        import importlib
        import pyatspi as _pa
        importlib.reload(_pa)
    except Exception as e:  # pragma: no cover -- diagnostic only
        print(f"[gtk-a11y-mcp] bus reconnect attempt failed: {e}", file=sys.stderr)


def _with_bus_retry(fn: Callable[..., Any]) -> Callable[..., Any]:
    """Decorator: retry once on a DBus-flavored exception, after a reconnect.

    Non-DBus exceptions propagate immediately. After one retry, the second
    failure (DBus or otherwise) propagates verbatim.
    """
    @functools.wraps(fn)
    def wrapped(*args: Any, **kwargs: Any) -> Any:
        try:
            return fn(*args, **kwargs)
        except Exception as e:
            if not _is_dbus_exception(e):
                raise
            print(
                f"[gtk-a11y-mcp] AT-SPI bus error in {fn.__name__}: {e}; "
                "reconnecting and retrying once",
                file=sys.stderr,
            )
            _reconnect_bus()
            return fn(*args, **kwargs)
    return wrapped


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


# Roles the user can interact with -- click, toggle, navigate, type.
# Reused by `_format_state_suffix` (only these get the `[disabled]` marker
# when `sensitive` is absent) and the audit rules.
_ACTIONABLE_ROLES = frozenset({
    "push button", "toggle button", "menu item", "link",
    "check box", "radio button", "tree item", "list item",
})

# Roles that take typed input. Also reused by the audit.
_INPUT_ROLES = frozenset({
    "text", "entry", "password text", "spin button", "combo box",
})

# Roles that should surface the pseudo-state `disabled` in dump_tree when
# AT-SPI's `sensitive` is absent. Containers without `sensitive` are noise.
_DISABLED_RELEVANT_ROLES = _ACTIONABLE_ROLES | _INPUT_ROLES

# States surfaced in dump_tree lines. Positive states (membership = "yes")
# are shown when present; absence of `sensitive` becomes the pseudo-state
# `disabled` -- but only for actionable / input roles where it actually
# changes how the widget behaves.
_TREE_DUMP_STATES = ("focused", "checked", "selected", "expanded", "pressed")


def _get_states(node) -> list[str]:
    """Return AT-SPI state names (lowercase) for a node.

    Returns [] if the node lacks a state set or pyatspi can't be loaded.
    Tolerates mock state-set members already being strings, which lets
    tests skip the real pyatspi state-int -> name mapping.
    """
    try:
        state_set = node.getState()
    except Exception:
        return []
    try:
        raw = state_set.getStates()
    except Exception:
        return []
    out: list[str] = []
    pyatspi = None
    for s in raw:
        if isinstance(s, str):
            out.append(s)
            continue
        if pyatspi is None:
            try:
                pyatspi = _import_pyatspi()
            except Exception:
                return out
        try:
            out.append(pyatspi.stateToString(s))
        except Exception:
            continue
    return out


def _format_state_suffix(states: list[str], role: str = "") -> str:
    """Render the curated state list for a dump_tree line.

    Returns an empty string when nothing interesting is present. `role` is
    used to gate the `disabled` pseudo-state: only actionable / input roles
    surface it. A passive container with no `sensitive` is just noise.
    """
    bits: list[str] = [s for s in _TREE_DUMP_STATES if s in states]
    if "sensitive" not in states and role in _DISABLED_RELEVANT_ROLES:
        bits.append("disabled")
    if not bits:
        return ""
    return " [" + ", ".join(bits) + "]"


def _get_attributes(node) -> dict[str, str]:
    """Parse AT-SPI attributes (list of "key:value" strings) into a dict.

    Returns {} if the node has no Attributes interface. Keys without a `:`
    are stored with empty string values (rare but tolerated by AT-SPI).
    """
    try:
        raw = node.getAttributes()
    except Exception:
        return {}
    if not raw:
        return {}
    out: dict[str, str] = {}
    for entry in raw:
        if not isinstance(entry, str):
            continue
        if ":" in entry:
            k, v = entry.split(":", 1)
            out[k] = v
        else:
            out[entry] = ""
    return out


def _get_description(node) -> str:
    """Read the AT-SPI description (often the tooltip). Empty string if absent."""
    desc = getattr(node, "description", None)
    if desc is None:
        return ""
    return desc if isinstance(desc, str) else ""


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
    attrs = _get_attributes(node)
    acc_id = attrs.get("id")
    if acc_id:
        line += f" #{acc_id}"
    states = _get_states(node)
    if states:
        line += _format_state_suffix(states, role)
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


def _state_filter_matches(filter_spec: str | None, states: list[str]) -> bool:
    """Match a `state=` filter against a node's states.

    `None` -> always True. A bare state name -> must be present.
    A `!state` form -> must be absent. Comparisons are case-insensitive.
    """
    if filter_spec is None:
        return True
    spec = filter_spec.strip().lower()
    if spec.startswith("!"):
        return spec[1:] not in states
    return spec in states


def _walk_match(
    node,
    role: str | None,
    name: str | None,
    results: list,
    max_results: int = 50,
    path: str = "",
    state: str | None = None,
    accessible_id: str | None = None,
    description: str | None = None,
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
        # Read filters one at a time and short-circuit on rejection -- each
        # _get_* call is a bus round-trip. Most trees have many widgets and
        # any explicit filter (id / description / state) typically matches
        # very few of them.
        add = True

        attrs = _get_attributes(node)
        acc_id = attrs.get("id")
        if accessible_id is not None and acc_id != accessible_id:
            add = False

        desc = ""
        if add:
            desc = _get_description(node)
            if description is not None and description.lower() not in desc.lower():
                add = False

        states: list[str] = []
        if add:
            states = _get_states(node)
            if not _state_filter_matches(state, states):
                add = False

        if add:
            attrs_no_id = {k: v for k, v in attrs.items() if k != "id"}
            results.append({
                "path": here,
                "role": node_role,
                "name": node_name,
                "accessible_id": acc_id,
                "description": desc,
                "attributes": attrs_no_id,
                "states": states,
            })
    try:
        count = node.childCount
    except Exception:
        return
    for i in range(count):
        try:
            child = node.getChildAtIndex(i)
        except Exception:
            continue
        _walk_match(
            child, role, name, results, max_results, here,
            state=state, accessible_id=accessible_id, description=description,
        )


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


def _wait_until(
    predicate: Callable[[], Any],
    timeout_s: float,
    poll_ms: int,
    *,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
) -> tuple[Any, float]:
    """Poll `predicate()` until it returns truthy or `timeout_s` elapses.

    Returns the (last_value, elapsed_ms). `last_value` is whatever the
    predicate returned on the final iteration -- truthy on success,
    falsy on timeout. Tests inject `sleep` and `monotonic` to mock the
    clock without sleeping in the test process.
    """
    start = monotonic()
    deadline = start + max(0.0, float(timeout_s))
    interval = max(0.001, poll_ms / 1000.0)
    while True:
        value = predicate()
        if value:
            return value, (monotonic() - start) * 1000.0
        if monotonic() >= deadline:
            return value, (monotonic() - start) * 1000.0
        sleep(interval)


# ---------------------------------------------------------------------------
# Keyboard input
#
# Two backends: AT-SPI's generateKeyboardEvent (Wayland-iffy, X11-ok) and a
# ydotool CLI fallback (Wayland-friendly when `ydotoold` daemon is running).
# Each backend takes an injectable `send`/`runner` hook for unit tests.
# ---------------------------------------------------------------------------

# AT-SPI uses X11 keysym names. Map our friendly names + common aliases.
_ATSPI_KEYSYM_ALIASES = {
    "enter": "Return",
    "esc": "Escape",
    "del": "Delete",
    "ins": "Insert",
    "pageup": "Prior",
    "pagedown": "Next",
    "space": "space",
    "backspace": "BackSpace",
}
_ATSPI_MODIFIER_KEYSYMS = {
    "ctrl": "Control_L",
    "control": "Control_L",
    "shift": "Shift_L",
    "alt": "Alt_L",
    "super": "Super_L",
    "meta": "Super_L",
    "win": "Super_L",
}

# ydotool wants Linux input event codes (linux/input-event-codes.h). Only the
# common ones — exotic keys fall through to AT-SPI only.
_YDOTOOL_KEY_MAP = {
    # Letters
    "a": 30, "b": 48, "c": 46, "d": 32, "e": 18, "f": 33, "g": 34,
    "h": 35, "i": 23, "j": 36, "k": 37, "l": 38, "m": 50, "n": 49,
    "o": 24, "p": 25, "q": 16, "r": 19, "s": 31, "t": 20, "u": 22,
    "v": 47, "w": 17, "x": 45, "y": 21, "z": 44,
    # Digits
    "0": 11, "1": 2, "2": 3, "3": 4, "4": 5, "5": 6, "6": 7,
    "7": 8, "8": 9, "9": 10,
    # F-keys
    "f1": 59, "f2": 60, "f3": 61, "f4": 62, "f5": 63, "f6": 64,
    "f7": 65, "f8": 66, "f9": 67, "f10": 68, "f11": 87, "f12": 88,
    # Common navigation + editing
    "tab": 15, "return": 28, "enter": 28, "escape": 1, "esc": 1,
    "backspace": 14, "delete": 111, "del": 111, "space": 57,
    "up": 103, "down": 108, "left": 105, "right": 106,
    "home": 102, "end": 107, "pageup": 104, "pagedown": 109,
    "insert": 110, "ins": 110,
}
_YDOTOOL_MOD_MAP = {
    "ctrl": 29, "control": 29,         # KEY_LEFTCTRL
    "shift": 42,                       # KEY_LEFTSHIFT
    "alt": 56,                         # KEY_LEFTALT
    "super": 125, "meta": 125, "win": 125,  # KEY_LEFTMETA
}


def _parse_key_spec(spec: str) -> list[tuple[list[str], str]]:
    """Parse a key spec like "Ctrl+S Tab" into [(modifiers, key), ...].

    Whitespace-separated tokens; each token is `+`-separated with the last
    segment being the key and all earlier ones modifiers (lowercased).
    """
    tokens = [t for t in spec.strip().split() if t]
    if not tokens:
        raise ValueError("empty key spec")
    out: list[tuple[list[str], str]] = []
    for tok in tokens:
        parts = tok.split("+")
        if not parts[-1]:
            raise ValueError(f"empty key in token: {tok!r}")
        modifiers = [p.lower() for p in parts[:-1] if p]
        out.append((modifiers, parts[-1]))
    return out


def _press_keys_atspi(
    parsed: list[tuple[list[str], str]],
    send: Callable[[str, str], None] | None = None,
) -> None:
    """Inject keys via pyatspi.Registry.generateKeyboardEvent.

    `send(keysym, kind)` where kind in {"press", "release", "both"}. Default
    sends via real pyatspi; tests inject a recorder.
    """
    if send is None:
        pyatspi = _import_pyatspi()
        kind_map = {
            "press": pyatspi.KEY_PRESS,
            "release": pyatspi.KEY_RELEASE,
            "both": pyatspi.KEY_PRESSRELEASE,
        }

        def send(keysym: str, kind: str) -> None:
            pyatspi.Registry.generateKeyboardEvent(0, keysym, kind_map[kind])

    for modifiers, key in parsed:
        for mod in modifiers:
            send(_ATSPI_MODIFIER_KEYSYMS.get(mod, mod), "press")
        send(_ATSPI_KEYSYM_ALIASES.get(key.lower(), key), "both")
        for mod in reversed(modifiers):
            send(_ATSPI_MODIFIER_KEYSYMS.get(mod, mod), "release")


def _press_keys_ydotool(
    parsed: list[tuple[list[str], str]],
    runner: Callable[[list[str]], None] | None = None,
) -> None:
    """Inject keys via the `ydotool key` CLI. Requires `ydotoold` daemon."""
    if runner is None:
        def runner(cmd: list[str]) -> None:
            subprocess.check_call(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for modifiers, key in parsed:
        codes_down: list[str] = []
        codes_up: list[str] = []
        for mod in modifiers:
            code = _YDOTOOL_MOD_MAP.get(mod)
            if code is None:
                raise ValueError(f"ydotool: unknown modifier {mod!r}")
            codes_down.append(f"{code}:1")
            codes_up.append(f"{code}:0")
        key_code = _YDOTOOL_KEY_MAP.get(key.lower())
        if key_code is None:
            raise ValueError(f"ydotool: unknown key {key!r}")
        sequence = (
            codes_down
            + [f"{key_code}:1", f"{key_code}:0"]
            + list(reversed(codes_up))
        )
        runner(["ydotool", "key", *sequence])


def _screen_locked_dbus(
    runner: Callable[[list[str]], str] | None = None,
) -> bool | None:
    """Return whether the GNOME ScreenSaver reports the session as locked.

    Returns ``None`` when the answer can't be determined (D-Bus call
    failed, gdbus missing, no GNOME ScreenSaver). The default runner
    shells out to `gdbus`.
    """
    if runner is None:
        def runner(cmd: list[str]) -> str:
            return subprocess.check_output(cmd, text=True, timeout=2.0)
    try:
        out = runner([
            "gdbus", "call", "--session",
            "--dest", "org.gnome.ScreenSaver",
            "--object-path", "/org/gnome/ScreenSaver",
            "--method", "org.gnome.ScreenSaver.GetActive",
        ])
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError):
        return None
    text = out.strip().lower()
    if "true" in text:
        return True
    if "false" in text:
        return False
    return None


def _active_frame_app(pyatspi) -> str | None:
    """Return the app name of the AT-SPI frame currently in STATE_ACTIVE."""
    try:
        active_state = pyatspi.STATE_ACTIVE
    except AttributeError:
        return None
    desktop = pyatspi.Registry.getDesktop(0)
    for i in range(desktop.childCount):
        try:
            app = desktop.getChildAtIndex(i)
        except Exception:
            continue
        for j in range(getattr(app, "childCount", 0) or 0):
            try:
                frame = app.getChildAtIndex(j)
                if frame.getRoleName() != "frame":
                    continue
                if frame.getState().contains(active_state):
                    return app.name or ""
            except Exception:
                continue
    return None


@mcp.tool()
@_with_bus_retry
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
@_with_bus_retry
def dump_tree(app_name: str, max_depth: int = 8) -> str:
    """Pretty-print the AT-SPI widget tree for an app (substring match on name)."""
    app = _find_app(app_name)
    if app is None:
        return f"No app matching '{app_name}' on AT-SPI bus. {LAUNCH_HINT}"
    lines: list[str] = []
    _walk_tree(app, 0, max_depth, lines)
    return "\n".join(lines)


@mcp.tool()
@_with_bus_retry
def find_widgets(
    app_name: str,
    role: str | None = None,
    name: str | None = None,
    state: str | None = None,
    accessible_id: str | None = None,
    description: str | None = None,
) -> list[dict[str, Any]]:
    """Find widgets by role, name (substring), state, accessible_id, and/or description.

    `accessible_id` is exact-match (locale-independent, the recommended
    selector in modern libadwaita). `description` is substring match.
    `state` is a single state name like "focused" / "checked"; prefix
    with "!" to require absence (`state="!sensitive"` -> disabled widgets).

    Each result dict carries: path, role, name, accessible_id (or None),
    description, attributes (dict, minus the id), states (list).
    """
    app = _find_app(app_name)
    if app is None:
        return []
    results: list[dict[str, Any]] = []
    _walk_match(
        app, role, name, results,
        state=state, accessible_id=accessible_id, description=description,
    )
    return results


@mcp.tool()
@_with_bus_retry
def get_attributes(app_name: str, role: str, name: str) -> dict[str, Any]:
    """Return the full AT-SPI Attributes dict for a widget.

    Includes the `id` field if present. Use this when you need app-specific
    hints (level, container, xalign, etc.) that aren't surfaced by
    ``find_widgets``.
    """
    app = _find_app(app_name)
    if app is None:
        return {"error": f"No app '{app_name}' on AT-SPI bus."}
    results: list[dict[str, Any]] = []
    _walk_match(app, role, name, results, max_results=1)
    if not results:
        return {"error": f"No {role}='{name}' in {app_name}."}
    node = _resolve_path(app, results[0]["path"])
    if node is None:
        return {"error": f"Couldn't resolve {results[0]['path']}."}
    return _get_attributes(node)


@mcp.tool()
@_with_bus_retry
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
@_with_bus_retry
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
@_with_bus_retry
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
@_with_bus_retry
def focus(app_name: str, role: str, name: str) -> str:
    """Move keyboard focus to a widget via the Component.grabFocus interface.

    Use before ``press_keys`` for app-scoped key dispatch -- AT-SPI keyboard
    injection is session-wide, so whoever owns focus receives the keys.
    """
    app = _find_app(app_name)
    if app is None:
        return f"No app '{app_name}' on AT-SPI bus. {LAUNCH_HINT}"
    results: list[dict[str, Any]] = []
    _walk_match(app, role, name, results, max_results=1)
    if not results:
        return f"No {role}='{name}' in {app_name}."
    node = _resolve_path(app, results[0]["path"])
    if node is None:
        return f"Couldn't resolve {results[0]['path']}."
    try:
        comp = node.queryComponent()
    except NotImplementedError:
        return f"Widget at {results[0]['path']} has no Component interface."
    try:
        success = comp.grabFocus()
    except Exception as e:
        return f"grabFocus failed: {e}"
    # Some pyatspi versions return None instead of bool; treat None as ok.
    if success is False:
        return f"grabFocus returned false on {results[0]['path']}"
    return f"Focused: {results[0]['path']}"


@mcp.tool()
@_with_bus_retry
def press_keys(keys: str) -> dict[str, Any]:
    """Synthesize key events at the session level (no per-app routing).

    Spec syntax:
      - Single key: "Return", "Escape", "Tab", "Up", "F5", "a"
      - Modifier combo: "Ctrl+S", "Ctrl+Shift+T", "Alt+F4"
      - Sequence (space-separated): "Ctrl+a Delete Tab"

    Modifiers: ctrl, shift, alt, super (aliases: control, meta, win).
    Tries AT-SPI's generateKeyboardEvent first; falls back to ydotool when
    AT-SPI fails or is unavailable (common on Wayland sessions). Returns
    ``{sent, backend, atspi_error?}`` on success, ``{error, ...}`` otherwise.
    """
    try:
        parsed = _parse_key_spec(keys)
    except ValueError as e:
        return {"error": str(e)}
    try:
        _press_keys_atspi(parsed)
        return {"sent": keys, "backend": "atspi"}
    except Exception as atspi_err:
        try:
            _press_keys_ydotool(parsed)
            return {
                "sent": keys,
                "backend": "ydotool",
                "atspi_error": str(atspi_err),
            }
        except (OSError, subprocess.CalledProcessError, ValueError) as ydotool_err:
            # OSError covers FileNotFoundError (ydotool missing) AND
            # PermissionError (user not in the `input` group / no access to
            # /dev/uinput / ydotoold socket).
            return {
                "error": "both keyboard backends failed",
                "atspi_error": str(atspi_err),
                "ydotool_error": str(ydotool_err),
            }


# Roles that own a top-level window. `frame` is GTK's main-window role;
# `dialog` covers modal/non-modal dialogs; `window` is the rarer toplevel.
_WINDOW_ROLES = ("frame", "dialog", "window")


def _window_for_widget(app, widget_path: str):
    """Resolve the first window-like ancestor named in `widget_path`.

    Paths look like ``application[A]/frame[Main]/panel[]/push button[Save]``.
    We walk through the segments and resolve up to the first
    frame/dialog/window. Returns None if the path doesn't include one.
    """
    parts = widget_path.split("/")
    for i, segment in enumerate(parts):
        for window_role in _WINDOW_ROLES:
            if segment.startswith(f"{window_role}["):
                return _resolve_path(app, "/".join(parts[: i + 1]))
    return None


def _activate_window(window) -> bool:
    """Best-effort: bring a frame/dialog/window to the front via grabFocus.

    Returns True on success, False if Component interface is missing or
    grabFocus reports failure. Wayland compositors may refuse cross-process
    window activation; that's a False, not an exception.
    """
    if window is None:
        return False
    try:
        comp = window.queryComponent()
    except NotImplementedError:
        return False
    try:
        result = comp.grabFocus()
    except Exception:
        return False
    # Older pyatspi returns None on success; treat None as "we asked, no error".
    return result is None or bool(result)


def _default_activate(app, widget_path: str) -> bool:
    """Production default for screenshot_widget's activation hook."""
    return _activate_window(_window_for_widget(app, widget_path))


def _activate_app_impl(
    app_name: str,
    *,
    find_app: Callable[[str], Any] | None = None,
) -> dict[str, Any]:
    if find_app is None:
        find_app = _find_app
    app = find_app(app_name)
    if app is None:
        return {"ok": False, "error": f"No app '{app_name}' on AT-SPI bus."}
    try:
        count = app.childCount
    except Exception:
        return {"ok": False, "error": "app has no children"}
    for i in range(count):
        try:
            child = app.getChildAtIndex(i)
            role = child.getRoleName()
        except Exception:
            continue
        if role in _WINDOW_ROLES:
            child_name = child.name or ""
            return {
                "ok": _activate_window(child),
                "window_path": f"application[{app.name or ''}]/{role}[{child_name}]",
                "hint": (
                    "Wayland compositors may refuse cross-process activation; "
                    "ok=true means grabFocus didn't error -- the window may or "
                    "may not actually be on top."
                ),
            }
    return {"ok": False, "error": "no frame/dialog/window found in app"}


def _widget_extents(node) -> tuple[int, int, int, int]:
    """Return (x, y, width, height) in desktop coords for a widget."""
    pyatspi = _import_pyatspi()
    comp = node.queryComponent()
    e = comp.getExtents(pyatspi.DESKTOP_COORDS)
    return (e.x, e.y, e.width, e.height)


def _capture_full_screen_png() -> bytes:
    """Capture the whole screen via gnome-screenshot, return PNG bytes."""
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
        path = f.name
    try:
        subprocess.check_call(["gnome-screenshot", "-f", path])
        with open(path, "rb") as f:
            return f.read()
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


def _crop_png(image_bytes: bytes, box: tuple[int, int, int, int]) -> bytes:
    """Crop PNG bytes to (x, y, w, h); negative origins clip to image bounds."""
    try:
        from PIL import Image as PILImage
    except ImportError as e:
        raise RuntimeError(
            "Pillow missing (apt: python3-pil) -- can't crop screenshots"
        ) from e
    img = PILImage.open(io.BytesIO(image_bytes))
    x, y, w, h = box
    img_w, img_h = img.size
    left = max(0, x)
    top = max(0, y)
    right = min(img_w, x + w)
    bottom = min(img_h, y + h)
    if right <= left or bottom <= top:
        raise RuntimeError(
            f"Widget box {box} doesn't overlap screen {img_w}x{img_h}"
        )
    cropped = img.crop((left, top, right, bottom))
    out = io.BytesIO()
    cropped.save(out, format="PNG")
    return out.getvalue()


def _screenshot_widget_impl(
    app_name: str,
    role: str,
    name: str,
    padding_px: int,
    *,
    find_app: Callable[[str], Any] | None = None,
    capture: Callable[[], bytes] | None = None,
    get_extents: Callable[[Any], tuple[int, int, int, int]] | None = None,
    activate: Callable[[Any, str], bool] | None = _default_activate,
    activate_settle_ms: int = 200,
    sleep: Callable[[float], None] = time.sleep,
) -> bytes:
    """Core screenshot_widget logic. Returns cropped PNG bytes. The `_impl`
    exists for unit tests to inject every collaborator.

    If `activate` is callable, it's invoked as ``activate(app, widget_path)``
    before capturing -- typically a grabFocus on the widget's window. The
    `activate_settle_ms` sleep gives the compositor time to repaint. Pass
    ``activate=None`` to skip activation entirely.
    """
    if find_app is None:
        find_app = _find_app
    if capture is None:
        capture = _capture_full_screen_png
    if get_extents is None:
        get_extents = _widget_extents

    app = find_app(app_name)
    if app is None:
        raise RuntimeError(f"No app '{app_name}' on AT-SPI bus.")
    results: list[dict[str, Any]] = []
    _walk_match(app, role, name, results, max_results=1)
    if not results:
        raise RuntimeError(f"No {role}='{name}' in {app_name}.")
    path = results[0]["path"]
    node = _resolve_path(app, path)
    if node is None:
        raise RuntimeError(f"Couldn't resolve {path}.")

    # Best-effort window activation BEFORE we read extents and capture --
    # the compositor may reposition the window on raise, which would shift
    # the widget's screen coords.
    if activate is not None:
        try:
            activate(app, path)
        except Exception:
            pass  # never let activation block the capture
        if activate_settle_ms > 0:
            sleep(activate_settle_ms / 1000.0)

    x, y, w, h = get_extents(node)
    if w <= 0 or h <= 0:
        raise RuntimeError(
            f"Widget {path} has zero-area extents ({w}x{h}); "
            "widget may be hidden or offscreen."
        )
    screen = capture()
    box = (x - padding_px, y - padding_px, w + 2 * padding_px, h + 2 * padding_px)
    return _crop_png(screen, box)


@mcp.tool()
@_with_bus_retry
def screenshot_widget(
    app_name: str,
    role: str,
    name: str,
    padding_px: int = 4,
    activate_first: bool = True,
) -> Image:
    """Screenshot of a single widget, cropped to its AT-SPI extents.

    Much cheaper in tokens than a full-window screenshot when you only need
    one button / label / list item.

    `activate_first=True` (default) calls grabFocus on the widget's window
    before capturing, then waits ~200 ms for the compositor to repaint --
    this prevents the common pitfall where an occluded window means you
    screenshot whatever is in front of it instead. On Wayland the
    compositor may refuse cross-process activation; the capture still
    proceeds. Set ``activate_first=False`` if the window is already focused
    or activation is unwanted.

    Raises RuntimeError if the widget can't be resolved or is offscreen /
    hidden (zero-area extents).
    """
    return Image(
        data=_screenshot_widget_impl(
            app_name, role, name, padding_px,
            activate=_default_activate if activate_first else None,
        ),
        format="png",
    )


@mcp.tool()
@_with_bus_retry
def activate_app(app_name: str) -> dict[str, Any]:
    """Bring an app's main window to the front (best-effort).

    Walks the app's children for the first frame / dialog / window and
    calls grabFocus on it via AT-SPI's Component interface. On Wayland the
    compositor may silently refuse cross-process activation -- ``ok=true``
    means the call didn't error, not that the window is provably on top.
    For multi-window apps, use ``find_widgets`` to get a widget path, then
    ``screenshot_widget`` (which activates the widget's own window).

    Returns ``{ok, window_path, hint?, error?}``.
    """
    return _activate_app_impl(app_name)


# ---------------------------------------------------------------------------
# Accessibility audit -- rule-based walk that turns the tree-walker into an
# a11y linter. Each rule is a pure predicate on a NodeInfo snapshot.
# _ACTIONABLE_ROLES and _INPUT_ROLES are defined near the top of the file
# because dump_tree's [disabled] pseudo-state gate also relies on them.
# ---------------------------------------------------------------------------


class _NodeInfo:
    """Snapshot of a node assembled once and passed to every rule."""
    __slots__ = ("path", "role", "name", "description", "accessible_id",
                 "attributes", "states", "has_action", "has_text",
                 "has_labelled_by")

    def __init__(self, path, role, name, description, accessible_id,
                 attributes, states, has_action, has_text, has_labelled_by):
        self.path = path
        self.role = role
        self.name = name
        self.description = description
        self.accessible_id = accessible_id
        self.attributes = attributes
        self.states = states
        self.has_action = has_action
        self.has_text = has_text
        self.has_labelled_by = has_labelled_by


def _node_has_action(node) -> bool:
    try:
        node.queryAction()
        return True
    except NotImplementedError:
        return False
    except Exception:
        return False


def _node_has_labelled_by(node) -> bool:
    """Return True if the node has at least one RELATION_LABELLED_BY."""
    try:
        relations = node.getRelationSet()
    except Exception:
        return False
    try:
        pyatspi = _import_pyatspi()
        target = pyatspi.RELATION_LABELLED_BY
    except Exception:
        return False
    for rel in relations or ():
        try:
            if rel.getRelationType() == target:
                return True
        except Exception:
            continue
    return False


# Each rule: (id, severity, predicate). Predicate returns either None or a
# string message describing the issue. Severity is purely informational
# ("error" or "warning").
def _rule_empty_name_actionable(info: _NodeInfo) -> str | None:
    if info.role not in _ACTIONABLE_ROLES:
        return None
    if info.name.strip() or info.description.strip():
        return None
    return f"{info.role} has no name or description; screen readers will read silence"


def _rule_name_equals_role(info: _NodeInfo) -> str | None:
    if not info.name:
        return None
    n = info.name.strip().lower()
    if not n:
        return None
    r = info.role.lower()
    role_tail = r.split()[-1] if r else ""
    if n == r or (role_tail and n == role_tail):
        return f"name {info.name!r} is the role itself; looks like a placeholder"
    return None


def _rule_name_only_whitespace(info: _NodeInfo) -> str | None:
    # Note: a single space is sometimes intentional (a deliberately-blank
    # spacer widget that still wants to be tabbable). The rule still fires
    # in that case -- false positive rate is low enough to be worth it.
    if info.name and not info.name.strip():
        return f"name is {len(info.name)} whitespace chars; screen readers will read nothing useful"
    return None


def _rule_clickable_without_action(info: _NodeInfo) -> str | None:
    if info.role in _ACTIONABLE_ROLES and not info.has_action:
        return f"role {info.role!r} typically needs Action interface; widget exposes none"
    return None


def _rule_actionable_not_focusable(info: _NodeInfo) -> str | None:
    # Known false positive class: widgets inside a focus-trapping popover
    # have STATE_FOCUSABLE temporarily removed while the popover is open;
    # a snapshot taken at that moment will flag them. Reviewers should
    # ignore hits matching that pattern.
    if info.role not in _ACTIONABLE_ROLES:
        return None
    if "focusable" in info.states:
        return None
    return f"actionable {info.role!r} lacks STATE_FOCUSABLE; keyboard users can't reach it"


def _rule_editable_without_label(info: _NodeInfo) -> str | None:
    if info.role not in _INPUT_ROLES:
        return None
    if "editable" not in info.states:
        return None
    if info.name.strip() or info.description.strip() or info.has_labelled_by:
        return None
    return f"editable {info.role!r} has no name, description, or labelled-by relation"


def _rule_image_without_description(info: _NodeInfo) -> str | None:
    if info.role != "image":
        return None
    if info.name.strip() or info.description.strip():
        return None
    return "image has no name or description; alt text equivalent is missing"


# Map rule id -> (severity, predicate). Stable order via list.
_AUDIT_RULES: list[tuple[str, str, Callable[[_NodeInfo], str | None]]] = [
    ("empty-name-actionable", "error", _rule_empty_name_actionable),
    ("name-equals-role", "warning", _rule_name_equals_role),
    ("name-only-whitespace", "error", _rule_name_only_whitespace),
    ("clickable-without-action", "warning", _rule_clickable_without_action),
    ("actionable-not-focusable", "warning", _rule_actionable_not_focusable),
    ("editable-without-label", "error", _rule_editable_without_label),
    ("image-without-description", "warning", _rule_image_without_description),
]


def _build_node_info(node, path: str) -> _NodeInfo:
    try:
        role = node.getRoleName()
        name = node.name or ""
    except Exception:
        role = ""
        name = ""
    attrs = _get_attributes(node)
    acc_id = attrs.get("id")
    attrs_no_id = {k: v for k, v in attrs.items() if k != "id"}
    return _NodeInfo(
        path=path,
        role=role,
        name=name,
        description=_get_description(node),
        accessible_id=acc_id,
        attributes=attrs_no_id,
        states=_get_states(node),
        has_action=_node_has_action(node),
        has_text=False,  # reserved for future rules; cheap to set lazily
        has_labelled_by=_node_has_labelled_by(node),
    )


def _audit_walk(
    node,
    path: str,
    issues: list[dict[str, Any]],
    enabled_rules: list[tuple[str, str, Callable[[_NodeInfo], str | None]]],
    seen_ids: dict[str, list[tuple[str, str]]],
    max_issues: int,
    checked: list[int],  # [counter] for in-place increment
) -> None:
    if len(issues) >= max_issues:
        return
    try:
        node_role = node.getRoleName()
        node_name = node.name or ""
    except Exception:
        return
    here = f"{path}/{node_role}[{node_name}]" if path else f"{node_role}[{node_name}]"
    info = _build_node_info(node, here)
    checked[0] += 1
    if info.accessible_id:
        # Store (path, role) so the duplicate-id post-pass can surface the
        # role on every emitted issue instead of an empty string.
        seen_ids.setdefault(info.accessible_id, []).append((here, info.role))
    for rule_id, severity, predicate in enabled_rules:
        if len(issues) >= max_issues:
            return
        msg = predicate(info)
        if msg is not None:
            issues.append({
                "path": here,
                "role": info.role,
                "rule": rule_id,
                "severity": severity,
                "message": msg,
            })
    try:
        count = node.childCount
    except Exception:
        return
    for i in range(count):
        try:
            child = node.getChildAtIndex(i)
        except Exception:
            continue
        _audit_walk(child, here, issues, enabled_rules, seen_ids, max_issues, checked)


def _audit_impl(
    app_name: str,
    rules: list[str] | None = None,
    max_issues: int = 200,
    *,
    find_app: Callable[[str], Any] | None = None,
) -> dict[str, Any]:
    if find_app is None:
        find_app = _find_app
    app = find_app(app_name)
    if app is None:
        return {"error": f"No app '{app_name}' on AT-SPI bus. {LAUNCH_HINT}"}
    # Determine which rules run. Unknown names are surfaced so users notice typos.
    available = {rid: (rid, sev, pred) for rid, sev, pred in _AUDIT_RULES}
    if rules is None:
        enabled = list(_AUDIT_RULES)
        unknown: list[str] = []
    else:
        enabled = [available[r] for r in rules if r in available]
        unknown = [r for r in rules if r not in available]
    issues: list[dict[str, Any]] = []
    seen_ids: dict[str, list[tuple[str, str]]] = {}
    checked = [0]
    _audit_walk(app, "", issues, enabled, seen_ids, max_issues, checked)
    # Post-pass: duplicate accessible-ids. Only run if not explicitly disabled.
    if rules is None or "duplicate-accessible-id" in rules:
        for acc_id, occurrences in seen_ids.items():
            if len(occurrences) <= 1:
                continue
            for p, r in occurrences:
                if len(issues) >= max_issues:
                    break
                issues.append({
                    "path": p,
                    "role": r,
                    "rule": "duplicate-accessible-id",
                    "severity": "error",
                    "message": f"accessible-id {acc_id!r} appears {len(occurrences)} times in this app",
                })
    result: dict[str, Any] = {
        "issues": issues,
        "checked_widgets": checked[0],
        "rules_run": [r[0] for r in enabled]
            + (["duplicate-accessible-id"] if (rules is None or "duplicate-accessible-id" in rules) else []),
    }
    if unknown:
        result["unknown_rules"] = unknown
    return result


@mcp.tool()
@_with_bus_retry
def audit(
    app_name: str,
    rules: list[str] | None = None,
    max_issues: int = 200,
) -> dict[str, Any]:
    """Walk the AT-SPI tree and report accessibility issues.

    Available rule ids (pass `rules=[...]` to narrow):
      - empty-name-actionable: actionable widget with no name and no description
      - name-equals-role: name like "button" on a push-button (placeholder)
      - name-only-whitespace: non-empty whitespace-only name
      - clickable-without-action: actionable role without Action interface
      - actionable-not-focusable: actionable role without STATE_FOCUSABLE
      - editable-without-label: editable input with no label / description
      - image-without-description: image with no name and no description
      - duplicate-accessible-id: accessible-id repeated within the app

    Returns ``{issues, checked_widgets, rules_run, unknown_rules?, error?}``.
    """
    return _audit_impl(app_name, rules=rules, max_issues=max_issues)


def _resolve_one_widget(
    app_name: str,
    role: str,
    name: str,
    *,
    find_app: Callable[[str], Any] | None = None,
) -> tuple[Any, str, str | None]:
    """Find an app + a single widget by role+name. Returns (node, path, error).

    On success: (node, path, None). On failure: (None, "", error-string)
    matching the wording the existing tools use (so callers stay consistent).
    """
    if find_app is None:
        find_app = _find_app
    app = find_app(app_name)
    if app is None:
        return None, "", f"No app '{app_name}' on AT-SPI bus. {LAUNCH_HINT}"
    results: list[dict[str, Any]] = []
    _walk_match(app, role, name, results, max_results=1)
    if not results:
        return None, "", f"No {role}='{name}' in {app_name}."
    path = results[0]["path"]
    node = _resolve_path(app, path)
    if node is None:
        return None, path, f"Couldn't resolve {path}."
    return node, path, None


def _get_value_impl(
    app_name: str, role: str, name: str,
    *, find_app: Callable[[str], Any] | None = None,
) -> dict[str, Any]:
    node, path, err = _resolve_one_widget(app_name, role, name, find_app=find_app)
    if err is not None:
        return {"error": err}
    try:
        v = node.queryValue()
    except NotImplementedError:
        return {"error": f"Widget at {path} has no Value interface."}
    return {
        "path": path,
        "value": float(v.currentValue),
        "minimum": float(v.minimumValue),
        "maximum": float(v.maximumValue),
        "minimum_increment": float(getattr(v, "minimumIncrement", 0.0)),
    }


def _set_value_impl(
    app_name: str, role: str, name: str, value: float,
    *, find_app: Callable[[str], Any] | None = None,
) -> str:
    node, path, err = _resolve_one_widget(app_name, role, name, find_app=find_app)
    if err is not None:
        return err
    try:
        v = node.queryValue()
    except NotImplementedError:
        return f"Widget at {path} has no Value interface."
    try:
        v.currentValue = float(value)
    except Exception as e:
        return f"setting currentValue failed: {e}"
    return f"Set value={value} on {path}."


def _select_item_impl(
    app_name: str, parent_path: str, item_name: str,
    *, find_app: Callable[[str], Any] | None = None,
) -> str:
    if find_app is None:
        find_app = _find_app
    app = find_app(app_name)
    if app is None:
        return f"No app '{app_name}' on AT-SPI bus. {LAUNCH_HINT}"
    parent = _resolve_path(app, parent_path)
    if parent is None:
        return f"Couldn't resolve parent path: {parent_path}"
    try:
        sel = parent.querySelection()
    except NotImplementedError:
        return f"Parent at {parent_path} has no Selection interface."
    try:
        count = parent.childCount
    except Exception:
        count = 0
    needle = item_name.lower()
    for i in range(count):
        try:
            child = parent.getChildAtIndex(i)
            child_name = (child.name or "")
        except Exception:
            continue
        if needle in child_name.lower():
            try:
                sel.selectChild(i)
            except Exception as e:
                return f"selectChild({i}) failed: {e}"
            return f"Selected child #{i} '{child_name}' under {parent_path}."
    return f"No child containing '{item_name}' under {parent_path}."


def _set_checked_impl(
    app_name: str, role: str, name: str, checked: bool,
    *, find_app: Callable[[str], Any] | None = None,
) -> str:
    node, path, err = _resolve_one_widget(app_name, role, name, find_app=find_app)
    if err is not None:
        return err
    is_checked = "checked" in _get_states(node)
    if is_checked == checked:
        return f"Already {'checked' if checked else 'unchecked'}: {path} (no-op)."
    try:
        action = node.queryAction()
    except NotImplementedError:
        return f"Widget at {path} has no Action interface; can't toggle."
    try:
        action.doAction(0)
    except Exception as e:
        return f"doAction failed: {e}"
    return f"Toggled {path} to {'checked' if checked else 'unchecked'}."


@mcp.tool()
@_with_bus_retry
def get_value(app_name: str, role: str, name: str) -> dict[str, Any]:
    """Read AT-SPI Value: {value, minimum, maximum, minimum_increment}.

    Works on sliders, spin buttons, progress bars, scroll bars.
    """
    return _get_value_impl(app_name, role, name)


@mcp.tool()
@_with_bus_retry
def set_value(app_name: str, role: str, name: str, value: float) -> str:
    """Set a slider / spin button / etc. to a specific numeric value.

    More semantic than clicking; works regardless of widget pixel layout.
    """
    return _set_value_impl(app_name, role, name, value)


@mcp.tool()
@_with_bus_retry
def select_item(app_name: str, parent_path: str, item_name: str) -> str:
    """Select a child item by name within a Selection-capable container.

    More reliable than ``click`` for combo boxes and list views: the
    Selection interface works even when the visible list cells live in a
    transient popup that comes and goes.

    `parent_path`: a path from ``find_widgets`` to the container widget
    (combo box, list, tree). `item_name` is substring-matched on each
    child's name.
    """
    return _select_item_impl(app_name, parent_path, item_name)


@mcp.tool()
@_with_bus_retry
def set_checked(app_name: str, role: str, name: str, checked: bool) -> str:
    """Set a check box / toggle button to a specific state (idempotent).

    Reads STATE_CHECKED; no-ops when already in the desired state. Unlike
    a raw ``click``, this won't accidentally flip an already-correct box.
    """
    return _set_checked_impl(app_name, role, name, checked)


@mcp.tool()
@_with_bus_retry
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
@_with_bus_retry
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


def _launch_app_impl(
    command: str,
    args: list[str] | None,
    wait_seconds: float,
    app_name_match: str | None,
    extra_env: dict[str, str] | None,
    *,
    spawn: Callable[[list[str], dict[str, str]], Any] | None = None,
    find_app: Callable[[str], Any] | None = None,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
) -> dict[str, Any]:
    """Core launch logic. The `_impl` exists for unit tests to inject
    `spawn`, `find_app`, and the clock. ``spawn(argv, env) -> proc-like``;
    only ``.pid`` is read from the return value."""
    if spawn is None:
        def spawn(argv: list[str], env: dict[str, str]):
            return subprocess.Popen(
                argv,
                env=env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )

    env = {**os.environ, "GTK_A11Y": "atspi"}
    if extra_env:
        env.update(extra_env)
    argv = [command, *(args or [])]
    proc = spawn(argv, env)
    pid = getattr(proc, "pid", None)

    if app_name_match is None or wait_seconds <= 0:
        return {
            "pid": pid,
            "found_on_bus": False,
            "elapsed_ms": 0.0,
            "hint": "no app_name_match given -- returned immediately after spawn",
        }

    if find_app is None:
        find_app = _find_app

    def probe() -> dict[str, Any] | None:
        app = find_app(app_name_match)
        if app is None:
            return None
        return {"app_name": app.name or "", "child_count": app.childCount}

    value, elapsed_ms = _wait_until(
        probe, wait_seconds, 250, sleep=sleep, monotonic=monotonic
    )
    if not value:
        return {
            "pid": pid,
            "found_on_bus": False,
            "elapsed_ms": elapsed_ms,
            "hint": LAUNCH_HINT,
        }
    return {"pid": pid, "found_on_bus": True, "elapsed_ms": elapsed_ms, **value}


@mcp.tool()
@_with_bus_retry
def launch_app(
    command: str,
    args: list[str] | None = None,
    wait_seconds: float = 10.0,
    app_name_match: str | None = None,
    extra_env: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Launch a GTK app with GTK_A11Y=atspi pre-set; optionally wait on the bus.

    `args` is a list of argv elements (never a shell string -- avoids
    injection). The subprocess is detached via `start_new_session=True` so
    the launched app outlives this MCP server's process group.

    When `app_name_match` is provided and `wait_seconds > 0`, polls the
    AT-SPI bus for an app whose name contains the match (substring,
    case-insensitive -- same convention as ``_find_app``).

    Returns: {pid, found_on_bus, elapsed_ms, app_name?, child_count?, hint?}.
    """
    return _launch_app_impl(command, args, wait_seconds, app_name_match, extra_env)


@mcp.tool()
@_with_bus_retry
def wait_for_app(
    app_name: str,
    timeout_s: float = 10.0,
    poll_ms: int = 250,
) -> dict[str, Any]:
    """Block until an app matching ``app_name`` appears on the AT-SPI bus.

    Saves a ScheduleWakeup-after-launch dance: launch the app with
    ``GTK_A11Y=atspi`` in the same turn, then call this tool. Returns
    ``{found, app_name, child_count, elapsed_ms, hint?}``. Substring
    match on app name (matches ``_find_app``).
    """
    def probe() -> dict[str, Any] | None:
        app = _find_app(app_name)
        if app is None:
            return None
        return {"app_name": app.name or "", "child_count": app.childCount}

    value, elapsed_ms = _wait_until(probe, timeout_s, poll_ms)
    if not value:
        return {"found": False, "elapsed_ms": elapsed_ms, "hint": LAUNCH_HINT}
    return {"found": True, "elapsed_ms": elapsed_ms, **value}


@mcp.tool()
@_with_bus_retry
def wait_for_widget(
    app_name: str,
    role: str | None = None,
    name: str | None = None,
    timeout_s: float = 10.0,
    poll_ms: int = 250,
) -> dict[str, Any]:
    """Block until a widget matching ``role`` and/or ``name`` exists.

    Useful after a click that triggers an async refresh. Returns
    ``{found, path, role, name, elapsed_ms}``. On timeout ``found`` is
    False and ``path`` is empty.
    """
    def probe() -> dict[str, Any] | None:
        app = _find_app(app_name)
        if app is None:
            return None
        results: list[dict[str, Any]] = []
        _walk_match(app, role, name, results, max_results=1)
        if not results:
            return None
        return results[0]

    value, elapsed_ms = _wait_until(probe, timeout_s, poll_ms)
    if not value:
        return {"found": False, "path": "", "role": "", "name": "", "elapsed_ms": elapsed_ms}
    return {"found": True, "elapsed_ms": elapsed_ms, **value}


def _wait_for_state_impl(
    app_name: str,
    role: str,
    name: str,
    state: str,
    present: bool,
    timeout_s: float,
    poll_ms: int,
    *,
    find_app: Callable[[str], Any] | None = None,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
) -> dict[str, Any]:
    """Poll until a widget's state matches the desired present/absent. The
    `_impl` exists for unit tests to inject find_app + clock."""
    if find_app is None:
        find_app = _find_app
    target_state = state.lower()

    def probe() -> dict[str, Any] | None:
        app = find_app(app_name)
        if app is None:
            return None
        results: list[dict[str, Any]] = []
        _walk_match(app, role, name, results, max_results=1)
        if not results:
            return None
        states = results[0].get("states", [])
        satisfied = (target_state in states) == bool(present)
        if not satisfied:
            return None
        return {"path": results[0]["path"], "states": states}

    value, elapsed_ms = _wait_until(
        probe, timeout_s, poll_ms, sleep=sleep, monotonic=monotonic,
    )
    if not value:
        return {
            "found": False,
            "satisfied": False,
            "elapsed_ms": elapsed_ms,
            "path": "",
            "states": [],
        }
    return {
        "found": True,
        "satisfied": True,
        "elapsed_ms": elapsed_ms,
        **value,
    }


@mcp.tool()
@_with_bus_retry
def wait_for_state(
    app_name: str,
    role: str,
    name: str,
    state: str,
    present: bool = True,
    timeout_s: float = 10.0,
    poll_ms: int = 250,
) -> dict[str, Any]:
    """Block until a widget's state matches.

    `state`: AT-SPI state name (e.g. "sensitive", "checked", "focused").
    `present=True` waits for the state to appear; `False` waits for it to
    vanish. Useful after a click that triggers async validation:

        click(...)              # submit form
        wait_for_state(..., state="sensitive", present=True)  # Save unblocks

    Returns ``{found, satisfied, elapsed_ms, path, states}``.
    """
    return _wait_for_state_impl(
        app_name, role, name, state, present, timeout_s, poll_ms,
    )


@mcp.tool()
@_with_bus_retry
def click_by_path(app_name: str, path: str) -> str:
    """Invoke the default Action ('click') on a widget by its exact path.

    Use the ``path`` returned by ``find_widgets`` -- bypasses the
    ambiguity error that ``click(role, name)`` raises when two widgets
    share a name (e.g. a tree row and a list cell).
    """
    app = _find_app(app_name)
    if app is None:
        return f"No app '{app_name}' on AT-SPI bus. {LAUNCH_HINT}"
    node = _resolve_path(app, path)
    if node is None:
        return f"Couldn't resolve path: {path}"
    try:
        action = node.queryAction()
    except NotImplementedError:
        return f"Widget at {path} has no Action interface."
    try:
        action.doAction(0)
    except Exception as e:
        return f"doAction failed: {e}"
    return f"Clicked: {path}"


@mcp.tool()
@_with_bus_retry
def screen_status() -> dict[str, Any]:
    """Pre-flight diagnostics for ``screenshot`` and friends.

    Returns ``{session_type, locked, active_app}``. ``locked`` is
    ``None`` when the GNOME ScreenSaver D-Bus answer wasn't available.
    ``active_app`` is the AT-SPI app whose frame is in STATE_ACTIVE,
    or ``None`` if no frame reports active. Use to decide whether
    ``screenshot`` will return anything useful: if the screen is
    locked, the compositor isn't painting client surfaces; if the
    active app isn't yours, ``screenshot(window_only=True)`` will grab
    a different window than you expect.
    """
    out: dict[str, Any] = {
        "session_type": os.environ.get("XDG_SESSION_TYPE", ""),
        "locked": _screen_locked_dbus(),
        "active_app": None,
    }
    try:
        pyatspi = _import_pyatspi()
        out["active_app"] = _active_frame_app(pyatspi)
    except Exception as e:
        out["active_app_error"] = str(e)
    return out


# ---------------------------------------------------------------------------
# script: batched multi-step tool dispatch. Saves MCP round-trips for known
# flows. The dispatch table lives at the bottom of the module so every tool
# it references is already defined.
# ---------------------------------------------------------------------------

# Tools deliberately excluded from script() batching:
#  - screenshot / screenshot_widget: return Image; embedding in batch results
#    bloats the response. Call them individually.
#  - audit: large output and slow on big trees; call separately.
_SCRIPT_DISPATCH: dict[str, Callable[..., Any]] = {
    # Read tools
    "list_apps": list_apps,
    "find_widgets": find_widgets,
    "dump_tree": dump_tree,
    "get_text": get_text,
    "get_attributes": get_attributes,
    "get_value": get_value,
    "status": status,
    "screen_status": screen_status,
    # Interaction
    "click": click,
    "click_by_path": click_by_path,
    "type_text": type_text,
    "focus": focus,
    "press_keys": press_keys,
    "set_value": set_value,
    "select_item": select_item,
    "set_checked": set_checked,
    # Waiters
    "wait_for_app": wait_for_app,
    "wait_for_widget": wait_for_widget,
    "wait_for_state": wait_for_state,
    # Lifecycle
    "launch_app": launch_app,
    "activate_app": activate_app,
}

# Patterns we treat as a failed step. Strings come from existing tool error
# messages -- prefixes for those that always lead with the failure, substrings
# for messages where the failure word lands inside the result.
_SCRIPT_FAIL_PREFIXES = (
    "No app", "No ", "Couldn't", "Ambiguous", "Widget ",
    "Parent at", "no app",
)
_SCRIPT_FAIL_SUBSTRINGS = (
    "failed", "no Action interface", "no Component interface",
    "no Value interface", "no Selection interface",
    "no EditableText interface",
)


def _step_failed(result: Any) -> bool:
    if isinstance(result, dict):
        if "error" in result:
            return True
        if result.get("found") is False:
            return True
        return False
    if isinstance(result, str):
        if any(result.startswith(p) for p in _SCRIPT_FAIL_PREFIXES):
            return True
        return any(s in result for s in _SCRIPT_FAIL_SUBSTRINGS)
    return False


def _script_impl(
    steps: list[dict[str, Any]],
    *,
    dispatch: dict[str, Callable[..., Any]] | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    if dispatch is None:
        dispatch = _SCRIPT_DISPATCH
    results: list[dict[str, Any]] = []
    stopped_at: int | None = None
    for idx, step in enumerate(steps):
        tool_name = step.get("tool")
        args = step.get("args", {})
        continue_on_error = bool(step.get("continue_on_error", False))
        wait_ms_after = step.get("wait_ms_after", 0)
        if not isinstance(args, dict):
            results.append({
                "step": idx, "tool": tool_name, "ok": False,
                "error": "args must be a dict",
            })
            stopped_at = idx
            break
        fn = dispatch.get(tool_name)
        if fn is None:
            results.append({
                "step": idx, "tool": tool_name, "ok": False,
                "error": f"unknown tool: {tool_name!r}",
            })
            stopped_at = idx
            break
        try:
            output = fn(**args)
        except TypeError as e:
            results.append({
                "step": idx, "tool": tool_name, "ok": False,
                "error": f"bad args: {e}",
            })
            stopped_at = idx
            break
        except Exception as e:
            results.append({
                "step": idx, "tool": tool_name, "ok": False,
                "error": f"{type(e).__name__}: {e}",
            })
            stopped_at = idx
            break
        failed = _step_failed(output)
        results.append({
            "step": idx, "tool": tool_name, "ok": not failed, "output": output,
        })
        if failed and not continue_on_error:
            stopped_at = idx
            break
        # Sleep only between steps; the post-step delay on the final step
        # would just delay the response to the MCP client.
        if wait_ms_after and idx < len(steps) - 1:
            sleep(wait_ms_after / 1000.0)
    return {"results": results, "stopped_at": stopped_at}


@mcp.tool()
@_with_bus_retry
def script(steps: list[dict[str, Any]]) -> dict[str, Any]:
    """Run a sequence of tool calls in one MCP round-trip.

    Each step shape:
        {"tool": "click", "args": {...}, "wait_ms_after": 200,
         "continue_on_error": false}

    Stops at the first failing step unless that step's ``continue_on_error``
    is true. Failure is detected by inspecting the tool's return value:
    dict with an ``error`` key, dict with ``found=False`` (waiters), or a
    string starting with the tool's error wording.

    Excluded tools: ``screenshot``, ``screenshot_widget`` (return Image),
    ``audit`` (large output). Call those individually.

    Returns ``{results, stopped_at}``. ``stopped_at`` is ``None`` when every
    step ran successfully, otherwise the index of the failing step.
    """
    return _script_impl(steps)


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
