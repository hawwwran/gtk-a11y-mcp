# Tier 2 — New capabilities

Three features that expand what the server can do, not just what it
exposes more nicely. Together they cover the read-side metadata that
Tier 1 left on the table, the semantic interaction interfaces beyond
"click", and a brand-new audit use case.

## 2.1 Value / Selection / Toggle interfaces

### Problem

`click` works for buttons. For sliders, spin buttons, progress meters,
combo boxes, list selections, and check boxes, the *semantic*
interfaces are Value and Selection (and the Action interface in the
case of toggle). Clicking a slider in the middle is not the same as
setting it to 0.7. Right now there's no way to do the latter at all.
Combo box selection by name is fragile because the visible list cells
appear and disappear with popups — Selection-interface select is much
more reliable.

### API

```python
@mcp.tool()
def get_value(app_name: str, role: str, name: str) -> dict[str, Any]:
    """Read AT-SPI Value: {value, minimum, maximum, minimum_increment}."""

@mcp.tool()
def set_value(app_name: str, role: str, name: str, value: float) -> str:
    """Set a slider/spinbutton/progress to a numeric value."""

@mcp.tool()
def select_item(app_name: str, parent_path: str, item_name: str) -> str:
    """Select an item by name within a container (combo box, list view).

    Uses the parent's Selection interface (more reliable than clicking
    the list cell, which may be in a transient popup).
    """

@mcp.tool()
def set_checked(app_name: str, role: str, name: str, checked: bool) -> str:
    """Set a checkbox / toggle button to a specific state.

    Reads STATE_CHECKED; if it already matches, no-op. Otherwise click.
    Idempotent, unlike a raw click which always toggles.
    """
```

### Implementation notes

- `get_value` / `set_value`: `node.queryValue()` exposes
  `.currentValue`, `.minimumValue`, `.maximumValue`,
  `.minimumIncrement`. Setter assigns to `.currentValue`.
- `select_item`: walk children of `parent_path`, find the one whose
  name matches; call
  `parent.querySelection().selectChild(child_index)`.
- `set_checked`: read STATE_CHECKED, compare with desired; if mismatch,
  invoke the default Action.
- All four reuse the existing `_walk_match` + `_resolve_path` flow for
  resolving widgets.

### Tests

- `test_get_value_returns_value_min_max` — mock node with `queryValue`
  → record.
- `test_set_value_assigns_currentValue` — mock Value object; verify
  attribute was set.
- `test_set_value_returns_error_if_no_value_interface` — `NotImplementedError`.
- `test_select_item_calls_selection_on_parent` — mock parent's
  `querySelection().selectChild(i)`; verify i matches the child index.
- `test_select_item_returns_error_if_item_not_found`
- `test_set_checked_noop_when_state_already_matches` — mock node with
  STATE_CHECKED present; calling `set_checked(..., True)` does NOT
  invoke Action.
- `test_set_checked_clicks_when_state_mismatch` — STATE_CHECKED absent
  with desired=True → Action invoked.

---

## 2.2 accessible-id / description / attributes surfacing

### Problem

`name` (the visible label) is locale-dependent and changes when the
designer reworks copy. GTK4 / libadwaita exposes a stable
`accessible-id` (set by the app developer, usually constant), a
`description` (often the tooltip text), and an `attributes` dict (app
hints). None of these are surfaced. Agents end up keying on visible
labels, which is the most brittle option.

### API

`find_widgets` and `dump_tree` results gain three new fields per node:
- `accessible_id` — parsed from `getAttributes()` (key `id`)
- `description` — `node.description` (AT-SPI string field)
- `attributes` — `getAttributes()` as a `dict[str, str]`
  (excluding the `id` key which is already its own field)

`find_widgets` gains two new filter kwargs:
- `accessible_id: str | None` — exact match
- `description: str | None` — substring match (same convention as `name`)

New tool:

```python
@mcp.tool()
def get_attributes(app_name: str, role: str, name: str) -> dict[str, str]:
    """Return the AT-SPI Attributes dict for a widget."""
```

`click`, `get_text`, `type_text`, etc. gain an optional
`accessible_id` kwarg as an alternative selector — when present, it's
used in lieu of `name`. (Keeping `name` working for backwards
compatibility with existing agent scripts.)

### Implementation notes

- `_get_attributes(node) -> dict[str, str]`: AT-SPI returns a list of
  `"key:value"` strings; parse each on the first `:`. Tolerate missing
  `:` (rare) by keying the whole string with empty value.
- `_get_accessible_id(attrs: dict) -> str | None`: just `attrs.pop("id", None)`.
- `_walk_match` builds these into result dicts.
- `dump_tree` keeps lines terse — append `#<id>` after the name if an
  accessible_id is present:
  `push button 'Save' #save-button [focused]`
- The `accessible_id` selector goes into a small refactor: replace the
  inline `_walk_match(..., name=name)` call with `_resolve_widget(app,
  role, name=None, accessible_id=None)` that picks whichever was given.

### Tests

- `test_get_attributes_parses_key_value_strings` — `["id:foo",
  "level:1"]` → `{"id": "foo", "level": "1"}`.
- `test_get_attributes_tolerates_no_colon` — `["weird"]` → `{"weird": ""}`.
- `test_find_widgets_filters_by_accessible_id` — exact match.
- `test_find_widgets_filters_by_description` — substring match.
- `test_dump_tree_appends_accessible_id_marker` — `#save-button`.
- `test_click_by_accessible_id_resolves_widget` — give `accessible_id`,
  no `name`.
- `test_walk_match_includes_description_in_result_dicts`.

---

## 2.3 Accessibility audit

### Problem

The infrastructure for walking an AT-SPI tree is right there; turning
it into an a11y linter is a small step and a big audience expansion.
GTK app maintainers want a way to find a11y bugs without manually
clicking through with Orca; AI agents reviewing a UI want the same
info to understand what's interactive and what isn't.

### API

```python
@mcp.tool()
def audit(
    app_name: str,
    rules: list[str] | None = None,
    max_issues: int = 200,
) -> dict[str, Any]:
    """Walk the AT-SPI tree and report accessibility issues.

    rules: list of rule IDs to enable. None = all rules.
    Returns: {issues: [{path, role, rule, severity, message}, ...],
              checked_widgets: int, rules_run: [...]}
    """
```

### Rules (v1)

| ID | Severity | Message |
|---|---|---|
| `empty-name-actionable` | error | Push button / menu item / link with empty `name` and empty `description` |
| `name-equals-role` | warning | `name == role` (e.g. button named "button") — likely placeholder |
| `name-only-whitespace` | error | `name` is non-empty but only whitespace |
| `clickable-without-action` | warning | Role is one of `{push button, link, menu item, check box, radio button, toggle button}` but no Action interface |
| `focusable-not-focusable` | warning | Has `STATE_FOCUSED` ever observed but lacks `STATE_FOCUSABLE` (not detectable from a single snapshot; we'll detect `STATE_FOCUSABLE` absent on actionable widgets instead) |
| `editable-without-label` | error | `STATE_EDITABLE` widget with empty `name` AND no `description` AND no `labelled-by` relation |
| `image-without-description` | warning | Role `image` with empty `name` and empty `description` |
| `duplicate-accessible-id` | error | Two widgets share the same `accessible-id` within one app |

(Final list may shrink in implementation if a rule is too noisy or too
unreliable to fire correctly — but the design is rule-based so adding
or removing rules later is mechanical.)

### Implementation notes

- Single tree walk that runs every enabled rule against every node.
- Each rule is `Callable[[NodeInfo], Issue | None]` where `NodeInfo`
  bundles `path, role, name, description, accessible_id, states,
  attributes, has_action, relations`. The walker builds NodeInfo once
  per node; the rules are cheap predicates.
- `duplicate-accessible-id` is post-pass over a `dict[str, list[path]]`
  built during the walk.
- `clickable-without-action` requires checking
  `node.queryAction()` — wrap in `try: ... except NotImplementedError`.
- `labelled-by` relation: `node.getRelationSet()` returns a list of
  Relation objects; look for `pyatspi.RELATION_LABELLED_BY`.
- Severity: keep it simple — `error` or `warning` strings.
- Performance: the cap (`max_issues`) lets us short-circuit on
  pathological trees.

### Tests

- For each rule: build a small mock tree that should trigger it, and
  one that shouldn't. Assert the audit picks up exactly the offending
  node(s).
- `test_audit_runs_only_specified_rules` — when `rules=["empty-name-actionable"]`,
  the result's `rules_run` is just that, and other potential issues
  are skipped.
- `test_audit_dupe_accessible_id_post_pass` — two widgets with the
  same `accessible_id` produce one duplicate-id issue each.
- `test_audit_respects_max_issues` — stop at the cap, return how many
  were actually checked.

---

## Sequencing

Implement in this order:

1. **accessible-id / description / attributes** first — the audit rules
   will use these.
2. **Value / Selection / Toggle** — independent, drops into the same
   `_walk_match` resolution.
3. **Audit** — biggest single feature; depends on (1) for `accessible-id`
   and `description` already being plumbed.
