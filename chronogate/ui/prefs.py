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
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
)

_KEY_REOPEN = "startup/reopen_last"
_KEY_LAST_PATH = "startup/last_path"
_KEY_EXPORT_ROOT = "export/root"
_KEY_FIJI_PATH = "integrations/fiji_path"


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


def export_root() -> str:
    """A fixed folder for all default exports ('' = next to the opened data)."""
    return _qsettings().value(_KEY_EXPORT_ROOT, "", type=str)


def set_export_root(path: str) -> None:
    s = _qsettings()
    s.setValue(_KEY_EXPORT_ROOT, str(path))
    s.sync()


def fiji_path() -> str:
    """The Fiji launcher/app to hand exports to ('' = not configured)."""
    return _qsettings().value(_KEY_FIJI_PATH, "", type=str)


def set_fiji_path(path: str) -> None:
    s = _qsettings()
    s.setValue(_KEY_FIJI_PATH, str(path))
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

        self.export_dir = QLineEdit(export_root())
        self.export_dir.setPlaceholderText("(empty — next to the opened data)")
        self.export_dir.setToolTip(
            "Every default export (Export dialog suggestion, Ctrl+R report) goes "
            "under this folder. Leave empty to export next to the opened .ptu, "
            "in a chronogate_exports folder.")
        btn_browse = QPushButton("Browse…")
        btn_browse.clicked.connect(self._browse_export_dir)
        export_row = QHBoxLayout()
        export_row.addWidget(QLabel("Export folder"))
        export_row.addWidget(self.export_dir, 1)
        export_row.addWidget(btn_browse)

        self.fiji_edit = QLineEdit(fiji_path())
        self.fiji_edit.setPlaceholderText("(not set — the Fiji button stays disabled)")
        self.fiji_edit.setToolTip(
            "The Fiji application or launcher. On macOS pick Fiji.app; ChronoGate "
            "resolves the launcher inside it. Enables 'Export & open in Fiji' in "
            "the export dialog.")
        btn_fiji = QPushButton("Browse…")
        btn_fiji.clicked.connect(self._browse_fiji)
        fiji_row = QHBoxLayout()
        fiji_row.addWidget(QLabel("Fiji app"))
        fiji_row.addWidget(self.fiji_edit, 1)
        fiji_row.addWidget(btn_fiji)

        self.buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)

        lay = QVBoxLayout(self)
        lay.addWidget(self.chk_reopen)
        lay.addWidget(note)
        lay.addLayout(export_row)
        lay.addLayout(fiji_row)
        lay.addWidget(self.buttons)

    def _browse_export_dir(self) -> None:
        d = QFileDialog.getExistingDirectory(
            self, "Choose the export folder", self.export_dir.text(),
            QFileDialog.Option.ShowDirsOnly | QFileDialog.Option.DontUseNativeDialog)
        if d:
            self.export_dir.setText(d)

    def _browse_fiji(self) -> None:
        # A macOS .app is a directory; a Linux/Windows launcher is a file. Offer
        # a file picker (it can still descend into a bundle), and default the
        # start location to /Applications where Fiji.app usually lives.
        start = self.fiji_edit.text() or "/Applications"
        path, _ = QFileDialog.getOpenFileName(
            self, "Choose the Fiji application or launcher", start,
            "Applications (*.app);;All files (*)", "",
            QFileDialog.Option.DontUseNativeDialog)
        if path:
            self.fiji_edit.setText(path)

    def accept(self) -> None:
        set_reopen_last(self.chk_reopen.isChecked())
        set_export_root(self.export_dir.text().strip())
        set_fiji_path(self.fiji_edit.text().strip())
        super().accept()
