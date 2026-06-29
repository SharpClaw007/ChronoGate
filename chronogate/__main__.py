"""Command-line entry point: ``python -m chronogate [file.ptu] [options]``.

Resolves a .ptu path (CLI arg, a directory to search, or a file dialog
defaulting to the example data folder), then opens the interactive viewer.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .loader import UnsupportedFileError

# Where the example z-stack lives, used as the file-dialog default.
_DEFAULT_DATA_DIR = Path(__file__).resolve().parent.parent / "3_FLIM_stack_ptu"


def _first_ptu_under(directory: Path) -> Path | None:
    matches = sorted(directory.rglob("*.ptu"))
    return matches[0] if matches else None


def _pick_file_dialog() -> Path | None:
    """Open a Tk open-file dialog defaulting to the example folder, if possible."""
    try:
        import tkinter as tk
        from tkinter import filedialog
    except Exception:
        return None
    root = tk.Tk()
    root.withdraw()
    initial = _DEFAULT_DATA_DIR if _DEFAULT_DATA_DIR.exists() else Path.cwd()
    chosen = filedialog.askopenfilename(
        title="Open a PicoQuant .ptu file",
        initialdir=str(initial),
        filetypes=[("PicoQuant PTU", "*.ptu"), ("All files", "*.*")],
    )
    root.destroy()
    return Path(chosen) if chosen else None


def _resolve_path(arg: str | None) -> Path | None:
    if arg:
        p = Path(arg)
        if p.is_dir():
            return _first_ptu_under(p)
        return p
    # No argument: try a file dialog; if that's unavailable, grab the first
    # example file so the tool still does something useful headlessly.
    chosen = _pick_file_dialog()
    if chosen:
        return chosen
    if _DEFAULT_DATA_DIR.exists():
        return _first_ptu_under(_DEFAULT_DATA_DIR)
    return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="chronogate",
        description="Interactive time-gating viewer for PicoQuant FLIM (.ptu) data.",
    )
    parser.add_argument(
        "path", nargs="?", help="A .ptu file, or a folder to search (defaults to a file dialog)."
    )
    parser.add_argument("--channel", type=int, default=0, help="Detector channel (default 0).")
    parser.add_argument(
        "--pick-frame", type=int, default=None, metavar="N",
        help="Show only frame N instead of summing all frames in the file.",
    )
    parser.add_argument(
        "--settings", type=str, default=None, help="Load gate/view settings from a JSON file at startup."
    )
    args = parser.parse_args(argv)

    path = _resolve_path(args.path)
    if path is None:
        print("No .ptu file selected or found. Pass a file path explicitly.", file=sys.stderr)
        return 2
    if not path.exists():
        print(f"File not found: {path}", file=sys.stderr)
        return 2

    # Import the viewer lazily so --help works without a display/matplotlib GUI.
    from .viewer import GatingViewer

    try:
        viewer = GatingViewer(
            path,
            channel=args.channel,
            sum_frames=(args.pick_frame is None),
        )
    except UnsupportedFileError as exc:
        # Defensive parsing: report exactly what was found, no traceback noise.
        print(f"Cannot open this file: {exc}", file=sys.stderr)
        return 1

    if args.settings:
        from .export import load_settings

        viewer._apply_settings(load_settings(args.settings))

    viewer.show()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
