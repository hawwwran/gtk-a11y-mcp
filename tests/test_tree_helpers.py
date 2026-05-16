"""Unit tests for the tree-walking helpers using mock AT-SPI nodes."""

from __future__ import annotations

from gtk_a11y_mcp.server import _resolve_path, _walk_match, _walk_tree


class MockStateSet:
    """Stand-in for a pyatspi StateSet: getStates() returns names directly."""

    def __init__(self, states: list[str]):
        self._states = list(states)

    def getStates(self) -> list[str]:
        return list(self._states)


class MockValue:
    def __init__(self, current: float, minimum: float, maximum: float, increment: float = 0.0):
        self.currentValue = current
        self.minimumValue = minimum
        self.maximumValue = maximum
        self.minimumIncrement = increment


class MockSelection:
    def __init__(self):
        self.selections: list[int] = []

    def selectChild(self, i: int) -> None:
        self.selections.append(i)


class MockAction:
    def __init__(self):
        self.calls: list[int] = []

    def doAction(self, i: int) -> None:
        self.calls.append(i)


class MockComponent:
    """Records grabFocus calls and lets tests pick a return value."""

    def __init__(self, grab_focus_returns: bool = True):
        self.grab_focus_calls = 0
        self._returns = grab_focus_returns

    def grabFocus(self):
        self.grab_focus_calls += 1
        return self._returns


class MockNode:
    """Minimal stand-in for a pyatspi Accessible: getRoleName(), name, childCount, getChildAtIndex()."""

    def __init__(
        self,
        role: str,
        name: str = "",
        children: list["MockNode"] | None = None,
        states: list[str] | None = None,
        attributes: list[str] | None = None,
        description: str = "",
        value: MockValue | None = None,
        selection: MockSelection | None = None,
        action: MockAction | None = None,
        component: MockComponent | None = None,
    ):
        self._role = role
        self.name = name
        self._children = children or []
        self.childCount = len(self._children)
        self._states = states
        self._attributes = attributes
        self.description = description
        self._value = value
        self._selection = selection
        self._action = action
        self._component = component

    def getRoleName(self) -> str:
        return self._role

    def getChildAtIndex(self, i: int) -> "MockNode":
        return self._children[i]

    def getState(self):
        if self._states is None:
            raise AttributeError("no state set on this mock")
        return MockStateSet(self._states)

    def getAttributes(self):
        if self._attributes is None:
            return []
        return list(self._attributes)

    def queryValue(self):
        if self._value is None:
            raise NotImplementedError("no Value interface on this mock")
        return self._value

    def querySelection(self):
        if self._selection is None:
            raise NotImplementedError("no Selection interface on this mock")
        return self._selection

    def queryAction(self):
        if self._action is None:
            raise NotImplementedError("no Action interface on this mock")
        return self._action

    def queryComponent(self):
        if self._component is None:
            raise NotImplementedError("no Component interface on this mock")
        return self._component

    def getRelationSet(self):
        # Default: no relations. Subclasses or wrappers can override.
        return getattr(self, "_relations", []) or []


def _sample_tree() -> MockNode:
    return MockNode(
        "application", "Settings",
        children=[
            MockNode(
                "frame", "Settings",
                children=[
                    MockNode("push button", "Save"),
                    MockNode("push button", "Cancel"),
                    MockNode(
                        "panel", "",
                        children=[
                            MockNode("text", "URL", children=[]),
                            MockNode("push button", "Save"),
                        ],
                    ),
                ],
            ),
        ],
    )


def test_walk_tree_renders_indented_tree():
    lines: list[str] = []
    _walk_tree(_sample_tree(), 0, 5, lines)
    assert lines[0] == "application 'Settings'"
    assert "  frame 'Settings'" in lines
    assert "    push button 'Save'" in lines
    assert "    push button 'Cancel'" in lines


def test_walk_tree_respects_max_depth():
    lines: list[str] = []
    _walk_tree(_sample_tree(), 0, 1, lines)
    # depth 0 = application, depth 1 = frame; push buttons (depth 2) excluded
    assert any("frame" in line for line in lines)
    assert not any("push button" in line for line in lines)


def test_walk_match_filters_by_role():
    results: list = []
    _walk_match(_sample_tree(), role="push button", name=None, results=results)
    assert len(results) == 3
    assert all(r["role"] == "push button" for r in results)


def test_walk_match_filters_by_name_substring():
    results: list = []
    _walk_match(_sample_tree(), role=None, name="save", results=results)
    assert len(results) == 2
    assert all("save" in r["name"].lower() for r in results)


def test_walk_match_role_and_name():
    results: list = []
    _walk_match(_sample_tree(), role="push button", name="cancel", results=results)
    assert len(results) == 1
    assert results[0]["name"] == "Cancel"


def test_walk_match_paths_are_unique_and_traceable():
    results: list = []
    _walk_match(_sample_tree(), role="push button", name="save", results=results)
    paths = [r["path"] for r in results]
    assert len(paths) == len(set(paths))
    # Top-level Save vs nested Save have distinct paths
    assert any("/panel" in p for p in paths)
    assert any("/panel" not in p[len("application[Settings]/frame[Settings]"):] for p in paths)


def test_walk_match_max_results_cap():
    results: list = []
    _walk_match(_sample_tree(), role=None, name=None, results=results, max_results=2)
    assert len(results) == 2


def test_resolve_path_finds_top_level():
    tree = _sample_tree()
    node = _resolve_path(tree, "application[Settings]/frame[Settings]/push button[Save]")
    assert node is not None
    assert node.name == "Save"


def test_resolve_path_finds_nested():
    tree = _sample_tree()
    target = "application[Settings]/frame[Settings]/panel[]/push button[Save]"
    node = _resolve_path(tree, target)
    assert node is not None
    assert node.name == "Save"


def test_resolve_path_missing_returns_none():
    assert _resolve_path(_sample_tree(), "application[Settings]/frame[Other]") is None


def test_walk_match_handles_node_without_name():
    tree = MockNode(
        "frame", "",
        children=[MockNode("push button", "OK")],
    )
    results: list = []
    _walk_match(tree, role="push button", name="ok", results=results)
    assert len(results) == 1
    assert results[0]["path"] == "frame[]/push button[OK]"


# ---------------------------------------------------------------------------
# State surfacing: _get_states, dump_tree formatting, find_widgets filter
# ---------------------------------------------------------------------------

from gtk_a11y_mcp.server import _format_state_suffix, _get_states, _state_filter_matches


def test_get_states_returns_names_for_mock_node():
    node = MockNode("push button", "Save", states=["focused", "sensitive", "showing"])
    assert _get_states(node) == ["focused", "sensitive", "showing"]


def test_get_states_returns_empty_when_no_state_set():
    node = MockNode("push button", "Save")  # no states arg -> getState raises
    assert _get_states(node) == []


def test_format_state_suffix_shows_curated_positive_states():
    suffix = _format_state_suffix(["focused", "sensitive", "showing", "visible"], "push button")
    assert suffix == " [focused]"


def test_format_state_suffix_shows_disabled_for_actionable_role():
    suffix = _format_state_suffix(["showing", "visible"], "push button")
    assert suffix == " [disabled]"


def test_format_state_suffix_combines_checked_and_disabled():
    suffix = _format_state_suffix(["checked", "showing"], "check box")
    assert suffix == " [checked, disabled]"


def test_format_state_suffix_no_disabled_for_passive_container():
    # Panels / frames are not actionable; missing `sensitive` is noise.
    suffix = _format_state_suffix(["showing", "visible"], "panel")
    assert suffix == ""


def test_format_state_suffix_empty_when_only_passive_states():
    # Even an actionable role: sensitive present + no positives = quiet.
    suffix = _format_state_suffix(["sensitive", "showing", "visible"], "push button")
    assert suffix == ""


def test_walk_tree_includes_state_suffix_when_states_present():
    tree = MockNode(
        "frame", "Settings",
        states=["sensitive", "showing"],
        children=[
            MockNode("push button", "Save", states=["focused", "sensitive", "showing"]),
            MockNode("push button", "Cancel", states=["showing", "visible"]),  # disabled
        ],
    )
    lines: list[str] = []
    _walk_tree(tree, 0, 5, lines)
    save_line = next(l for l in lines if "Save" in l)
    cancel_line = next(l for l in lines if "Cancel" in l)
    assert save_line.endswith("[focused]")
    assert cancel_line.endswith("[disabled]")


def test_walk_tree_omits_suffix_when_node_has_no_state_set():
    # Mock without states arg has no getState() — _get_states returns [].
    tree = MockNode("frame", "Settings", children=[MockNode("push button", "OK")])
    lines: list[str] = []
    _walk_tree(tree, 0, 5, lines)
    assert lines == ["frame 'Settings'", "  push button 'OK'"]


def test_walk_match_attaches_full_states_list():
    tree = MockNode(
        "frame", "",
        children=[MockNode("push button", "Save",
                           states=["focused", "sensitive", "showing", "visible"])],
    )
    results: list = []
    _walk_match(tree, role="push button", name="save", results=results)
    assert results[0]["states"] == ["focused", "sensitive", "showing", "visible"]


def test_walk_match_attaches_empty_states_when_no_state_set():
    tree = MockNode("frame", "", children=[MockNode("push button", "OK")])
    results: list = []
    _walk_match(tree, role="push button", name=None, results=results)
    assert results[0]["states"] == []


def test_state_filter_matches_positive():
    assert _state_filter_matches("focused", ["focused", "showing"]) is True
    assert _state_filter_matches("focused", ["showing"]) is False


def test_state_filter_matches_negated():
    # disabled = not sensitive
    assert _state_filter_matches("!sensitive", ["focused", "showing"]) is True
    assert _state_filter_matches("!sensitive", ["sensitive", "showing"]) is False


def test_state_filter_matches_case_insensitive():
    assert _state_filter_matches("FOCUSED", ["focused"]) is True
    assert _state_filter_matches("!SENSITIVE", ["showing"]) is True


def test_state_filter_none_matches_everything():
    assert _state_filter_matches(None, []) is True
    assert _state_filter_matches(None, ["whatever"]) is True


def test_walk_match_filters_by_state():
    tree = MockNode(
        "frame", "",
        children=[
            MockNode("push button", "Save", states=["focused", "sensitive"]),
            MockNode("push button", "Cancel", states=["sensitive"]),
        ],
    )
    results: list = []
    _walk_match(tree, role="push button", name=None, results=results, state="focused")
    assert len(results) == 1
    assert results[0]["name"] == "Save"


def test_walk_match_filters_by_negated_state_finds_disabled():
    tree = MockNode(
        "frame", "",
        children=[
            MockNode("push button", "Save", states=["sensitive"]),
            MockNode("push button", "GreyedOut", states=["showing"]),
        ],
    )
    results: list = []
    _walk_match(tree, role="push button", name=None, results=results, state="!sensitive")
    assert len(results) == 1
    assert results[0]["name"] == "GreyedOut"


class _CallCountingMockNode(MockNode):
    """Mock that counts how many times each bus-style method is invoked,
    so tests can prove _walk_match short-circuits on filter rejection."""

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.calls = {"attrs": 0, "desc": 0, "state": 0}

    def getAttributes(self):
        self.calls["attrs"] += 1
        return super().getAttributes()

    @property
    def description(self):
        return self.__dict__.get("_desc_val", "")

    @description.setter
    def description(self, value):
        self.__dict__["_desc_val"] = value

    # Override _get_description's getattr path: keep description simple, but
    # also expose a callable for read-counting.
    def getState(self):
        self.calls["state"] += 1
        return super().getState()


def test_walk_match_short_circuits_on_id_mismatch_without_reading_description_or_states():
    # Build a child that fails the id filter; description and state reads
    # should NOT happen for it.
    child = _CallCountingMockNode(
        "push button", "Save",
        attributes=["id:wrong-id"],
        description="Some tooltip",
        states=["focused", "sensitive"],
    )
    tree = MockNode("application", "A", children=[child])
    results: list = []
    _walk_match(
        tree, role="push button", name=None, results=results,
        accessible_id="right-id",
    )
    assert results == []
    # Attributes were read once (to learn the id); description and state
    # were never read because the id filter short-circuited first.
    assert child.calls["attrs"] == 1
    assert child.calls["state"] == 0


def test_walk_match_short_circuits_on_description_mismatch_without_reading_states():
    child = _CallCountingMockNode(
        "push button", "Save",
        attributes=["id:save"],
        description="Save the document",
        states=["focused", "sensitive"],
    )
    tree = MockNode("application", "A", children=[child])
    results: list = []
    _walk_match(
        tree, role="push button", name=None, results=results,
        description="nonexistent-substring",
    )
    assert results == []
    assert child.calls["state"] == 0


def test_walk_match_reads_everything_when_no_filters_set_and_match_succeeds():
    # No filters -> every node that matches role/name produces a full
    # result dict, which requires reading attrs, description, states.
    child = _CallCountingMockNode(
        "push button", "Save",
        attributes=["id:save"],
        description="Save",
        states=["focused", "sensitive"],
    )
    tree = MockNode("application", "A", children=[child])
    results: list = []
    _walk_match(tree, role="push button", name=None, results=results)
    assert len(results) == 1
    assert child.calls["attrs"] == 1
    assert child.calls["state"] == 1


# ---------------------------------------------------------------------------
# _wait_until: predicate polling with mocked clock
# ---------------------------------------------------------------------------

from gtk_a11y_mcp.server import _screen_locked_dbus, _wait_until


class FakeClock:
    """Mock monotonic + sleep so we can drive _wait_until deterministically."""

    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds

    def monotonic(self) -> float:
        return self.now


def test_wait_until_returns_first_truthy_predicate_value():
    clock = FakeClock()
    calls = iter([None, None, "found-it"])
    value, elapsed_ms = _wait_until(
        lambda: next(calls),
        timeout_s=10.0,
        poll_ms=250,
        sleep=clock.sleep,
        monotonic=clock.monotonic,
    )
    assert value == "found-it"
    # Two sleeps before success; each interval = 0.25 s -> ~500 ms elapsed.
    assert clock.sleeps == [0.25, 0.25]
    assert 400 <= elapsed_ms <= 600


def test_wait_until_returns_falsy_on_timeout_without_sleeping_past_deadline():
    clock = FakeClock()
    value, elapsed_ms = _wait_until(
        lambda: None,
        timeout_s=1.0,
        poll_ms=250,
        sleep=clock.sleep,
        monotonic=clock.monotonic,
    )
    assert value is None
    # Deadline is 1.0 s; we should not sleep again past that.
    assert sum(clock.sleeps) <= 1.0 + 1e-6
    assert elapsed_ms >= 1000.0


def test_wait_until_short_circuits_when_predicate_truthy_first_call():
    clock = FakeClock()
    value, elapsed_ms = _wait_until(
        lambda: True,
        timeout_s=5.0,
        poll_ms=250,
        sleep=clock.sleep,
        monotonic=clock.monotonic,
    )
    assert value is True
    assert clock.sleeps == []
    assert elapsed_ms == 0.0


# ---------------------------------------------------------------------------
# _screen_locked_dbus: parses gdbus output via injected runner
# ---------------------------------------------------------------------------


def test_screen_locked_returns_true_when_gdbus_says_true():
    assert _screen_locked_dbus(runner=lambda _cmd: "(true,)\n") is True


def test_screen_locked_returns_false_when_gdbus_says_false():
    assert _screen_locked_dbus(runner=lambda _cmd: "(false,)\n") is False


def test_screen_locked_returns_none_when_gdbus_unavailable():
    def boom(_cmd):
        raise FileNotFoundError("no gdbus")

    assert _screen_locked_dbus(runner=boom) is None


def test_screen_locked_returns_none_when_gdbus_call_fails():
    import subprocess

    def boom(_cmd):
        raise subprocess.CalledProcessError(1, _cmd, output="error")

    assert _screen_locked_dbus(runner=boom) is None


def test_screen_locked_returns_none_when_response_unparseable():
    assert _screen_locked_dbus(runner=lambda _cmd: "weird\n") is None


# ---------------------------------------------------------------------------
# Keyboard input: _parse_key_spec + backend dispatchers
# ---------------------------------------------------------------------------

import pytest

from gtk_a11y_mcp.server import (
    _parse_key_spec,
    _press_keys_atspi,
    _press_keys_ydotool,
)


def test_parse_key_spec_single_key():
    assert _parse_key_spec("Tab") == [([], "Tab")]


def test_parse_key_spec_modifier_combo():
    assert _parse_key_spec("Ctrl+S") == [(["ctrl"], "S")]


def test_parse_key_spec_multi_modifiers():
    assert _parse_key_spec("Ctrl+Shift+T") == [(["ctrl", "shift"], "T")]


def test_parse_key_spec_sequence_of_tokens():
    assert _parse_key_spec("Ctrl+a Delete Tab") == [
        (["ctrl"], "a"),
        ([], "Delete"),
        ([], "Tab"),
    ]


def test_parse_key_spec_normalizes_modifier_case():
    assert _parse_key_spec("CTRL+S") == [(["ctrl"], "S")]


def test_parse_key_spec_rejects_empty_spec():
    with pytest.raises(ValueError):
        _parse_key_spec("")


def test_parse_key_spec_rejects_token_ending_in_plus():
    with pytest.raises(ValueError):
        _parse_key_spec("Ctrl+")


def test_press_keys_atspi_dispatches_modifier_then_key_then_release():
    calls: list[tuple[str, str]] = []

    def record(keysym: str, kind: str) -> None:
        calls.append((keysym, kind))

    _press_keys_atspi(_parse_key_spec("Ctrl+S"), send=record)
    assert calls == [
        ("Control_L", "press"),
        ("S", "both"),
        ("Control_L", "release"),
    ]


def test_press_keys_atspi_translates_friendly_aliases():
    calls: list[tuple[str, str]] = []
    _press_keys_atspi(
        _parse_key_spec("Enter Esc Backspace"),
        send=lambda k, t: calls.append((k, t)),
    )
    keys = [k for k, _ in calls]
    assert keys == ["Return", "Escape", "BackSpace"]


def test_press_keys_atspi_releases_multiple_modifiers_in_reverse_order():
    calls: list[tuple[str, str]] = []
    _press_keys_atspi(
        _parse_key_spec("Ctrl+Shift+T"),
        send=lambda k, t: calls.append((k, t)),
    )
    assert calls == [
        ("Control_L", "press"),
        ("Shift_L", "press"),
        ("T", "both"),
        ("Shift_L", "release"),
        ("Control_L", "release"),
    ]


def test_press_keys_ydotool_emits_correct_sequence():
    cmds: list[list[str]] = []
    _press_keys_ydotool(
        _parse_key_spec("Ctrl+S"),
        runner=lambda cmd: cmds.append(cmd),
    )
    # KEY_LEFTCTRL=29, KEY_S=31. Press ctrl, press+release s, release ctrl.
    assert cmds == [["ydotool", "key", "29:1", "31:1", "31:0", "29:0"]]


def test_press_keys_ydotool_handles_plain_key():
    cmds: list[list[str]] = []
    _press_keys_ydotool(_parse_key_spec("Tab"), runner=lambda cmd: cmds.append(cmd))
    assert cmds == [["ydotool", "key", "15:1", "15:0"]]


def test_press_keys_ydotool_handles_sequence():
    cmds: list[list[str]] = []
    _press_keys_ydotool(
        _parse_key_spec("Ctrl+a Delete"),
        runner=lambda cmd: cmds.append(cmd),
    )
    assert cmds == [
        ["ydotool", "key", "29:1", "30:1", "30:0", "29:0"],  # Ctrl+a
        ["ydotool", "key", "111:1", "111:0"],                # Delete
    ]


def test_press_keys_ydotool_rejects_unknown_key():
    with pytest.raises(ValueError):
        _press_keys_ydotool(
            _parse_key_spec("ScrollLock"),
            runner=lambda _cmd: None,
        )


def test_press_keys_ydotool_rejects_unknown_modifier():
    with pytest.raises(ValueError):
        _press_keys_ydotool(
            [(["bogus"], "s")],
            runner=lambda _cmd: None,
        )


def test_press_keys_top_level_handles_permission_error():
    # User not in `input` group -> PermissionError from subprocess. Should
    # bubble up as a clean "both backends failed" dict, NOT an unhandled
    # exception escaping into the MCP transport.
    from gtk_a11y_mcp.server import press_keys
    from unittest.mock import patch

    def fake_atspi(parsed):
        raise RuntimeError("simulated AT-SPI failure on Wayland")

    def fake_ydotool(parsed, runner=None):
        raise PermissionError(13, "Permission denied: /dev/uinput")

    with patch("gtk_a11y_mcp.server._press_keys_atspi", fake_atspi), \
         patch("gtk_a11y_mcp.server._press_keys_ydotool", fake_ydotool):
        result = press_keys("Ctrl+S")

    assert "error" in result
    assert "both keyboard backends failed" in result["error"]
    assert "Permission denied" in result["ydotool_error"]


# ---------------------------------------------------------------------------
# launch_app: spawn + bus polling, all hooks injected
# ---------------------------------------------------------------------------

from gtk_a11y_mcp.server import _launch_app_impl


class FakeProc:
    def __init__(self, pid: int):
        self.pid = pid


def test_launch_app_passes_argv_as_list_and_sets_GTK_A11Y():
    captured: dict[str, Any] = {}

    def spawn(argv, env):
        captured["argv"] = argv
        captured["env"] = env
        return FakeProc(pid=4242)

    result = _launch_app_impl(
        "/usr/bin/gnome-calculator", ["--debug"], 0, None, None, spawn=spawn,
    )
    assert captured["argv"] == ["/usr/bin/gnome-calculator", "--debug"]
    assert captured["env"]["GTK_A11Y"] == "atspi"
    assert result["pid"] == 4242
    assert result["found_on_bus"] is False  # no app_name_match


def test_launch_app_merges_extra_env_over_GTK_A11Y_default():
    captured: dict[str, Any] = {}

    def spawn(argv, env):
        captured["env"] = env
        return FakeProc(pid=1)

    _launch_app_impl(
        "x", None, 0, None, {"GTK_A11Y": "test-override", "FOO": "bar"},
        spawn=spawn,
    )
    assert captured["env"]["GTK_A11Y"] == "test-override"
    assert captured["env"]["FOO"] == "bar"


def test_launch_app_returns_immediately_when_no_match_requested():
    def spawn(argv, env):
        return FakeProc(pid=7)

    # find_app would raise if called -- we shouldn't reach it.
    def find_app(_name):
        raise AssertionError("should not poll when app_name_match is None")

    result = _launch_app_impl(
        "x", None, 10.0, None, None, spawn=spawn, find_app=find_app,
    )
    assert result["found_on_bus"] is False
    assert result["elapsed_ms"] == 0.0


def test_launch_app_polls_until_app_appears():
    clock = FakeClock()
    appearances = iter([None, None, MockNode("calculator", "Calculator")])

    result = _launch_app_impl(
        "x", None, 10.0, "Calculator", None,
        spawn=lambda *_a: FakeProc(pid=11),
        find_app=lambda _name: next(appearances),
        sleep=clock.sleep,
        monotonic=clock.monotonic,
    )
    assert result["found_on_bus"] is True
    assert result["app_name"] == "Calculator"
    assert result["pid"] == 11


def test_launch_app_returns_not_found_on_timeout():
    clock = FakeClock()
    result = _launch_app_impl(
        "x", None, 1.0, "MissingApp", None,
        spawn=lambda *_a: FakeProc(pid=99),
        find_app=lambda _name: None,
        sleep=clock.sleep,
        monotonic=clock.monotonic,
    )
    assert result["found_on_bus"] is False
    assert result["elapsed_ms"] >= 1000.0
    assert result["pid"] == 99
    assert "hint" in result


def test_launch_app_default_args_is_none():
    captured: dict[str, Any] = {}

    def spawn(argv, env):
        captured["argv"] = argv
        return FakeProc(pid=1)

    _launch_app_impl("/usr/bin/foo", None, 0, None, None, spawn=spawn)
    assert captured["argv"] == ["/usr/bin/foo"]


# ---------------------------------------------------------------------------
# screenshot_widget: _crop_png + _screenshot_widget_impl with mock backends
# ---------------------------------------------------------------------------

import io as _io

from PIL import Image as PILImage

from gtk_a11y_mcp.server import _crop_png, _screenshot_widget_impl


def _make_png(width: int, height: int, color: tuple[int, int, int] = (200, 50, 50)) -> bytes:
    img = PILImage.new("RGB", (width, height), color)
    out = _io.BytesIO()
    img.save(out, format="PNG")
    return out.getvalue()


def _png_dimensions(png_bytes: bytes) -> tuple[int, int]:
    return PILImage.open(_io.BytesIO(png_bytes)).size


def test_crop_png_returns_subimage_of_requested_size():
    src = _make_png(200, 100)
    cropped = _crop_png(src, (20, 10, 50, 30))
    assert _png_dimensions(cropped) == (50, 30)


def test_crop_png_clips_negative_origin_to_image_bounds():
    src = _make_png(100, 100)
    # Asking for box starting at (-10,-10) of size 50: actual usable rect is
    # 0..40, 0..40 -> 40x40 cropped image.
    cropped = _crop_png(src, (-10, -10, 50, 50))
    assert _png_dimensions(cropped) == (40, 40)


def test_crop_png_clips_oversize_to_image_bounds():
    src = _make_png(100, 100)
    cropped = _crop_png(src, (80, 80, 50, 50))
    assert _png_dimensions(cropped) == (20, 20)


def test_crop_png_raises_when_box_outside_image():
    src = _make_png(100, 100)
    with pytest.raises(RuntimeError):
        _crop_png(src, (200, 200, 10, 10))


def test_screenshot_widget_impl_crops_to_extents_plus_padding():
    # Tree: a frame containing a single push button "Save".
    tree = MockNode(
        "application", "MyApp",
        children=[MockNode("frame", "Main",
            children=[MockNode("push button", "Save")])],
    )
    captured: dict[str, Any] = {}

    def fake_find_app(name):
        return tree

    def fake_capture():
        captured["captured"] = True
        return _make_png(800, 600)

    def fake_extents(node):
        # button is at (100, 200), 50x30
        captured["node_role"] = node.getRoleName()
        return (100, 200, 50, 30)

    cropped = _screenshot_widget_impl(
        "MyApp", "push button", "Save", padding_px=4,
        find_app=fake_find_app, capture=fake_capture, get_extents=fake_extents,
        activate=None,  # avoid the real 200ms settle in tests
    )
    # 50+2*4 by 30+2*4 = 58x38
    assert _png_dimensions(cropped) == (58, 38)
    assert captured["node_role"] == "push button"
    assert captured["captured"] is True


def test_screenshot_widget_impl_raises_when_app_missing():
    with pytest.raises(RuntimeError, match="No app"):
        _screenshot_widget_impl(
            "Nope", "x", "y", 0,
            find_app=lambda _n: None,
            capture=lambda: b"",
            get_extents=lambda _n: (0, 0, 0, 0),
            activate=None,
        )


def test_screenshot_widget_impl_raises_when_widget_missing():
    tree = MockNode("application", "MyApp", children=[])
    with pytest.raises(RuntimeError, match="No push button"):
        _screenshot_widget_impl(
            "MyApp", "push button", "ghost", 0,
            find_app=lambda _n: tree,
            capture=lambda: _make_png(100, 100),
            get_extents=lambda _n: (0, 0, 0, 0),
            activate=None,
        )


def test_screenshot_widget_impl_raises_when_widget_has_zero_area():
    tree = MockNode("application", "A",
        children=[MockNode("push button", "Hidden")])
    with pytest.raises(RuntimeError, match="zero-area"):
        _screenshot_widget_impl(
            "A", "push button", "Hidden", 0,
            find_app=lambda _n: tree,
            capture=lambda: _make_png(100, 100),
            get_extents=lambda _n: (0, 0, 0, 0),
            activate=None,  # don't try to activate in tests
        )


# ---------------------------------------------------------------------------
# Window activation: _window_for_widget, _activate_window, _activate_app_impl,
# and screenshot_widget's activation hook
# ---------------------------------------------------------------------------

from gtk_a11y_mcp.server import (
    _activate_app_impl,
    _activate_window,
    _window_for_widget,
)


def test_window_for_widget_finds_frame_in_path():
    frame = MockNode("frame", "Main",
                     children=[MockNode("push button", "Save")])
    tree = MockNode("application", "A", children=[frame])
    path = "application[A]/frame[Main]/push button[Save]"
    resolved = _window_for_widget(tree, path)
    assert resolved is not None
    assert resolved.getRoleName() == "frame"
    assert resolved.name == "Main"


def test_window_for_widget_finds_dialog_when_no_frame():
    dialog = MockNode("dialog", "Prefs",
                      children=[MockNode("push button", "Apply")])
    tree = MockNode("application", "A", children=[dialog])
    path = "application[A]/dialog[Prefs]/push button[Apply]"
    resolved = _window_for_widget(tree, path)
    assert resolved is not None
    assert resolved.getRoleName() == "dialog"


def test_window_for_widget_returns_none_when_path_has_no_window_role():
    # Pathological -- shouldn't happen in real GTK trees, but be defensive.
    tree = MockNode("application", "A", children=[MockNode("panel", "P")])
    path = "application[A]/panel[P]"
    assert _window_for_widget(tree, path) is None


def test_activate_window_calls_grab_focus_and_returns_true():
    comp = MockComponent(grab_focus_returns=True)
    frame = MockNode("frame", "Main", component=comp)
    assert _activate_window(frame) is True
    assert comp.grab_focus_calls == 1


def test_activate_window_returns_false_when_no_component_interface():
    frame = MockNode("frame", "Main")  # no component
    assert _activate_window(frame) is False


def test_activate_window_returns_false_when_grab_focus_returns_false():
    comp = MockComponent(grab_focus_returns=False)
    frame = MockNode("frame", "Main", component=comp)
    assert _activate_window(frame) is False
    assert comp.grab_focus_calls == 1


def test_activate_window_returns_false_for_none_node():
    assert _activate_window(None) is False


def test_activate_app_impl_finds_frame_and_grabs_focus():
    comp = MockComponent(grab_focus_returns=True)
    frame = MockNode("frame", "Main", component=comp)
    tree = MockNode("application", "MyApp", children=[frame])
    result = _activate_app_impl("MyApp", find_app=lambda _n: tree)
    assert result["ok"] is True
    assert "frame[Main]" in result["window_path"]
    assert comp.grab_focus_calls == 1


def test_activate_app_impl_returns_error_when_app_missing():
    result = _activate_app_impl("Nope", find_app=lambda _n: None)
    assert result["ok"] is False
    assert "No app" in result["error"]


def test_activate_app_impl_returns_error_when_no_window_in_children():
    tree = MockNode("application", "A", children=[MockNode("panel", "P")])
    result = _activate_app_impl("A", find_app=lambda _n: tree)
    assert result["ok"] is False
    assert "no frame" in result["error"]


def test_screenshot_widget_impl_calls_activate_before_extents():
    """The window must be raised BEFORE we measure extents -- compositors
    can shift a window's geometry on raise, so reading coords after the
    activation settle is the only safe order."""
    order: list[str] = []

    def fake_find_app(_name):
        return MockNode("application", "A",
                        children=[MockNode("frame", "Main",
                            children=[MockNode("push button", "Save")])])

    def fake_activate(app, path):
        order.append(f"activate({path})")
        return True

    def fake_extents(_node):
        order.append("extents")
        return (10, 20, 30, 40)

    def fake_capture():
        order.append("capture")
        return _make_png(800, 600)

    _screenshot_widget_impl(
        "A", "push button", "Save", padding_px=0,
        find_app=fake_find_app, capture=fake_capture, get_extents=fake_extents,
        activate=fake_activate, activate_settle_ms=0,
    )
    assert order[0].startswith("activate(")
    assert order.index("extents") < order.index("capture")


def test_screenshot_widget_impl_skips_activate_when_None():
    tree = MockNode("application", "A",
                    children=[MockNode("frame", "Main",
                        children=[MockNode("push button", "Save")])])

    def boom(*_a, **_kw):
        raise AssertionError("activate should not be called when None")

    _screenshot_widget_impl(
        "A", "push button", "Save", padding_px=0,
        find_app=lambda _n: tree,
        capture=lambda: _make_png(100, 100),
        get_extents=lambda _n: (10, 20, 30, 40),
        activate=None,
    )


def test_screenshot_widget_impl_sleeps_for_settle_ms():
    sleeps: list[float] = []
    tree = MockNode("application", "A",
                    children=[MockNode("frame", "Main",
                        children=[MockNode("push button", "Save")])])

    _screenshot_widget_impl(
        "A", "push button", "Save", padding_px=0,
        find_app=lambda _n: tree,
        capture=lambda: _make_png(100, 100),
        get_extents=lambda _n: (10, 20, 30, 40),
        activate=lambda _a, _p: True,
        activate_settle_ms=300,
        sleep=lambda s: sleeps.append(s),
    )
    assert sleeps == [0.3]


def test_screenshot_widget_impl_swallows_activate_exception():
    """A failing activation must NOT block the capture."""
    tree = MockNode("application", "A",
                    children=[MockNode("frame", "Main",
                        children=[MockNode("push button", "Save")])])

    def crashing_activate(_a, _p):
        raise RuntimeError("compositor said no")

    cropped = _screenshot_widget_impl(
        "A", "push button", "Save", padding_px=0,
        find_app=lambda _n: tree,
        capture=lambda: _make_png(100, 100),
        get_extents=lambda _n: (10, 20, 30, 40),
        activate=crashing_activate,
        activate_settle_ms=0,
    )
    # Capture went through despite the activate failure.
    assert _png_dimensions(cropped) == (30, 40)


# ---------------------------------------------------------------------------
# Attributes / description / accessible-id
# ---------------------------------------------------------------------------

from gtk_a11y_mcp.server import _get_attributes, _get_description


def test_get_attributes_parses_key_value_strings():
    node = MockNode("button", attributes=["id:save-btn", "level:1", "container:main"])
    assert _get_attributes(node) == {
        "id": "save-btn",
        "level": "1",
        "container": "main",
    }


def test_get_attributes_tolerates_entry_without_colon():
    node = MockNode("button", attributes=["weird"])
    assert _get_attributes(node) == {"weird": ""}


def test_get_attributes_returns_empty_when_no_attributes_interface():
    node = MockNode("button")  # _attributes=None -> [] from mock
    assert _get_attributes(node) == {}


def test_get_attributes_splits_only_on_first_colon():
    # Values can contain colons (e.g. "url:http://x") -- only the first split matters.
    node = MockNode("link", attributes=["id:link1", "url:http://example.com"])
    attrs = _get_attributes(node)
    assert attrs["url"] == "http://example.com"


def test_get_description_returns_description_field():
    node = MockNode("button", description="Saves the document")
    assert _get_description(node) == "Saves the document"


def test_get_description_returns_empty_when_missing():
    node = MockNode("button")  # description defaults to ""
    assert _get_description(node) == ""


def test_walk_match_attaches_accessible_id_and_description_and_attributes():
    tree = MockNode("application", "A", children=[
        MockNode("push button", "Save",
                 attributes=["id:save-btn", "container:dialog"],
                 description="Saves changes"),
    ])
    results: list = []
    _walk_match(tree, role="push button", name=None, results=results)
    r = results[0]
    assert r["accessible_id"] == "save-btn"
    assert r["description"] == "Saves changes"
    assert r["attributes"] == {"container": "dialog"}  # id has been extracted
    assert "id" not in r["attributes"]


def test_walk_match_attaches_none_accessible_id_when_absent():
    tree = MockNode("application", "A",
                    children=[MockNode("push button", "OK")])
    results: list = []
    _walk_match(tree, role="push button", name=None, results=results)
    assert results[0]["accessible_id"] is None
    assert results[0]["description"] == ""


def test_walk_match_filters_by_accessible_id():
    tree = MockNode("application", "A", children=[
        MockNode("push button", "A", attributes=["id:save-btn"]),
        MockNode("push button", "B", attributes=["id:cancel-btn"]),
    ])
    results: list = []
    _walk_match(tree, role="push button", name=None, results=results,
                accessible_id="save-btn")
    assert len(results) == 1
    assert results[0]["name"] == "A"


def test_walk_match_filters_by_description_substring():
    tree = MockNode("application", "A", children=[
        MockNode("push button", "X", description="Saves the document"),
        MockNode("push button", "Y", description="Cancels editing"),
    ])
    results: list = []
    _walk_match(tree, role="push button", name=None, results=results,
                description="saves")
    assert len(results) == 1
    assert results[0]["name"] == "X"


def test_walk_tree_appends_accessible_id_marker():
    tree = MockNode("application", "App",
                    children=[MockNode("push button", "Save",
                                       attributes=["id:save-btn"])])
    lines: list[str] = []
    _walk_tree(tree, 0, 5, lines)
    save_line = next(l for l in lines if "Save" in l)
    assert "#save-btn" in save_line


def test_walk_tree_no_marker_when_no_accessible_id():
    tree = MockNode("application", "App",
                    children=[MockNode("push button", "Save")])
    lines: list[str] = []
    _walk_tree(tree, 0, 5, lines)
    save_line = next(l for l in lines if "Save" in l)
    assert "#" not in save_line


# ---------------------------------------------------------------------------
# Value / Selection / Toggle interfaces
# ---------------------------------------------------------------------------

from gtk_a11y_mcp.server import (
    _get_value_impl,
    _set_value_impl,
    _select_item_impl,
    _set_checked_impl,
)


def test_get_value_returns_value_min_max():
    tree = MockNode("application", "A", children=[
        MockNode("slider", "Volume",
                 value=MockValue(current=0.7, minimum=0.0, maximum=1.0, increment=0.1)),
    ])
    result = _get_value_impl("A", "slider", "Volume", find_app=lambda _n: tree)
    assert result["value"] == 0.7
    assert result["minimum"] == 0.0
    assert result["maximum"] == 1.0
    assert result["minimum_increment"] == 0.1
    assert result["path"].endswith("slider[Volume]")


def test_get_value_errors_when_no_value_interface():
    tree = MockNode("application", "A", children=[
        MockNode("push button", "Save"),  # no value
    ])
    result = _get_value_impl("A", "push button", "Save", find_app=lambda _n: tree)
    assert "error" in result
    assert "Value interface" in result["error"]


def test_get_value_errors_when_widget_not_found():
    tree = MockNode("application", "A", children=[])
    result = _get_value_impl("A", "slider", "ghost", find_app=lambda _n: tree)
    assert "No slider" in result["error"]


def test_set_value_assigns_currentValue():
    slider = MockNode("slider", "Brightness",
                      value=MockValue(current=0.3, minimum=0.0, maximum=1.0))
    tree = MockNode("application", "A", children=[slider])
    msg = _set_value_impl("A", "slider", "Brightness", 0.85, find_app=lambda _n: tree)
    assert slider._value.currentValue == 0.85
    assert "Set value=0.85" in msg


def test_set_value_errors_when_no_value_interface():
    tree = MockNode("application", "A", children=[MockNode("push button", "Save")])
    msg = _set_value_impl("A", "push button", "Save", 1.0, find_app=lambda _n: tree)
    assert "no Value interface" in msg


def test_select_item_calls_selection_on_parent():
    sel = MockSelection()
    combo = MockNode("combo box", "Theme", selection=sel, children=[
        MockNode("menu item", "Light"),
        MockNode("menu item", "Dark"),
        MockNode("menu item", "System"),
    ])
    tree = MockNode("application", "A", children=[combo])
    parent_path = "application[A]/combo box[Theme]"
    msg = _select_item_impl("A", parent_path, "dark", find_app=lambda _n: tree)
    assert sel.selections == [1]
    assert "Dark" in msg


def test_select_item_errors_when_no_selection_interface():
    parent = MockNode("panel", "P", children=[MockNode("menu item", "A")])
    tree = MockNode("application", "A", children=[parent])
    parent_path = "application[A]/panel[P]"
    msg = _select_item_impl("A", parent_path, "A", find_app=lambda _n: tree)
    assert "no Selection interface" in msg


def test_select_item_errors_when_child_missing():
    sel = MockSelection()
    parent = MockNode("combo box", "Theme", selection=sel,
                      children=[MockNode("menu item", "Light")])
    tree = MockNode("application", "A", children=[parent])
    parent_path = "application[A]/combo box[Theme]"
    msg = _select_item_impl("A", parent_path, "dark", find_app=lambda _n: tree)
    assert sel.selections == []
    assert "No child" in msg


def test_set_checked_noop_when_state_already_matches():
    action = MockAction()
    cb = MockNode("check box", "Notifications", action=action,
                  states=["checked", "sensitive", "showing"])
    tree = MockNode("application", "A", children=[cb])
    msg = _set_checked_impl("A", "check box", "Notifications", True,
                            find_app=lambda _n: tree)
    assert action.calls == []
    assert "no-op" in msg


def test_set_checked_clicks_when_off_and_should_be_on():
    action = MockAction()
    cb = MockNode("check box", "Notifications", action=action,
                  states=["sensitive", "showing"])
    tree = MockNode("application", "A", children=[cb])
    msg = _set_checked_impl("A", "check box", "Notifications", True,
                            find_app=lambda _n: tree)
    assert action.calls == [0]
    assert "Toggled" in msg


def test_set_checked_clicks_when_on_and_should_be_off():
    action = MockAction()
    cb = MockNode("check box", "Notifications", action=action,
                  states=["checked", "sensitive", "showing"])
    tree = MockNode("application", "A", children=[cb])
    msg = _set_checked_impl("A", "check box", "Notifications", False,
                            find_app=lambda _n: tree)
    assert action.calls == [0]


def test_set_checked_errors_when_no_action_interface():
    cb = MockNode("check box", "Foo", states=["sensitive", "showing"])
    tree = MockNode("application", "A", children=[cb])
    msg = _set_checked_impl("A", "check box", "Foo", True, find_app=lambda _n: tree)
    assert "no Action interface" in msg


# ---------------------------------------------------------------------------
# audit: rule-based linter
# ---------------------------------------------------------------------------

from gtk_a11y_mcp.server import _audit_impl


def _audit(tree, **kwargs):
    return _audit_impl("A", find_app=lambda _n: tree, **kwargs)


def _issues_with_rule(result, rule_id):
    return [i for i in result["issues"] if i["rule"] == rule_id]


def test_audit_empty_name_actionable_fires_on_nameless_button():
    tree = MockNode("application", "A", children=[
        MockNode("push button", "", states=["focusable", "sensitive"],
                 action=MockAction()),
    ])
    result = _audit(tree)
    matches = _issues_with_rule(result, "empty-name-actionable")
    assert len(matches) == 1
    assert matches[0]["severity"] == "error"


def test_audit_empty_name_actionable_quiet_when_description_present():
    tree = MockNode("application", "A", children=[
        MockNode("push button", "", description="Save changes",
                 states=["focusable", "sensitive"], action=MockAction()),
    ])
    result = _audit(tree)
    assert _issues_with_rule(result, "empty-name-actionable") == []


def test_audit_name_equals_role_fires_on_placeholder_name():
    tree = MockNode("application", "A", children=[
        MockNode("push button", "Button", states=["focusable", "sensitive"],
                 action=MockAction()),
    ])
    result = _audit(tree)
    assert len(_issues_with_rule(result, "name-equals-role")) == 1


def test_audit_name_only_whitespace():
    tree = MockNode("application", "A", children=[
        MockNode("push button", "   ", states=["focusable", "sensitive"],
                 action=MockAction()),
    ])
    result = _audit(tree)
    matches = _issues_with_rule(result, "name-only-whitespace")
    assert len(matches) == 1


def test_audit_clickable_without_action():
    tree = MockNode("application", "A", children=[
        MockNode("push button", "Save", states=["focusable", "sensitive"]),
        # ^^ no action= -> no Action interface
    ])
    result = _audit(tree)
    matches = _issues_with_rule(result, "clickable-without-action")
    assert len(matches) == 1


def test_audit_actionable_not_focusable():
    tree = MockNode("application", "A", children=[
        MockNode("push button", "Save", states=["sensitive"],  # no focusable
                 action=MockAction()),
    ])
    result = _audit(tree)
    matches = _issues_with_rule(result, "actionable-not-focusable")
    assert len(matches) == 1


def test_audit_editable_without_label():
    tree = MockNode("application", "A", children=[
        MockNode("entry", "", states=["editable", "sensitive", "focusable"]),
    ])
    result = _audit(tree)
    matches = _issues_with_rule(result, "editable-without-label")
    assert len(matches) == 1


def test_audit_editable_with_description_passes():
    tree = MockNode("application", "A", children=[
        MockNode("entry", "", description="Email address",
                 states=["editable", "sensitive", "focusable"]),
    ])
    result = _audit(tree)
    assert _issues_with_rule(result, "editable-without-label") == []


def test_audit_image_without_description():
    tree = MockNode("application", "A", children=[
        MockNode("image", "", states=["sensitive"]),
    ])
    result = _audit(tree)
    matches = _issues_with_rule(result, "image-without-description")
    assert len(matches) == 1


def test_audit_duplicate_accessible_id():
    tree = MockNode("application", "A", children=[
        MockNode("push button", "A", attributes=["id:dupe"],
                 states=["focusable", "sensitive"], action=MockAction()),
        MockNode("menu item", "B", attributes=["id:dupe"],
                 states=["focusable", "sensitive"], action=MockAction()),
        MockNode("push button", "C", attributes=["id:unique"],
                 states=["focusable", "sensitive"], action=MockAction()),
    ])
    result = _audit(tree)
    dupes = _issues_with_rule(result, "duplicate-accessible-id")
    # Each occurrence of the dupe shows up once -> 2 issues, each carrying
    # the role of the offending widget (not just an empty string).
    assert len(dupes) == 2
    assert all("dupe" in i["message"] for i in dupes)
    roles = {i["role"] for i in dupes}
    assert roles == {"push button", "menu item"}


def test_audit_respects_specified_rules():
    tree = MockNode("application", "A", children=[
        MockNode("push button", "", states=["focusable", "sensitive"]),
        # ^^ would trigger empty-name-actionable AND clickable-without-action
    ])
    result = _audit(tree, rules=["empty-name-actionable"])
    rule_set = {i["rule"] for i in result["issues"]}
    assert rule_set == {"empty-name-actionable"}
    assert result["rules_run"] == ["empty-name-actionable"]


def test_audit_reports_unknown_rules():
    tree = MockNode("application", "A", children=[])
    result = _audit(tree, rules=["empty-name-actionable", "nonsense-rule"])
    assert result["unknown_rules"] == ["nonsense-rule"]


def test_audit_returns_error_when_app_missing():
    result = _audit_impl("Nope", find_app=lambda _n: None)
    assert "error" in result


def test_audit_counts_checked_widgets():
    tree = MockNode("application", "A", children=[
        MockNode("frame", "F",
            children=[
                MockNode("push button", "Save", states=["focusable", "sensitive"],
                         action=MockAction()),
                MockNode("push button", "Cancel", states=["focusable", "sensitive"],
                         action=MockAction()),
            ]),
    ])
    result = _audit(tree)
    # application + frame + 2 buttons = 4
    assert result["checked_widgets"] == 4


def test_audit_respects_max_issues():
    children = [
        MockNode("push button", "", states=["focusable", "sensitive"])  # empty-name-actionable
        for _ in range(10)
    ]
    tree = MockNode("application", "A", children=children)
    result = _audit(tree, max_issues=3)
    assert len(result["issues"]) <= 3


def test_audit_clean_tree_returns_no_issues():
    tree = MockNode("application", "A", children=[
        MockNode("frame", "Main",
            children=[
                MockNode("push button", "Save", description="Save document",
                         states=["focusable", "sensitive"], action=MockAction()),
                MockNode("entry", "Email", description="Your email",
                         states=["editable", "focusable", "sensitive"]),
            ]),
    ])
    result = _audit(tree)
    assert result["issues"] == []


# ---------------------------------------------------------------------------
# wait_for_state: polling-based state assertion
# ---------------------------------------------------------------------------

from gtk_a11y_mcp.server import _wait_for_state_impl


def _tree_with_button(states):
    return MockNode("application", "A", children=[
        MockNode("push button", "Save", states=states),
    ])


def test_wait_for_state_returns_when_state_already_present():
    clock = FakeClock()
    tree = _tree_with_button(["sensitive", "showing"])
    result = _wait_for_state_impl(
        "A", "push button", "Save", "sensitive", True, 5.0, 100,
        find_app=lambda _n: tree,
        sleep=clock.sleep, monotonic=clock.monotonic,
    )
    assert result["satisfied"] is True
    assert "sensitive" in result["states"]
    assert clock.sleeps == []  # no polling needed


def test_wait_for_state_returns_when_state_appears_after_polls():
    clock = FakeClock()
    # First two probes: state absent. Third: present.
    trees = iter([
        _tree_with_button(["showing"]),
        _tree_with_button(["showing"]),
        _tree_with_button(["sensitive", "showing"]),
    ])
    result = _wait_for_state_impl(
        "A", "push button", "Save", "sensitive", True, 5.0, 250,
        find_app=lambda _n: next(trees),
        sleep=clock.sleep, monotonic=clock.monotonic,
    )
    assert result["satisfied"] is True
    assert len(clock.sleeps) == 2


def test_wait_for_state_returns_when_state_vanishes_with_present_false():
    clock = FakeClock()
    trees = iter([
        _tree_with_button(["sensitive", "showing"]),
        _tree_with_button(["sensitive", "showing"]),
        _tree_with_button(["showing"]),  # sensitive vanished
    ])
    result = _wait_for_state_impl(
        "A", "push button", "Save", "sensitive", False, 5.0, 250,
        find_app=lambda _n: next(trees),
        sleep=clock.sleep, monotonic=clock.monotonic,
    )
    assert result["satisfied"] is True


def test_wait_for_state_times_out_when_never_satisfied():
    clock = FakeClock()
    tree = _tree_with_button(["showing"])  # sensitive absent forever
    result = _wait_for_state_impl(
        "A", "push button", "Save", "sensitive", True, 1.0, 250,
        find_app=lambda _n: tree,
        sleep=clock.sleep, monotonic=clock.monotonic,
    )
    assert result["satisfied"] is False
    assert result["elapsed_ms"] >= 1000.0


def test_wait_for_state_returns_not_found_when_widget_missing():
    clock = FakeClock()
    tree = MockNode("application", "A", children=[])
    result = _wait_for_state_impl(
        "A", "push button", "ghost", "sensitive", True, 1.0, 250,
        find_app=lambda _n: tree,
        sleep=clock.sleep, monotonic=clock.monotonic,
    )
    assert result["found"] is False


# ---------------------------------------------------------------------------
# script: multi-step batched dispatch
# ---------------------------------------------------------------------------

from gtk_a11y_mcp.server import _script_impl, _step_failed


def test_step_failed_detects_error_dict():
    assert _step_failed({"error": "bad"}) is True
    assert _step_failed({"found": False, "elapsed_ms": 0}) is True
    assert _step_failed({"found": True, "path": "x"}) is False


def test_step_failed_detects_error_strings():
    assert _step_failed("No app 'X' on AT-SPI bus.") is True
    assert _step_failed("Couldn't resolve foo.") is True
    assert _step_failed("Ambiguous: 2 matches") is True
    assert _step_failed("Widget at foo has no Action interface.") is True
    assert _step_failed("doAction failed: ...") is True
    # Successes
    assert _step_failed("Clicked: foo/bar") is False
    assert _step_failed("Focused: foo") is False
    assert _step_failed("Set text on foo.") is False


def test_script_runs_steps_in_order():
    calls: list[str] = []

    def tool_a(x):
        calls.append(f"a({x})")
        return f"Clicked: a-{x}"

    def tool_b():
        calls.append("b()")
        return "Focused: b"

    result = _script_impl(
        [
            {"tool": "a", "args": {"x": 1}},
            {"tool": "b", "args": {}},
        ],
        dispatch={"a": tool_a, "b": tool_b},
    )
    assert calls == ["a(1)", "b()"]
    assert result["stopped_at"] is None
    assert [r["ok"] for r in result["results"]] == [True, True]


def test_script_halts_on_first_failure():
    calls: list[str] = []

    def good():
        calls.append("good")
        return "Clicked: ok"

    def bad():
        calls.append("bad")
        return "No app 'X' on AT-SPI bus."

    def never():
        calls.append("never")
        return "should not run"

    result = _script_impl(
        [
            {"tool": "good", "args": {}},
            {"tool": "bad", "args": {}},
            {"tool": "never", "args": {}},
        ],
        dispatch={"good": good, "bad": bad, "never": never},
    )
    assert calls == ["good", "bad"]
    assert result["stopped_at"] == 1
    assert [r["ok"] for r in result["results"]] == [True, False]


def test_script_continue_on_error_keeps_going():
    calls: list[str] = []

    def bad():
        calls.append("bad")
        return "No app 'X' on bus."

    def after():
        calls.append("after")
        return "Clicked: ok"

    result = _script_impl(
        [
            {"tool": "bad", "args": {}, "continue_on_error": True},
            {"tool": "after", "args": {}},
        ],
        dispatch={"bad": bad, "after": after},
    )
    assert calls == ["bad", "after"]
    assert result["stopped_at"] is None


def test_script_rejects_unknown_tool_name():
    result = _script_impl(
        [{"tool": "nonsense", "args": {}}], dispatch={},
    )
    assert result["stopped_at"] == 0
    assert "unknown tool" in result["results"][0]["error"]


def test_script_rejects_non_dict_args():
    result = _script_impl(
        [{"tool": "foo", "args": "string-not-dict"}],
        dispatch={"foo": lambda: None},
    )
    assert result["stopped_at"] == 0
    assert "args must be a dict" in result["results"][0]["error"]


def test_script_passes_args_as_kwargs():
    captured: dict[str, Any] = {}

    def tool(a, b=5):
        captured["a"] = a
        captured["b"] = b
        return "ok"

    _script_impl(
        [{"tool": "t", "args": {"a": 1, "b": 9}}], dispatch={"t": tool},
    )
    assert captured == {"a": 1, "b": 9}


def test_script_calls_sleep_for_wait_ms_after_between_steps():
    sleeps: list[float] = []

    def tool():
        return "Clicked: x"

    # Three steps so we can see the first two sleeps fire and the final
    # post-step sleep get skipped.
    _script_impl(
        [
            {"tool": "t", "args": {}, "wait_ms_after": 250},
            {"tool": "t", "args": {}, "wait_ms_after": 500},
            {"tool": "t", "args": {}, "wait_ms_after": 999},  # final step -> skipped
        ],
        dispatch={"t": tool},
        sleep=lambda s: sleeps.append(s),
    )
    assert sleeps == [0.25, 0.5]


def test_script_does_not_sleep_after_final_step():
    sleeps: list[float] = []

    def tool():
        return "Clicked: x"

    # Final step has wait_ms_after set, but we shouldn't sleep -- the result
    # would just be delayed for no reason.
    _script_impl(
        [
            {"tool": "t", "args": {}, "wait_ms_after": 100},
            {"tool": "t", "args": {}, "wait_ms_after": 100},
        ],
        dispatch={"t": tool},
        sleep=lambda s: sleeps.append(s),
    )
    assert sleeps == [0.1]  # only between step 0 and step 1


def test_script_no_sleep_when_only_one_step():
    sleeps: list[float] = []
    _script_impl(
        [{"tool": "t", "args": {}, "wait_ms_after": 250}],
        dispatch={"t": lambda: "ok"},
        sleep=lambda s: sleeps.append(s),
    )
    assert sleeps == []


def test_script_handles_tool_exception():
    def boom():
        raise RuntimeError("kaboom")

    result = _script_impl(
        [{"tool": "boom", "args": {}}], dispatch={"boom": boom},
    )
    assert result["stopped_at"] == 0
    assert "RuntimeError" in result["results"][0]["error"]
    assert "kaboom" in result["results"][0]["error"]


# ---------------------------------------------------------------------------
# Bus reconnect resilience: _is_dbus_exception, _with_bus_retry
# ---------------------------------------------------------------------------

from gtk_a11y_mcp.server import _is_dbus_exception, _with_bus_retry


class FakeDBusException(Exception):
    """Mimics dbus.DBusException by name + module."""


# Force the module to look dbus-ish for the detector.
FakeDBusException.__module__ = "dbus.exceptions"


class FakeGError(Exception):
    pass


FakeGError.__module__ = "gi.repository.GLib"


def test_is_dbus_exception_detects_dbus_module():
    assert _is_dbus_exception(FakeDBusException("bus gone")) is True


def test_is_dbus_exception_detects_gerror_in_glib_module():
    assert _is_dbus_exception(FakeGError("bus gone")) is True


def test_is_dbus_exception_rejects_unrelated_exceptions():
    assert _is_dbus_exception(ValueError("nope")) is False
    assert _is_dbus_exception(TypeError("nope")) is False
    assert _is_dbus_exception(RuntimeError("nope")) is False


def test_with_bus_retry_passes_through_on_success():
    calls = [0]

    @_with_bus_retry
    def fn():
        calls[0] += 1
        return "ok"

    assert fn() == "ok"
    assert calls[0] == 1


def test_with_bus_retry_retries_once_on_dbus_exception():
    calls = [0]

    @_with_bus_retry
    def fn():
        calls[0] += 1
        if calls[0] == 1:
            raise FakeDBusException("bus restart")
        return "recovered"

    assert fn() == "recovered"
    assert calls[0] == 2


def test_with_bus_retry_propagates_dbus_after_second_failure():
    calls = [0]

    @_with_bus_retry
    def fn():
        calls[0] += 1
        raise FakeDBusException(f"still broken {calls[0]}")

    with pytest.raises(FakeDBusException, match="still broken 2"):
        fn()
    assert calls[0] == 2


def test_with_bus_retry_does_not_retry_on_non_dbus_exception():
    calls = [0]

    @_with_bus_retry
    def fn():
        calls[0] += 1
        raise ValueError("agent's bug, not the bus")

    with pytest.raises(ValueError):
        fn()
    assert calls[0] == 1  # not retried


def test_with_bus_retry_preserves_function_metadata():
    @_with_bus_retry
    def my_tool():
        """Original docstring."""
        return 1

    assert my_tool.__name__ == "my_tool"
    assert my_tool.__doc__ == "Original docstring."


def test_with_bus_retry_passes_args_and_kwargs_to_inner_fn():
    captured: dict[str, Any] = {}

    @_with_bus_retry
    def fn(a, b=2):
        captured["a"] = a
        captured["b"] = b
        return a + b

    assert fn(10, b=5) == 15
    assert captured == {"a": 10, "b": 5}


def test_with_bus_retry_kicks_in_on_a_real_decorated_tool():
    """End-to-end: a tool stacked with @mcp.tool() / @_with_bus_retry
    should survive a transient DBusException from its underlying bus call.

    We monkeypatch the bus entry point so the FIRST call raises a fake
    DBusException; the wrapper catches it, runs _reconnect_bus, and on the
    retry the same patched function returns a real tree. This proves the
    decorator stack is wired correctly (not just _with_bus_retry in
    isolation).
    """
    from unittest.mock import patch
    from gtk_a11y_mcp.server import find_widgets

    tree = MockNode("application", "Demo", children=[
        MockNode("push button", "Save"),
    ])
    call_count = [0]

    def flaky_find_app(_name):
        call_count[0] += 1
        if call_count[0] == 1:
            raise FakeDBusException("registryd restart")
        return tree

    reconnect_called = [0]

    def fake_reconnect():
        reconnect_called[0] += 1

    with patch("gtk_a11y_mcp.server._find_app", flaky_find_app), \
         patch("gtk_a11y_mcp.server._reconnect_bus", fake_reconnect):
        result = find_widgets("Demo", role="push button")

    assert call_count[0] == 2          # one failure + one retry
    assert reconnect_called[0] == 1    # reconnect ran between attempts
    assert len(result) == 1
    assert result[0]["name"] == "Save"
