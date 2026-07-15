"""Persistent application preferences (QSettings) and the Preferences dialog.

Deliberately tiny: one module owns every key, so a preference cannot be read
under one name and written under another. Settings live in the platform's
native store (a plist on macOS, the registry on Windows, an ini elsewhere);
the ``CHRONOGATE_PREFS_INI`` environment variable redirects them to an ini
file so tests never touch the user's real configuration.
"""

from __future__ import annotations

import os

from PySide6.QtCore import QSettings
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QLabel,
    QVBoxLayout,
)

_KEY_REOPEN = "startup/reopen_last"
_KEY_LAST_PATH = "startup/last_path"


def _qsettings() -> QSettings:
    ini = os.environ.get("CHRONOGATE_PREFS_INI")
    if ini:
        return QSettings(ini, QSettings.Format.IniFormat)
    return QSettings("ChronoGate", "ChronoGate")


def reopen_last() -> bool:
    """Whether the app should reopen the last .ptu/stack at launch (default off)."""
    return _qsettings().value(_KEY_REOPEN, False, type=bool)


def set_reopen_last(on: bool) -> None:
    s = _qsettings()
    s.setValue(_KEY_REOPEN, bool(on))
    s.sync()


def last_path() -> str:
    """The most recently loaded .ptu file ('' if none was ever recorded)."""
    return _qsettings().value(_KEY_LAST_PATH, "", type=str)


def set_last_path(path: str) -> None:
    s = _qsettings()
    s.setValue(_KEY_LAST_PATH, str(path))
    s.sync()


class PreferencesDialog(QDialog):
    """The Preferences window. Reads the stored values on open; writes them
    only on OK (Cancel changes nothing)."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Preferences")

        self.chk_reopen = QCheckBox("Open the last used .ptu file / stack at launch")
        self.chk_reopen.setChecked(reopen_last())
        self.chk_reopen.setToolTip(
            "On the next start, skip the welcome screen and load whatever was "
            "open last (same plane included). A file that has moved or vanished "
            "falls back to the welcome screen.")
        last = last_path()
        note = QLabel(f"Last opened: {last}" if last else "Last opened: (nothing yet)")
        note.setStyleSheet("color: palette(mid);")
        note.setWordWrap(True)

        self.buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)

        lay = QVBoxLayout(self)
        lay.addWidget(self.chk_reopen)
        lay.addWidget(note)
        lay.addWidget(self.buttons)

    def accept(self) -> None:
        set_reopen_last(self.chk_reopen.isChecked())
        super().accept()
