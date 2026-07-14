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
    # the image extent must match the data shape (else it's squished into a
    # corner and the panel looks blank; also keeps mouse-picking in pixel coords)
    ny, nx = c.model.intensity.shape
    assert list(c.im.get_extent()) == [-0.5, nx - 0.5, ny - 0.5, -0.5], c.im.get_extent()
    assert c.im.get_visible() and tuple(c.ic.ax.get_xlim()) == (-0.5, nx - 0.5)
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

    # the readout above the image reports the SELECTED pixel's total-in-gate
    r, col = np.unravel_index(int(c.model.intensity.argmax()), c.model.intensity.shape)
    c._add_pixel(int(r), int(col))
    floor = c._floor_per_pixel() if c.apply_floor else 0.0
    gated = c.model.gate(c.gate_lo_bin, c.gate_hi_bin, floor_per_bin=floor)
    title = c.ic.ax.get_title()
    assert f"px({r},{col})" in title and f"{int(gated[r, col]):,} photons in gate" in title, title
    w.picks.btn_clear.click()
    assert "px(" not in c.ic.ax.get_title(), "readout reverts to the image total on clear"

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


def test_hover_probe_blits() -> None:
    """The hover curve tracks the cursor live -- drawn by blitting, not a full redraw."""
    w = _window()
    c = w.controller
    ax = c.ic.ax
    assert c.hover_probe and not c._hovering
    assert c.hover_line.get_animated(), "hover artists must be animated (kept out of "\
                                        "ordinary draws, so the blit background is clean)"
    assert not c.hover_line.get_visible()

    # One motion opens a hover session: the axes freeze and the curve is painted.
    c._on_image_motion(_FakeEvent(ax, 0, 0, 33.0, 22.0))
    assert c._hovering and c._hover_bg is not None, "the static background is cached"
    assert c._hover_rc == (22, 33)
    assert c.hover_pick == {"kind": "pixel", "r": 22, "c": 33, "label": "px(22,33)"}
    assert c.hover_line.get_visible() and c.hover_line.get_xdata().size == c.model.n_bins
    assert np.allclose(c.hover_line.get_ydata(),
                       c._smooth(c.model.pixel_decay(22, 23, 33, 34), c.smooth_bins))
    assert "px(22,33)" in c.hover_text.get_text() and "in gate" in c.hover_text.get_text()
    probe = w.probe_label.text()
    assert probe.startswith("px(22,33)") and f"{int(c._gated[22, 33]):,}" in probe, probe

    # The hovered pixel is NOT a pick: it draws over the locked/pinned decays.
    assert not c.picks and not c._shown_picks()

    # The frozen y-range must fit the brightest pixel in the image, so the axis
    # never rescales mid-sweep (which would invalidate the cached background).
    assert c.dc.ax.get_ylim()[1] >= c.model.peak_counts_per_bin()
    ylim = c.dc.ax.get_ylim()

    # Moving to another pixel repaints the curve without re-doing the axes.
    c._on_image_motion(_FakeEvent(ax, 0, 0, 90.0, 80.0))
    assert c.hover_pick["r"] == 80 and c.hover_pick["c"] == 90
    assert c.dc.ax.get_ylim() == ylim, "the axis must stay frozen across the sweep"
    assert np.allclose(c.hover_line.get_ydata(),
                       c._smooth(c.model.pixel_decay(80, 81, 90, 91), c.smooth_bins))

    # Staying inside the same pixel is free.
    rc = c._hover_rc
    c._on_image_motion(_FakeEvent(ax, 0, 0, 90.2, 80.1))
    assert c._hover_rc == rc

    # Leaving closes the session: the hover artists go away and the axes unfreeze.
    c._on_image_leave()
    assert not c._hovering and c.hover_pick is None and c._hover_bg is None
    assert not c.hover_line.get_visible() and c._hover_fill is None
    assert w.probe_label.text() == ""

    # A click locks the hovered pixel in and closes the session.
    c._on_image_motion(_FakeEvent(ax, 0, 0, 50.0, 50.0))
    assert c._hovering
    c._add_pixel(50, 50)
    assert not c._hovering and c.hover_pick is None
    assert len(c.picks) == 1 and c.picks[0]["r"] == 50
    assert "px(50,50)" in c.ic.ax.get_title(), "the image readout reports the LOCKED pick"

    # Hovering over a locked pick draws on top of it, it does not replace it.
    c._on_image_motion(_FakeEvent(ax, 0, 0, 33.0, 22.0))
    assert c._shown_picks() == c.picks, "the locked decay stays on the panel while hovering"
    assert c.hover_line.get_visible(), "...with the hovered pixel blitted over it"
    c._on_image_leave()

    # Hover off = no session at all.
    w.picks.hover.setChecked(False)
    c._on_image_motion(_FakeEvent(ax, 0, 0, 33.0, 22.0))
    assert not c._hovering and c.hover_pick is None, "hover off -> no preview"
    w.picks.hover.setChecked(True)
    print("OK: hover blits the pixel's decay live; axis frozen; click locks it in.")


def _flush_timer(timer) -> None:
    """Spin the event loop until a debounce timer has fired."""
    import time
    from PySide6.QtWidgets import QApplication
    deadline = time.monotonic() + 2.0
    while timer.isActive() and time.monotonic() < deadline:
        QApplication.instance().processEvents()


def _flush_selection(panel) -> None:
    """Let the pixel list's debounced selection reach the controller."""
    _flush_timer(panel._sel_timer)


def test_pixel_list() -> None:
    """The ranked, filterable pixel table."""
    from PySide6.QtWidgets import QApplication
    from chronogate import metrics
    w = _window()
    c = w.controller
    p = w.pixels
    w.show()                     # a dock only signals its visibility on a live window
    QApplication.instance().processEvents()

    # Closed by default and free while closed: no ranking is computed.
    assert not w.pixel_dock.isVisible()
    c.refresh_pixel_list()
    assert p.table.rowCount() == 0, "a closed dock must not cost anything"

    w.act_pixels.trigger()       # View ▸ Pixel list -> seeds the filter and ranks
    QApplication.instance().processEvents()
    assert w.pixel_dock.isVisible()
    assert p.table.rowCount() > 0, "opening the dock populates the list"
    assert p.current_metric() == "in_gate"
    # The columns are driven by the metrics registry, not hard-coded here.
    headers = [p.table.horizontalHeaderItem(i).text() for i in range(p.table.columnCount())]
    assert headers[:2] == ["row", "col"]
    assert len(headers) == 2 + len(metrics.metrics())

    # Ranked high->low by photons in gate: row 0 IS the brightest pixel in the gate.
    r0, c0 = int(p.table.item(0, 0).text()), int(p.table.item(0, 1).text())
    best = np.unravel_index(int(c._gated.argmax()), c._gated.shape)
    assert (r0, c0) == (int(best[0]), int(best[1])), (r0, c0, best)
    assert p.table.rowCount() == p.limit.value(), "the list shows the top N"
    assert "truncated" in p.summary.text(), "a top-N cut is stated, not hidden"

    # Choosing a row selects that pixel: decay, crosshair and readout all follow.
    # (Selection is debounced, so a rubber-band drag costs one refresh, not 200.)
    p.table.selectRow(3)
    _flush_selection(p)
    r3, c3 = int(p.table.item(3, 0).text()), int(p.table.item(3, 1).text())
    assert c.picks and (c.picks[0]["r"], c.picks[0]["c"]) == (r3, c3)
    assert f"px({r3},{c3})" in c.ic.ax.get_title()
    assert len(c._pick_markers) == 2, "the chosen pixel is marked on the image"
    # ...and selecting a pixel elsewhere highlights its row rather than rebuilding.
    c._add_pixel(r0, c0)
    assert p.table.selectionModel().selectedRows()[0].row() == 0

    # A range filter narrows the list to pixels inside the band.
    p.fmin.setValue(1.0)
    p.fmax.setValue(3.0)
    c.refresh_pixel_list()
    vals = [float(p.table.item(i, 2).text().replace(",", "")) for i in range(p.table.rowCount())]
    assert vals and all(1.0 <= v <= 3.0 for v in vals), vals

    # Switching metric reseeds the bounds to the new scale (a stale 1..3 filter on
    # phasor g would otherwise match nothing).
    p.metric.setCurrentIndex([m.key for m in metrics.metrics()].index("total"))
    assert p.current_metric() == "total" and p.table.rowCount() > 0
    lo, hi = metrics.value_range(c._metric_ctx(), "total")
    assert abs(p.fmin.value() - lo) < 1e-6 and abs(p.fmax.value() - hi) < 1e-6

    w.pixel_dock.hide()
    print("OK: pixel list ranks/filters/truncates; a row selects that pixel.")


def test_pixel_list_multi_select() -> None:
    """Ctrl/Shift-style multi-select pools the chosen rows into one group."""
    from PySide6.QtCore import QItemSelection, QItemSelectionModel
    from PySide6.QtWidgets import QApplication, QTableWidget
    w = _window()
    c = w.controller
    p = w.pixels
    w.show()
    QApplication.instance().processEvents()
    w.act_pixels.trigger()
    QApplication.instance().processEvents()
    assert p.table.selectionMode() == QTableWidget.ExtendedSelection, \
        "Ctrl/⌘-click and Shift-range need ExtendedSelection"

    # Select rows 0..4 as a range (what a Shift-click does).
    sm = p.table.selectionModel()
    model, last = p.table.model(), p.table.columnCount() - 1
    span = QItemSelection(model.index(0, 0), model.index(4, last))
    sm.select(span, QItemSelectionModel.ClearAndSelect | QItemSelectionModel.Rows)
    _flush_selection(p)

    want = [(int(p.table.item(i, 0).text()), int(p.table.item(i, 1).text())) for i in range(5)]
    assert p.selected_pixels() == want
    assert len(c.picks) == 1 and c.picks[0]["kind"] == "mask", "many rows -> one pooled group"
    mask = c.picks[0]["mask"]
    assert int(mask.sum()) == 5 and all(mask[r, cc] for r, cc in want)
    assert c.select_mask is mask, "the group is spotlighted on the image"
    assert c.mask_im.get_visible()
    # A handful of single-pixel dots under the veil would be invisible: ring them.
    assert len(c._pick_markers) == 1
    rings = c._pick_markers[0]
    assert sorted(zip(rings.get_ydata(), rings.get_xdata())) == sorted(want), \
        "every pixel in a small group is ringed on the image"

    # Its decay is the pooled mean of those five pixels, and the readout is their sum.
    assert len(c._pick_lines) == 1
    assert np.allclose(c._pick_decay(c.picks[0]), c.model.mask_decay(mask))
    total = int(c._gated[mask].sum())
    assert "list sel (5 px)" in c.ic.ax.get_title() and f"{total:,}" in c.ic.ax.get_title()

    # A gate change re-ranks (debounced, so holding an arrow key doesn't re-rank
    # per keypress) and rebuilds the table -- but must not lose the group's selection.
    c.nudge_gate(1, 0)
    assert c._pixel_timer.isActive(), "the re-rank is debounced, not run per keypress"
    _flush_timer(c._pixel_timer)
    assert len(p.selected_pixels()) == 5, "the group stays selected across a rebuild"
    assert len(c.picks) == 1 and c.picks[0]["kind"] == "mask"

    # Dropping back to one row returns to a single-pixel pick.
    p.table.selectRow(2)
    _flush_selection(p)
    assert c.picks[0]["kind"] == "pixel" and c.select_mask is None
    assert not c.mask_im.get_visible(), "one pixel is not a group -- no spotlight"
    w.pixel_dock.hide()
    print("OK: multi-select pools rows into one group (decay, spotlight, combined total).")


def test_selection_export() -> None:
    """A selection must be able to leave the program, or the feature is a dead end."""
    import csv as _csv
    import tifffile
    from chronogate import metrics
    w = _window()
    c = w.controller

    # Pin one pixel, then select a group of pixels: both should export.
    c._add_pixel(100, 100)
    c._on_pin()
    c._on_pixel_rows([(10, 20), (11, 21), (12, 22)])
    assert len(c._shown_picks()) == 2

    out = Path(tempfile.mkdtemp())
    paths = c.export(out)
    for role in ("selection_mask", "selection_decay", "selection_pixels"):
        assert role in paths and Path(paths[role]).exists(), role

    # The label map: 0 unselected, k = the k-th selection (pinned pixel = 1, group = 2).
    lab = tifffile.imread(paths["selection_mask"])
    assert lab.shape == c.model.intensity.shape
    assert lab[100, 100] == 1, "the pinned pixel is label 1"
    assert lab[10, 20] == 2 and lab[12, 22] == 2, "the group is label 2"
    assert int((lab == 2).sum()) == 3 and int((lab > 0).sum()) == 4

    # The pooled decay CSV: one column per selection, on the real time axis.
    rows = list(_csv.reader(open(paths["selection_decay"])))
    assert len(rows) == c.model.n_bins + 1
    assert rows[0][0] == "time_ns" and len(rows[0]) == 3, rows[0]
    group_decay = np.array([float(r[2]) for r in rows[1:]])
    assert np.allclose(group_decay, c.model.mask_decay(c.picks[0]["mask"]))

    # The pixel table: one row per selected pixel, every registered metric present.
    prows = list(_csv.DictReader(open(paths["selection_pixels"])))
    assert len(prows) == 4, "1 pinned pixel + 3 in the group"
    for key in (m.key for m in metrics.metrics()):
        assert key in prows[0], key
    got = {(int(r["row"]), int(r["col"])) for r in prows}
    assert got == {(100, 100), (10, 20), (11, 21), (12, 22)}
    gated = c._gated
    for r in prows:
        assert abs(float(r["in_gate"]) - gated[int(r["row"]), int(r["col"])]) < 1e-6

    # Provenance records what each selection was.
    prov = json.loads(Path(paths["provenance"]).read_text())
    assert prov["selection"]["pixel_counts"] == [1, 3]
    assert "list sel (3 px)" in prov["selection"]["labels"][1]

    # With nothing picked, no selection files are written at all.
    c._clear_picks()
    plain = c.export(Path(tempfile.mkdtemp()))
    assert not any(k.startswith("selection") for k in plain)
    assert json.loads(Path(plain["provenance"]).read_text())["selection"] is None
    print("OK: selections export as a label map + pooled decays + a per-pixel metric table.")


def test_settings_roundtrip_restores_selection() -> None:
    """Save/load must not silently lose the selection or the pixel-list setup."""
    from PySide6.QtWidgets import QApplication
    w = _window()
    c = w.controller
    w.show()
    QApplication.instance().processEvents()

    # A phasor lasso: 160k pixels, saved as its polygon rather than 262k booleans.
    c._enter_mode("phasor")
    verts = [(0.5, 0.05), (1.0, 0.05), (1.0, 0.45), (0.5, 0.45)]
    c._on_phasor_lasso(verts)
    lasso_mask = c.picks[0]["mask"].copy()
    n = int(lasso_mask.sum())
    assert n > 1000

    c._enter_mode("intensity")
    c.hover_probe = False
    w.act_pixels.trigger()
    QApplication.instance().processEvents()
    w.pixels.limit.setValue(37)

    s = c._settings()
    assert s["picks"][0]["kind"] == "lasso", "a lasso is saved as its polygon"
    assert len(json.dumps(s)) < 20_000, "a 160k-pixel selection must not bloat the file"
    assert s["pixel_list"]["open"] and s["pixel_list"]["limit"] == 37
    assert s["hover_probe"] is False

    # Wipe the state, then restore it.
    c._clear_picks()
    c.hover_probe = True
    w.pixel_dock.hide()
    w.pixels.limit.setValue(200)

    c.apply_settings(s)
    QApplication.instance().processEvents()
    assert len(c.picks) == 1 and c.picks[0]["kind"] == "mask"
    assert np.array_equal(c.picks[0]["mask"], lasso_mask), "the lasso is re-cut exactly"
    assert c.select_mask is not None and c.mask_im.get_visible()
    assert c.hover_probe is False
    assert not w.pixel_dock.isHidden() and w.pixels.limit.value() == 37

    # A pinned pixel + an ROI survive too.
    c._add_pixel(60, 61)
    c._on_pin()
    c._add_pick({"kind": "roi", "r0": 5, "r1": 9, "c0": 5, "c1": 9, "label": "roi"})
    s2 = c._settings()
    c._clear_picks()
    c.apply_settings(s2)
    assert len(c.pinned_picks) == 1 and c.pinned_picks[0]["r"] == 60
    assert len(c.picks) == 1 and c.picks[0]["kind"] == "roi" and c.picks[0]["r1"] == 9
    w.pixel_dock.hide()
    print(f"OK: settings round-trip restores the {n:,}-px lasso (as a polygon), picks, "
          f"pins, hover and the pixel list.")


def test_pin_glyph_is_in_the_plot_font() -> None:
    """The plot font has no pushpin: a 📌 in the legend is a tofu box + a warning."""
    import warnings as _w
    from matplotlib.font_manager import findfont, FontProperties
    from chronogate.ui import controller as ctrl
    w = _window()
    c = w.controller
    c._add_pixel(50, 50)
    c._on_pin()
    labels = [ln.get_label() for ln in c._pick_lines]
    assert any(lab.startswith(ctrl._PIN_MARK_PLOT) for lab in labels), labels
    assert not any("📌" in lab for lab in labels), "the emoji must not reach matplotlib"
    # ...but the Qt list, which renders emoji fine, keeps it.
    assert "📌" in w.picks.list.item(0).text()

    # Drawing a pinned decay must not warn about a missing glyph.
    with _w.catch_warnings():
        _w.simplefilter("error", UserWarning)
        c.dc.fig.canvas.draw()
    print("OK: the pinned marker renders in the plot font (no missing-glyph warning).")


def test_pixel_cursor_and_goto() -> None:
    """The arrow-key pixel cursor and the exact (row, col) jump."""
    w = _window()
    c = w.controller
    ny, nx = c.model.intensity.shape

    # No pixel picked -> nudge_pixel declines, so the arrow key can nudge the gate.
    c._clear_picks()
    assert c.nudge_pixel(0, 1) is False

    c._add_pixel(60, 60)
    assert (w.picks.row.value(), w.picks.col.value()) == (60, 60), "boxes track the pick"
    assert c.nudge_pixel(-1, 0) is True
    assert (c.picks[0]["r"], c.picks[0]["c"]) == (59, 60)
    c.nudge_pixel(0, 10)                       # a Shift+Right stride
    assert (c.picks[0]["r"], c.picks[0]["c"]) == (59, 70)
    assert (w.picks.row.value(), w.picks.col.value()) == (59, 70)

    # The cursor clamps at the border rather than wrapping or throwing.
    c._add_pixel(0, 0)
    c.nudge_pixel(-5, -5)
    assert (c.picks[0]["r"], c.picks[0]["c"]) == (0, 0)
    c._add_pixel(ny - 1, nx - 1)
    c.nudge_pixel(5, 5)
    assert (c.picks[0]["r"], c.picks[0]["c"]) == (ny - 1, nx - 1)

    # Typed coordinates: the Go button selects exactly that pixel.
    w.picks.row.setValue(12)
    w.picks.col.setValue(34)
    w.picks.btn_go.click()
    assert c.picks[0] == {"kind": "pixel", "r": 12, "c": 34, "label": "px(12,34)"}
    assert f"px(12,34)" in c.ic.ax.get_title()

    # An ROI is not a pixel cursor, so the arrows fall back to the gate.
    c._add_pick({"kind": "roi", "r0": 5, "r1": 9, "c0": 5, "c1": 9, "label": "roi"})
    assert c.nudge_pixel(0, 1) is False
    print("OK: arrow-key pixel cursor steps/clamps; (row, col) jump selects exactly.")


def test_pick_markers_on_image() -> None:
    """A picked pixel is invisible at 1/512 of the panel -- it needs a marker."""
    w = _window()
    c = w.controller
    c._clear_picks()
    assert not c._pick_markers
    c._add_pixel(64, 96)
    assert len(c._pick_markers) == 2, "a pixel pick draws a box + a crosshair"
    xs, ys = c._pick_markers[1].get_data()
    assert (int(xs[0]), int(ys[0])) == (96, 64), "the crosshair sits on the picked pixel"
    c._add_pick({"kind": "roi", "r0": 10, "r1": 20, "c0": 30, "c1": 44, "label": "roi"})
    assert len(c._pick_markers) == 1, "an ROI draws just its box"
    assert c._pick_markers[0].get_width() == 14 and c._pick_markers[0].get_height() == 10
    c._clear_picks()
    assert not c._pick_markers, "clearing removes the markers"
    print("OK: picks are marked on the image (crosshair for a pixel, box for an ROI).")


def test_phasor_lasso_selection() -> None:
    """Lasso a phasor cluster -> those pixels are selected by lifetime signature."""
    w = _window()
    c = w.controller
    c._enter_mode("phasor")
    g, s = c._phasor_maps()
    assert c._phasor_maps() is c._phasor_gs, "the (g, s) maps are cached, not recomputed"

    # A lasso around the whole phasor plane selects every pixel that has photons.
    c._on_phasor_lasso([(-1.0, -1.0), (2.0, -1.0), (2.0, 2.0), (-1.0, 2.0)])
    finite = int((np.isfinite(g) & np.isfinite(s)).sum())
    assert c.select_mask is not None and int(c.select_mask.sum()) == finite
    assert len(c.picks) == 1 and c.picks[0]["kind"] == "mask"
    assert len(c._pick_lines) == 1, "the selected population's pooled decay is drawn"
    assert c._pick_decay(c.picks[0]).size == c.model.n_bins

    # An empty lasso is refused rather than clearing the selection.
    before = c.select_mask
    c._on_phasor_lasso([(-1.0, -1.0), (-0.9, -1.0), (-0.9, -0.9)])
    assert c.select_mask is before, "an empty lasso must not wipe the current selection"

    # Back on the image, the selection is highlighted and reports its own counts.
    c._enter_mode("intensity")
    assert c.mask_im.get_visible(), "selected pixels are tinted on the image"
    assert c.mask_im.get_array().shape[:2] == c.model.intensity.shape
    floor = c._floor_per_pixel() if c.apply_floor else 0.0
    gated = c.model.gate(c.gate_lo_bin, c.gate_hi_bin, floor_per_bin=floor)
    total = int(gated[c.select_mask].sum())
    assert "phasor sel" in c.ic.ax.get_title() and f"{total:,}" in c.ic.ax.get_title()

    # Clicking a pixel supersedes the lasso; Clear wipes everything.
    c._add_pixel(40, 40)
    assert c.select_mask is None and not c.mask_im.get_visible()
    c._clear_picks()
    assert c.select_mask is None and not c._shown_picks()
    print(f"OK: phasor lasso selects {finite:,} px, highlights them, pools their decay.")


def test_floor_slider_summed_range_and_scale() -> None:
    import math
    w = _window()
    c = w.controller
    f = w.display.floor
    d = c.model.decay
    # The floor slider is in summed-decay units, ranged over the whole curve
    # (lowest → highest recorded value); the value stored per pixel = value/npix.
    assert f._decimals == 0
    assert f._min == int(d.min()) and f._max == int(d.max()), (f._min, f._max)
    assert abs(f.value() - c.noise_floor_pp * c.model.n_pixels) < 1.0, "shows the summed floor"
    # scale follows the y-axis: log-Y -> an index-mapped slider whose midpoint is
    # the geometric mean; linear -> a direct value slider over [min, max].
    assert c.log_scale and f.slider.maximum() == f._STEPS
    lo, hi = f._bounds()
    mid = f._value_from_pos(f._STEPS // 2)
    assert abs(mid - math.sqrt(lo * hi)) / math.sqrt(lo * hi) < 0.03, "log midpoint ~ geometric mean"
    w.act_log.toggled.emit(False)                  # linear -> direct integer slider
    assert f.slider.minimum() == int(f._min) and f.slider.maximum() == int(f._max)
    assert f._value_from_pos(int(f._max)) == int(f._max), "linear slider position == value"
    w.act_log.toggled.emit(True)
    # Setting the slider updates the per-pixel value (summed / npix).
    f.spin.setValue(f._max)
    c._on_noise_floor(f.value())
    assert abs(c.noise_floor_pp - f._max / c.model.n_pixels) < 1e-9
    # the "auto" button resets to the robust-baseline value
    w.display.btn_floor_auto.click()
    assert abs(c.noise_floor_pp - c.model.auto_noise_floor_pp()) < 1e-9
    assert abs(f.value() - round(c.model.auto_noise_floor_pp() * c.model.n_pixels)) <= 1
    print("OK: floor slider spans the summed decay; scale follows the y-axis; auto button resets.")


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
        test_hover_probe_blits()
        test_pixel_list()
        test_pixel_list_multi_select()
        test_selection_export()
        test_settings_roundtrip_restores_selection()
        test_pin_glyph_is_in_the_plot_font()
        test_pixel_cursor_and_goto()
        test_pick_markers_on_image()
        test_phasor_lasso_selection()
        test_fit_overlay()
        test_wave_c_views()
        test_cache_lockscale_and_t0()
        test_probe_and_batch_export()
        test_threaded_decode()
        test_floor_slider_summed_range_and_scale()
    except AssertionError as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        raise SystemExit(1)
    print("All UI smoke tests passed.")
