"""Manage org.gnome.desktop.interface toolkit-accessibility lifecycle.

GNOME ships with toolkit-accessibility=false, which gates GTK apps' AT-SPI
export. This module flips the key on first acquire(), records the prior
value + owning PID in a state file, and restores the prior value on clean
shutdown. On next startup, cleanup_orphans() detects state files whose PID
is dead and restores the recorded prior value -- catches kill -9 / crash.
"""

from __future__ import annotations

import atexit
import json
import os
import signal
import subprocess
from pathlib import Path

GSETTINGS_SCHEMA = "org.gnome.desktop.interface"
GSETTINGS_KEY = "toolkit-accessibility"


def _state_dir() -> Path:
    base = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(base) / "gtk-a11y-mcp"


def _state_file() -> Path:
    return _state_dir() / "state.json"


def _gsettings_get() -> bool:
    out = subprocess.check_output(
        ["gsettings", "get", GSETTINGS_SCHEMA, GSETTINGS_KEY], text=True
    ).strip()
    return out == "true"


def _gsettings_set(value: bool) -> None:
    subprocess.check_call(
        [
            "gsettings",
            "set",
            GSETTINGS_SCHEMA,
            GSETTINGS_KEY,
            "true" if value else "false",
        ]
    )


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


_acquired = False
_prior_value: bool | None = None


def cleanup_orphans() -> None:
    """If a stale state file points at a dead PID, restore prior value."""
    sf = _state_file()
    if not sf.exists():
        return
    try:
        data = json.loads(sf.read_text())
    except (OSError, json.JSONDecodeError):
        sf.unlink(missing_ok=True)
        return
    pid = int(data.get("pid", 0))
    prior = bool(data.get("prior_value", False))
    if _pid_alive(pid):
        return
    try:
        _gsettings_set(prior)
    except (subprocess.CalledProcessError, FileNotFoundError):
        pass
    sf.unlink(missing_ok=True)


def acquire() -> None:
    """Idempotent: ensure toolkit-accessibility=true. Records prior value
    + owning PID on first call so release() / cleanup_orphans() can revert.
    """
    global _acquired, _prior_value
    if _acquired:
        return
    prior = _gsettings_get()
    _prior_value = prior
    if not prior:
        _gsettings_set(True)
    _state_dir().mkdir(parents=True, exist_ok=True)
    _state_file().write_text(
        json.dumps({"pid": os.getpid(), "prior_value": prior})
    )
    _acquired = True


def release() -> None:
    """Restore prior value and delete state file. Idempotent."""
    global _acquired, _prior_value
    if not _acquired:
        return
    try:
        if _prior_value is False:
            _gsettings_set(False)
    except (subprocess.CalledProcessError, FileNotFoundError):
        pass
    _state_file().unlink(missing_ok=True)
    _acquired = False
    _prior_value = None


def is_acquired() -> bool:
    return _acquired


def get_status() -> dict:
    try:
        current = _gsettings_get()
    except (subprocess.CalledProcessError, FileNotFoundError):
        current = None
    return {
        "acquired_by_us": _acquired,
        "prior_value": _prior_value,
        "current_value": current,
        "state_file": str(_state_file()),
    }


def install_signal_handlers() -> None:
    """Register atexit + SIGTERM/SIGINT/SIGHUP handlers to release on shutdown."""
    atexit.register(release)

    def _handler(signum, _frame):
        release()
        signal.signal(signum, signal.SIG_DFL)
        os.kill(os.getpid(), signum)

    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        try:
            signal.signal(sig, _handler)
        except (OSError, ValueError):
            pass
