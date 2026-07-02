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
    # A second pick REPLACES the first (single decay at a time, not cumulative).
    c._add_pixel(70, 70)
    assert len(c.picks) == 1 and c.picks[0]["r"] == 70, "new pick must replace, not accumulate"
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


def test_cache_lockscale_and_t0() -> None:
    _window()  # ensure a QApplication exists
    from chronogate.ui.main_window import MainWindow
    w = MainWindow(None, open_dir=str(DATA_DIR))
    c = w.controller
    c.load_folder(str(DATA_DIR))
    # frame cache: revisiting a plane is a cache hit (no re-decode)
    c.step_z(1)
    key = (str(c.stack[c.z_index]), c.channel, c.sum_frames)
    assert c._cube_cache.get(key) is not None, "current plane must be cached"
    c.step_z(-1); c.step_z(1)
    assert c._cube_cache.get(key) is not None, "revisited plane must still be cached"

    # lock scale freezes the colour range across planes
    c.z_index = 0; c._reload_model_busy(); c._refit_ranges(); c._refresh_image()
    c._on_lock_scale(True)
    vmax = c._locked_clim["intensity"][1]
    c.step_z(1)
    assert abs(c.im.get_clim()[1] - vmax) < 1e-6, "locked vmax persists across planes"
    c._on_lock_scale(False)

    # manual t0 override persists across a plane change
    t = c.model.t0_ns() + 0.4
    c._on_t0(t)
    c.step_z(1)
    assert abs(c.model.t0_ns() - t) < 0.25, "manual t0 persists across planes"
    c._on_t0_auto()
    assert c.manual_t0_ns is None
    print("OK: frame cache hits, lock-scale freezes clim, manual t0 persists & resets.")


def test_wave_c_views() -> None:
    w = _window()
    c = w.controller
    # phasor mode: renders (hexbin + semicircle) and disables picks
    c._enter_mode("phasor")
    assert c.mode == "phasor" and len(c._phasor_artists) >= 1
    c._on_image_press(_FakeEvent(c.ic.ax, 100, 100, 50.0, 50.0))
    assert c._press_xy is None, "picks must be disabled in phasor mode"
    # lifetime: HSV toggle + tau histogram inset (pool photons so tau resolves)
    w.binning.bin.setValue(8)
    c._enter_mode("lifetime"); c.hsv_lifetime = True; c._refresh_image()
    assert c._tau_hist_ax is not None, "tau histogram inset should be drawn"
    c._enter_mode("intensity")
    assert c._tau_hist_ax is None and c.im.get_visible(), "image restored after phasor/hist"
    # pin decay: pin one, click another -> two decays shown
    r, col = np.unravel_index(int(c.model.intensity.argmax()), c.model.intensity.shape)
    c._add_pixel(int(r), int(col)); c._on_pin()
    assert len(c.pinned_picks) == 1 and len(c.picks) == 0
    c._add_pixel(int(r) + 8, int(col) + 8)
    assert len(c._shown_picks()) == 2, "pinned + live decays overlaid"
    c._clear_picks(); assert not c._shown_picks()
    # channel combine (the example file has 2 channels)
    if c.model.cube.n_channels >= 2:
        for mode in ("ratio A/B", "merge RGB", "single"):
            c.combine = mode; c._refresh_image()
    print("OK: phasor · HSV lifetime · τ-histogram · pin decay · channel combine all render.")


def test_probe_and_batch_export() -> None:
    import tempfile
    from pathlib import Path as P
    from chronogate.loader import probe_ptu
    files = sorted(DATA_DIR.rglob("*.ptu"))
    assert probe_ptu(files[0]) == "image", "the example stack must probe as a FLIM image"
    assert probe_ptu(DATA_DIR / "nope.ptu") == "error"
    _window()  # ensure app
    from chronogate.ui.main_window import MainWindow
    w = MainWindow(None, open_dir=str(DATA_DIR))
    c = w.controller
    c.load_folder(str(DATA_DIR))
    assert len(c.stack) > 1
    c.stack = c.stack[:3]                    # keep the batch quick
    out = tempfile.mkdtemp()
    n = c.batch_export(out)
    assert n == 3
    tiffs = list(P(out).rglob("*_gated_raw.tif"))
    assert len(tiffs) == 3, "batch writes one export per plane"
    # provenance carries the version stamps + resolved t0
    meta = json.loads(next(P(out).rglob("*_provenance.json")).read_text())["metadata"]
    assert meta["chronogate_version"] and "t0_bin" in meta and "ptufile_version" in meta
    print(f"OK: probe classifies files; batch exported {n} planes; provenance versioned.")


def test_threaded_decode() -> None:
    import time
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    _window()  # ensure app
    from chronogate.ui.main_window import MainWindow
    w = MainWindow(None, open_dir=str(DATA_DIR))
    c = w.controller
    c.load_folder(str(DATA_DIR))       # synchronous initial load
    c.async_decode = True              # subsequent decodes go on a QThread
    z0 = c.z_index
    c.step_z(2)                        # uncached plane -> background decode
    t0 = time.monotonic()
    while c._decode_thread is not None and time.monotonic() - t0 < 30:
        app.processEvents()
    assert c.z_index == z0 + 2 and c.model is not None, "threaded decode must deliver the model"
    assert c._decode_thread is None, "the decode thread must be torn down"
    c.stop_decode()                    # idempotent when idle (close-time join)
    print("OK: background (QThread) decode delivers the model and joins cleanly.")


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
    if not list(DATA_DIR.rglob("*.ptu")):
        # The sample stack is not version-controlled (large), so on CI / a fresh
        # checkout there is nothing to drive the GUI with. Skip cleanly; the
        # numeric analysis is covered by test_gating.py (which needs no data).
        print(f"No sample .ptu under {DATA_DIR.name}; skipping UI smoke tests.")
        raise SystemExit(0)
    try:
        test_window_builds_and_renders()
        test_welcome_state_and_folder_load()
        test_lifetime_export_and_settings_roundtrip()
        test_picks_and_keyboard_helpers()
        test_fit_overlay()
        test_wave_c_views()
        test_cache_lockscale_and_t0()
        test_probe_and_batch_export()
        test_threaded_decode()
        test_floor_slider_per_pixel_and_scale()
    except AssertionError as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        raise SystemExit(1)
    print("All UI smoke tests passed.")
