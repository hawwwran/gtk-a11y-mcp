"""Unit tests for the tree-walking helpers using mock AT-SPI nodes."""

from __future__ import annotations

from gtk_a11y_mcp.server import _resolve_path, _walk_match, _walk_tree


class MockNode:
    """Minimal stand-in for a pyatspi Accessible: getRoleName(), name, childCount, getChildAtIndex()."""

    def __init__(self, role: str, name: str = "", children: list["MockNode"] | None = None):
        self._role = role
        self.name = name
        self._children = children or []
        self.childCount = len(self._children)

    def getRoleName(self) -> str:
        return self._role

    def getChildAtIndex(self, i: int) -> "MockNode":
        return self._children[i]


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
