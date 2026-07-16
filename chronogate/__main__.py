"""Command-line entry point: ``python -m chronogate [file.ptu] [options]``.

Resolves a .ptu path (a CLI arg or a directory to search) and opens the Qt app.
With no argument, the app itself shows a native file picker at startup -- so this
module stays Qt-free (and ``--help`` works without PySide6 or a display).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .loader import UnsupportedFileError

# Where the example z-stack lives, used as the file-dialog default.
_DEFAULT_DATA_DIR = Path(__file__).resolve().parent.parent / "3_FLIM_stack_ptu"


def _reexec_native_arm64() -> None:
    """On Apple Silicon under Rosetta, re-run this same interpreter natively.

    The Qt GUI is much more stable as a native arm64 process. This works only for
    a *universal2* interpreter (which has an arm64 slice) -- a single-architecture
    x86-64 build (e.g. a conda env) is left as-is, and the app prints a warning
    at launch suggesting a native Python or the ``ChronoGate.command`` launcher.
    """
    import os
    import subprocess

    # A frozen (PyInstaller) app is already built for the right architecture, and
    # sys.executable is the bundle, not a Python that understands -m chronogate.
    if getattr(sys, "frozen", False):
        return
    if sys.platform != "darwin" or os.environ.get("CHRONOGATE_NATIVE"):
        return
    try:
        translated = subprocess.run(
            ["sysctl", "-in", "sysctl.proc_translated"],
            capture_output=True, text=True).stdout.strip()
    except Exception:  # noqa: BLE001
        return
    if translated != "1":  # not running under Rosetta -> already native
        return
    exe = sys.executable
    try:  # can THIS interpreter run as arm64 (i.e. is it universal2)?
        can = subprocess.run(
            ["arch", "-arm64", exe, "-c", "print(1)"],
            capture_output=True, text=True, timeout=30).stdout.strip() == "1"
    except Exception:  # noqa: BLE001
        can = False
    if not can:
        return
    os.environ["CHRONOGATE_NATIVE"] = "1"  # guard against a re-exec loop
    try:
        os.execvp("arch", ["arch", "-arm64", exe, "-m", "chronogate", *sys.argv[1:]])
    except Exception:  # noqa: BLE001 - fall through and run translated
        pass


def _first_ptu_under(directory: Path) -> Path | None:
    matches = sorted(directory.rglob("*.ptu"))
    return matches[0] if matches else None


def _resolve_path(arg: str | None) -> Path | None:
    """Resolve an explicit path/folder arg, or None to defer to the app's picker."""
    if not arg:
        return None
    p = Path(arg)
    if p.is_dir():
        return _first_ptu_under(p)
    return p


def main(argv: list[str] | None = None) -> int:
    _reexec_native_arm64()  # go native on Apple Silicon before loading the GUI

    parser = argparse.ArgumentParser(
        prog="chronogate",
        description="Interactive time-gating viewer for PicoQuant FLIM (.ptu) data.",
    )
    parser.add_argument(
        "path", nargs="?", help="A .ptu file, or a folder to search (omit for a file picker)."
    )
    parser.add_argument("--channel", type=int, default=0, help="Detector channel (default 0).")
    parser.add_argument(
        "--pick-frame", type=int, default=None, metavar="N",
        help="Show only frame N instead of summing all frames in the file.",
    )
    parser.add_argument(
        "--settings", type=str, default=None, help="Load gate/view settings from a JSON file at startup."
    )
    parser.add_argument(
        "--lifetime", action="store_true",
        help="Start in two-gate rapid-lifetime (RLD) mode.",
    )
    args = parser.parse_args(argv)

    path = _resolve_path(args.path)
    if args.path and path is None:
        print(f"No .ptu file found under: {args.path}", file=sys.stderr)
        return 2
    if path is not None and not path.exists():
        print(f"File not found: {path}", file=sys.stderr)
        return 2

    # A default directory for the app's startup picker (when no path is given).
    open_dir = str(_DEFAULT_DATA_DIR if _DEFAULT_DATA_DIR.exists() else Path.cwd())

    # Import the Qt app lazily so --help works without PySide6 / a display.
    from .ui.app import launch

    try:
        return launch(
            path,
            channel=args.channel,
            sum_frames=(args.pick_frame is None),
            settings_path=args.settings,
            start_lifetime=args.lifetime,
            open_dir=open_dir,
        )
    except UnsupportedFileError as exc:
        # Defensive parsing: report exactly what was found, no traceback noise.
        print(f"Cannot open this file: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
