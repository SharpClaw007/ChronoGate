"""Application bootstrap: build a themed QApplication and show the main window.

Heavy imports (PySide6, matplotlib backend, the window) are deferred into
:func:`launch` so that merely importing :mod:`chronogate.ui` -- or running the
GUI-free tests -- never pulls in Qt.
"""

from __future__ import annotations

import sys


def _warn_if_rosetta() -> None:
    """Warn if we're an x86-64 process translated by Rosetta on Apple Silicon.

    Qt GUIs are noticeably crash-prone in that translated environment; a native
    arm64 Python avoids it entirely.
    """
    if sys.platform != "darwin":
        return
    try:
        import ctypes
        libc = ctypes.CDLL(None)
        val = ctypes.c_int(0)
        size = ctypes.c_size_t(ctypes.sizeof(val))
        rc = libc.sysctlbyname(b"sysctl.proc_translated",
                               ctypes.byref(val), ctypes.byref(size), None, 0)
        if rc == 0 and val.value == 1:
            print(
                "WARNING: ChronoGate is running under Rosetta (x86-64 Python on Apple\n"
                "         Silicon). The Qt GUI can be unstable there. For best stability\n"
                "         run a native arm64 Python, e.g.\n"
                "           /opt/homebrew/bin/python3 -m chronogate\n"
                "         or  CHRONOGATE_PYTHON=/opt/homebrew/bin/python3 ./ChronoGate.command",
                file=sys.stderr)
    except Exception:  # noqa: BLE001 - a best-effort advisory only
        pass


def launch(path=None, channel: int = 0, sum_frames: bool = True,
           settings_path: str | None = None, start_lifetime: bool = False,
           open_dir: str | None = None) -> int:
    """Create the QApplication, theme it, show the window, and run the event loop.

    If ``path`` is None the app opens to its welcome screen (no file required);
    the user opens a file or folder from there. ``open_dir`` seeds the in-app file
    dialogs. Returns the Qt exit code. Raises
    :class:`~chronogate.loader.UnsupportedFileError` (before the loop starts) if a
    file given on the command line cannot be decoded.
    """
    _warn_if_rosetta()

    import matplotlib
    matplotlib.use("QtAgg")

    from PySide6.QtWidgets import QApplication

    from . import theme
    from .icon import app_icon
    from .main_window import MainWindow
    from ..export import load_settings

    theme.apply_matplotlib_theme()

    app = QApplication.instance() or QApplication(sys.argv)
    app.setApplicationName("ChronoGate")
    app.setApplicationDisplayName("ChronoGate")
    app.setStyle("Fusion")
    theme.apply_qt_palette(app)
    app.setStyleSheet(theme.CLINICAL_QSS())
    app.setWindowIcon(app_icon())

    win = MainWindow(path, channel=channel, sum_frames=sum_frames, open_dir=open_dir)
    if settings_path and win.controller.model is not None:
        win.controller.apply_settings(load_settings(settings_path))
    if start_lifetime:
        win.controller.enter_lifetime()
    win.show()
    return app.exec()
