# PyInstaller spec for ChronoGate (one-folder; a macOS .app on Darwin).
#
#   pyinstaller packaging/chronogate.spec --noconfirm
#
# Produces  dist/ChronoGate/            (Windows/Linux one-folder)
#      and  dist/ChronoGate.app/        (macOS bundle)
#
# The sample data (3_FLIM_stack_ptu) is deliberately NOT bundled -- that is the
# user's data, not the program. PySide6 / matplotlib / numpy are pulled in by
# PyInstaller's bundled hooks; we only add what those miss.
import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules

# The spec runs with the repo root as the working directory (that is where
# PyInstaller is invoked from), so resolve paths from there.
ROOT = Path.cwd()
ICON_ICNS = ROOT / "ChronoGate.app" / "Contents" / "Resources" / "ChronoGate.icns"
ICON_ICO = ROOT / "packaging" / "chronogate.ico"

# Read the version without importing the package (no deps needed at spec time).
_ver: dict = {}
exec((ROOT / "chronogate" / "__init__.py").read_text().split("__all__")[0], _ver)
VERSION = _ver.get("__version__", "0.0.0")

hiddenimports = collect_submodules("chronogate")

a = Analysis(
    [str(ROOT / "run.py")],
    pathex=[str(ROOT)],
    binaries=[],
    datas=[],
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=[
        # Trim weight: the interactive/dev stacks are never used by the app.
        "tkinter", "pytest", "PySide6.QtWebEngineCore", "PySide6.QtWebEngineWidgets",
        "PySide6.Qt3DCore", "PySide6.QtCharts", "PySide6.QtDataVisualization",
        "PySide6.QtQuick", "PySide6.QtQml", "PySide6.QtMultimedia",
    ],
    noarchive=False,
)
pyz = PYZ(a.pure)

_icon = None
if sys.platform == "win32" and ICON_ICO.exists():
    _icon = str(ICON_ICO)
elif sys.platform == "darwin" and ICON_ICNS.exists():
    _icon = str(ICON_ICNS)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="ChronoGate",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,                 # UPX corrupts Qt plugins on macOS; off everywhere
    console=False,             # GUI app: no console window on Windows
    disable_windowed_traceback=False,
    argv_emulation=False,
    icon=_icon,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="ChronoGate",
)

if sys.platform == "darwin":
    app = BUNDLE(
        coll,
        name="ChronoGate.app",
        icon=str(ICON_ICNS) if ICON_ICNS.exists() else None,
        bundle_identifier="edu.rice.chronogate",
        version=VERSION,
        info_plist={
            "CFBundleName": "ChronoGate",
            "CFBundleDisplayName": "ChronoGate",
            "CFBundleShortVersionString": VERSION,
            "NSHighResolutionCapable": True,
            # Native arm64 GUI; never launch translated (matches the app's own guard).
            "LSArchitecturePriority": ["arm64", "x86_64"],
        },
    )
