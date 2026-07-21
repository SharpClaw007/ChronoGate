"""Generate the annotated screenshots used by the ChronoGate SOP.

Drives the real app offscreen, captures specific panels / dialogs / states, and
bakes numbered callouts onto them (see :mod:`annotate`). Re-run this whenever the
UI changes and the SOP figures refresh automatically:

    QT_QPA_PLATFORM=offscreen PYTHONPATH=. .venv/bin/python docs/sop/figures.py

Outputs PNGs into ``docs/sop/img/``. Requires the sample ``.ptu`` stack locally
(the same data the UI tests use); the generated PNGs are committed, so building
the PDF or reading the SOP needs no data.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("MPLBACKEND", "Agg")

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))
IMG = Path(__file__).resolve().parent / "img"
DATA = ROOT / "3_FLIM_stack_ptu"

import matplotlib  # noqa: E402
matplotlib.use("Agg")
from PySide6.QtWidgets import QApplication  # noqa: E402

from docs.sop.annotate import annotate, rect_of, save  # noqa: E402

_APP = None


def _app():
    global _APP
    _APP = QApplication.instance() or QApplication([])
    return _APP


def _sample() -> Path | None:
    hits = sorted(DATA.rglob("*.ptu"), key=lambda p: p.stat().st_size)
    return hits[0] if hits else None


def _pump(win, until, tries: int = 200):
    """Pump the event loop until ``until(win)`` is true (async decode finishes)."""
    app = _app()
    for _ in range(tries):
        if until(win):
            return True
        app.processEvents()
    return until(win)


def _window(path):
    from chronogate.ui import theme
    from chronogate.ui.main_window import MainWindow
    theme.apply_matplotlib_theme()
    app = _app()
    app.setStyleSheet(theme.CLINICAL_QSS())
    theme.apply_qt_palette(app)
    win = MainWindow(path)
    win.resize(1420, 850)
    win.show()
    _pump(win, lambda w: w.controller.model is not None)
    _pump(win, lambda w: True, tries=10)   # a few extra frames to settle layout
    return win


# --------------------------------------------------------------------------- #
def fig_overview(win) -> None:
    """The whole window with the six panels called out."""
    pm = win.grab()
    marks = [
        {"rect": rect_of(win.filep, win), "n": 1},
        {"rect": rect_of(win.gate, win), "n": 2},
        {"rect": rect_of(win.display, win), "n": 3},
        {"rect": rect_of(win.binning, win), "n": 4},
        {"rect": rect_of(win.lifetime, win), "n": 5},
        {"rect": rect_of(win.picks, win), "n": 6, "badge": "bl"},
    ]
    save(annotate(pm, marks), IMG / "overview.png")


def fig_gate(win) -> None:
    """The Gate panel: start/end, t0 and its auto/reset controls."""
    g = win.gate
    pm = g.grab()
    marks = [
        {"rect": rect_of(g.spin_lo, g), "n": 1},
        {"rect": rect_of(g.spin_hi, g), "n": 2},
        {"rect": rect_of(g.t0, g), "n": 3},
        {"rect": rect_of(g.btn_t0_auto, g), "n": 4, "badge": "tr"},
        {"rect": rect_of(g.btn_t0_reset, g), "n": 5, "badge": "tr"},
    ]
    save(annotate(pm, marks), IMG / "gate.png")


def fig_binning(win) -> None:
    """The Binning panel: factor, target, Auto and the reset button."""
    b = win.binning
    pm = b.grab()
    marks = [
        {"rect": rect_of(b.bin, b), "n": 1},
        {"rect": rect_of(b.target, b), "n": 2},
        {"rect": rect_of(b.btn_auto, b), "n": 3, "badge": "tr"},
        {"rect": rect_of(b.btn_bin_reset, b), "n": 4, "badge": "tr"},
    ]
    save(annotate(pm, marks), IMG / "binning.png")


def fig_display(win) -> None:
    """The Display panel: threshold, noise floor with auto/reset, colormap."""
    d = win.display
    pm = d.grab()
    marks = [
        {"rect": rect_of(d.thr, d), "n": 1},
        {"rect": rect_of(d.floor, d), "n": 2},
        {"rect": rect_of(d.btn_floor_auto, d), "n": 3, "badge": "tr"},
        {"rect": rect_of(d.btn_floor_reset, d), "n": 4, "badge": "tr"},
        {"rect": rect_of(d.cmap, d), "n": 5},
    ]
    save(annotate(pm, marks), IMG / "display.png")


def fig_reconv_dialog() -> None:
    """The IRF reconvolution fit dialog with its sections called out."""
    from chronogate.ui.reconv_dialog import ReconvDialog
    _app()
    dlg = ReconvDialog(has_selection=True, resolution_ns=0.097,
                       default_center_ns=0.5, default_threshold=50)
    dlg.resize(430, dlg.sizeHint().height())
    dlg.show()
    _pump(dlg, lambda d: True, tries=10)
    pm = dlg.grab()
    marks = [
        {"rect": rect_of(dlg.rb_gauss, dlg), "n": 1},
        {"rect": rect_of(dlg.rb_measured, dlg), "n": 2},
        {"rect": rect_of(dlg.cb_model, dlg), "n": 3},
        {"rect": rect_of(dlg.cb_obj, dlg), "n": 4},
        {"rect": rect_of(dlg.sp_thresh, dlg), "n": 5},
        {"rect": rect_of(dlg.rb_map, dlg), "n": 6},
    ]
    save(annotate(pm, marks), IMG / "reconv_dialog.png")


def fig_export_dialog() -> None:
    """The export dialog: artefact checkboxes, folder, report + Fiji."""
    from chronogate.ui.export_dialog import ExportDialog
    _app()
    dlg = ExportDialog(has_selection=True, sel_rows=1200, sel_bytes=48000,
                       n_planes=1, default_dir="/data/exports", fiji_configured=True)
    dlg.resize(560, dlg.sizeHint().height())
    dlg.show()
    _pump(dlg, lambda d: True, tries=10)
    pm = dlg.grab()
    marks = [
        {"rect": rect_of(dlg.chk_raw, dlg), "n": 1},
        {"rect": rect_of(dlg.chk_report, dlg), "n": 2},
        {"rect": rect_of(dlg.chk_restrict, dlg), "n": 3},
    ]
    if getattr(dlg, "btn_fiji", None) is not None:
        marks.append({"rect": rect_of(dlg.btn_fiji, dlg), "n": 4, "badge": "tr"})
    save(annotate(pm, marks), IMG / "export_dialog.png")


def build_all() -> list[str]:
    IMG.mkdir(parents=True, exist_ok=True)
    made: list[str] = []
    sample = _sample()
    if sample is not None:
        win = _window(sample)
        for fn in (fig_overview, fig_gate, fig_binning, fig_display):
            fn(win)
            made.append(fn.__name__)
    else:
        print(f"WARNING: no sample .ptu under {DATA.name}; skipping window figures "
              "(committed PNGs are used instead).", file=sys.stderr)
    for fn in (fig_reconv_dialog, fig_export_dialog):
        fn()
        made.append(fn.__name__)
    return made


if __name__ == "__main__":
    names = build_all()
    print("generated figures:", ", ".join(names))
    print("into:", IMG)
