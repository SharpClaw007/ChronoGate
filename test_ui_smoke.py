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
# Isolate app preferences: the suite must never read or write the user's real
# QSettings (every load records a "last opened" path there).
os.environ["CHRONOGATE_PREFS_INI"] = os.path.join(tempfile.mkdtemp(), "prefs.ini")

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


def test_tau_hist_selection_overlay() -> None:
    """'Is this cluster's lifetime different?' -- the τ-histogram inset overlays
    the selection's distribution (same bins, accent colour) over the image's."""
    w = _window()
    c = w.controller
    w.binning.bin.setValue(8)            # pool photons so τ resolves
    c._enter_mode("lifetime")
    assert c._tau_hist_ax is not None

    def sel_patches():
        return [p for p in c._tau_hist_ax.patches if p.get_gid() == "tau_sel"]

    assert not sel_patches(), "no selection -> no overlay"

    c._add_pick({"kind": "roi", "r0": 100, "r1": 140, "c0": 100, "c1": 140,
                 "label": "roi[100:140,100:140]"})
    got = sel_patches()
    assert got, "the selection's τ distribution is overlaid on the inset"

    # The overlay is the exact histogram of the selection's finite τ values,
    # over the same 40 display-range bins as the whole-image histogram.
    tau, _ = c._compute_lifetime_map()
    v = tau[c._pick_pixel_mask(c.picks[0])]
    v = v[np.isfinite(v)]
    assert v.size, "the test ROI must contain valid lifetimes"
    counts, _ = np.histogram(v, bins=40, range=c.im.get_clim())
    assert np.allclose([p.get_height() for p in got], counts)

    c._clear_picks()
    assert not sel_patches(), "clearing the pick clears the overlay"
    c._enter_mode("intensity")
    w.binning.bin.setValue(1)
    print("OK: τ-histogram inset overlays the selection's distribution.")


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


def test_batch_export_recuts_selection_per_plane() -> None:
    """A phasor lasso is a polygon in (g, s) space: a batch export must re-cut it
    against EACH plane's phasor, not stamp the drawing plane's pixel mask onto
    every z. Located picks (a pinned pixel) simply carry across."""
    import tifffile
    _window()  # ensure app
    from chronogate.ui.main_window import MainWindow
    w = MainWindow(None, open_dir=str(DATA_DIR))
    c = w.controller
    c.load_folder(str(DATA_DIR))
    c.stack = c.stack[:2]
    c.z_index = 0
    c._reload_model_busy()
    c._refit_ranges()
    c._refresh_image()

    # Pin a pixel, then lasso a phasor cluster on plane 0.
    c._add_pixel(100, 100)
    c._on_pin()
    c._enter_mode("phasor")
    verts = [(0.2, 0.02), (0.9, 0.02), (0.9, 0.5), (0.2, 0.5)]
    c._on_phasor_lasso(verts)
    assert c.picks and c.picks[0]["kind"] == "mask"
    mask_plane0 = c.picks[0]["mask"].copy()
    c._enter_mode("intensity")

    out = Path(tempfile.mkdtemp())
    n = c.batch_export(out)
    assert n == 2

    # After the batch, the live state is exactly what the user had.
    assert np.array_equal(c.picks[0]["mask"], mask_plane0)
    assert len(c.pinned_picks) == 1 and c.pinned_picks[0]["r"] == 100

    # Each plane's label map holds the lasso re-cut against THAT plane's phasor.
    for i in range(2):
        c.z_index = i
        c._reload_model_busy()
        expect = c._lasso_mask(np.asarray(verts))
        stem = c.stack[i].stem
        tif = next(Path(out).rglob(f"{stem}*_selection_mask.tif"))
        lab = tifffile.imread(tif)
        assert np.array_equal(lab == 2, expect), f"plane {i}: lasso not re-cut"
        assert lab[100, 100] == 1, f"plane {i}: the pinned pixel must carry across"
    assert not np.array_equal(mask_plane0, c._lasso_mask(np.asarray(verts))), \
        "planes differ, so the re-cut masks should differ (else this test proves nothing)"
    c.z_index = 0
    c._reload_model_busy()
    print("OK: batch export re-cuts the lasso per plane and carries located picks.")


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
    p.metric_box.setCurrentIndex([m.key for m in metrics.metrics()].index("total"))
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


def test_selection_aggregates_in_provenance() -> None:
    """The numbers a paper quotes (mean/median τ, photons per selection) must
    leave the program with the data -- recorded per label in the provenance."""
    from chronogate import metrics
    w = _window()
    c = w.controller
    c._add_pixel(100, 100)
    c._on_pin()
    c._on_pixel_rows([(10, 20), (11, 21), (12, 22)])

    from chronogate.export import ExportOptions
    out = Path(tempfile.mkdtemp())
    # Aggregates are cheap: they ride along even with the pixel table omitted.
    paths = c.export(out, options=ExportOptions(pixel_table=False))
    sel = json.loads(Path(paths["provenance"]).read_text())["selection"]
    aggs = sel["aggregates"]
    assert len(aggs) == 2 == len(sel["labels"]), "one aggregate block per label"

    # The group's numbers match mask_stats exactly (NaN travels as null, not NaN).
    st = metrics.mask_stats(c._metric_ctx(), c.picks[0]["mask"])
    for key, d in st.items():
        got = aggs[1][key]
        assert got["n"] == d["n"]
        for stat in ("mean", "median", "std"):
            if np.isfinite(d[stat]):
                assert abs(got[stat] - d[stat]) < 1e-9, (key, stat)
            else:
                assert got[stat] is None, (key, stat)
    assert json.dumps(sel)   # strictly JSON-serialisable (no NaN literals)
    c._clear_picks()
    print("OK: per-selection aggregates ride along in the provenance JSON.")


def test_big_pixel_table_warning_and_optout() -> None:
    """A 160k-pixel lasso is a ~10 MB pixel CSV. It is never capped -- it is the
    data the user asked for -- but a big one is written knowingly, not by surprise."""
    w = _window()
    c = w.controller

    from chronogate.export import ExportOptions
    from chronogate.ui.export_dialog import ExportDialog

    # Opting out of the pixel table still exports the mask and pooled decays,
    # and the provenance says the table was omitted rather than staying silent.
    c._on_pixel_rows([(10, 20), (11, 21), (12, 22)])
    out = Path(tempfile.mkdtemp())
    paths = c.export(out, options=ExportOptions(pixel_table=False))
    assert "selection_mask" in paths and "selection_decay" in paths
    assert "selection_pixels" not in paths and not list(out.glob("*_selection_pixels.csv"))
    prov = json.loads(Path(paths["provenance"]).read_text())
    assert prov["selection"]["pixel_counts"] == [3]
    assert "omitted" in prov["selection"]["pixel_table"]

    # The size estimate counts every selected pixel across all shown picks.
    rows, nbytes = c._selection_size_estimate()
    assert rows == 3 and nbytes > 0

    # In the export dialog a small selection pre-checks the pixel table; a huge
    # one starts unchecked (never capped -- the box is right there to tick) and
    # its label announces the row count and the approximate size.
    dlg = ExportDialog(w, has_selection=True, sel_rows=rows, sel_bytes=nbytes,
                       n_planes=1, default_dir="/o")
    assert dlg.chk_pixels.isChecked() and dlg.chk_pixels.isEnabled()

    big = np.ones(c.model.intensity.shape, dtype=bool)
    c._add_pick({"kind": "mask", "mask": big, "label": "everything"})
    rows2, nbytes2 = c._selection_size_estimate()
    assert rows2 >= int(big.sum())
    dlg2 = ExportDialog(w, has_selection=True, sel_rows=rows2, sel_bytes=nbytes2,
                        n_planes=1, default_dir="/o")
    assert dlg2.chk_pixels.isEnabled() and not dlg2.chk_pixels.isChecked(), \
        "a huge table starts opted out, not silently written"
    assert "MB" in dlg2.chk_pixels.text() and f"{rows2:,}" in dlg2.chk_pixels.text()
    assert dlg2.options().pixel_table is False
    dlg2.chk_pixels.setChecked(True)          # never capped: one click re-opts in
    assert dlg2.options().pixel_table is True
    print("OK: big pixel tables are announced (row count + size) and start opted out.")


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


def test_pick_undo() -> None:
    """Ctrl+Z for selections: a stray click must not cost a carefully built group.
    Pixel-cursor walks coalesce into one history entry; pin and clear are always
    undoable; an empty history refuses politely."""
    w = _window()
    c = w.controller
    c._clear_picks()

    # A stray click wipes a group; one undo restores it (mask AND spotlight).
    c._on_pixel_rows([(10, 20), (11, 21), (12, 22)])
    mask = c.picks[0]["mask"].copy()
    c._add_pixel(50, 50)
    assert c.select_mask is None
    assert c.undo_pick() is True
    assert c.picks and c.picks[0]["kind"] == "mask"
    assert np.array_equal(c.picks[0]["mask"], mask)
    assert c.select_mask is not None and c.mask_im.get_visible()

    # Walking the pixel cursor is ONE history entry, not one per step.
    c._add_pixel(60, 60)
    c.nudge_pixel(0, 1)
    depth = len(c._pick_undo)
    c.nudge_pixel(0, 1)
    c.nudge_pixel(1, 0)
    assert len(c._pick_undo) == depth, "pixel-to-pixel steps must coalesce"
    assert c.undo_pick() and c.picks[0] == {"kind": "pixel", "r": 60, "c": 60,
                                            "label": "px(60,60)"}
    assert c.undo_pick() and c.picks[0]["kind"] == "mask", \
        "undoing past the walk lands back on the group"

    # Pin and clear are each one undo step.
    c._add_pixel(30, 30)
    c._on_pin()
    assert len(c.pinned_picks) == 1 and not c.picks
    assert c.undo_pick()
    assert not c.pinned_picks and c.picks[0]["r"] == 30, "undo un-pins"
    c._clear_picks()
    assert not c._shown_picks()
    assert c.undo_pick()
    assert c.picks and c.picks[0]["r"] == 30, "undo restores a cleared pick"

    # Draining the history ends with a refusal, not a crash.
    while c.undo_pick():
        pass
    assert c.undo_pick() is False

    # The action is wired with a real shortcut.
    assert not w.act_undo_pick.shortcut().isEmpty()
    c._clear_picks()
    print("OK: pick undo restores groups/pins/clears; pixel walks coalesce.")


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


def test_compare_pinned_groups() -> None:
    """Pin a *group*, select another, and compare: two pooled decays overlaid and
    each pick's aggregate stats (median RLD τ) stated side by side in the list."""
    from chronogate import metrics
    w = _window()
    c = w.controller
    c._clear_picks()

    # Two disjoint groups around the brightest pixel (so τ has photons to work
    # with), and a gate hugging the decay so its split halves both hold signal.
    w.binning.bin.setValue(4)               # pool photons -> τ resolves
    t0 = c.model.t0_bin
    c._set_gate("A", t0, t0 + 40)
    c._refresh_decay()
    c._refresh_image()
    r, col = np.unravel_index(int(c.model.intensity.argmax()), c.model.intensity.shape)
    r, col = int(r), int(col)
    g1 = [(r, col), (r, col + 1), (r + 1, col)]
    g2 = [(r + 4, col + 4), (r + 5, col + 5)]

    c._on_pixel_rows(g1)
    assert c.picks[0]["kind"] == "mask"
    c._on_pin()                              # freeze group 1
    c._on_pixel_rows(g2)                     # group 2 live
    assert len(c.pinned_picks) == 1 and c.pinned_picks[0]["kind"] == "mask"
    assert len(c._shown_picks()) == 2 and len(c._pick_lines) == 2, \
        "two pooled decays are overlaid for comparison"

    # Each list row states that pick's aggregate lifetime next to its counts.
    items = [w.picks.list.item(i).text() for i in range(w.picks.list.count())]
    assert len(items) == 2 and "3 px" in items[0] and "2 px" in items[1]
    tau_map = metrics.get("tau").compute(c._metric_ctx())
    for text, pick in zip(items, c._shown_picks()):
        vals = tau_map[c._pick_pixel_mask(pick)]
        finite = vals[np.isfinite(vals)]
        if finite.size:
            assert f"med τ {float(np.median(finite)):.2f} ns" in text, text
        else:
            assert "med τ" not in text, text
    assert any("med τ" in t for t in items), "at least one group must resolve a τ"

    c._clear_picks()
    print("OK: pinned groups compare side by side (pooled decays + per-group median τ).")


def test_selection_stats_in_stats_panel() -> None:
    """Selecting a population must state its aggregate (mean/median τ, photons,
    spread) in the Stats panel -- not leave the user to export a CSV to learn it."""
    from chronogate import metrics
    w = _window()
    c = w.controller

    def stats_rows():
        # isHidden(), not isVisible(): the window is never shown in this test.
        return {w.stats._keys[i].text(): w.stats._vals[i].text()
                for i in range(w.stats._ROWS) if not w.stats._keys[i].isHidden()}

    # With nothing picked, the panel keeps its whole-image gate stats.
    c._clear_picks()
    assert "Gate" in stats_rows()

    # A multi-pixel group: the panel reports the selection, not the whole image.
    picked = [(10, 20), (11, 21), (12, 22)]
    c._on_pixel_rows(picked)
    rows = stats_rows()
    assert "Selection" in rows and "3 px" in rows["Selection"], rows
    total = int(c._gated[c.picks[0]["mask"]].sum())
    assert f"{total:,}" in rows["In gate"], rows["In gate"]
    st = metrics.mask_stats(c._metric_ctx(), c.picks[0]["mask"], keys=["in_gate"])
    assert f"{st['in_gate']['mean']:.1f}" in rows["In gate"]
    assert any("τ" in k for k in rows), "the aggregate lifetime row is present"

    # A single-pixel pick is a 1-px selection (aggregates are still stated).
    c._add_pixel(50, 60)
    rows = stats_rows()
    assert "1 px" in rows["Selection"], rows["Selection"]

    # Clearing the picks restores the whole-image stats.
    c._clear_picks()
    assert "Gate" in stats_rows()
    print("OK: the Stats panel states the selection's aggregates and reverts on clear.")


def test_rld_gates_valid_at_load_and_mode_keyed() -> None:
    """Gate B is a real (later) gate from the moment a file loads, and the τ
    metric's gates follow the *mode*: the user's A/B pair in lifetime mode (so
    the τ column matches the τ map), a split of the current gate elsewhere."""
    w = _window()
    c = w.controller

    # At load, B is already a valid, later gate -- not a copy of A.
    assert c.gateB_lo_bin > c.gate_lo_bin, "gate B must be configured at load"
    assert c.gateB_hi_bin >= c.gateB_lo_bin
    assert c.gateB_hi_bin < c.model.n_bins

    # Intensity mode: τ uses the current gate split into equal halves.
    a = (c.gate_lo_bin, c.gate_hi_bin)
    half = (a[1] - a[0] + 1) // 2
    early, late = c._rld_gates()
    assert early == (a[0], a[0] + half - 1) and late == (a[0] + half, a[0] + 2 * half - 1)

    # Lifetime mode: τ uses the user's A/B pair verbatim (matches the τ map).
    c.enter_lifetime()
    assert c._rld_gates() == ((c.gate_lo_bin, c.gate_hi_bin),
                              (c.gateB_lo_bin, c.gateB_hi_bin))

    # Back in intensity mode the split rule applies again -- even though a B
    # gate has been configured -- so the τ column always reflects the gate
    # you are actually looking at.
    c._enter_mode("intensity")
    a = tuple(sorted((c.gate_lo_bin, c.gate_hi_bin)))
    half = (a[1] - a[0] + 1) // 2
    assert c._rld_gates() == ((a[0], a[0] + half - 1),
                              (a[0] + half, a[0] + 2 * half - 1)), \
        "intensity mode must split the current gate, not reuse the lifetime pair"
    print("OK: gate B valid from load; τ gates follow the mode (A/B in lifetime, split elsewhere).")


def test_tau_map_shared_cache() -> None:
    """One τ map serves the pick legend, the pixel list and the selection stats:
    computed once per (model, gates, floor, min-counts), not once per consumer."""
    from chronogate import gating as gt, metrics
    w = _window()
    c = w.controller

    calls = []
    orig = c.model.rapid_lifetime

    def counting(*a, **k):
        calls.append(1)
        return orig(*a, **k)

    c.model.rapid_lifetime = counting
    m1 = c._tau_map()
    m2 = c._tau_map()
    assert m1 is m2 and len(calls) == 1, "repeat calls must hit the cache"

    # The τ metric (pixel list, mask_stats, exports) reads the same cache.
    assert metrics.get("tau").compute(c._metric_ctx()) is m1
    assert len(calls) == 1, "the metric must not recompute what the cache holds"

    # A gate change is a different key: recomputed once, then cached again.
    c.nudge_gate(1, 1)
    m3 = c._tau_map()
    assert m3 is not m1 and len(calls) >= 2
    n_after = len(calls)
    assert c._tau_map() is m3 and len(calls) == n_after

    # Swapping the model (rebinning) drops the cache via the weakref key.
    c.model.rapid_lifetime = orig
    w.binning.bin.setValue(2)
    m4 = c._tau_map()
    assert m4.shape == c.model.intensity.shape and m4 is not m3
    w.binning.bin.setValue(1)
    print("OK: τ map cached once per analysis state and shared by every consumer.")


def test_phasor_grid_under_hexbin() -> None:
    """The phasor gridlines must draw UNDER the hexbin cloud, not across it."""
    w = _window()
    c = w.controller
    c._enter_mode("phasor")
    ax = c.ic.ax
    hexbins = [a for a in c._phasor_artists if hasattr(a, "get_offsets")]
    assert hexbins, "the phasor hexbin must exist"
    assert ax.get_axisbelow() is True, "gridlines must be drawn below the data"
    assert ax.xaxis.get_zorder() < min(h.get_zorder() for h in hexbins)
    c._enter_mode("intensity")
    print("OK: phasor gridlines render under the hexbin.")


def test_second_harmonic_ui() -> None:
    """View ▸ Phasor 2nd harmonic: the maps, title and calibration all follow the
    harmonic, and each harmonic keeps its own calibration."""
    w = _window()
    c = w.controller
    c._enter_mode("phasor")
    assert c.harmonic == 1 and "harmonic 1" in c.ic.ax.get_title()
    g1, s1 = (a.copy() for a in c._phasor_maps())

    w.act_harmonic2.trigger()
    assert c.harmonic == 2 and "harmonic 2" in c.ic.ax.get_title()
    g2, s2 = c._phasor_maps()
    keep = np.isfinite(g1) & np.isfinite(g2)
    assert not np.allclose(g2[keep], g1[keep]), "harmonic 2 is a different map"
    gd, sd = c.model.phasor(harmonic=2)
    assert np.allclose(g2[keep], gd[keep]) and np.allclose(s2[keep], sd[keep])

    # Calibrate at harmonic 2; harmonic 1 stays uncalibrated.
    assert c.calibrate_phasor(3.0)
    assert c.phasor_cal is not None and 2 in c.phasor_cals
    w.act_harmonic2.trigger()            # back to harmonic 1
    assert c.harmonic == 1 and c.phasor_cal is None
    assert "uncalibrated" in c.ic.ax.get_title()

    # Settings round-trip restores the harmonic and its calibration.
    w.act_harmonic2.trigger()            # harmonic 2 again (calibrated)
    s = c._settings()
    assert s["harmonic"] == 2 and s["phasor_cals"].get(2)
    c.clear_phasor_calibration()
    w.act_harmonic2.trigger()            # wipe: harmonic 1, no calibrations
    c.apply_settings(s)
    assert c.harmonic == 2 and c.phasor_cal is not None
    assert c.phasor_cal["tau_ref_ns"] == 3.0

    # Legacy settings (v0.9 single phasor_cal) land on harmonic 1.
    c.apply_settings({**s, "harmonic": 1, "phasor_cals": None,
                      "phasor_cal": {"tau_ref_ns": 2.0, "phi": 0.1, "mod": 0.9}})
    assert c.harmonic == 1 and c.phasor_cal["tau_ref_ns"] == 2.0

    c.clear_phasor_calibration()
    c._enter_mode("intensity")
    print("OK: second harmonic toggles maps/title, keeps per-harmonic calibration, persists.")


def test_phasor_reference_marker_and_ruler() -> None:
    """A calibrated phasor shows WHERE the reference should sit (and a τ ruler
    along the semicircle), so the calibration is visibly verifiable."""
    from chronogate import gating
    w = _window()
    c = w.controller
    c._enter_mode("phasor")

    def gids():
        return {a.get_gid() for a in c._phasor_artists}

    # Uncalibrated: τ positions on the plot would be meaningless -- no marker.
    assert "tau_ref_marker" not in gids() and "tau_ruler" not in gids()

    assert c.calibrate_phasor(3.0)
    assert "tau_ref_marker" in gids(), "the reference point is marked"
    assert "tau_ruler" in gids(), "the semicircle carries a τ ruler"
    marker = next(a for a in c._phasor_artists if a.get_gid() == "tau_ref_marker")
    period_ns = c.model.period_bins() * c.model.resolution_ns
    gt, st = gating.phasor_reference(3.0, period_ns)
    x, y = marker.get_data()
    assert abs(float(x[0]) - gt) < 1e-9 and abs(float(y[0]) - st) < 1e-9, \
        "the marker sits exactly on the τref semicircle position"
    ruler = next(a for a in c._phasor_artists if a.get_gid() == "tau_ruler")
    rx, ry = ruler.get_data()
    assert len(rx) >= 3, "several τ ticks"
    assert np.allclose((np.asarray(rx) - 0.5) ** 2 + np.asarray(ry) ** 2, 0.25,
                       atol=1e-9), "ruler ticks sit on the universal semicircle"

    c.clear_phasor_calibration()
    assert "tau_ref_marker" not in gids() and "tau_ruler" not in gids()
    c._enter_mode("intensity")
    print("OK: calibrated phasor marks the reference point and a τ ruler; clears cleanly.")


def test_phasor_calibration_ui() -> None:
    """Calibrating from a reference makes the phasor quantitative: the maps, the
    metrics and the plot all rotate/scale together, it survives a settings
    round-trip, and clearing it restores the raw t0-referenced phasor."""
    from chronogate import gating, metrics
    w = _window()
    c = w.controller
    c._enter_mode("phasor")
    assert "uncalibrated" in c.ic.ax.get_title(), c.ic.ax.get_title()

    g0, s0 = (a.copy() for a in c._phasor_maps())
    keep = np.isfinite(g0) & np.isfinite(s0)

    assert c.calibrate_phasor(3.0), "calibration from the whole image must succeed"
    assert c.phasor_cal is not None and c.phasor_cal["tau_ref_ns"] == 3.0
    phi, mod = c.phasor_cal["phi"], c.phasor_cal["mod"]

    # Every pixel is rotated/scaled by the calibration factor...
    g1, s1 = c._phasor_maps()
    ge, se = gating.apply_phasor_calibration(g0, s0, phi, mod)
    assert np.allclose(g1[keep], ge[keep]) and np.allclose(s1[keep], se[keep])
    # ...and the measured reference (the median of the raw cloud) lands exactly
    # on the true semicircle position of a 3 ns mono-exponential.
    period_ns = c.model.period_bins() * c.model.resolution_ns
    gt, st = gating.phasor_reference(3.0, period_ns)
    gm, sm = gating.apply_phasor_calibration(
        float(np.median(g0[keep])), float(np.median(s0[keep])), phi, mod)
    assert abs(float(gm) - gt) < 1e-9 and abs(float(sm) - st) < 1e-9
    assert "calibrated" in c.ic.ax.get_title()

    # The g/s pixel-list metrics see the calibrated values (one shared map).
    assert np.allclose(metrics.get("g").compute(c._metric_ctx())[keep], g1[keep])

    # Settings round-trip: the calibration is part of the analysis state.
    s = c._settings()
    c.clear_phasor_calibration()
    assert c.phasor_cal is None
    g_raw, _ = c._phasor_maps()
    assert np.allclose(g_raw[keep], g0[keep]), "clearing restores the raw phasor"
    c.apply_settings(s)
    assert c.phasor_cal is not None and abs(c.phasor_cal["phi"] - phi) < 1e-12
    g2, _ = c._phasor_maps()
    assert np.allclose(g2[keep], g1[keep])

    c.clear_phasor_calibration()
    c._enter_mode("intensity")
    print("OK: phasor calibration rotates maps+metrics+plot, persists, and clears.")


def test_export_dialog_defaults_and_mapping() -> None:
    """The export dialog: choose the artefacts, their parameters and the folder.

    Defaults mirror the old always-everything export; the selection artefacts
    are only offered when a selection exists; the per-pixel table starts
    unchecked for huge selections (announced, never capped); batch is only
    offered for a multi-plane stack; an empty folder cannot be accepted.
    """
    from PySide6.QtWidgets import QDialogButtonBox
    from chronogate.export import ExportOptions
    from chronogate.ui.export_dialog import ExportDialog

    w = _window()
    # No selection: selection artefacts off + disabled, everything else on.
    dlg = ExportDialog(w, raster_label="gated intensity", has_selection=False,
                       n_planes=1, default_dir="/somewhere/out")
    assert dlg.chk_raw.isChecked() and dlg.chk_png.isChecked() and dlg.chk_decay.isChecked()
    assert not dlg.chk_sel.isEnabled() and not dlg.chk_sel.isChecked()
    assert not dlg.chk_pixels.isEnabled()
    assert not dlg.chk_batch.isEnabled(), "single plane offers no batch"
    assert dlg.out_dir() == "/somewhere/out"
    assert dlg.options() == ExportOptions(selection=False, pixel_table=False)
    assert "gated intensity" in dlg.chk_raw.text()
    dlg.chk_png.setChecked(False)
    assert dlg.options() == ExportOptions(color_png=False, selection=False,
                                          pixel_table=False)
    # The one-page report is offered too (off by default; it adds its own files).
    assert not dlg.chk_report.isChecked()
    dlg.chk_report.setChecked(True)
    assert dlg.options().report is True
    # Restrict-to-selection needs a selection to restrict to.
    assert not dlg.chk_restrict.isEnabled() and not dlg.chk_restrict.isChecked()

    # With a small selection both selection boxes are on; unchecking the parent
    # pulls the pixel table with it. Batch is offered for a stack and can be
    # pre-checked (the Export-all-planes menu entry).
    d2 = ExportDialog(w, raster_label="apparent lifetime τ", has_selection=True,
                      sel_rows=3, sel_bytes=120, n_planes=4, default_dir="/o",
                      batch=True)
    assert d2.chk_sel.isChecked() and d2.chk_pixels.isChecked()
    assert d2.chk_restrict.isEnabled() and not d2.chk_restrict.isChecked()
    d2.chk_restrict.setChecked(True)
    assert d2.options().restrict_to_selection is True
    assert d2.chk_batch.isEnabled() and d2.chk_batch.isChecked() and d2.batch()
    d2.chk_sel.setChecked(False)
    assert not d2.chk_pixels.isEnabled()
    assert d2.options().selection is False and d2.options().pixel_table is False
    ok = d2.buttons.button(QDialogButtonBox.Ok)
    d2.dir_edit.setText("")
    assert not ok.isEnabled(), "no folder, no export"
    d2.dir_edit.setText("/o")
    assert ok.isEnabled()
    print("OK: export dialog maps checkboxes/folder/batch onto ExportOptions.")


def test_export_dialog_drives_export() -> None:
    """Accepting the dialog exports exactly the chosen artefacts to the chosen
    folder; cancelling exports nothing; the provenance names what was omitted;
    the batch checkbox routes to batch_export."""
    from PySide6.QtWidgets import QDialog
    from chronogate.ui import export_dialog as ed

    w = _window()
    c = w.controller
    out = Path(tempfile.mkdtemp())
    orig = ed.ExportDialog.exec
    def fake_exec(self):
        self.dir_edit.setText(str(out))
        self.chk_png.setChecked(False)
        self.chk_decay.setChecked(False)
        self.chk_report.setChecked(True)
        return QDialog.Accepted
    ed.ExportDialog.exec = fake_exec
    try:
        c._on_export()
    finally:
        ed.ExportDialog.exec = orig
    names = {p.name for p in out.iterdir()}
    assert any(n.endswith("_gated_raw.tif") for n in names)
    assert any(n.endswith("_gate12-263_provenance.json") for n in names)
    assert not any(n.endswith("_gated_color.png") for n in names)
    assert not any(n.endswith("_gate12-263_decay.csv") for n in names)
    # Ticking the report box writes the one-pager (PNG + PDF) alongside.
    assert any(n.endswith("_report.png") for n in names)
    assert any(n.endswith("_report.pdf") for n in names)
    prov = json.loads(next(out.glob("*_gate12-263_provenance.json")).read_text())
    assert set(prov["omitted"]) == {"color_png", "decay_csv"}

    # The suggested folder is a fresh run-stamped subfolder: an export into the
    # default location can never sit next to stale files from an earlier run
    # (which read as "it exported everything despite my choices").
    import re
    seen_dirs = []
    ed.ExportDialog.exec = lambda self: (seen_dirs.append(self.out_dir()),
                                         QDialog.Rejected)[1]
    try:
        c._on_export()
    finally:
        ed.ExportDialog.exec = orig
    assert re.search(r"chronogate_exports/run-\d{8}-\d{6}$", seen_dirs[0]), seen_dirs
    # The report box says it brings its own data files.
    from chronogate.ui.export_dialog import ExportDialog
    dlg = ExportDialog(w, has_selection=False, n_planes=1, default_dir="/o")
    assert "own" in dlg.chk_report.text(), "report label states its own files"

    # Cancel writes nothing.
    out2 = Path(tempfile.mkdtemp())
    ed.ExportDialog.exec = lambda self: (self.dir_edit.setText(str(out2)),
                                         QDialog.Rejected)[1]
    try:
        c._on_export()
    finally:
        ed.ExportDialog.exec = orig
    assert not list(out2.iterdir()), "cancelled dialog must export nothing"

    # The batch checkbox routes to batch_export with the same options + folder.
    ran = {}
    c.batch_export = lambda out_dir=None, options=None: ran.update(
        out_dir=out_dir, options=options) or 1
    def fake_exec_batch(self):
        self.dir_edit.setText("/batch/out")
        self.chk_batch.setChecked(True)
        self.chk_raw.setChecked(False)
        self.chk_report.setChecked(True)
        return QDialog.Accepted
    ed.ExportDialog.exec = fake_exec_batch
    try:
        c._on_batch_export()
    finally:
        ed.ExportDialog.exec = orig
    assert ran["out_dir"] == "/batch/out" and ran["options"].raw_tiff is False
    assert ran["options"].report is True, "the report choice must reach the batch"
    print("OK: export dialog drives export/batch_export; cancel is a no-op.")


def test_prefs_reopen_last_and_dialog() -> None:
    """Preferences: a checkbox makes the app reopen the last .ptu/stack at
    launch. Loading records the path; the startup path resolves CLI > last-file
    > welcome; a vanished file falls back to the welcome screen; the dialog
    reads/writes the pref (Cancel writes nothing)."""
    from chronogate.ui import prefs
    from chronogate.ui.app import _startup_path
    from chronogate.ui.main_window import MainWindow

    ini = str(Path(tempfile.mkdtemp()) / "prefs.ini")
    saved = os.environ["CHRONOGATE_PREFS_INI"]
    os.environ["CHRONOGATE_PREFS_INI"] = ini
    try:
        assert prefs.reopen_last() is False, "reopen-last defaults to off"

        # A successful load records the file (its plane, not just the folder).
        w = _window()
        expect = str(w.controller.model.cube.path)
        assert prefs.last_path() == expect

        # Stepping planes moves the record: reopen brings back the last VIEWED z.
        w2 = MainWindow(None, open_dir=str(DATA_DIR))
        w2.controller.load_folder(str(DATA_DIR))
        z0_path = str(w2.controller.model.cube.path)
        w2.controller.step_z(1)
        expect = str(w2.controller.model.cube.path)
        assert expect != z0_path and prefs.last_path() == expect

        # Startup resolution: an explicit CLI path always wins; with the pref
        # off nothing auto-opens; on, the recorded file comes back; a recorded
        # file that no longer exists means the welcome screen, not a crash.
        assert _startup_path("/cli/wins.ptu") == "/cli/wins.ptu"
        assert _startup_path(None) is None
        prefs.set_reopen_last(True)
        assert _startup_path(None) == expect
        prefs.set_last_path("/nope/gone.ptu")
        assert _startup_path(None) is None

        # The dialog: checkbox mirrors the pref; OK writes, Cancel does not.
        prefs.set_reopen_last(False)
        dlg = prefs.PreferencesDialog(w)
        assert not dlg.chk_reopen.isChecked()
        dlg.chk_reopen.setChecked(True)
        dlg.accept()
        assert prefs.reopen_last() is True
        dlg2 = prefs.PreferencesDialog(w)
        dlg2.chk_reopen.setChecked(False)
        dlg2.reject()
        assert prefs.reopen_last() is True, "Cancel must not write"

        # Reachable from the menu bar (app menu on macOS via PreferencesRole).
        assert w.act_prefs.isEnabled()
    finally:
        os.environ["CHRONOGATE_PREFS_INI"] = saved
    print("OK: reopen-last preference (record, resolve, dialog, stale path).")


def test_one_page_report_ui() -> None:
    """File ▸ Export report… writes the four-role one-pager for the current
    mode (τ histogram / phasor cloud / intensity histogram as the primary),
    with a sha256 content hash of the source .ptu in the provenance and the
    headline number (uncertainty visible) leading the summary panel."""
    import hashlib
    w = _window()
    c = w.controller
    assert w.act_report.isEnabled(), "report is inert only before a load"

    # Lifetime mode with a selection: τ-histogram primary, headline + hash.
    w.act_lifetime.trigger()
    w.binning.bin.setValue(8)
    c._on_pixel_rows([(10, 20), (11, 21), (12, 22)])
    lines = c._report_summary_lines()
    assert any("median τ" in l for l in lines[:2]), "headline (with IQR) leads"
    assert any("sha256" in l for l in lines), "content hash on the page itself"
    out = Path(tempfile.mkdtemp())
    paths = c.export_report(out)
    for role in ("report_png", "report_pdf", "decay_csv", "primary_csv",
                 "raw_tiff", "provenance"):
        assert Path(paths[role]).exists(), f"missing {role}"
    prov = json.loads(Path(paths["provenance"]).read_text())
    h = hashlib.sha256(c.model.cube.path.read_bytes()).hexdigest()
    assert prov["metadata"]["source_sha256"] == h, "hash, not just a name"
    assert prov["settings"]["mode"] == "lifetime"
    with open(paths["primary_csv"]) as fh:
        assert fh.readline().strip() == "bin_lo,bin_hi,count,selection_count"

    # Phasor mode: the primary CSV becomes the (g, s) cloud.
    w.act_phasor.trigger()
    paths2 = c.export_report(out)
    with open(paths2["primary_csv"]) as fh:
        assert fh.readline().strip() == "g,s"
    assert paths2["report_png"] != paths["report_png"], "mode-stamped basenames"

    # Intensity mode works too, and the menu action routes via a folder picker.
    w.act_intensity.trigger()
    paths3 = c.export_report(out)
    assert Path(paths3["report_png"]).exists()

    import chronogate.ui.controller as ctrl_mod
    seen = {}
    c.export_report = lambda out_dir=None: seen.update(d=out_dir) or {}
    real = ctrl_mod.QFileDialog
    class _FakeDialog:
        Option = real.Option
        @staticmethod
        def getExistingDirectory(*a, **k):
            return "/tmp/report_out"
    ctrl_mod.QFileDialog = _FakeDialog
    try:
        c._on_export_report()
    finally:
        ctrl_mod.QFileDialog = real
    assert seen["d"] == "/tmp/report_out"
    print("OK: one-page report from all three modes; hash + headline verified.")


def test_restrict_export_to_selection() -> None:
    """Restrict-to-selection masks everything outside the selected pixels:
    the raster keeps its full-frame geometry (coordinates stay valid) but is
    NaN elsewhere, the decay CSV becomes the selection's pooled counts, the
    report's headline/panels cover only the selection, and the provenance
    states the restriction. Without a selection the flag is a no-op."""
    import csv as csvmod
    import tifffile
    from chronogate.export import ExportOptions

    w = _window()
    c = w.controller
    px = [(10, 20), (11, 21), (12, 22)]
    c._on_pixel_rows(px)
    out = Path(tempfile.mkdtemp())
    paths = c.export(out, options=ExportOptions(restrict_to_selection=True))

    # Raster: finite exactly on the selection, NaN everywhere else.
    img = tifffile.imread(paths["raw_tiff"])
    assert img.shape == c.model.intensity.shape, "full-frame geometry kept"
    finite = np.argwhere(np.isfinite(img))
    assert sorted(map(tuple, finite)) == sorted(px)
    prov = json.loads(Path(paths["provenance"]).read_text())
    assert prov["restricted_to_selection"] is True

    # Decay CSV: the pooled counts of the selected pixels, not the whole image.
    mask = np.zeros(c.model.intensity.shape, bool)
    for r, cc in px:
        mask[r, cc] = True
    want = np.rint(c.model.mask_decay(mask) * mask.sum()).astype(int)
    with open(paths["decay_csv"]) as fh:
        rows = list(csvmod.reader(fh))[1:]
    got = np.array([int(r[1]) for r in rows])
    assert got.shape == want.shape and (got == want).all()
    assert got.sum() < c.model.decay.sum(), "restricted decay is a subset"

    # Restricted report: scope stated on the page; the intensity histogram
    # covers exactly the selected pixels.
    rpaths = c.export_report(out, restrict=True)
    lines = c._report_summary_lines(mask=mask)
    assert any("selection only" in l for l in lines)
    with open(rpaths["primary_csv"]) as fh:
        rows = list(csvmod.reader(fh))[1:]
    assert sum(int(r[2]) for r in rows) == len(px)

    # No selection -> the flag quietly exports the full frame.
    c._clear_picks()
    paths2 = c.export(out, options=ExportOptions(restrict_to_selection=True))
    img2 = tifffile.imread(paths2["raw_tiff"])
    assert np.isfinite(img2).sum() > len(px)
    prov2 = json.loads(Path(paths2["provenance"]).read_text())
    assert prov2["restricted_to_selection"] is False, "no selection, no restriction"
    print("OK: restrict-to-selection masks raster/decay/report and says so.")


def test_no_qt_virtual_shadowing() -> None:
    """No widget attribute may shadow a Qt virtual method.

    PySide6 dispatches C++ virtual calls through Python attribute lookup, so
    ``self.metric = QComboBox()`` on a widget makes Qt's own DPI query crash with
    "Error calling Python override of QWidget::metric(): ... not callable" —
    an instant, data-independent crash the moment Qt asks (e.g. a floating dock
    resolving its devicePixelRatio). The trigger is environment-dependent
    (display config), so tests can pass while a real launch dies.
    """
    from PySide6.QtWidgets import QWidget
    # Qt virtuals commonly queried behind the app's back.
    virtuals = ("metric", "event", "eventFilter", "paintEngine", "devType",
                "sizeHint", "minimumSizeHint", "heightForWidth",
                "hasHeightForWidth", "initPainter", "redirected", "sharedPainter")
    w = _window()
    offenders = []
    for widget in [w, *w.findChildren(QWidget)]:
        for name in virtuals:
            if name in widget.__dict__ and not callable(widget.__dict__[name]):
                offenders.append(f"{type(widget).__name__}.{name} = "
                                 f"{type(widget.__dict__[name]).__name__}")
    assert not offenders, f"Qt virtuals shadowed by attributes: {offenders}"
    print("OK: no widget attribute shadows a Qt virtual method.")


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
        test_selection_aggregates_in_provenance()
        test_big_pixel_table_warning_and_optout()
        test_settings_roundtrip_restores_selection()
        test_pin_glyph_is_in_the_plot_font()
        test_pixel_cursor_and_goto()
        test_pick_undo()
        test_pick_markers_on_image()
        test_phasor_lasso_selection()
        test_fit_overlay()
        test_wave_c_views()
        test_cache_lockscale_and_t0()
        test_tau_hist_selection_overlay()
        test_probe_and_batch_export()
        test_batch_export_recuts_selection_per_plane()
        test_threaded_decode()
        test_compare_pinned_groups()
        test_selection_stats_in_stats_panel()
        test_rld_gates_valid_at_load_and_mode_keyed()
        test_tau_map_shared_cache()
        test_phasor_grid_under_hexbin()
        test_phasor_reference_marker_and_ruler()
        test_second_harmonic_ui()
        test_phasor_calibration_ui()
        test_floor_slider_summed_range_and_scale()
        test_no_qt_virtual_shadowing()
        test_export_dialog_defaults_and_mapping()
        test_export_dialog_drives_export()
        test_prefs_reopen_last_and_dialog()
        test_one_page_report_ui()
        test_restrict_export_to_selection()
    except AssertionError as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        raise SystemExit(1)
    print("All UI smoke tests passed.")
