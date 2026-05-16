# Tier 3 — Speculative / niche

Three smaller wins. None of them solves a blocker the way Tier 1 does,
but each removes a class of agent frustration.

## 3.1 `wait_for_state`

### Problem

`wait_for_widget` waits for a widget to **exist**. Plenty of flows need
to wait for a widget that already exists to change *state* — e.g. wait
for the "Save" button to become sensitive after the form passes
validation, or for a checkbox to flip to CHECKED after a model update.

### API

```python
@mcp.tool()
def wait_for_state(
    app_name: str,
    role: str,
    name: str,
    state: str,
    present: bool = True,
    timeout_s: float = 10.0,
    poll_ms: int = 250,
) -> dict[str, Any]:
    """Block until the named widget's state matches.

    state: AT-SPI state name (e.g. "sensitive", "checked").
    present: True = wait for state to appear; False = wait for it to vanish.
    Returns: {found, satisfied, elapsed_ms, path, states}
    """
```

### Implementation notes

- Polling, **not** event subscription. Event subscription via
  `pyatspi.Registry.registerEventListener` exists but requires running
  a GLib main loop in our thread, which fights with MCP's request/
  response model. Polling at 100–500 ms is fine in practice and keeps
  the code simple.
- Reuse `_wait_until` (already polls with mockable clock).
- Resolve widget once at the start; if it disappears later, treat as
  `found=False`.

### Tests

- `test_wait_for_state_returns_when_state_appears` — fake state set
  starts without `checked`, gains it on poll 3.
- `test_wait_for_state_returns_when_state_vanishes_with_present_false`
- `test_wait_for_state_times_out` — predicate never satisfied.
- `test_wait_for_state_returns_not_found_if_widget_missing`.

---

## 3.2 `script` — multi-step batch

### Problem

A predictable flow ("type a username, type a password, click sign in,
wait for the dashboard") takes 4–5 MCP round-trips. Each round-trip is
~50–200ms of overhead plus an LLM token round. For agents running
known scripts (regression tests, repeated setup steps), batching them
in one call is a big speedup.

### API

```python
@mcp.tool()
def script(steps: list[dict[str, Any]]) -> dict[str, Any]:
    """Run a sequence of tool calls in one MCP round-trip.

    Each step:
      {"tool": "type_text", "args": {...}, "wait_ms_after": 200}
      {"tool": "wait_for_widget", "args": {...}}  # wait_ms_after optional

    Returns:
      {results: [{step: i, tool: "...", ok: bool, output: ...}], stopped_at: int | None}

    On first failure (any tool that returns a string starting with
    "No app" / "Couldn't resolve" / "Ambiguous" / "...failed"), or the
    first wait_* with found=False, execution halts unless step has
    "continue_on_error": true.
    """
```

### Implementation notes

- Dispatch table maps tool name to the underlying Python function
  (not the MCP wrapper — call the impl directly).
- Each step's `args` is `**kwargs` to the underlying function. We need
  to be defensive: validate the tool name is in our allowlist, validate
  args keys are valid kwargs (otherwise pass-through could expose
  unintended params).
- `wait_ms_after`: `time.sleep(ms/1000)` between steps, optional.
- Failure detection: agents currently signal failure via the return
  string format. Build a small set of failure detectors (regex /
  startswith) keyed by tool. Don't re-engineer the entire error model.
- Returned `output` is whatever the tool returned. For `screenshot` /
  `screenshot_widget` results (Image objects), embed only metadata
  (size in bytes, no payload) — the agent should use those tools
  individually if they need the image.

### Tests

- `test_script_runs_steps_in_order` — three steps, all succeed, order
  preserved.
- `test_script_halts_on_first_failure` — second step fails; third is
  not run.
- `test_script_continues_on_error_when_flagged`.
- `test_script_rejects_unknown_tool_name`.
- `test_script_passes_args_as_kwargs_to_underlying_fn`.
- `test_script_calls_sleep_for_wait_ms_after` (mock sleep).

---

## 3.3 Bus reconnect resilience

### Problem

`getDesktop(0)` on a stale connection can raise `dbus.exceptions.
DBusException`. AT-SPI bus restarts are rare but happen (e.g. after a
display-server restart, an `at-spi2-registryd` crash, a long-lived
MCP server outliving a user session). One spurious error from the
underlying bus is currently unrecoverable for the rest of the session.

### API

No new MCP tools. Internal-only:

```python
def _with_bus_retry(fn: Callable[[], T], retries: int = 1) -> T:
    """Run fn; on DBusException, force pyatspi to reconnect and retry."""
```

Wrap every entry point that touches `pyatspi` (`list_apps`,
`find_widgets`, `dump_tree`, `click`, `get_text`, `type_text`,
`status`, `wait_for_app`, `wait_for_widget`, plus everything new from
Tier 1 & 2) in `_with_bus_retry`.

### Implementation notes

- "Force reconnect" in pyatspi: rebind the Registry. Easiest is
  `importlib.reload(pyatspi)` — heavy but bulletproof. Lighter:
  `pyatspi.Registry = pyatspi.registry.Registry()` (re-instantiates).
  Pick the lighter one if it works; otherwise reload module.
- Only retry on `dbus.exceptions.DBusException` (or its subclasses).
  Don't retry on widget-resolution errors — those mean the agent's
  intent is wrong, not the bus.
- The retry path must not loop forever: hard cap of 1 retry.
- Log to stderr when a retry happens so it's visible in MCP server
  logs (FastMCP forwards stderr to the host).

### Tests

- `test_with_bus_retry_passes_through_on_success`.
- `test_with_bus_retry_retries_once_on_dbus_exception` — first call
  raises DBusException, second succeeds; result is from second call.
- `test_with_bus_retry_re_raises_after_max_retries` — both calls fail;
  exception propagates.
- `test_with_bus_retry_does_not_retry_on_non_dbus_exception` —
  TypeError surfaces immediately, no second attempt.

---

## Sequencing

Independent of each other; implement in plan order — wait_for_state
first (smallest), then script (depends on knowing which tools exist
and what they return — easier after Tiers 1 & 2 are in), then bus
resilience (touches everything else, so do it last).
