"""Headless smoke test for the PySide6 UI.

Builds the real :class:`MainWindow` on the offscreen Qt platform and drives the
controller's slots directly -- no window is shown and no native dialog is opened
-- to confirm the wiring holds end to end: both render modes, a gate edit, the
lifetime computation, export of all four artefacts, and a settings round-trip.

The analysis correctness lives in ``test_gating.py`` (which stays Qt-free); this
file only checks that the GUI plumbing is intact. It skips cleanly if PySide6 is
not installed.

Run directly::

    python test_ui_smoke.py

or under pytest (``pytest test_ui_smoke.py``); it skips if PySide6 is missing.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

# Must be set before any Qt import.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("MPLBACKEND", "QtAgg")

try:  # pragma: no cover - environment guard
    import pytest
    pytest.importorskip("PySide6")
except ImportError:
    try:
        import PySide6  # noqa: F401
    except ImportError:
        print("PySide6 not installed; skipping UI smoke test.")
        raise SystemExit(0)

import numpy as np

DATA_DIR = Path(__file__).resolve().parent / "3_FLIM_stack_ptu"


def _example_file() -> Path:
    candidates = sorted(DATA_DIR.rglob("*.ptu"), key=lambda p: p.stat().st_size)
    if not candidates:
        raise FileNotFoundError(f"No .ptu files under {DATA_DIR}.")
    return candidates[0]


_APP = None


def _window(path=None):
    """A MainWindow on a single shared (offscreen) QApplication.

    With no ``path`` it opens to the welcome state (no file loaded).
    """
    global _APP
    import matplotlib
    matplotlib.use("QtAgg")
    from PySide6.QtWidgets import QApplication
    from chronogate.ui import theme
    from chronogate.ui.main_window import MainWindow

    theme.apply_matplotlib_theme()
    _APP = QApplication.instance() or QApplication([])
    return MainWindow(_example_file() if path is None else path)


def test_welcome_state_and_folder_load() -> None:
    from chronogate.ui.main_window import MainWindow
    _window()  # ensure a QApplication exists
    w = MainWindow(None, open_dir=str(DATA_DIR))
    assert w.controller.model is None
    assert w._stack.currentWidget() is w._welcome
    assert not w.act_export.isEnabled()
    assert w.act_open.isEnabled(), "Open must stay enabled on the welcome screen"

    # Opening a folder finds and loads the numbered stack.
    w.controller.load_folder(str(DATA_DIR))
    assert w.controller.model is not None
    assert len(w.controller.stack) > 1, "folder should resolve to a multi-plane stack"
    assert w._stack.currentWidget() is w._workspace
    assert w.act_export.isEnabled() and w.filep.z.maximum() == len(w.controller.stack) - 1
    z0 = w.controller.z_index
    w.controller.step_z(1)
    assert w.controller.z_index == z0 + 1
    print(f"OK: welcome state + folder load ({len(w.controller.stack)} planes) + z-step.")


def test_window_builds_and_renders() -> None:
    w = _window()
    c = w.controller
    assert c.mode == "intensity"
    assert c.im.get_array() is not None and c.decay_line.get_xdata().size > 0
    assert "ChronoGate" in w.windowTitle()
    assert not w.lifetime.isEnabled(), "lifetime panel should be disabled in intensity mode"
    print("OK: window builds; intensity decay + image render.")


def test_lifetime_export_and_settings_roundtrip() -> None:
    w = _window()
    c = w.controller

    # Switch to lifetime via the toolbar action and pool photons so tau resolves.
    w.act_lifetime.trigger()
    assert c.mode == "lifetime" and w.lifetime.isEnabled()
    c.rld_min_counts = 5.0
    w.binning.bin.setValue(8)  # emits valueChanged -> live rebuild
    tau, rl = c._compute_lifetime_map()
    finite = tau[np.isfinite(tau)]
    assert finite.size > 0, "no valid lifetime pixels after 8x8 binning"
    assert 0 < float(np.median(finite)) < c.model.cube.period_ns

    # Edit gate A through the ns spin boxes (the editingFinished path).
    c.edit_target = "A"
    w.gate.spin_lo.setValue(2.0)
    w.gate.spin_hi.setValue(7.0)
    c._on_gate_text()
    assert c._get_gate("A") != (c.model.t0_bin, c.model.n_bins - 1)

    # Export the lifetime map: float tau TIFF + colormapped PNG + CSV + provenance.
    out = Path(tempfile.mkdtemp())
    paths = c.export(out)
    assert all(Path(p).exists() for p in paths.values())
    prov = json.loads(next(out.glob("*RLD*_provenance.json")).read_text())
    assert prov["settings"]["mode"] == "lifetime"
    assert prov["raw_tiff_dtype"] == "float32"

    # Settings round-trip restores state and disables the lifetime panel.
    s = c._settings()
    c.apply_settings({**s, "mode": "intensity"})
    assert c.mode == "intensity" and not w.lifetime.isEnabled()
    print(f"OK: lifetime export ({len(paths)} files) + settings round-trip; "
          f"median tau ~ {np.median(finite):.2f} ns.")


def test_irf_flow() -> None:
    from chronogate.loader import Irf
    w = _window()
    c = w.controller
    m = c.model
    # A synthetic IRF on the sample's grid (no IRF .ptu needed for the test).
    peak = 30
    irf_counts = np.exp(-0.5 * ((np.arange(m.n_bins) - peak) / 3.0) ** 2) * 1000.0
    irf = Irf(counts=irf_counts, resolution_ns=m.resolution_ns, n_bins=m.n_bins,
              channel=0, path=Path("synthetic_irf.ptu"))
    c.irf = irf
    c.irf_channel = 0
    c._reapply_irf()
    w.irf.set_irf_controls_enabled(True)

    assert c.model.irf is not None
    assert c.model.t0_bin == peak, "t0 must come from the IRF peak"
    lo, hi = c.model.instrument_window
    assert lo <= peak <= hi, "instrument window must bracket the IRF peak"

    # Instrument view + sample view both render; subtraction + scale apply.
    c.irf_view = "instrument"; c._refresh_image()
    c.irf_view = "sample"
    c.irf_subtract = True; c.model.irf_subtract = True
    c.irf_scale = 0.5; c.model.irf_scale = 0.5
    c._refresh_image()

    # Provenance carries the IRF fields.
    out = Path(tempfile.mkdtemp())
    paths = c.export(out)
    s = json.loads(next(out.glob("*_provenance.json")).read_text())["settings"]
    assert s["irf_file"] == "synthetic_irf.ptu" and s["t0_from_irf"] and s["irf_subtract"]
    assert s["instrument_window"] == [lo, hi]
    print("OK: IRF flow — t0 from peak, instrument window, subtraction, provenance.")


class _FakeEvent:
    """Minimal stand-in for a matplotlib MouseEvent (bypasses coord translation)."""
    def __init__(self, ax, x, y, xdata, ydata, button=1):
        self.inaxes, self.button = ax, button
        self.x, self.y, self.xdata, self.ydata = x, y, xdata, ydata


def test_picks_and_keyboard_helpers() -> None:
    w = _window()
    c = w.controller
    c._add_pixel(40, 40)
    assert len(c.picks) == 1 and w.picks.list.count() == 1
    w.picks.btn_clear.click()
    assert len(c.picks) == 0 and w.picks.list.count() == 0

    # A press far from the release = a box drag -> an ROI pick (the reported bug).
    ax = c.ic.ax
    c._on_image_press(_FakeEvent(ax, 100, 100, 50.0, 60.0))
    c._on_image_release(_FakeEvent(ax, 240, 240, 150.0, 180.0))
    assert len(c.picks) == 1 and c.picks[0]["kind"] == "roi", "box drag must add an ROI"
    w.picks.btn_clear.click()

    # A press+release at the same spot = a click -> a single-pixel pick.
    c._on_image_press(_FakeEvent(ax, 100, 100, 50.0, 60.0))
    c._on_image_release(_FakeEvent(ax, 101, 101, 50.0, 60.0))
    assert len(c.picks) == 1 and c.picks[0]["kind"] == "pixel", "click must add a pixel"
    w.picks.btn_clear.click()

    c.enter_lifetime()
    before = c._get_gate("A")
    c.nudge_gate(1, 1)
    after = c._get_gate("A")
    assert after[0] == before[0] + 1 and after[1] == before[1] + 1
    print("OK: pixel pick/clear and gate-nudge shortcut work.")


def test_fit_overlay() -> None:
    from chronogate import gating
    w = _window()
    c = w.controller
    r, col = np.unravel_index(int(c.model.intensity.argmax()), c.model.intensity.shape)
    c._add_pixel(int(r), int(col))          # a bright, decaying pixel
    n_plain = len(c._pick_lines)
    raw = c.model.pixel_decay(int(r), int(r) + 1, int(col), int(col) + 1)
    fit_ok = gating.fit_mono_exponential(c.model.cube.time_axis_ns, raw, c.model.t0_ns()) is not None

    w.picks.fit.setChecked(True)            # exp fit on -> rebuild with overlay
    assert c.fit_curve
    if fit_ok:
        assert len(c._pick_lines) == n_plain + 1, "fit adds one smooth overlay line"
        labels = [w.picks.list.item(i).text() for i in range(w.picks.list.count())]
        assert any("τ" in t for t in labels), "the fit reports an apparent tau"
    w.picks.fit.setChecked(False)
    assert not c.fit_curve and len(c._pick_lines) == n_plain
    print(f"OK: exp-fit overlay toggles (fit {'drawn' if fit_ok else 'skipped: no decay'}).")


def test_floor_slider_per_pixel_and_scale() -> None:
    import math
    w = _window()
    c = w.controller
    f = w.display.floor
    # The floor is a fractional, per-pixel control ranged 0 .. brightest pixel.
    assert f._decimals == 3 and f._max == c.model.peak_counts_per_bin()
    assert abs(f.value() - c.model.auto_noise_floor_pp()) < 0.01, "default is the auto per-pixel floor"
    # Fractional => the slider is always index-based; the scale sets the mapping.
    assert f.slider.maximum() == f._STEPS
    assert c.log_scale                              # log-Y on by default
    lo, hi = f._bounds()
    mid = f._value_from_pos(f._STEPS // 2)
    assert abs(mid - math.sqrt(lo * hi)) / math.sqrt(lo * hi) < 0.03, "log midpoint ~ geometric mean"
    w.act_log.toggled.emit(False)                  # uncheck Log Y -> linear mapping
    assert not c.log_scale
    lo, hi = f._bounds()
    mid = f._value_from_pos(f._STEPS // 2)
    assert abs(mid - (lo + hi) / 2) / ((lo + hi) / 2) < 0.03, "linear midpoint ~ arithmetic mean"
    # The custom input box keeps an exact fractional value across a scale switch.
    v = round(min(2.5, f._max), 2)
    f.spin.setValue(v)
    assert f.value() == v
    w.act_log.toggled.emit(True)
    assert c.log_scale and f.value() == v
    # Cranking the floor to the top can drive the whole gated image to zero.
    c.apply_floor = True
    c.noise_floor_pp = float(f._max)
    gated = c.model.gate(c.gate_lo_bin, c.gate_hi_bin, floor_per_bin=c.noise_floor_pp)
    assert float(gated.sum()) == 0.0, "max per-pixel floor should zero the image"
    print("OK: per-pixel floor slider follows the y-axis scale; fractional spinbox stays exact; max zeros the image.")


if __name__ == "__main__":
    try:
        test_window_builds_and_renders()
        test_welcome_state_and_folder_load()
        test_lifetime_export_and_settings_roundtrip()
        test_irf_flow()
        test_picks_and_keyboard_helpers()
        test_fit_overlay()
        test_floor_slider_per_pixel_and_scale()
    except AssertionError as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        raise SystemExit(1)
    print("All UI smoke tests passed.")
