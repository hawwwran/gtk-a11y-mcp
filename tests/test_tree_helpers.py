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
