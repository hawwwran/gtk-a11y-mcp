"""Lifecycle unit tests. gsettings is mocked so tests don't touch GNOME state."""

from __future__ import annotations

import json
import os
from unittest.mock import patch

import pytest

from gtk_a11y_mcp import lifecycle


@pytest.fixture
def tmp_state(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    lifecycle._acquired = False
    lifecycle._prior_value = None
    yield tmp_path
    lifecycle._acquired = False
    lifecycle._prior_value = None


def test_acquire_flips_when_off_and_records_state(tmp_state):
    with patch.object(lifecycle, "_gsettings_get", return_value=False), \
         patch.object(lifecycle, "_gsettings_set") as mock_set:
        lifecycle.acquire()
    mock_set.assert_called_once_with(True)
    sf = lifecycle._state_file()
    assert sf.exists()
    data = json.loads(sf.read_text())
    assert data == {"pid": os.getpid(), "prior_value": False}


def test_acquire_no_flip_when_already_on(tmp_state):
    with patch.object(lifecycle, "_gsettings_get", return_value=True), \
         patch.object(lifecycle, "_gsettings_set") as mock_set:
        lifecycle.acquire()
    mock_set.assert_not_called()
    data = json.loads(lifecycle._state_file().read_text())
    assert data["prior_value"] is True


def test_acquire_is_idempotent(tmp_state):
    with patch.object(lifecycle, "_gsettings_get", return_value=False), \
         patch.object(lifecycle, "_gsettings_set") as mock_set:
        lifecycle.acquire()
        lifecycle.acquire()
        lifecycle.acquire()
    mock_set.assert_called_once_with(True)


def test_release_restores_when_we_flipped_it(tmp_state):
    with patch.object(lifecycle, "_gsettings_get", return_value=False), \
         patch.object(lifecycle, "_gsettings_set") as mock_set:
        lifecycle.acquire()
        mock_set.reset_mock()
        lifecycle.release()
    mock_set.assert_called_once_with(False)
    assert not lifecycle._state_file().exists()


def test_release_no_op_when_already_was_on(tmp_state):
    with patch.object(lifecycle, "_gsettings_get", return_value=True), \
         patch.object(lifecycle, "_gsettings_set") as mock_set:
        lifecycle.acquire()
        mock_set.reset_mock()
        lifecycle.release()
    mock_set.assert_not_called()


def test_release_idempotent(tmp_state):
    with patch.object(lifecycle, "_gsettings_get", return_value=False), \
         patch.object(lifecycle, "_gsettings_set"):
        lifecycle.release()
        lifecycle.release()


def test_cleanup_orphans_dead_pid_restores_and_clears(tmp_state):
    sf = lifecycle._state_file()
    sf.parent.mkdir(parents=True, exist_ok=True)
    sf.write_text(json.dumps({"pid": 99999999, "prior_value": False}))
    with patch.object(lifecycle, "_gsettings_set") as mock_set:
        lifecycle.cleanup_orphans()
    mock_set.assert_called_once_with(False)
    assert not sf.exists()


def test_cleanup_orphans_live_pid_leaves_alone(tmp_state):
    sf = lifecycle._state_file()
    sf.parent.mkdir(parents=True, exist_ok=True)
    sf.write_text(json.dumps({"pid": os.getpid(), "prior_value": False}))
    with patch.object(lifecycle, "_gsettings_set") as mock_set:
        lifecycle.cleanup_orphans()
    mock_set.assert_not_called()
    assert sf.exists()


def test_cleanup_orphans_no_state_file(tmp_state):
    with patch.object(lifecycle, "_gsettings_set") as mock_set:
        lifecycle.cleanup_orphans()
    mock_set.assert_not_called()


def test_cleanup_orphans_corrupt_state_file(tmp_state):
    sf = lifecycle._state_file()
    sf.parent.mkdir(parents=True, exist_ok=True)
    sf.write_text("not valid json {{{")
    with patch.object(lifecycle, "_gsettings_set") as mock_set:
        lifecycle.cleanup_orphans()
    mock_set.assert_not_called()
    assert not sf.exists()


def test_cleanup_orphans_orphan_with_prior_true_does_not_change(tmp_state):
    """If we crashed but the prior value was already true, restoring is a no-op
    semantically (and we still call _gsettings_set(True), which is harmless)."""
    sf = lifecycle._state_file()
    sf.parent.mkdir(parents=True, exist_ok=True)
    sf.write_text(json.dumps({"pid": 99999999, "prior_value": True}))
    with patch.object(lifecycle, "_gsettings_set") as mock_set:
        lifecycle.cleanup_orphans()
    mock_set.assert_called_once_with(True)
    assert not sf.exists()


def test_pid_alive_self():
    assert lifecycle._pid_alive(os.getpid())


def test_pid_alive_dead():
    assert not lifecycle._pid_alive(99999999)
