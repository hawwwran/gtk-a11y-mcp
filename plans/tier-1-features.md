# Tier 1 — Highest leverage, smallest surface

Four additions that give an AI agent driving a GTK app the information and
input primitives it currently doesn't have. Order is implementation order —
states unblock the rest by giving us a way to assert "this is now enabled
/ focused / checked" in tests for the later features.

## 1.1 Widget state surfacing in `dump_tree` and `find_widgets`

### Problem

`dump_tree` and `find_widgets` only emit `role` and `name`. AT-SPI also
exposes a `StateSet` per node — `FOCUSED`, `SENSITIVE` (i.e. enabled),
`CHECKED`, `SELECTED`, `EXPANDED`, `PRESSED`, `EDITABLE`, `SHOWING`,
`VISIBLE`, etc. The agent can't currently tell a greyed-out button from
an active one, or read a checkbox's value, without a screenshot. That's
the single largest blindspot in the read-side API.

### API

New helper:

```python
def _get_states(node) -> list[str]:
    """Return state names (lowercase) for a node, [] if unavailable."""
```

Uses `pyatspi.stateToString` per state in `node.getState().getStates()`.

`dump_tree` lines change format to:

```
role 'name' [focused, checked]
role 'disabled-name' [disabled]
panel ''                      # no interesting states -> no suffix
```

The tree dump uses a **curated set** of states to stay readable:

- Positive states shown when present: `focused`, `checked`, `selected`,
  `expanded`, `pressed`
- Pseudo-state `disabled` shown when `sensitive` is **absent**
- All other states omitted (visible/showing/opaque are noise)

`find_widgets` result dicts gain a `states` key with the **full** state
list — agents that need the noisy states can still filter on them.

`find_widgets` gains an optional kwarg:

```python
def find_widgets(app_name, role=None, name=None, state=None) -> list[dict]
```

`state` is a single state-name string (e.g. `"focused"`). Filter is
"node has this state". For "must NOT have", prefix with `!` (e.g.
`!sensitive` → disabled widgets only).

### Implementation notes

- `_walk_match` already returns dicts; just attach `"states"` to each.
- `_walk_tree` formats the tree-dump-curated subset.
- The negative `!sensitive` filter is the only special case — keep it
  simple, no full predicate language.
- Mock nodes in tests need a `getState()` returning an object with
  `.getStates()` → list of state-name strings (we'll mock `stateToString`
  by making the mock state list already contain strings, and abstract
  the state-name lookup behind a helper that takes a state-set object).

### Tests

- `test_get_states_returns_state_names` — basic readout from a mock.
- `test_walk_tree_shows_curated_states` — tree dump includes `[focused]`.
- `test_walk_tree_shows_disabled_when_not_sensitive` — pseudo-state.
- `test_walk_tree_omits_states_when_none_interesting` — clean nodes show
  only `role 'name'`.
- `test_walk_match_attaches_full_states_list` — find_widgets dict has
  full state list.
- `test_walk_match_filters_by_state` — positive filter.
- `test_walk_match_filters_by_negated_state` — `!sensitive` returns only
  disabled widgets.

---

## 1.2 Keyboard input: `focus` and `press_keys`

### Problem

Some flows can't be expressed as widget clicks: `Ctrl+S` in a text
editor, `Tab` to traverse a form, `Esc` to dismiss a dialog,
arrow-key navigation in a list/tree view, accelerators in menus.
Currently the only way to drive these is to find an equivalent widget
to click, which often doesn't exist.

### API

```python
@mcp.tool()
def focus(app_name: str, role: str, name: str) -> str:
    """Move keyboard focus to a widget. Returns "Focused: <path>" or error."""

@mcp.tool()
def press_keys(keys: str) -> dict:
    """Synthesize key events at the session level (no per-app routing).

    keys: a key spec. Supports:
      - Single key: "Return", "Escape", "Tab", "Up", "F5", "a"
      - Modifier combo: "Ctrl+S", "Ctrl+Shift+T", "Alt+F4"
      - Sequence (space-separated tokens, each processed in order):
          "Ctrl+a Delete Tab"

    Returns: {sent: <list of tokens>, backend: "atspi"|"ydotool"}.
    """
```

`focus` is widget-scoped (uses AT-SPI Component.grabFocus). `press_keys`
is session-scoped (AT-SPI keyboard injection has no target — whoever has
focus receives it). Typical flow: `focus(...)` then `press_keys(...)`.

### Implementation notes

- `_parse_key_spec(spec)` → list of `(modifiers: list[str], key: str)`
  tuples. Tokens split on whitespace; each token split on `+`.
  Modifiers normalized to lowercase: `ctrl`, `shift`, `alt`, `super`.
- Two backends, tried in order:
  1. **AT-SPI** via `pyatspi.Registry.generateKeyboardEvent`. For each
     token, press modifiers (KEY_PRESS), press key
     (KEY_PRESSRELEASE), release modifiers (KEY_RELEASE). Keysym names
     follow X11 conventions (`Return`, `Escape`, `Control_L`).
  2. **ydotool** fallback when AT-SPI errors out (Wayland compositors
     often refuse the AT-SPI keyboard event). Map our key spec to
     ydotool's `key` syntax: `ydotool key ctrl+s` (lowercase, `+`-
     separated, name maps).
- `focus` calls `node.queryComponent().grabFocus()`. If Component
  interface unavailable, return an error string the same shape as other
  tools.
- Wayland caveat: even AT-SPI keyboard injection may be ignored by
  `gnome-shell`. The ydotool path needs `ydotoold` running and the user
  in the `input` group. Surface both backends in the returned dict so
  the caller can debug.

### Tests

- `test_parse_key_spec_single_key` — `"Tab"` → `[([], "Tab")]`
- `test_parse_key_spec_modifier_combo` — `"Ctrl+Shift+T"` →
  `[(["ctrl","shift"], "T")]`
- `test_parse_key_spec_sequence` — `"Ctrl+a Delete"` → two tuples
- `test_parse_key_spec_normalizes_modifier_case` — `"CTRL+S"` →
  `(["ctrl"], "S")`
- `test_press_keys_dispatches_to_atspi_first` — injected fake-backend
  records the call.
- `test_press_keys_falls_back_to_ydotool_on_atspi_failure` — fake
  AT-SPI raises; fake ydotool runs.
- `focus` is integration-flavored; cover via the existing mock-node
  pattern: mock `queryComponent().grabFocus()` and check the call.

---

## 1.3 `launch_app`

### Problem

Every interaction starts with the user shelling out
`GTK_A11Y=atspi <command>` from a separate terminal, then calling
`wait_for_app`. The MCP server has all the pieces to do this itself.

### API

```python
@mcp.tool()
def launch_app(
    command: str,
    args: list[str] | None = None,
    wait_seconds: float = 10.0,
    app_name_match: str | None = None,
    extra_env: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Launch a GTK app with GTK_A11Y=atspi set; optionally wait for it on the bus.

    Returns:
      {pid, found_on_bus, elapsed_ms, app_name, hint?}

    If app_name_match is None and wait_seconds > 0, polls list_apps for a
    new app to appear and matches by pid via the AT-SPI BusName mapping.
    Simpler v1: only block when app_name_match is provided.
    """
```

### Implementation notes

- `subprocess.Popen([command, *args], env={**os.environ, "GTK_A11Y": "atspi", **(extra_env or {})}, stdout=DEVNULL, stderr=DEVNULL, start_new_session=True)`.
- **No shell=True** — pass `command` and `args` as a list to avoid
  shell injection.
- Detach with `start_new_session=True` so the launched app survives the
  MCP server's process group.
- When `app_name_match` is given, reuse `_wait_until` + `_find_app` —
  same pattern as `wait_for_app`.
- If `app_name_match` is `None`, return the pid immediately without
  blocking.

### Tests

- `test_launch_app_uses_GTK_A11Y_env` — mock `subprocess.Popen`, verify
  env passed contains `GTK_A11Y=atspi`.
- `test_launch_app_passes_args_as_list_not_shell` — `subprocess.Popen`
  called with a list, no `shell=True`.
- `test_launch_app_returns_pid_immediately_when_no_match` — no
  polling.
- `test_launch_app_polls_for_app_name_match` — inject fake `_find_app`
  that returns `None` then a node; verify polling exited as soon as
  the node appeared.
- `test_launch_app_returns_not_found_on_timeout` — fake `_find_app`
  always returns None.
- `test_launch_app_merges_extra_env` — extra_env overrides

---

## 1.4 `screenshot_widget`

### Problem

`screenshot(window_only=True)` returns the whole window. If the agent
wants to see a specific button, the image is ~100x larger than needed
and the LLM has to find the widget visually in the screenshot too.

### API

```python
@mcp.tool()
def screenshot_widget(
    app_name: str,
    role: str,
    name: str,
    padding_px: int = 4,
) -> Image:
    """Screenshot of a single widget, cropped to its AT-SPI extents."""
```

### Implementation notes

- Resolve the widget via the existing `_walk_match` → `_resolve_path`
  pipeline.
- Query Component interface: `comp = node.queryComponent()`,
  `extents = comp.getExtents(pyatspi.DESKTOP_COORDS)`. Result is a
  `BoundingBox(x, y, width, height)`.
- Capture full-screen with `gnome-screenshot -f /tmp/...`.
- Open with PIL, crop to `(x - pad, y - pad, x + w + pad, y + h + pad)`,
  re-encode as PNG.
- Return `Image(data=..., format="png")` (mcp.server.fastmcp.Image).
- If the widget has zero-area extents (offscreen/hidden), return an
  error-shaped image? No — return a string error instead, like other
  tools. (`Image` type means we have to return Image; use a tiny 1x1
  image with the error in the alt? cleaner: change the signature to
  return `Image | str` … MCP tools return JSON-encodable; we'll return
  Image on success and raise `RuntimeError` on failure, which fastmcp
  turns into a JSON error.)
- Pillow is already on PATH (`PIL` 10.x). Add it to optional deps in
  pyproject.

### Tests

- `test_screenshot_widget_crops_with_padding` — mock
  `_capture_full_screen` to return a known image and mock
  `_widget_extents` to return `(100, 200, 50, 30)`; verify the returned
  bytes are a PNG of the right size.
- `test_screenshot_widget_zero_area_raises` — extents of (0,0,0,0)
  raises RuntimeError.
- `test_screenshot_widget_resolves_widget_via_role_name` — verify the
  same widget-resolution path as other tools.

---

## Sequencing

Implement in this order so each step's tests can build on the previous:

1. **States** first — adds the `states` field everywhere; nothing else
   depends on it yet but it's the cheapest win and exercises the test
   pattern we'll reuse.
2. **`focus` + `press_keys`** — independent of states.
3. **`launch_app`** — independent.
4. **`screenshot_widget`** — independent.

Each step ships with its tests, full pytest run is green before the
next step starts.
