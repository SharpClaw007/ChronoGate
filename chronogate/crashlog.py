"""Crash logging, so a frozen GUI build never dies silently.

A PyInstaller windowed build has ``console=False``: stdout and stderr go
nowhere, so an unhandled exception makes the window vanish with no diagnostic
at all. That is survivable on a developer's machine (run it from a terminal and
read the traceback) but not on a shared lab PC, where nobody can attach a
debugger and "it just closes" is the entire bug report.

This module writes a timestamped crash log next to the user's own data --
``%LOCALAPPDATA%\\ChronoGate\\logs`` on Windows, ``~/Library/Logs/ChronoGate``
on macOS, ``~/.local/state/chronogate`` elsewhere -- and tells the user where it
went. It covers both failure modes:

* **Python exceptions** via :func:`sys.excepthook` (and Qt's own hook, since
  PySide6 routes slot exceptions through it).
* **Hard crashes** -- a segfault inside Qt, numpy or a GPU driver -- via
  :mod:`faulthandler`, which writes the C-level stack to the same file. Those
  never reach Python at all, so nothing else would record them.

Everything here is best-effort: a logger that raises while reporting a crash
would replace a useful traceback with a useless one, so every step is guarded.
"""

from __future__ import annotations

import atexit
import faulthandler
import os
import platform
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path

# Keep the newest N logs; a lab machine should not accumulate these forever.
_KEEP_LOGS = 20

# faulthandler needs the stream to stay open for the life of the process.
_fault_file = None
_log_path: Path | None = None


def log_dir() -> Path:
    """The per-user directory for crash logs (created on demand).

    Always a location the user can write to without admin rights -- the install
    directory (``C:\\Program Files\\ChronoGate``) is read-only for a normal lab
    account, so logs must never go there.
    """
    override = os.environ.get("CHRONOGATE_LOG_DIR")
    if override:
        return Path(override)
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
        return base / "ChronoGate" / "logs"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Logs" / "ChronoGate"
    state = os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state")
    return Path(state) / "chronogate"


def current_log_path() -> Path | None:
    """The log file for this session, or None if logging could not start."""
    return _log_path


def _prune_old_logs(directory: Path) -> None:
    try:
        logs = sorted(directory.glob("chronogate-*.log"), key=lambda p: p.stat().st_mtime)
        for old in logs[:-_KEEP_LOGS]:
            old.unlink(missing_ok=True)
    except OSError:
        pass  # pruning is housekeeping; never let it break startup


def _environment_banner(version: str) -> str:
    """Facts worth having before the first line of any traceback."""
    frozen = getattr(sys, "frozen", False)
    lines = [
        f"ChronoGate {version}",
        f"started   {datetime.now(timezone.utc).isoformat(timespec='seconds')}",
        f"python    {sys.version.split()[0]} ({platform.machine()})",
        f"platform  {platform.platform()}",
        f"frozen    {bool(frozen)}",
        f"executable {sys.executable}",
    ]
    try:  # Qt version is often the difference between two identical-looking bugs
        from PySide6 import __version__ as pyside_version
        from PySide6.QtCore import qVersion

        lines.append(f"pyside6   {pyside_version} (Qt {qVersion()})")
    except Exception:  # noqa: BLE001 - the banner must never be the thing that fails
        lines.append("pyside6   <not importable>")
    return "\n".join(lines) + "\n" + "-" * 60 + "\n"


def install(version: str = "unknown") -> Path | None:
    """Start crash logging. Returns the log path, or None if it could not start.

    Safe to call more than once; later calls are no-ops.
    """
    global _fault_file, _log_path
    if _log_path is not None:
        return _log_path
    try:
        directory = log_dir()
        directory.mkdir(parents=True, exist_ok=True)
        _prune_old_logs(directory)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        path = directory / f"chronogate-{stamp}.log"
        # Line-buffered: a hard crash must not lose what was already written.
        _fault_file = open(path, "w", encoding="utf-8", buffering=1)
        _fault_file.write(_environment_banner(version))
        # Catches segfaults/aborts inside Qt or numpy, which never reach Python.
        faulthandler.enable(file=_fault_file, all_threads=True)
        atexit.register(_close)
        _log_path = path
    except OSError:
        return None  # read-only profile, full disk: run without logging

    sys.excepthook = _excepthook
    # PySide6 raises unhandled slot exceptions through threading's hook too.
    try:
        import threading

        threading.excepthook = _thread_excepthook
    except Exception:  # noqa: BLE001
        pass
    return _log_path


def _close() -> None:
    global _fault_file
    try:
        if _fault_file is not None:
            _fault_file.flush()
            _fault_file.close()
    except OSError:
        pass
    _fault_file = None


def _record(header: str, exc_type, exc, tb) -> None:
    """Append a formatted traceback to the log (best effort)."""
    try:
        if _fault_file is None:
            return
        _fault_file.write(
            f"\n{header} at {datetime.now(timezone.utc).isoformat(timespec='seconds')}\n"
        )
        traceback.print_exception(exc_type, exc, tb, file=_fault_file)
        _fault_file.flush()
    except Exception:  # noqa: BLE001
        pass


def _notify(exc: BaseException) -> None:
    """Tell the user where the log is -- a dialog if Qt is alive, else stderr.

    Without this the log exists but nobody knows to look for it, which on a lab
    PC is the same as having no log.
    """
    message = (
        f"ChronoGate hit an unexpected error and may not be able to continue.\n\n"
        f"{type(exc).__name__}: {exc}\n\n"
        f"A diagnostic log was saved to:\n{_log_path}\n\n"
        f"Please send that file to whoever maintains ChronoGate."
    )
    try:
        from PySide6.QtWidgets import QApplication, QMessageBox

        if QApplication.instance() is not None:
            QMessageBox.critical(None, "ChronoGate — unexpected error", message)
            return
    except Exception:  # noqa: BLE001 - Qt may be gone or mid-teardown
        pass
    print(message, file=sys.stderr)


def _excepthook(exc_type, exc, tb) -> None:
    # Ctrl+C should stay quiet and behave like it always does.
    if issubclass(exc_type, KeyboardInterrupt):
        sys.__excepthook__(exc_type, exc, tb)
        return
    _record("UNHANDLED EXCEPTION", exc_type, exc, tb)
    _notify(exc)
    sys.__excepthook__(exc_type, exc, tb)


def _thread_excepthook(args) -> None:
    # Worker-thread failures (file loading, the fit map) land here.
    _record(
        f"UNHANDLED EXCEPTION in thread {getattr(args.thread, 'name', '?')}",
        args.exc_type, args.exc_value, args.exc_traceback,
    )
