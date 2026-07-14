"""The analysis/drawing core of the Qt app.

`ViewerController` owns the loaded model, every matplotlib artist, and all of the
gating/lifetime/refresh logic ported (largely verbatim) from the old
`GatingViewer`. It is deliberately toolkit-light: it draws onto two embedded
canvases (`DecayCanvas`, `ImageCanvas`) and talks to the widgets only through
named panel attributes and a handful of Qt signals. The Qt *layout* lives in
``main_window.py`` / ``panels.py``; the *computation* lives here, so the proven
refresh code keeps working and stays unit-testable.

Two structural notes vs. the old single-figure viewer:

* There are now **two figures**, so every method draws on the canvas it actually
  mutated (decay refreshes draw the decay canvas; image refreshes draw the image
  canvas; `_apply_gate` draws both).
* The old ``self._syncing`` boolean is replaced by Qt **signal blocking** when we
  push state back into widgets (see ``_blocked`` / ``sync_widgets_from_state``).
"""

from __future__ import annotations

import warnings
import weakref
from contextlib import contextmanager
from pathlib import Path

import numpy as np
import matplotlib
from matplotlib.colors import to_rgb
from matplotlib.path import Path as MplPath
from matplotlib.widgets import LassoSelector, SpanSelector
from matplotlib.patches import Rectangle

from PySide6.QtCore import QObject, Qt, Signal, QSignalBlocker, QThread, QTimer
from PySide6.QtWidgets import QApplication, QFileDialog

from .. import gating, metrics
from ..loader import find_stack, load_ptu, FrameCache
from .. import export as export_mod
from ..export import export_all, load_settings, save_settings
from . import theme


class _DecodeWorker(QObject):
    """Runs a (possibly slow) ``load_ptu`` on a background QThread.

    Lives in the worker thread (``moveToThread``); emits ``progress`` per frame
    and ``done``/``failed`` back to the controller (a main-thread QObject, so the
    connections are queued and the continuation runs on the GUI thread).
    """

    progress = Signal(int, int)
    done = Signal(object)     # FlimCube
    failed = Signal(str)

    def __init__(self, path, channel: int, sum_frames: bool):
        super().__init__()
        self._path, self._channel, self._sum = path, channel, sum_frames

    def run(self) -> None:
        try:
            cube = load_ptu(self._path, channel=self._channel, sum_frames=self._sum,
                            progress=lambda d, t: self.progress.emit(int(d), int(t)))
            self.done.emit(cube)
        except Exception as exc:  # noqa: BLE001 - surfaced to the user cleanly
            self.failed.emit(str(exc))

# Use Qt's own file dialog rather than the macOS native panel: the native
# NSOpenPanel is a known crash source with PySide6 (worse under Rosetta).
_DLG_OPT = QFileDialog.Option.DontUseNativeDialog

# Distinct colours for overlaid per-pixel/ROI decays (cycled).
_PICK_COLORS = [
    "#D1495B", "#2FA84F", "#7C3AED", "#9C6644", "#E76FA1",
    "#8A8D2B", "#0EA5B5", "#E8833A", "#C026D3", "#65A30D",
]

INTENSITY_CMAPS = ["viridis", "gray", "magma", "inferno", "cividis"]
LIFETIME_CMAPS = ["turbo", "viridis", "plasma", "inferno", "cividis"]

# Above this many pixels, a mask selection is shown only as a tint: one ring per
# pixel would be denser than the pixels themselves and read as a solid blob.
_MASK_MARKER_LIMIT = 500

# "Pinned" markers. matplotlib draws with DejaVu Sans, which has no U+1F4CC
# PUSHPIN -- it renders as a tofu box and warns on every redraw -- so the plot
# legend gets a star (which DejaVu does have) while the Qt list keeps the emoji.
_PIN_MARK_PLOT = "★ "   # BLACK STAR
_PIN_MARK_LIST = "📌 "

# A mask pick with no recipe (no lasso polygon) is saved as a coordinate list.
# Beyond this it would bloat the settings file, so it is dropped instead.
_MAX_SAVED_PIXELS = 50_000


@contextmanager
def _blocked(*objs):
    """Block Qt signals on ``objs`` for the duration (RAII via QSignalBlocker)."""
    blockers = [QSignalBlocker(o) for o in objs if o is not None]
    try:
        yield
    finally:
        del blockers


class ViewerController(QObject):
    statusMessage = Signal(str)
    headerChanged = Signal(str)
    titleChanged = Signal(str)

    def __init__(self, decay_canvas, image_canvas, channel=0, sum_frames=True, open_dir=None):
        super().__init__()
        self.dc = decay_canvas
        self.ic = image_canvas
        self.w = None  # the MainWindow (set in bind_view), used for panels + dialogs
        self.open_dir = open_dir  # default directory for the file dialogs

        # No file is loaded until load_path()/load_folder(); the app opens to a
        # welcome screen. A file may be one plane of a numbered z-stack.
        self.stack: list = []
        self.z_index = 0
        self.channel = channel
        self.sum_frames = sum_frames
        self._cube_cache = FrameCache()  # decoded cubes, so z/channel revisits are instant
        self._decode_thread = None       # background decode (QThread) state
        self._decode_worker = None
        self._decode_cont = None
        self._decode_key = None
        self._busy = False
        # Threaded decode needs a running event loop to deliver the result; the
        # launched app turns this on, tests/headless keep it synchronous.
        self.async_decode = False

        # Analysis state (the authoritative values; widgets render these).
        self.lock_scale = False           # freeze the colour range across frames
        self._locked_clim: dict = {}      # mode -> (vmin, vmax) while locked
        self._auto_clim: dict = {}        # mode -> last auto-scaled (vmin, vmax)
        self.manual_t0_ns = None          # None = auto (smoothed-peak) t0
        self.log_scale = True
        self.apply_floor = True
        self.threshold = 0
        self.box_size = 1
        self.smooth_bins = 5
        self.fit_curve = False   # overlay a mono-exponential fit on picked decays
        self.bin_size = 1
        self.bin_target = 100
        self.cmap = "viridis"
        self.mode = "intensity"          # "intensity" | "lifetime"
        self.edit_target = "A"
        self.lifetime_cmap = "turbo"
        self.rld_min_counts = 10.0
        self.hsv_lifetime = False   # intensity-weight the lifetime image
        self.combine = "single"     # "single" | "ratio A/B" | "merge RGB" (intensity mode)
        self._lifetime_init = False
        self.picks: list[dict] = []       # the live (replaced-on-click) pick
        self.pinned_picks: list[dict] = []  # frozen picks kept for comparison
        self._pick_lines: list = []
        self._pick_markers: list = []     # crosshair/box drawn on the image per pick
        self._gate_fills: list = []
        self._gate_bands: list = []
        self._picks_ymax = 1.0
        self._press_xy = None
        self._press_data = None

        # Hover probe: the decay of the pixel under the cursor, drawn live as the
        # mouse moves. Clicking locks it into `picks`; leaving the image drops it.
        #
        # A full matplotlib redraw costs ~60 ms -- far too slow for a curve that has
        # to track the cursor -- so a hover session is **blitted**: the static parts
        # of the decay panel (axes, grid, gate, floor, pinned/locked decays) are
        # rendered once and cached as a bitmap, and each frame only restores that
        # bitmap and paints the handful of `animated` hover artists over it.
        self.hover_probe = True
        self.hover_pick: dict | None = None
        self._hover_rc = None
        self._hovering = False        # a hover session is open (axes frozen for blit)
        self._hover_bg = None         # the cached background bitmap
        self._hover_fill = None       # per-frame gate shading under the hover curve

        # A selection of many pixels, spotlighted on the image and pooled into one
        # decay. Two ways in: a lasso round a phasor cluster (selection by lifetime
        # *signature*), or multi-selecting rows in the pixel list (by *rank*).
        self.select_mask = None           # bool (Y, X) or None
        self._lasso = None
        self._lasso_verts = None
        self._phasor_key = None           # cache key for the (g, s) maps
        self._phasor_gs = None

        # Cached from the last intensity refresh, so a hover can restate the
        # readout without recomputing the whole gated image.
        self._gated = None
        self._title_base = ""
        self._title_suffix = ""
        self._img_total = 0
        self._unit = "photons"

        # Pixel list (the dock): rebuilt when the numbers change, not when the
        # selection does -- clicking a row must not rebuild the table under it.
        # A rebuild ranks every pixel on every metric (~30 ms), so it is debounced:
        # holding an arrow key to walk the gate must not re-rank once per keypress.
        self._pixel_key = None
        self._skip_pixel_list = False
        self._pixel_timer = QTimer(self)
        self._pixel_timer.setSingleShot(True)
        self._pixel_timer.setInterval(120)
        self._pixel_timer.timeout.connect(self.refresh_pixel_list)

        # Filled in when a file is loaded (see load_path).
        self.model = None
        self.noise_floor_pp = 0.0   # noise floor in counts/bin per pixel
        self.gate_lo_bin = 0
        self.gate_hi_bin = 0
        self.gateB_lo_bin = 0
        self.gateB_hi_bin = 0

        self._build_artists()

    # ------------------------------------------------------- async decode
    def _decode_async(self, path, channel, sum_frames, cont) -> None:
        """Get the decoded cube for (path, channel, sum_frames), then call
        ``cont(cube, error)`` on the main thread. Cache hits are synchronous;
        misses decode on a background QThread with a live progress bar, so the
        UI never freezes. Overlapping decodes are refused (busy guard)."""
        key = (str(path), channel, sum_frames)
        cube = self._cube_cache.get(key)
        if cube is not None:
            cont(cube, None)
            return
        if not self.async_decode:      # synchronous (no running event loop)
            try:
                cube = load_ptu(path, channel=channel, sum_frames=sum_frames)
                print(cube.summary())
                self._cube_cache.put(key, cube)
            except Exception as exc:   # noqa: BLE001
                cont(None, str(exc))
                return
            cont(cube, None)
            return
        if self._decode_thread is not None:
            self.statusMessage.emit("Still loading a file — please wait…")
            return
        self._decode_key, self._decode_cont = key, cont
        self._set_busy(True, Path(path).name)
        self._decode_thread = QThread()
        self._decode_worker = _DecodeWorker(path, channel, sum_frames)
        self._decode_worker.moveToThread(self._decode_thread)
        self._decode_thread.started.connect(self._decode_worker.run)
        self._decode_worker.progress.connect(self._on_decode_progress)
        self._decode_worker.done.connect(self._on_decode_done)
        self._decode_worker.failed.connect(self._on_decode_failed)
        self._decode_thread.start()

    def _on_decode_progress(self, done: int, total: int) -> None:
        if self.w is not None and total > 1:
            self.w.set_progress(done, total)

    def _on_decode_done(self, cube) -> None:
        self._cube_cache.put(self._decode_key, cube)
        print(cube.summary())
        cont = self._decode_cont
        self._teardown_decode()
        if cont is not None:
            cont(cube, None)

    def _on_decode_failed(self, msg: str) -> None:
        cont = self._decode_cont
        self._teardown_decode()
        if cont is not None:
            cont(None, msg)

    def stop_decode(self) -> None:
        """Join any running decode thread (called on window close) so a QThread is
        never destroyed while still running -- the exact SIGABRT we've hit before."""
        th, wk = self._decode_thread, self._decode_worker
        self._decode_thread = self._decode_worker = None
        self._decode_cont = self._decode_key = None
        if th is not None:
            th.quit()
            th.wait()
            if wk is not None:
                wk.deleteLater()
            th.deleteLater()

    def _teardown_decode(self) -> None:
        self._set_busy(False)
        th, wk = self._decode_thread, self._decode_worker
        self._decode_thread = self._decode_worker = None
        self._decode_cont = self._decode_key = None
        if th is not None:
            th.quit()
            th.wait()
            wk.deleteLater()
            th.deleteLater()

    def _set_busy(self, on: bool, name: str = "") -> None:
        self._busy = on
        if self.w is None:
            return
        self.w.set_busy(on, name)

    # ----------------------------------------------------------------- loading
    def _load_current(self, progress=None) -> gating.GatingModel:
        # A decoded cube is cached per (file, channel, sum_frames), so stepping
        # back to a plane already visited (or a channel already seen) is instant;
        # only a first-time decode touches the disk.
        path = self.stack[self.z_index]
        key = (str(path), self.channel, self.sum_frames)
        cube = self._cube_cache.get(key)
        if cube is None:
            cube = load_ptu(path, channel=self.channel, sum_frames=self.sum_frames,
                            progress=progress)
            print(cube.summary())
            self._cube_cache.put(key, cube)
        return gating.GatingModel(cube, bin_factor=self.bin_size)

    def _floor_per_pixel(self) -> float:
        """The floor actually subtracted from each pixel (counts/bin per pixel)."""
        return self.noise_floor_pp

    def _floor_slider_range(self) -> tuple[int, int]:
        """Slider bounds for the noise floor, in summed-decay units: the lowest
        and highest recorded values across the whole decay curve (the floor line
        is drawn on that curve, so it can sweep it end to end)."""
        d = self.model.decay
        return int(d.min()), int(d.max())

    # --------------------------------------------------------------- artists
    def _build_artists(self) -> None:
        self._phasor_artists = []
        self._tau_hist_ax = None
        ax = self.dc.ax
        (self.decay_line,) = ax.plot([], [], color=theme.ACCENT, drawstyle="steps-post",
                                     lw=1.4, label="_nolegend_")
        self.t0_line = ax.axvline(0.0, color=theme.MUTED, ls="--", lw=1, label="_nolegend_")
        self.floor_line = ax.axhline(0.0, color=theme.FLOOR, ls=":", lw=1.4,
                                     visible=False, label="_nolegend_")

        # The hover artists. `animated=True` keeps them out of ordinary draws, so
        # they never get baked into the cached blit background.
        (self.hover_line,) = ax.plot([], [], color=theme.SELECT, lw=1.8,
                                     drawstyle="steps-post", animated=True, visible=False,
                                     label="_nolegend_", zorder=8)
        (self.hover_fit_line,) = ax.plot([], [], color=theme.SELECT, lw=1.6, ls="--",
                                         animated=True, visible=False,
                                         label="_nolegend_", zorder=9)
        self.hover_text = ax.text(
            0.015, 0.03, "", transform=ax.transAxes, fontsize=8, color=theme.SELECT,
            ha="left", va="bottom", animated=True, visible=False, zorder=10,
            bbox=dict(boxstyle="round,pad=0.35", fc=theme.PANEL, ec=theme.SELECT,
                      alpha=0.92, lw=0.8))
        # Any real draw of the decay canvas invalidates the cached bitmap, so
        # re-capture it there: resizes and stray refreshes then just work.
        self.dc.mpl_connect("draw_event", self._on_decay_draw)

        # A placeholder image until a file is loaded; load_path sets real data.
        self.im = self.ic.ax.imshow(np.zeros((2, 2)), cmap=self._image_cmap("intensity"),
                                    interpolation="nearest", origin="upper")
        self.cbar = self.ic.fig.colorbar(self.im, ax=self.ic.ax, fraction=0.046, pad=0.02,
                                         label="photons in gate")
        self.cbar.outline.set_edgecolor(theme.BORDER_HI)
        # A translucent RGBA layer over the image, tinting the pixels selected by
        # the phasor lasso (drawn above the image, below the pick markers).
        self.mask_im = self.ic.ax.imshow(np.zeros((2, 2, 4)), interpolation="nearest",
                                         origin="upper", zorder=4)
        self.mask_im.set_visible(False)

        # Draggable gate on the decay (computes only on release; cheap blit drag).
        self._span = SpanSelector(
            ax, self._on_gate, direction="horizontal", useblit=True, interactive=True,
            drag_from_anywhere=True, props=dict(alpha=0.18, facecolor=theme.GATE_A),
        )
        # Pixel/ROI picking on the image: a hand-rolled rubber-band using the raw
        # mouse events (a click adds a pixel; a drag adds a box). This is more
        # robust across HiDPI/blit than a matplotlib RectangleSelector.
        self._roi_patch = None
        self.ic.mpl_connect("button_press_event", self._on_image_press)
        self.ic.mpl_connect("motion_notify_event", self._on_image_motion)
        self.ic.mpl_connect("button_release_event", self._on_image_release)
        self.ic.mpl_connect("axes_leave_event", self._on_image_leave)

    # --------------------------------------------------------------- view bind
    def bind_view(self, window) -> None:
        """Wire panel/action signals; show the welcome state until a file loads."""
        self.w = window
        self._connect_signals()
        if self.model is None:
            self._show_empty()
        else:
            self._refit_ranges()
            self.sync_widgets_from_state()
            self._update_header()
            self._refresh_decay()
            self._refresh_image()

    def _show_empty(self) -> None:
        """No file loaded: hand the window to its welcome screen."""
        if self.w is not None:
            self.w.set_loaded(False)
        self.headerChanged.emit("No file open  —  File ▸ Open")
        self.titleChanged.emit("ChronoGate")

    def _connect_signals(self) -> None:
        w = self.w
        w.gate.spin_lo.editingFinished.connect(self._on_gate_text)
        w.gate.spin_hi.editingFinished.connect(self._on_gate_text)
        w.display.thr.valueChanged.connect(self._on_threshold)
        w.display.floor.valueChanged.connect(self._on_noise_floor)
        w.display.cmap.currentTextChanged.connect(self._on_cmap)
        w.display.btn_floor_auto.clicked.connect(self._on_floor_auto)
        w.display.lock.toggled.connect(self._on_lock_scale)
        w.display.vmin.valueChanged.connect(self._on_manual_clim)
        w.display.vmax.valueChanged.connect(self._on_manual_clim)
        w.gate.t0.valueChanged.connect(self._on_t0)
        w.gate.btn_t0_auto.clicked.connect(self._on_t0_auto)
        w.lifetime.radio_a.toggled.connect(self._on_edit_radio)
        w.lifetime.min_cts.valueChanged.connect(self._on_min_counts)
        w.lifetime.cmap_life.currentTextChanged.connect(self._on_lifetime_cmap)
        w.lifetime.hsv.toggled.connect(self._on_hsv_lifetime)
        w.filep.combine.currentTextChanged.connect(self._on_combine)
        w.picks.avg.valueChanged.connect(self._on_box_size)
        w.picks.smooth.valueChanged.connect(self._on_smooth)
        w.picks.fit.toggled.connect(self._on_fit_curve)
        w.picks.hover.toggled.connect(self._on_hover_probe)
        w.picks.btn_go.clicked.connect(self._on_goto_pixel)
        w.picks.row.editingFinished.connect(self._on_goto_pixel)
        w.picks.col.editingFinished.connect(self._on_goto_pixel)
        w.picks.btn_clear.clicked.connect(self._clear_picks)
        w.picks.btn_pin.clicked.connect(self._on_pin)
        w.pixels.metric.currentIndexChanged.connect(self._on_pixel_metric)
        w.pixels.desc.toggled.connect(lambda _=None: self.refresh_pixel_list())
        w.pixels.limit.valueChanged.connect(lambda _=None: self.refresh_pixel_list())
        w.pixels.fmin.editingFinished.connect(self.refresh_pixel_list)
        w.pixels.fmax.editingFinished.connect(self.refresh_pixel_list)
        w.pixels.refreshRequested.connect(self.refresh_pixel_list)
        w.pixels.pixelsChosen.connect(self._on_pixel_rows)
        w.pixel_dock.visibilityChanged.connect(self._on_pixel_dock)
        w.binning.bin.valueChanged.connect(self._on_bin_size)
        w.binning.target.valueChanged.connect(self._on_bin_target)
        w.binning.btn_auto.clicked.connect(self._on_auto_bin)
        w.filep.z.valueChanged.connect(self._on_zslice)
        w.filep.channel.currentIndexChanged.connect(self._on_channel)
        w.filep.btn_open.clicked.connect(self._on_open_file)
        w.filep.btn_open_folder.clicked.connect(self._on_open_folder)
        w.filep.btn_export.clicked.connect(self._on_export)
        w.filep.btn_save.clicked.connect(self._on_save)
        w.filep.btn_load.clicked.connect(self._on_load)
        w.act_intensity.triggered.connect(lambda: self._enter_mode("intensity"))
        w.act_lifetime.triggered.connect(lambda: self._enter_mode("lifetime"))
        w.act_phasor.triggered.connect(lambda: self._enter_mode("phasor"))
        w.act_log.toggled.connect(self._on_log)
        w.act_floor.toggled.connect(self._on_floor)

    def _refit_ranges(self) -> None:
        """Set slider/spin ranges to the current model's scale."""
        w = self.w
        pos = self.model.intensity[self.model.intensity > 0]
        tmax = int(np.percentile(pos, 99.9)) if pos.size else 1
        w.display.thr.setRange(0, max(1, tmax))
        w.display.floor.setRange(*self._floor_slider_range())
        w.display.floor.setScale(self.log_scale)  # slider matches the decay's y-scale
        period = self.model.cube.period_ns if np.isfinite(self.model.cube.period_ns) else 1e6
        w.gate.spin_lo.setRange(0.0, period)
        w.gate.spin_hi.setRange(0.0, period)
        w.gate.t0.setRange(0.0, period)
        ny, nx = self.model.intensity.shape
        with _blocked(w.picks.row, w.picks.col):
            w.picks.row.setRange(0, max(0, ny - 1))
            w.picks.col.setRange(0, max(0, nx - 1))
        w.filep.z.setRange(0, max(0, len(self.stack) - 1))
        w.filep.z.setEnabled(len(self.stack) > 1)
        with _blocked(w.filep.channel):
            w.filep.channel.clear()
            w.filep.channel.addItems([str(c) for c in range(self.model.cube.n_channels)])
            w.filep.channel.setCurrentIndex(self.channel)
        w.filep.channel.setEnabled(self.model.cube.n_channels > 1)
        w.filep.combine.setEnabled(self.model.cube.n_channels > 1)

    def sync_widgets_from_state(self) -> None:
        """Push the authoritative state into every widget (signals blocked)."""
        w = self.w
        res = self.model.resolution_ns
        lo_ns, hi_ns = gating.gate_bounds_ns(*self._get_gate(self.edit_target), res)
        widgets = [w.gate.spin_lo, w.gate.spin_hi, w.gate.t0, w.display.thr, w.display.floor,
                   w.display.cmap, w.lifetime.radio_a, w.lifetime.radio_b,
                   w.lifetime.min_cts, w.lifetime.cmap_life, w.picks.avg, w.picks.smooth, w.picks.fit,
                   w.picks.hover,
                   w.binning.bin, w.binning.target, w.filep.z, w.filep.channel, w.filep.combine,
                   w.lifetime.hsv,
                   w.act_intensity, w.act_lifetime, w.act_phasor, w.act_log, w.act_floor, w.display.lock]
        with _blocked(*widgets):
            w.gate.spin_lo.setValue(lo_ns)
            w.gate.spin_hi.setValue(hi_ns)
            w.gate.t0.setValue(self.model.t0_ns())
            w.display.lock.setChecked(self.lock_scale)
            w.display.vmin.setEnabled(self.lock_scale)
            w.display.vmax.setEnabled(self.lock_scale)
            w.display.thr.setValue(self.threshold)
            w.display.floor.setValue(min(self.noise_floor_pp * self.model.n_pixels,
                                         w.display.floor.maximum()))
            w.display.cmap.setCurrentText(self.cmap)
            w.lifetime.radio_a.setChecked(self.edit_target == "A")
            w.lifetime.radio_b.setChecked(self.edit_target == "B")
            w.lifetime.min_cts.setValue(int(self.rld_min_counts))
            w.lifetime.cmap_life.setCurrentText(self.lifetime_cmap)
            w.lifetime.hsv.setChecked(self.hsv_lifetime)
            w.picks.avg.setValue(self.box_size)
            w.picks.smooth.setValue(self.smooth_bins)
            w.picks.fit.setChecked(self.fit_curve)
            w.picks.hover.setChecked(self.hover_probe)
            w.binning.bin.setValue(self.bin_size)
            w.binning.target.setValue(self.bin_target)
            w.filep.z.setValue(min(self.z_index, w.filep.z.maximum()))
            w.filep.channel.setCurrentIndex(self.channel)
            w.filep.combine.setCurrentText(self.combine)
            w.act_intensity.setChecked(self.mode == "intensity")
            w.act_lifetime.setChecked(self.mode == "lifetime")
            w.act_phasor.setChecked(self.mode == "phasor")
            w.act_log.setChecked(self.log_scale)
            w.act_floor.setChecked(self.apply_floor)
        w.gate.set_active_label(self.edit_target, self.mode)
        w.set_lifetime_enabled(self.mode == "lifetime")

    def _update_header(self) -> None:
        bin_note = f" · bin {self.bin_size}×{self.bin_size}" if self.bin_size > 1 else ""
        self.headerChanged.emit(
            f"{self.model.cube.path.name}   ·   z {self.z_index + 1}/{len(self.stack)}"
            f"   ·   ch {self.channel}{bin_note}"
        )
        self.titleChanged.emit(f"ChronoGate — {self.model.cube.path.name}")

    # -------------------------------------------------------------- gate access
    def _get_gate(self, which: str) -> tuple[int, int]:
        if which == "B":
            return self.gateB_lo_bin, self.gateB_hi_bin
        return self.gate_lo_bin, self.gate_hi_bin

    def _set_gate(self, which: str, lo: int, hi: int) -> None:
        if which == "B":
            self.gateB_lo_bin, self.gateB_hi_bin = lo, hi
        else:
            self.gate_lo_bin, self.gate_hi_bin = lo, hi

    def _gates(self) -> list[tuple[str, int, int, str]]:
        out = [("A", self.gate_lo_bin, self.gate_hi_bin, theme.GATE_A)]
        if self.mode == "lifetime":
            out.append(("B", self.gateB_lo_bin, self.gateB_hi_bin, theme.GATE_B))
        return out

    # ---------------------------------------------------------------- overlays
    @staticmethod
    def _smooth(y: np.ndarray, window: int) -> np.ndarray:
        w = int(window)
        if w <= 1 or y.size < w:
            return y
        return np.convolve(y, np.ones(w) / w, mode="same")

    def _redraw_gate_overlays(self) -> None:
        for coll in self._gate_fills:
            coll.remove()
        for patch in self._gate_bands:
            patch.remove()
        self._gate_fills = []
        self._gate_bands = []
        ax = self.dc.ax
        res = self.model.resolution_ns
        x = self.model.cube.time_axis_ns
        floor_on = self.apply_floor and self.noise_floor_pp > 0
        for which, lo_bin, hi_bin, color in self._gates():
            lo_ns, hi_ns = gating.gate_bounds_ns(lo_bin, hi_bin, res)
            in_gate = (x >= lo_ns) & (x < hi_ns)
            if self.mode == "lifetime" and which != self.edit_target:
                self._gate_bands.append(ax.axvspan(lo_ns, hi_ns, color=color, alpha=0.10, lw=0))
            if self._shown_picks():
                lower = self._floor_per_pixel() if floor_on else 0.0
                for ln in self._pick_lines:
                    y = np.asarray(ln.get_ydata(), dtype=float)
                    self._gate_fills.append(ax.fill_between(
                        x, lower, y, where=in_gate & (y > lower), step="post",
                        color=ln.get_color(), alpha=0.25, lw=0))
            else:
                # The summed decay is a total; draw the per-pixel floor at its
                # equivalent total level (× pixel count) so it lines up.
                lower = self.noise_floor_pp * self.model.n_pixels if floor_on else 0.0
                y = self.model.decay.astype(float)
                self._gate_fills.append(ax.fill_between(
                    x, lower, y, where=in_gate & (y > lower), step="post",
                    color=color, alpha=0.32, lw=0))

    def _pick_region(self, pick: dict):
        """Resolve a *rectangular* pick to ``(r0, r1, c0, c1, tag)`` -- a single
        pixel grows to its avg N×N box; an ROI keeps its bounds. Mask picks have
        no rectangle; use :meth:`_pick_decay` / :meth:`_pick_total` for those."""
        ny, nx = self.model.intensity.shape
        if pick["kind"] == "pixel":
            r, c, b = pick["r"], pick["c"], max(1, self.box_size)
            half = b // 2
            r0, r1 = max(0, r - half), min(ny, r + half + 1)
            c0, c1 = max(0, c - half), min(nx, c + half + 1)
            tag = f"px({r},{c})" + (f"·{b}²" if b > 1 else "")
        else:
            r0, r1, c0, c1 = pick["r0"], pick["r1"], pick["c0"], pick["c1"]
            tag = f"roi[{r0}:{r1},{c0}:{c1}]"
        return r0, r1, c0, c1, tag

    def _pick_tag(self, pick: dict) -> str:
        """Short label for a pick of any kind (pixel, roi, or a mask of many pixels)."""
        if pick["kind"] == "mask":
            n = int(np.count_nonzero(pick["mask"]))
            return pick.get("label") or f"selection ({n:,} px)"
        return self._pick_region(pick)[4]

    def _pick_decay(self, pick: dict) -> np.ndarray:
        """That pick's decay, in counts/bin *per pixel* (so all kinds share a y-scale)."""
        if pick["kind"] == "mask":
            # Pooling tens of thousands of pixels is far too costly to redo on every
            # hover frame, so memoise it against the model it was computed from.
            cached = pick.get("_decay")
            if cached is not None and cached[0]() is self.model:
                return cached[1]
            decay = self.model.mask_decay(pick["mask"])
            pick["_decay"] = (weakref.ref(self.model), decay)
            return decay
        r0, r1, c0, c1, _ = self._pick_region(pick)
        return self.model.pixel_decay(r0, r1, c0, c1)

    def _pick_total(self, pick: dict, gated: np.ndarray) -> float:
        """That pick's total photons in the gate, read off the gated image."""
        if pick["kind"] == "mask":
            m = pick["mask"]
            return float(gated[m].sum()) if m.shape == gated.shape[:2] else 0.0
        r0, r1, c0, c1, _ = self._pick_region(pick)
        return float(gated[r0:r1, c0:c1].sum())

    def _active_pick(self) -> dict | None:
        """The pick the image readout speaks for: the locked pixel/region/lasso.

        The *hovered* pixel is deliberately not this -- it is reported on the decay
        panel and the status bar, both of which update for free, while the image
        title cannot be repainted at cursor speed.
        """
        return self.picks[0] if len(self.picks) == 1 else None

    def _redraw_pick_lines(self) -> None:
        for ln in self._pick_lines:
            ln.remove()
        self._pick_lines = []
        self._picks_ymax = 0.0
        x = self.model.cube.time_axis_ns
        ny, nx = self.model.intensity.shape
        # Per-pixel floor subtracted from each pick's in-gate count, matching the
        # shaded area under the decay (photons above the floor line).
        floor_pp = (self._floor_per_pixel()
                    if (self.apply_floor and self.noise_floor_pp > 0) else 0.0)
        list_items = []
        shown_picks = self._shown_picks()
        n_pinned = len(self.pinned_picks)
        for i, pick in enumerate(shown_picks):
            pinned = i < n_pinned
            tag = self._pick_tag(pick)
            raw = self._pick_decay(pick)
            shown = self._smooth(raw, self.smooth_bins)
            seg = raw[self.gate_lo_bin: self.gate_hi_bin + 1] - floor_pp
            in_gate = float(np.clip(seg, 0, None).sum())
            color = _PICK_COLORS[i % len(_PICK_COLORS)]

            # Optional mono-exponential fit overlay (a smooth visual guide).
            fit = (gating.fit_mono_exponential(x, raw, self.model.t0_ns(), floor_pp)
                   if self.fit_curve else None)
            tau_note = f"  τ≈{fit[1]:.2f} ns" if fit else ""
            body = f": {in_gate:.1f}/px in gate{tau_note}"
            # The Qt list renders any emoji; the *plot* font (DejaVu Sans) has no
            # pushpin, so a 📌 in the legend is a tofu box and a warning per redraw.
            legend_tag = (_PIN_MARK_PLOT + tag) if pinned else tag
            list_tag = (_PIN_MARK_LIST + tag) if pinned else tag
            # When fitting, fade the jagged raw steps so the smooth curve reads clearly.
            (ln,) = self.dc.ax.plot(x, shown, color=color, lw=1.2, drawstyle="steps-post",
                                    alpha=0.35 if fit else 1.0,
                                    label=f"{legend_tag}{body}")
            self._pick_lines.append(ln)
            self._picks_ymax = max(self._picks_ymax, float(shown.max()))
            if fit is not None:
                yfit = gating.mono_exponential_curve(x, self.model.t0_ns(), fit[0], fit[1], floor_pp)
                (fl,) = self.dc.ax.plot(x, yfit, color=color, lw=1.8, ls="--",
                                        label="_nolegend_")
                self._pick_lines.append(fl)
                self._picks_ymax = max(self._picks_ymax, float(np.nanmax(yfit)))
            list_items.append((f"{list_tag} — {in_gate:.1f}/px in gate{tau_note}", color))
        if self.w is not None:
            self.w.picks.set_items(list_items)

    # ------------------------------------------------------ on-image selection
    def _clear_pick_markers(self) -> None:
        for art in self._pick_markers:
            try:
                art.remove()
            except Exception:  # noqa: BLE001
                pass
        self._pick_markers = []

    def _redraw_pick_markers(self) -> None:
        """Mark every shown pick *on the image* in its decay colour.

        Without this, a picked pixel is invisible: one data pixel is a fraction of
        a screen pixel, so the crosshair -- not the pixel -- is what you actually
        see and step around with the arrow keys.
        """
        self._clear_pick_markers()
        if self.model is None or self.mode == "phasor":
            return
        ax = self.ic.ax
        for i, pick in enumerate(self._shown_picks()):
            if pick["kind"] == "mask":
                # The veil alone is enough for a big lasso, but a handful of
                # scattered pixels are single-pixel dots -- invisible without a ring
                # around each. Above the cap the rings would be denser than the
                # pixels, so the tint carries it on its own.
                m = pick["mask"]
                if int(m.sum()) <= _MASK_MARKER_LIMIT:
                    rr, cc = np.nonzero(m)
                    (art,) = ax.plot(cc, rr, linestyle="none", marker="o", ms=6,
                                     mew=1.2, mfc="none", mec=theme.SELECT, zorder=7)
                    self._pick_markers.append(art)
                continue
            color = _PICK_COLORS[i % len(_PICK_COLORS)]
            r0, r1, c0, c1, _ = self._pick_region(pick)
            rect = Rectangle((c0 - 0.5, r0 - 0.5), c1 - c0, r1 - r0, facecolor="none",
                             edgecolor=color, lw=1.4, zorder=6)
            ax.add_patch(rect)
            self._pick_markers.append(rect)
            if pick["kind"] == "pixel":
                # A 1x1 box is sub-pixel on screen; a crosshair makes it findable.
                (m,) = ax.plot([pick["c"]], [pick["r"]], marker="+", ms=13, mew=1.3,
                               color=color, zorder=7)
                self._pick_markers.append(m)

    def _redraw_mask_overlay(self, shape) -> None:
        """Spotlight the lasso-selected pixels: tint them, veil everything else.

        A tint alone is not enough -- a green wash over a viridis image reads as
        just another colormap value. Dimming the *unselected* pixels is what makes
        the selection unmistakable, whatever colormap is in use.
        """
        ny, nx = shape[:2]
        m = self.select_mask
        if m is None or m.shape != (ny, nx) or self.mode == "phasor":
            self.mask_im.set_visible(False)
            return
        rgba = np.zeros((ny, nx, 4), dtype=float)
        rgba[~m, 3] = 0.62                      # black veil over the rest
        rgba[m, :3] = to_rgb(theme.SELECT)
        rgba[m, 3] = 0.45
        self.mask_im.set_data(rgba)
        self.mask_im.set_extent((-0.5, nx - 0.5, ny - 0.5, -0.5))
        self.mask_im.set_visible(True)

    # ---------------------------------------------------------------- refresh
    def _refresh_decay(self) -> None:
        if self.model is None:
            return
        res = self.model.resolution_ns
        x = self.model.cube.time_axis_ns
        ax = self.dc.ax
        self._drop_hover_fill()      # a stale blit artist must not survive a real draw
        self.decay_line.set_data(x, self.model.decay.astype(float))
        self.t0_line.set_xdata([self.model.t0_ns(), self.model.t0_ns()])
        ax.set_xlim(0, x[-1] + res)

        # A hover session draws per-pixel curves, so the panel must be in per-pixel
        # units even with nothing picked.
        picks_mode = bool(self._shown_picks()) or self._hovering
        self.decay_line.set_visible(not picks_mode)
        self._redraw_pick_lines()

        if picks_mode:
            ax.set_ylabel("photons / pixel (mean)")
            floor_y = self._floor_per_pixel()
            data_max = self._picks_ymax
            if self._hovering:
                # Room for the brightest pixel in the image, so the frozen axis fits
                # every curve the cursor can land on.
                data_max = max(data_max, float(self.model.peak_counts_per_bin()))
        else:
            ax.set_ylabel("photons")
            # Per-pixel floor drawn at its equivalent total level on the summed decay.
            floor_y = self.noise_floor_pp * self.model.n_pixels
            data_max = float(self.model.decay.max())
        ymax = max(data_max, 1.0)   # scale to the data; a high floor pins to the top

        with warnings.catch_warnings():
            warnings.filterwarnings("ignore", message="Data has no positive values")
            if self.log_scale and data_max > 0:
                ax.set_yscale("log")
                bottom = 0.5
                if self.noise_floor_pp > 0 and 0 < floor_y < bottom:
                    bottom = max(1e-4, floor_y * 0.5)
                ax.set_ylim(bottom, ymax * 1.5)
            else:
                ax.set_yscale("linear")
                ax.set_ylim(0, ymax * 1.05)

        if self.noise_floor_pp > 0:
            line_y = min(floor_y, ymax)   # pin to the top when the floor exceeds the data
            self.floor_line.set_ydata([line_y, line_y])
            self.floor_line.set_visible(True)
        else:
            self.floor_line.set_visible(False)

        leg = ax.get_legend()
        if self._pick_lines:      # hovering with nothing picked has no labelled artist
            ax.legend(fontsize=7, loc="upper right", framealpha=0.85)
        elif leg is not None:
            leg.remove()

        # Clamp both gates, position the span on the active gate, recolour, sync.
        n = self.model.n_bins
        self.gate_hi_bin = min(self.gate_hi_bin, n - 1)
        self.gate_lo_bin = min(self.gate_lo_bin, self.gate_hi_bin)
        self.gateB_hi_bin = min(self.gateB_hi_bin, n - 1)
        self.gateB_lo_bin = min(self.gateB_lo_bin, self.gateB_hi_bin)
        lo_bin, hi_bin = self._get_gate(self.edit_target)
        self._span.extents = gating.gate_bounds_ns(lo_bin, hi_bin, res)
        self._recolor_span()
        self._sync_gate_textboxes()
        self._redraw_gate_overlays()
        # The cached bitmap is now stale; _on_decay_draw re-captures it on the
        # next real draw, and _hover_draw forces one if it has to.
        self._hover_bg = None
        self.dc.draw_idle()

    def _image_cmap(self, mode: str):
        name = self.lifetime_cmap if mode == "lifetime" else self.cmap
        cmap_obj = matplotlib.colormaps[name].copy()
        cmap_obj.set_bad(color=theme.BORDER)
        return cmap_obj

    @staticmethod
    def _clim_from(finite, lo_pct, hi_pct, floor_gap=1.0):
        if finite.size:
            vmin = float(np.percentile(finite, lo_pct))
            vmax = float(np.percentile(finite, hi_pct))
            if vmax <= vmin:
                vmax = vmin + floor_gap
        else:
            vmin, vmax = 0.0, floor_gap
        return vmin, vmax

    def _clim_for(self, mode: str, finite, lo_pct, hi_pct, floor_gap=1.0):
        """Colour limits for ``mode``: auto-scaled per frame, or frozen when the
        scale is locked (so z-planes are directly comparable). Also records the
        auto value and pushes the active range into the min/max boxes."""
        auto = self._clim_from(finite, lo_pct, hi_pct, floor_gap)
        self._auto_clim[mode] = auto
        if self.lock_scale:
            if self._locked_clim.get(mode) is None:
                self._locked_clim[mode] = auto
            vmin, vmax = self._locked_clim[mode]
        else:
            vmin, vmax = auto
        if self.w is not None and self.mode == mode:
            with _blocked(self.w.display.vmin, self.w.display.vmax):
                self.w.display.vmin.setValue(vmin)
                self.w.display.vmax.setValue(vmax)
        return vmin, vmax

    def _refresh_image(self) -> None:
        if self.model is None:
            return
        # The gated image is computed once per refresh in *every* mode (it is two
        # prefix-sum subtractions), so the hover readout can quote a pixel's
        # photons-in-gate instantly and can never quote a stale gate.
        floor = self._floor_per_pixel() if self.apply_floor else 0.0
        self._gated = self.model.gate(self.gate_lo_bin, self.gate_hi_bin, floor_per_bin=floor)
        self._img_total = int(self._gated.sum())
        self._unit = "photons" if self.bin_size == 1 else f"cts ·{self.bin_size}×{self.bin_size}"
        if self.mode == "lifetime":
            self._refresh_lifetime_image()
        elif self.mode == "phasor":
            self._refresh_phasor_image()
        else:
            self._refresh_intensity_image()
        if not self._skip_pixel_list and self.w is not None \
                and not self.w.pixel_dock.isHidden():
            self._pixel_timer.start()   # the numbers moved, so the ranking has too

    def _set_phasor_axes(self, on: bool) -> None:
        """Toggle the right panel between the image (colorbar, no ticks) and the
        phasor scatter (g/s axes, no image/colorbar). Removes phasor artists."""
        ax = self.ic.ax
        for a in self._phasor_artists:
            try:
                a.remove()
            except Exception:  # noqa: BLE001
                pass
        self._phasor_artists = []
        self.im.set_visible(not on)
        self.cbar.ax.set_visible(not on)
        self._enable_lasso(on)
        if on:
            self.mask_im.set_visible(False)
            self._clear_pick_markers()
            ax.set_xticks([0, 0.25, 0.5, 0.75, 1.0])
            ax.set_yticks([0, 0.25, 0.5])
            ax.set_xlabel("g"); ax.set_ylabel("s")
        else:
            ax.set_xticks([]); ax.set_yticks([])
            ax.set_xlabel(""); ax.set_ylabel("")

    # ------------------------------------------------------------ phasor lasso
    def _enable_lasso(self, on: bool) -> None:
        """The lasso only lives while the right panel *is* the phasor scatter."""
        if not on:
            if self._lasso is not None:
                try:
                    self._lasso.disconnect_events()
                except Exception:  # noqa: BLE001
                    pass
                self._lasso = None
            return
        if self._lasso is None:
            try:
                self._lasso = LassoSelector(
                    self.ic.ax, onselect=self._on_phasor_lasso, useblit=True,
                    props=dict(color=theme.SELECT, lw=1.4))
            except TypeError:      # older matplotlib without `props`
                self._lasso = LassoSelector(self.ic.ax, onselect=self._on_phasor_lasso)

    def _phasor_maps(self):
        """The per-pixel (g, s) maps, cached per (model, t0) -- the phasor is a full
        Fourier pass over the cube, far too costly to redo on every lasso.

        The model is held **weakly**, so swapping z/channel/binning drops the cache
        (a dead weakref can never compare equal to the live model) without this
        cache pinning a whole photon cube in memory.
        """
        key = self._phasor_key
        if key is None or key[0]() is not self.model or key[1] != self.model.t0_bin:
            self._phasor_key = (weakref.ref(self.model), self.model.t0_bin)
            self._phasor_gs = self.model.phasor()
        return self._phasor_gs

    def _lasso_mask(self, verts) -> np.ndarray:
        """Which pixels fall inside a polygon drawn on the phasor plot."""
        g, s = self._phasor_maps()
        pts = np.column_stack([g.ravel(), s.ravel()])
        ok = np.isfinite(pts).all(axis=1)
        inside = np.zeros(pts.shape[0], dtype=bool)
        inside[ok] = MplPath(np.asarray(verts)).contains_points(pts[ok])
        mask = inside.reshape(g.shape)
        if self.threshold > 0:
            mask &= (self.model.intensity >= self.threshold)
        return mask

    def _on_phasor_lasso(self, verts) -> None:
        """Pixels inside the drawn polygon become a selection: highlighted on the
        image and pooled into one decay curve. This is selection by *lifetime
        signature* rather than by location -- the point of having a phasor."""
        if self.model is None or len(verts) < 3:
            return
        mask = self._lasso_mask(verts)
        n = int(mask.sum())
        if n == 0:
            self.statusMessage.emit("Lasso caught no pixels — draw around a denser part of the cloud.")
            return
        self.select_mask = mask
        self._lasso_verts = np.asarray(verts)
        # Keep the polygon on the pick: it is the *recipe* for the mask, so a saved
        # settings file can restore a 160k-pixel selection from a few vertices.
        self._add_pick({"kind": "mask", "mask": mask, "verts": self._lasso_verts,
                        "label": f"phasor sel ({n:,} px)"})
        self.statusMessage.emit(
            f"Phasor lasso: {n:,} px ({100 * n / self.model.n_pixels:.1f}%) — their pooled decay is "
            f"on the left; press I for the intensity image to see them highlighted.")

    def _refresh_phasor_image(self) -> None:
        ax = self.ic.ax
        self._remove_tau_hist()
        self._set_phasor_axes(True)
        gmap, smap = self._phasor_maps()
        keep = np.isfinite(gmap) & np.isfinite(smap)
        if self.threshold > 0:
            keep &= (self.model.intensity >= self.threshold)
        g, s = gmap[keep], smap[keep]
        if g.size:
            hb = ax.hexbin(g, s, gridsize=100, cmap=self._image_cmap("intensity"),
                           mincnt=1, extent=(-0.05, 1.05, -0.02, 0.62))
            self._phasor_artists.append(hb)
        gc, sc = gating.phasor_semicircle()
        (ln,) = ax.plot(gc, sc, color=theme.MUTED, lw=1.3, zorder=6)
        self._phasor_artists.append(ln)
        sel = ""
        if self.select_mask is not None and self._lasso_verts is not None:
            v = np.vstack([self._lasso_verts, self._lasso_verts[:1]])   # close the loop
            (poly,) = ax.plot(v[:, 0], v[:, 1], color=theme.SELECT, lw=1.6, zorder=7)
            self._phasor_artists.append(poly)
            sel = f"  ·  {int(self.select_mask.sum()):,} px selected"
        ax.set_xlim(-0.05, 1.05)
        ax.set_ylim(-0.02, 0.62)
        ax.set_aspect("equal")
        ax.set_title(f"phasor  ·  {g.size:,} px  ·  harmonic 1{sel}\n"
                     f"drag a lasso around a cluster to select those pixels", fontsize=9)
        self.ic.draw_idle()
        if self.w is not None:
            gm = float(np.median(g)) if g.size else float("nan")
            sm = float(np.median(s)) if s.size else float("nan")
            self.w.stats.set_stats({
                "Mode": "phasor (g, s)",
                "Points": f"{g.size:,} / {self.model.n_pixels:,} px",
                "Median": f"g {gm:.3f} · s {sm:.3f}" if g.size else "—",
                "Selected": (f"{int(self.select_mask.sum()):,} px (lasso)"
                             if self.select_mask is not None else "— (drag a lasso)"),
            })

    def _refresh_intensity_image(self) -> None:
        self._set_phasor_axes(False)
        self._remove_tau_hist()
        res = self.model.resolution_ns
        # Per pixel: that pixel's decay integrated over the gate, minus the floor
        # (computed in _refresh_image, which every mode goes through).
        gated = self._gated
        lo_ns, hi_ns = gating.gate_bounds_ns(self.gate_lo_bin, self.gate_hi_bin, res)
        t0 = self.model.t0_ns()
        self._title_base = f"gate {lo_ns:.2f}–{hi_ns:.2f} ns  (t0{lo_ns - t0:+.1f}…{hi_ns - t0:+.1f})\n"
        self._title_suffix = ""

        combine = self.combine if self.model.cube.n_channels >= 2 else "single"
        mask = (self.model.intensity >= self.threshold) if self.threshold > 0 else None

        if combine == "single":
            display = gated.astype(float)
            if mask is not None:
                display[~mask] = np.nan
            vmin, vmax = self._clim_for("intensity", display[np.isfinite(display)], 1, 99)
            self.im.set_cmap(self._image_cmap("intensity"))
            self.im.set_data(display)
            self.im.set_clim(vmin, vmax)
            self.cbar.set_label("photons in gate")
        else:
            other = (self.channel + 1) % self.model.cube.n_channels
            gb = self._gated_channel(other)
            if combine.startswith("ratio"):
                with np.errstate(divide="ignore", invalid="ignore"):
                    display = gated.astype(float) / gb
                display[(gb <= 0)] = np.nan
                if mask is not None:
                    display[~mask] = np.nan
                vmin, vmax = self._clim_for("intensity", display[np.isfinite(display)], 2, 98)
                self.im.set_cmap(self._image_cmap("intensity"))
                self.im.set_data(display)
                self.im.set_clim(vmin, vmax)
                self.cbar.set_label(f"ratio ch{self.channel}/ch{other}")
                self._title_suffix = f"  ·  ratio ch{self.channel}/ch{other}"
            else:  # merge RGB
                rgb = self._merge_rgb(gated, gb, mask)
                self.im.set_data(rgb)
                self.cbar.set_label(f"merge R=ch{self.channel} G=ch{other}")
                self._title_suffix = f"  ·  merge R=ch{self.channel} G=ch{other}"
            display = gated.astype(float)

        self._fit_image_axes(gated.shape)
        self._redraw_mask_overlay(gated.shape)
        self._redraw_pick_markers()
        self._set_image_title(self._compose_title())
        self.ic.draw_idle()
        self._update_stats(gated, lo_ns, hi_ns)

    def _compose_title(self) -> str:
        """The image readout: the gate, then either the *selected* pixel/region/lasso's
        photons-in-gate (with the image total alongside) or the image total alone."""
        active = self._active_pick()
        if active is not None and self._gated is not None:
            sel = int(self._pick_total(active, self._gated))
            body = (f"{self._pick_tag(active)}: {sel:,} {self._unit} in gate"
                    f"  ·  image {self._img_total:,}")
        else:
            body = f"{self._img_total:,} {self._unit} in gate"
        return self._title_base + body + self._title_suffix

    def _set_image_title(self, text: str) -> None:
        """Set the image title, shrinking it if it would overrun the panel.

        The image panel narrows a lot when the pixel-list dock opens, and a title
        wider than the axes is clipped *from the left* -- eating the pixel
        coordinates, the very thing the readout exists to show.
        """
        width_in = self.ic.ax.get_window_extent().width / self.ic.fig.dpi
        longest = max((len(line) for line in text.split("\n")), default=0)
        # ~0.6 * fontsize is the average glyph advance in points; 72 pt = 1 inch.
        fits = (72.0 * width_in) / max(1, 0.6 * longest)
        self.ic.ax.set_title(text, fontsize=max(6.5, min(9.0, fits)))

    def _update_image_title(self) -> None:
        """Restate the readout for the current pick without redoing the gate maths."""
        if self.mode == "intensity" and self._gated is not None:
            self._set_image_title(self._compose_title())

    def _gated_channel(self, ch: int) -> np.ndarray:
        """Gated image for another channel of the current plane (cache-aware)."""
        key = (str(self.stack[self.z_index]), ch, self.sum_frames)
        cube = self._cube_cache.get(key)
        if cube is None:
            cube = load_ptu(self.stack[self.z_index], channel=ch, sum_frames=self.sum_frames)
            self._cube_cache.put(key, cube)
        m = gating.GatingModel(cube, bin_factor=self.bin_size)
        floor = self._floor_per_pixel() if self.apply_floor else 0.0
        return m.gate(self.gate_lo_bin, self.gate_hi_bin, floor_per_bin=floor)

    @staticmethod
    def _norm01(a):
        a = np.asarray(a, dtype=np.float64)
        pos = a[a > 0]
        hi = float(np.percentile(pos, 99)) if pos.size else 1.0
        return np.clip(a / max(hi, 1e-9), 0.0, 1.0)

    def _merge_rgb(self, ga, gb, mask):
        rgb = np.zeros(ga.shape + (3,), dtype=np.float64)
        rgb[..., 0] = self._norm01(ga)   # channel A -> red
        rgb[..., 1] = self._norm01(gb)   # channel B -> green
        if mask is not None:
            rgb[~mask] = 0.0
        return rgb

    def _fit_image_axes(self, shape) -> None:
        """Match the image extent and axes to the data (``set_data`` keeps the
        original 2x2 placeholder extent, so we must set it), so the image fills
        the panel and mouse picking uses true pixel coordinates."""
        ny, nx = shape[:2]
        self.im.set_extent((-0.5, nx - 0.5, ny - 0.5, -0.5))   # origin='upper'
        ax = self.ic.ax
        ax.set_aspect("equal")
        ax.set_xlim(-0.5, nx - 0.5)
        ax.set_ylim(ny - 0.5, -0.5)

    def _update_stats(self, gated, lo_ns: float, hi_ns: float) -> None:
        """Push gated-image statistics into the Stats panel (intensity mode).

        With a selection active the panel speaks for *it* instead: the whole point
        of selecting a population is to learn its aggregate numbers, and exporting
        a CSV to read a mean is a dead end.
        """
        if self.w is None:
            return
        active = self._active_pick()
        if active is not None:
            self._update_selection_stats(active, gated)
            return
        width_bins = abs(self.gate_hi_bin - self.gate_lo_bin) + 1
        total = int(self.model.intensity.sum())
        in_gate = float(gated.sum())
        signal = gated[gated > 0]
        npix = int(signal.size)
        unit = "photons" if self.bin_size == 1 else f"cts·{self.bin_size}²"
        self.w.stats.set_stats({
            "Gate": f"{lo_ns:.2f}–{hi_ns:.2f} ns  ({width_bins} bins · {hi_ns - lo_ns:.2f} ns)",
            f"In gate": f"{int(in_gate):,} {unit}"
                       + (f"  ({100 * in_gate / total:.1f}% of all)" if total > 0 else ""),
            "Signal px": f"{npix:,} / {self.model.n_pixels:,}",
            "Per-px": (f"mean {signal.mean():.1f} · med {np.median(signal):.0f} · max {int(signal.max()):,}"
                       if npix else "—"),
        })

    def _selection_stats(self, pick: dict) -> tuple[np.ndarray, dict]:
        """The pick's pixel mask and its aggregate metric statistics."""
        mask = self._pick_pixel_mask(pick)
        return mask, metrics.mask_stats(self._metric_ctx(), mask,
                                        keys=["in_gate", "total", "tau"])

    def _update_selection_stats(self, pick: dict, gated) -> None:
        """The Stats panel for a selection: its aggregate τ, photons and spread."""
        mask, st = self._selection_stats(pick)
        n = int(mask.sum())
        ing, tot, tau = st["in_gate"], st["total"], st["tau"]
        if tau["n"]:
            tau_row = (f"mean {tau['mean']:.2f} · med {tau['median']:.2f} "
                       f"± {tau['std']:.2f} ns  ({tau['n']:,} of {n:,} valid)")
        else:
            tau_row = "— (too few photons; try Binning ▸ Auto)"
        self.w.stats.set_stats({
            "Selection": f"{self._pick_tag(pick)} — {n:,} px",
            "In gate": (f"{int(self._pick_total(pick, gated)):,} {self._unit}"
                        f"  ·  mean {ing['mean']:.1f} ± {ing['std']:.1f}/px"
                        if ing["n"] else "—"),
            "τ (RLD)": tau_row,
            "Total": (f"mean {tot['mean']:.1f} · med {tot['median']:.0f} photons/px"
                      if tot["n"] else "—"),
        })

    def _remove_tau_hist(self) -> None:
        if self._tau_hist_ax is not None:
            try:
                self._tau_hist_ax.remove()
            except Exception:  # noqa: BLE001
                pass
            self._tau_hist_ax = None

    def _draw_tau_hist(self, finite, vmin, vmax) -> None:
        """A small τ-distribution histogram inset on the lifetime image, coloured
        by the τ colormap. The lock min/max restricts the shown range."""
        self._remove_tau_hist()
        if finite.size < 2 or vmax <= vmin:
            return
        from matplotlib.colors import Normalize
        ax = self.ic.ax.inset_axes([0.60, 0.70, 0.38, 0.28])
        counts, edges = np.histogram(finite, bins=40, range=(vmin, vmax))
        centers = 0.5 * (edges[:-1] + edges[1:])
        colors = matplotlib.colormaps[self.lifetime_cmap](Normalize(vmin, vmax)(centers))
        ax.bar(centers, counts, width=edges[1] - edges[0], color=colors, edgecolor="none")
        ax.patch.set_alpha(0.55)
        ax.set_yticks([])
        ax.tick_params(labelsize=6, length=2)
        ax.set_title("τ (ns)", fontsize=6)
        for spine in ("top", "right", "left"):
            ax.spines[spine].set_visible(False)
        self._tau_hist_ax = ax

    def _lifetime_rgb(self, tau, vmin, vmax):
        """Intensity-weighted lifetime image: hue from τ (the lifetime colormap),
        brightness from photon count, so dim/noisy pixels don't shout a false τ."""
        from matplotlib.colors import Normalize
        cmap = matplotlib.colormaps[self.lifetime_cmap]
        rgb = cmap(Normalize(vmin, vmax, clip=True)(tau))[..., :3]
        inten = self.model.intensity.astype(np.float64)
        pos = inten[inten > 0]
        imax = float(np.percentile(pos, 99)) if pos.size else 1.0
        v = np.clip(inten / max(imax, 1e-9), 0.0, 1.0)[..., None]
        rgb = rgb * v
        rgb[~np.isfinite(tau)] = 0.0
        return rgb

    def _compute_lifetime_map(self):
        floor = self._floor_per_pixel() if self.apply_floor else 0.0
        rl = self.model.rapid_lifetime(self._get_gate("A"), self._get_gate("B"),
                                       floor_per_bin=floor, min_counts=self.rld_min_counts)
        tau = np.asarray(rl["tau"], dtype=float).copy()
        if self.threshold > 0:
            tau[self.model.intensity < self.threshold] = np.nan
        return tau, rl

    def _refresh_lifetime_image(self) -> None:
        self._set_phasor_axes(False)
        tau, rl = self._compute_lifetime_map()
        finite = tau[np.isfinite(tau)]
        if finite.size == 0:
            self.statusMessage.emit(
                f"No pixels reached N ≥ {self.rld_min_counts:.0f} photons in BOTH gates — "
                f"increase binning (Auto) or lower 'min cts' to get a lifetime map.")
        vmin, vmax = self._clim_for("lifetime", finite, 2, 98, floor_gap=1e-3)
        self.im.set_cmap(self._image_cmap("lifetime"))
        self.im.set_clim(vmin, vmax)   # keeps the colorbar meaningful even in HSV
        if self.hsv_lifetime:
            self.im.set_data(self._lifetime_rgb(tau, vmin, vmax))
        else:
            self.im.set_data(tau)
        self._fit_image_axes(tau.shape)
        self._redraw_mask_overlay(tau.shape)
        self._redraw_pick_markers()
        self._draw_tau_hist(finite, vmin, vmax)
        self.cbar.set_label("apparent lifetime (ns)")

        res = self.model.resolution_ns
        a0, a1 = gating.gate_bounds_ns(*rl["early"], res)
        b0, b1 = gating.gate_bounds_ns(*rl["late"], res)
        med = float(np.median(finite)) if finite.size else float("nan")
        width_a = rl["early"][1] - rl["early"][0]
        width_b = rl["late"][1] - rl["late"][0]
        warn = "" if abs(width_a - width_b) <= 1 else "  ⚠ unequal width"
        self.ic.ax.set_title(
            f"RLD τ map  ·  Δt {rl['dt_ns']:.2f} ns\n"
            f"median τ ≈ {med:.2f} ns  ·  {finite.size:,} px{warn}", fontsize=9)
        self.ic.draw_idle()
        if self.w is not None:
            self.w.stats.set_stats({
                "Mode": "lifetime (two-gate RLD)",
                "Gates": f"A {a0:.1f}–{a1:.1f} · B {b0:.1f}–{b1:.1f} ns  (Δt {rl['dt_ns']:.2f}){warn}",
                "Median τ": f"{med:.2f} ns" if finite.size else "—",
                "Valid px": f"{finite.size:,} / {self.model.n_pixels:,}",
            })

    # --------------------------------------------------------- gate handlers
    def _apply_gate(self, xmin: float, xmax: float) -> None:
        res, n = self.model.resolution_ns, self.model.n_bins
        lo = gating.ns_to_bin(xmin, res, n)
        hi = gating.ns_to_bin(xmax, res, n)
        if hi < lo:
            lo, hi = hi, lo
        self._set_gate(self.edit_target, lo, hi)
        self._redraw_gate_overlays()
        self.dc.draw_idle()
        self._refresh_image()

    def _on_gate(self, xmin: float, xmax: float) -> None:
        self._apply_gate(xmin, xmax)
        self._sync_gate_textboxes()
        if self._shown_picks():
            self._refresh_decay()

    def _on_gate_text(self) -> None:
        start_ns = self.w.gate.spin_lo.value()
        end_ns = self.w.gate.spin_hi.value()
        lo, hi = self._ns_bounds_to_bins(start_ns, end_ns)
        self._set_gate(self.edit_target, lo, hi)
        self._refresh_decay()
        self._refresh_image()

    def _ns_bounds_to_bins(self, start_ns, end_ns):
        res, n = self.model.resolution_ns, self.model.n_bins
        lo = int(round(start_ns / res))
        hi = int(round(end_ns / res)) - 1
        lo = max(0, min(n - 1, lo))
        hi = max(0, min(n - 1, hi))
        if hi < lo:
            lo, hi = hi, lo
        return lo, hi

    def _sync_gate_textboxes(self) -> None:
        if self.w is None:
            return
        lo_bin, hi_bin = self._get_gate(self.edit_target)
        lo_ns, hi_ns = gating.gate_bounds_ns(lo_bin, hi_bin, self.model.resolution_ns)
        with _blocked(self.w.gate.spin_lo, self.w.gate.spin_hi):
            self.w.gate.spin_lo.setValue(lo_ns)
            self.w.gate.spin_hi.setValue(hi_ns)

    # ------------------------------------------------ rapid-lifetime (RLD) mode
    def _recolor_span(self) -> None:
        color = (theme.GATE_B if (self.mode == "lifetime" and self.edit_target == "B")
                 else theme.GATE_A)
        art = getattr(self._span, "_selection_artist", None) or getattr(self._span, "rect", None)
        if art is not None:
            try:
                art.set_facecolor(color)
                art.set_alpha(0.18)
            except Exception:
                pass

    def _default_lifetime_gates(self) -> tuple[tuple[int, int], tuple[int, int]]:
        """Equal-width A/B defaults: the first two quarters of the post-pulse axis."""
        n, t0 = self.model.n_bins, self.model.t0_bin
        width = max(2, (n - 1 - t0) // 4)
        a = (min(t0, n - 1), min(t0 + width - 1, n - 1))
        b = (min(t0 + width, n - 1), min(t0 + 2 * width - 1, n - 1))
        return a, b

    def _init_lifetime_gates(self) -> None:
        a, b = self._default_lifetime_gates()
        (self.gate_lo_bin, self.gate_hi_bin) = a
        (self.gateB_lo_bin, self.gateB_hi_bin) = b

    def _enter_mode(self, mode: str) -> None:
        if self.model is None:
            return
        self.mode = mode if mode in ("lifetime", "phasor") else "intensity"
        if self.mode == "lifetime" and not self._lifetime_init:
            self._init_lifetime_gates()
            self._lifetime_init = True
        if self.mode != "lifetime":
            self.edit_target = "A"
        if self.w is not None:
            self.w.set_lifetime_enabled(self.mode == "lifetime")
            with _blocked(self.w.lifetime.radio_a, self.w.lifetime.radio_b,
                          self.w.act_intensity, self.w.act_lifetime, self.w.act_phasor):
                self.w.lifetime.radio_a.setChecked(self.edit_target == "A")
                self.w.lifetime.radio_b.setChecked(self.edit_target == "B")
                self.w.act_intensity.setChecked(self.mode == "intensity")
                self.w.act_lifetime.setChecked(self.mode == "lifetime")
                self.w.act_phasor.setChecked(self.mode == "phasor")
            self.w.gate.set_active_label(self.edit_target, self.mode)
        msg = {
            "lifetime": "Lifetime mode: two-gate RLD  τ = Δt / ln(N_A/N_B). "
                        "Keep gates equal width; edit A/B with the radio or the ns boxes.",
            "phasor": "Phasor mode: each pixel → (g, s) on the universal semicircle "
                      "(fit-free). Uncalibrated (t0-referenced); use the intensity threshold to trim noise.",
            "intensity": "Intensity mode: single gate, photons integrated per pixel.",
        }[self.mode]
        self.statusMessage.emit(msg)
        self._refresh_decay()
        self._refresh_image()

    def _on_edit_radio(self, _checked=None) -> None:
        if self.mode != "lifetime":
            return
        self.edit_target = "A" if self.w.lifetime.radio_a.isChecked() else "B"
        self.w.gate.set_active_label(self.edit_target, self.mode)
        self._refresh_decay()

    def _on_min_counts(self, value) -> None:
        self.rld_min_counts = max(0.0, float(value))
        if self.mode == "lifetime":
            self._refresh_image()

    def _on_hsv_lifetime(self, checked) -> None:
        self.hsv_lifetime = bool(checked)
        if self.mode == "lifetime":
            self._refresh_image()

    def _on_combine(self, text) -> None:
        self.combine = text
        if self.mode == "intensity":
            self._refresh_image()

    def _on_lifetime_cmap(self, name) -> None:
        self.lifetime_cmap = name
        if self.mode == "lifetime":
            self._refresh_image()

    def enter_lifetime(self) -> None:
        """Public hook (CLI --lifetime) to start in lifetime mode."""
        self._enter_mode("lifetime")

    # ----------------------------------------------------- keyboard helpers
    def nudge_gate(self, d_lo: int, d_hi: int) -> None:
        """Shift/resize the active gate by whole bins (arrow-key shortcuts)."""
        if self.model is None:
            return
        n = self.model.n_bins
        lo, hi = self._get_gate(self.edit_target)
        lo = max(0, min(n - 1, lo + d_lo))
        hi = max(0, min(n - 1, hi + d_hi))
        if hi < lo:
            lo, hi = hi, lo
        self._set_gate(self.edit_target, lo, hi)
        self._refresh_decay()
        self._refresh_image()

    def step_z(self, delta: int) -> None:
        """Step the z-slice (PageUp/PageDown), keeping the slider in sync."""
        if self.model is None or len(self.stack) <= 1:
            return
        new_z = max(0, min(len(self.stack) - 1, self.z_index + delta))
        if new_z == self.z_index:
            return
        self.z_index = new_z
        with _blocked(self.w.filep.z):
            self.w.filep.z.setValue(self.z_index)
        self._reload_model_busy()
        self._refit_ranges()
        self._update_header()
        self._refresh_decay()
        self._refresh_image()

    # -------------------------------------------------- per-pixel decay picking
    def _on_image_press(self, event) -> None:
        if self.mode == "phasor":       # the right panel is a phasor plot, not the image
            self._press_xy = self._press_data = None
            return
        if (self.model is not None and event.inaxes is self.ic.ax
                and event.button == 1 and event.xdata is not None):
            self._press_xy = (event.x, event.y)
            self._press_data = (event.xdata, event.ydata)
        else:
            self._press_xy = None
            self._press_data = None

    def _on_image_motion(self, event) -> None:
        # While dragging, draw a rubber-band box from the press point to the cursor.
        if self._press_xy is None or self._press_data is None:
            self._hover_at(event)
            return
        if event.inaxes is not self.ic.ax or event.xdata is None:
            return
        x0, y0 = self._press_data
        x1, y1 = event.xdata, event.ydata
        if self._roi_patch is None:
            self._roi_patch = Rectangle((x0, y0), 0, 0, facecolor="none",
                                        edgecolor=theme.ACCENT, lw=1.2, ls="--", zorder=5)
            self.ic.ax.add_patch(self._roi_patch)
        self._roi_patch.set_bounds(min(x0, x1), min(y0, y1), abs(x1 - x0), abs(y1 - y0))
        self.ic.draw_idle()

    # ------------------------------------------------------------- hover probe
    def _hover_at(self, event) -> None:
        """Draw the decay of the pixel under the cursor, live, as the mouse moves."""
        if not self.hover_probe or self.model is None or self.mode == "phasor":
            return
        if event.inaxes is not self.ic.ax or event.xdata is None or event.ydata is None:
            self._on_image_leave(event)
            return
        ny, nx = self.model.intensity.shape
        r = max(0, min(ny - 1, int(round(event.ydata))))
        c = max(0, min(nx - 1, int(round(event.xdata))))
        if self._hover_rc == (r, c):
            return                      # still inside the same pixel: nothing to do
        self._hover_rc = (r, c)
        self.hover_pick = {"kind": "pixel", "r": r, "c": c, "label": f"px({r},{c})"}
        if not self._hovering:
            self._hover_begin()
        self._hover_draw()
        if self.w is not None:
            self.w.set_probe(self._probe_text(r, c))

    def _probe_text(self, r: int, c: int) -> str:
        """The status-bar readout for the pixel under the cursor."""
        parts = [f"px({r},{c})"]
        if self._gated is not None and self._gated.shape == self.model.intensity.shape:
            parts.append(f"{int(self._gated[r, c]):,} {self._unit} in gate")
        parts.append(f"{int(self.model.intensity[r, c]):,} total")
        return "   ·   ".join(parts)

    def _hover_begin(self) -> None:
        """Open a hover session: freeze the axes and cache the static background.

        The y-range is pinned to the image-wide per-pixel maximum for the whole
        session. That is required (a blit background bakes the axis in) and it is
        also what you want: with a fixed scale, sweeping the image *shows* one pixel
        being brighter than the last instead of the axis silently rescaling to hide it.
        """
        self._hovering = True
        self._refresh_decay()      # per-pixel units + the frozen y-range
        self.dc.draw()             # a real draw, so _on_decay_draw caches a background

    def _on_decay_draw(self, _event=None) -> None:
        """Re-cache the blit background after any real draw of the decay canvas."""
        if self._hovering:
            self._hover_bg = self.dc.copy_from_bbox(self.dc.fig.bbox)

    def _drop_hover_fill(self) -> None:
        if self._hover_fill is not None:
            try:
                self._hover_fill.remove()
            except Exception:  # noqa: BLE001
                pass
            self._hover_fill = None

    def _hover_draw(self) -> None:
        """One blitted frame: restore the cached bitmap, paint the hover artists."""
        if self._hover_bg is None:
            self.dc.draw()                 # re-caches via the draw_event hook
            if self._hover_bg is None:
                return
        ax = self.dc.ax
        x = self.model.cube.time_axis_ns
        pick = self.hover_pick
        raw = self._pick_decay(pick)
        shown = self._smooth(raw, self.smooth_bins)
        floor_pp = (self._floor_per_pixel()
                    if (self.apply_floor and self.noise_floor_pp > 0) else 0.0)

        fit = (gating.fit_mono_exponential(x, raw, self.model.t0_ns(), floor_pp)
               if self.fit_curve else None)
        self.hover_line.set_data(x, shown)
        self.hover_line.set_alpha(0.4 if fit else 1.0)
        self.hover_line.set_visible(True)
        if fit is not None:
            self.hover_fit_line.set_data(
                x, gating.mono_exponential_curve(x, self.model.t0_ns(), fit[0], fit[1], floor_pp))
        self.hover_fit_line.set_visible(fit is not None)

        # Shade the photons this pixel puts in the gate -- the same "area under the
        # curve above the floor" the locked picks show.
        self._drop_hover_fill()
        lo_ns, hi_ns = gating.gate_bounds_ns(self.gate_lo_bin, self.gate_hi_bin,
                                             self.model.resolution_ns)
        in_gate = (x >= lo_ns) & (x < hi_ns)
        self._hover_fill = ax.fill_between(
            x, floor_pp, shown, where=in_gate & (shown > floor_pp), step="post",
            color=theme.SELECT, alpha=0.22, lw=0, animated=True, zorder=7)

        self.hover_text.set_text(self._hover_summary(pick, fit))
        self.hover_text.set_visible(True)

        self.dc.restore_region(self._hover_bg)
        for art in (self._hover_fill, self.hover_line, self.hover_fit_line, self.hover_text):
            if art is not None and art.get_visible():
                ax.draw_artist(art)
        self.dc.blit(self.dc.fig.bbox)

    def _hover_summary(self, pick: dict, fit) -> str:
        """The live label pinned to the decay panel while hovering."""
        r, c = pick["r"], pick["c"]
        tag = self._pick_tag(pick)
        parts = [tag]
        if self._gated is not None and self._gated.shape == self.model.intensity.shape:
            parts.append(f"{int(self._pick_total(pick, self._gated)):,} {self._unit} in gate")
        parts.append(f"{int(self.model.intensity[r, c]):,} total")
        if fit is not None:
            parts.append(f"τ≈{fit[1]:.2f} ns")
        return "   ·   ".join(parts)

    def _end_hover(self, redraw: bool = True) -> None:
        """Close the hover session and restore the ordinary (autoscaled) decay."""
        self._hover_rc = None
        self.hover_pick = None
        if not self._hovering:
            return
        self._hovering = False
        self._hover_bg = None
        self._drop_hover_fill()
        for art in (self.hover_line, self.hover_fit_line, self.hover_text):
            art.set_visible(False)
        if redraw:
            self._refresh_decay()

    def _on_image_leave(self, event=None) -> None:
        """Cursor left the image: drop the preview, fall back to the locked pick."""
        if self.w is not None:
            self.w.set_probe("")
        self._end_hover()

    def _on_hover_probe(self, checked) -> None:
        self.hover_probe = bool(checked)
        if not self.hover_probe:
            self._on_image_leave()

    def _on_image_release(self, event) -> None:
        press_xy, press_data = self._press_xy, self._press_data
        self._press_xy = self._press_data = None
        if self._roi_patch is not None:
            self._roi_patch.remove()
            self._roi_patch = None
            self.ic.draw_idle()
        if (self.model is None or press_xy is None or event.inaxes is not self.ic.ax
                or event.button != 1 or event.xdata is None or event.ydata is None):
            return
        ny, nx = self.model.intensity.shape
        is_drag = abs(event.x - press_xy[0]) > 3 or abs(event.y - press_xy[1]) > 3
        if is_drag and press_data is not None:
            x0, y0 = press_data
            c0 = max(0, int(round(min(x0, event.xdata))))
            c1 = min(nx, int(round(max(x0, event.xdata))) + 1)
            r0 = max(0, int(round(min(y0, event.ydata))))
            r1 = min(ny, int(round(max(y0, event.ydata))) + 1)
            if (r1 - r0) * (c1 - c0) > 1:
                self._add_pick({"kind": "roi", "r0": r0, "r1": r1, "c0": c0, "c1": c1,
                                "label": f"roi[{r0}:{r1},{c0}:{c1}]"})
                return
        # A click (or a sub-pixel drag): pick the single pixel.
        self._add_pixel(int(round(event.ydata)), int(round(event.xdata)))

    def _add_pixel(self, r: int, c: int) -> None:
        ny, nx = self.model.intensity.shape
        r = max(0, min(ny - 1, r))
        c = max(0, min(nx - 1, c))
        self._add_pick({"kind": "pixel", "r": r, "c": c, "label": f"px({r},{c})"})

    def _add_pick(self, pick: dict) -> None:
        # A new pick *replaces* the live one (single decay at a time). Pinned
        # picks (via Pin) stay overlaid for comparison.
        if pick["kind"] != "mask":
            self.select_mask = None      # a hand-picked region supersedes the lasso
            self._lasso_verts = None
        self.picks = [pick]
        self._end_hover(redraw=False)    # the click locks in what was being previewed
        if self.w is not None and pick["kind"] == "pixel":
            self.w.picks.set_coords(pick["r"], pick["c"])
            self.ic.setFocus()           # so the arrow-key pixel cursor works at once
        self._refresh_decay()
        # Selecting a pixel must not rebuild the pixel list under the row you clicked.
        self._skip_pixel_list = True
        try:
            self._refresh_image()        # readout, markers and any lasso overlay
        finally:
            self._skip_pixel_list = False
        if self.w is not None and not self.w.pixel_dock.isHidden():
            self._reflect_pick_in_list()
        self.statusMessage.emit(f"Showing decay for {self._pick_tag(pick)}.")

    def _shown_picks(self) -> list:
        """Pinned picks plus the locked one, in draw order.

        The hovered pixel is *not* here: it is a blitted overlay drawn on top of
        these, so you compare the pixel under the cursor against your locked
        reference rather than replacing it.
        """
        return self.pinned_picks + self.picks

    def _validate_picks(self) -> None:
        """Drop picks that no longer fit the model (a new plane/channel can differ
        in size), so a stale pixel or mask can't index out of bounds."""
        ny, nx = self.model.intensity.shape

        def ok(p) -> bool:
            if p["kind"] == "mask":
                return p["mask"].shape == (ny, nx)
            if p["kind"] == "pixel":
                return 0 <= p["r"] < ny and 0 <= p["c"] < nx
            return p["r1"] <= ny and p["c1"] <= nx

        self.picks = [p for p in self.picks if ok(p)]
        self.pinned_picks = [p for p in self.pinned_picks if ok(p)]
        self._end_hover(redraw=False)
        self._pixel_key = None      # the metrics are on a new scale: reseed the filter
        if self.select_mask is not None and self.select_mask.shape != (ny, nx):
            self.select_mask = None
            self._lasso_verts = None

    # ------------------------------------------------- keyboard / typed picking
    def nudge_pixel(self, dr: int, dc: int) -> bool:
        """Step the selected pixel by (dr, dc) -- the arrow-key pixel cursor.

        Returns False when there is no single-pixel pick to move, which lets the
        shortcut fall through to its other job (nudging the gate).
        """
        if self.model is None or len(self.picks) != 1 or self.picks[0]["kind"] != "pixel":
            return False
        p = self.picks[0]
        self._add_pixel(p["r"] + dr, p["c"] + dc)
        return True

    def _on_goto_pixel(self) -> None:
        """Select the pixel typed into the row/col boxes (exact, reproducible)."""
        if self.model is None or self.w is None:
            return
        self._add_pixel(self.w.picks.row.value(), self.w.picks.col.value())

    # -------------------------------------------------------------- pixel list
    def _rld_gates(self) -> tuple[tuple[int, int], tuple[int, int]]:
        """The two gates the τ metric uses -- keyed on the *mode*, nothing subtler.

        In lifetime mode they are the user's A/B gates, so the τ column matches the
        τ map exactly. In every other mode the current gate is split into equal
        early/late halves -- the same two-gate RLD estimator applied to the gate you
        are actually looking at (a stale lifetime pair would quote a τ for gates the
        view no longer shows).
        """
        a = tuple(sorted((self.gate_lo_bin, self.gate_hi_bin)))
        b = tuple(sorted((self.gateB_lo_bin, self.gateB_hi_bin)))
        if self.mode == "lifetime":
            return a, b
        half = (a[1] - a[0] + 1) // 2
        if half < 1:
            return a, b
        return (a[0], a[0] + half - 1), (a[0] + half, a[0] + 2 * half - 1)

    def _metric_ctx(self) -> metrics.MetricContext:
        """The current analysis settings, packaged for the metric functions."""
        gate_a, gate_b = self._rld_gates()
        return metrics.MetricContext(
            model=self.model,
            gate_a=(self.gate_lo_bin, self.gate_hi_bin),   # the gate the image shows
            gate_b=gate_b,
            rld_gate_a=gate_a,
            floor_per_bin=self._floor_per_pixel() if self.apply_floor else 0.0,
            rld_min_counts=self.rld_min_counts,
            phasor_fn=self._phasor_maps,     # the cached (g, s) maps
        )

    def refresh_pixel_list(self) -> None:
        """Re-rank the pixels for the current gate. A no-op while the dock is closed,
        so the ranking never costs anything unless you are looking at it."""
        if self.model is None or self.w is None or self.w.pixel_dock.isHidden():
            return
        p = self.w.pixels
        key = p.current_metric()
        if self._pixel_key != key:
            self._seed_pixel_filter(key)   # calls back into here once seeded
            return
        table = metrics.rank(self._metric_ctx(), key,
                             vmin=p.fmin.value(), vmax=p.fmax.value(),
                             limit=p.limit.value(), descending=p.desc.isChecked())
        p.set_table(table, {m.key: m for m in metrics.metrics()})
        self._reflect_pick_in_list()

    def _reflect_pick_in_list(self) -> None:
        """Show the current pick as the table's selection (a rebuild would lose it)."""
        p = self.w.pixels
        pick = self.picks[0] if len(self.picks) == 1 else None
        if pick is None:
            p.select_matching(lambda r, c: False)
        elif pick["kind"] == "pixel":
            p.select_pixel(pick["r"], pick["c"])
        elif pick["kind"] == "mask":
            m = pick["mask"]
            p.select_matching(lambda r, c: bool(m[r, c]))
        else:                                       # an ROI: every pixel inside it
            r0, r1, c0, c1, _ = self._pick_region(pick)
            p.select_matching(lambda r, c: r0 <= r < r1 and c0 <= c < c1)

    def _seed_pixel_filter(self, key: str) -> None:
        """Reset the range filter to a metric's full span, so switching metric (or
        reloading a file) can never leave a bound from the *old* scale silently
        filtering every pixel out."""
        p = self.w.pixels
        self._pixel_key = key
        lo, hi = metrics.value_range(self._metric_ctx(), key)
        p.set_filter_bounds(lo, hi)
        with _blocked(p.desc):
            p.desc.setChecked(metrics.get(key).descending)
        self.refresh_pixel_list()

    def _on_pixel_metric(self, _idx=None) -> None:
        self._pixel_key = None          # force a reseed for the newly chosen metric
        self.refresh_pixel_list()

    def _on_pixel_dock(self, visible: bool) -> None:
        if visible:
            self.refresh_pixel_list()

    def _on_pixel_rows(self, picked: list) -> None:
        """Rows were chosen in the pixel list.

        One row selects that pixel. Several (Ctrl/⌘-click, Shift-range, Ctrl+A)
        become one **group**: their pooled decay on the left, all of them
        spotlighted on the image, their combined photons-in-gate in the readout.
        Two hundred individual curves would be unreadable; a pooled one is the
        thing you actually want to look at. (Pin still compares a few one by one.)
        """
        if self.model is None or not picked:
            return
        if len(picked) == 1:
            self._add_pixel(*picked[0])
            return
        mask = np.zeros(self.model.intensity.shape, dtype=bool)
        rows, cols = zip(*picked)
        mask[np.asarray(rows), np.asarray(cols)] = True
        n = int(mask.sum())
        self.select_mask = mask
        self._lasso_verts = None          # this selection is not a phasor polygon
        self._add_pick({"kind": "mask", "mask": mask, "label": f"list sel ({n:,} px)"})

    def _on_pin(self) -> None:
        """Freeze the live decay so the next click can be compared against it."""
        if not self.picks:
            self.statusMessage.emit("Click a pixel/region first, then Pin it.")
            return
        if len(self.pinned_picks) < len(_PICK_COLORS) - 1:
            self.pinned_picks.append(self.picks[0])
            self.picks = []
        self._refresh_decay()
        self._refresh_image()
        self.statusMessage.emit(f"Pinned {len(self.pinned_picks)} decay(s); click another to compare.")

    def _clear_picks(self) -> None:
        self.picks = []
        self.pinned_picks = []
        self._end_hover(redraw=False)
        self.select_mask = None
        self._lasso_verts = None
        self._refresh_decay()
        self._refresh_image()   # readout reverts to the whole-image total
        self.statusMessage.emit("Picks cleared; showing total decay.")

    # ----------------------------------------------------- other event handlers
    def _on_threshold(self, val) -> None:
        self.threshold = int(val)
        self._refresh_image()

    def _on_floor_auto(self) -> None:
        """Reset the noise floor to the auto (robust-baseline) value."""
        if self.model is None:
            return
        self.noise_floor_pp = self.model.auto_noise_floor_pp()
        if self.w is not None:
            with _blocked(self.w.display.floor):
                self.w.display.floor.setValue(min(self.noise_floor_pp * self.model.n_pixels,
                                                  self.w.display.floor.maximum()))
        self._refresh_decay()
        self._refresh_image()

    def _on_noise_floor(self, val) -> None:
        # The slider is in summed-decay units; store it per pixel for subtraction.
        self.noise_floor_pp = float(val) / max(1, self.model.n_pixels)
        self._refresh_decay()
        self._refresh_image()

    def _on_cmap(self, name) -> None:
        self.cmap = name
        if self.mode == "intensity":
            self._refresh_image()

    def _on_box_size(self, val) -> None:
        self.box_size = max(1, int(val))
        if self._shown_picks():
            self._refresh_decay()

    def _on_smooth(self, val) -> None:
        self.smooth_bins = max(1, int(val))
        if self._shown_picks():
            self._refresh_decay()

    def _on_fit_curve(self, checked) -> None:
        self.fit_curve = bool(checked)
        if self._shown_picks():
            self._refresh_decay()

    def _on_bin_size(self, val) -> None:
        self.bin_size = max(1, int(val))
        self._rebuild_binned_model()
        self.statusMessage.emit(
            f"Binning {self.bin_size}×{self.bin_size}: each pixel pools its neighborhood "
            f"(≈{self.bin_size**2}× more photons/pixel)." if self.bin_size > 1 else "Binning off (1×1).")

    def _on_bin_target(self, val) -> None:
        self.bin_target = max(1, int(val))

    def _on_auto_bin(self) -> None:
        b, n0 = gating.suggest_bin_factor(
            self.model.cube.intensity, target_photons=self.bin_target, min_intensity=self.threshold)
        self.bin_size = b
        with _blocked(self.w.binning.bin):
            self.w.binning.bin.setValue(b)
        self._rebuild_binned_model()
        self.statusMessage.emit(
            f"Auto-bin: median signal pixel ≈ {n0:.0f} photons → {b}×{b} "
            f"(≈{n0 * b * b:.0f} photons/px, target {self.bin_target}).")

    def _rebuild_binned_model(self) -> None:
        self.model = gating.GatingModel(self.model.cube, bin_factor=self.bin_size)
        self._apply_manual_t0()
        self._validate_picks()
        # Binning changes the per-pixel scale, so reset the floor to its default.
        self.noise_floor_pp = self.model.auto_noise_floor_pp()
        self._refit_ranges()
        with _blocked(self.w.display.floor):
            self.w.display.floor.setValue(min(self.noise_floor_pp * self.model.n_pixels,
                                              self.w.display.floor.maximum()))
        self._update_header()
        self._refresh_decay()
        self._refresh_image()

    def _on_log(self, checked) -> None:
        self.log_scale = bool(checked)
        if self.w is not None:
            self.w.display.floor.setScale(self.log_scale)  # keep the slider in step
        self._refresh_decay()

    def _on_floor(self, checked) -> None:
        self.apply_floor = bool(checked)
        self._refresh_decay()
        self._refresh_image()

    def _on_lock_scale(self, checked) -> None:
        self.lock_scale = bool(checked)
        if checked:
            self._locked_clim = dict(self._auto_clim)   # freeze the current ranges
        else:
            self._locked_clim = {}
        if self.w is not None:
            self.w.display.vmin.setEnabled(checked)
            self.w.display.vmax.setEnabled(checked)
        self._refresh_image()

    def _on_manual_clim(self, _val=None) -> None:
        if not self.lock_scale or self.w is None:
            return
        vmin = self.w.display.vmin.value()
        vmax = max(self.w.display.vmax.value(), vmin + 1e-6)
        self._locked_clim[self.mode] = (vmin, vmax)
        self._refresh_image()

    def _apply_manual_t0(self) -> None:
        """Re-apply a manual t0 (if set) to the current model, e.g. after reload."""
        if self.manual_t0_ns is not None and self.model is not None:
            self.model.set_t0(gating.ns_to_bin(
                self.manual_t0_ns, self.model.resolution_ns, self.model.n_bins))

    def _on_t0(self, val_ns) -> None:
        if self.model is None:
            return
        self.manual_t0_ns = float(val_ns)
        self.model.set_t0(gating.ns_to_bin(val_ns, self.model.resolution_ns, self.model.n_bins))
        self._refresh_decay()
        self._refresh_image()

    def _on_t0_auto(self) -> None:
        if self.model is None:
            return
        self.manual_t0_ns = None
        self.model.set_t0(gating.detect_t0_bin(self.model.decay))
        if self.w is not None:
            with _blocked(self.w.gate.t0):
                self.w.gate.t0.setValue(self.model.t0_ns())
        self._refresh_decay()
        self._refresh_image()

    def _on_channel(self, idx) -> None:
        if idx < 0:
            return
        self.channel = int(idx)
        self._reload_async()

    def _on_zslice(self, val) -> None:
        self.z_index = int(val)
        self._reload_async()

    def _reload_async(self) -> None:
        """Swap the model for the current (z, channel); a cache miss decodes in
        the background (progress bar), applied via :meth:`_after_reload`."""
        self._decode_async(self.stack[self.z_index], self.channel, self.sum_frames,
                           self._after_reload)

    def _after_reload(self, cube, err) -> None:
        if err is not None:
            self.statusMessage.emit(f"Could not load: {err}")
            return
        self.model = gating.GatingModel(cube, bin_factor=self.bin_size)
        self._apply_manual_t0()
        self._validate_picks()
        self._refit_ranges()
        self._update_header()
        self._refresh_decay()
        self._refresh_image()

    def _reload_model_busy(self) -> None:
        """Synchronous reload (used by settings-apply); cache-aware, so it only
        blocks on a genuine first-time decode."""
        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            self.model = self._load_current()
            self._apply_manual_t0()
        finally:
            QApplication.restoreOverrideCursor()

    # --------------------------------------------------------- file/layer choice
    def _dialog_dir(self) -> str:
        """Initial directory for file dialogs (current file's folder, else default)."""
        if self.model is not None:
            return str(self.model.cube.path.parent)
        return self.open_dir or ""

    def _on_open_file(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self.w, "Open a .ptu file / layer", self._dialog_dir(),
            "PicoQuant PTU (*.ptu);;All files (*)", "", _DLG_OPT)
        if path:
            self.load_path(path)

    def _on_open_folder(self) -> None:
        directory = QFileDialog.getExistingDirectory(
            self.w, "Open a folder with a .ptu stack", self._dialog_dir(),
            QFileDialog.Option.ShowDirsOnly | _DLG_OPT)
        if directory:
            self.load_folder(directory)

    def load_folder(self, directory) -> None:
        """Load the .ptu stack under ``directory`` (z-series across files).

        Probes files so it opens the first *decodable FLIM image* rather than
        failing on a point-mode or old-style file, and reports what it skipped.
        """
        from ..loader import probe_ptu
        directory = Path(directory)
        ptus = sorted(directory.rglob("*.ptu"))
        if not ptus:
            self.statusMessage.emit(f"No .ptu files found under {directory.name}.")
            return
        chosen, skipped = None, []
        for p in ptus:
            status = probe_ptu(p)
            if status == "image":
                chosen = p
                break
            skipped.append(status)
        if chosen is None:
            kinds = ", ".join(f"{skipped.count(k)} {k}" for k in sorted(set(skipped)))
            self.statusMessage.emit(
                f"No openable FLIM image in {directory.name} — {len(ptus)} .ptu ({kinds}).")
            return
        self._folder_skipped = skipped
        self.load_path(chosen)  # find_stack groups its numbered siblings

    def _folder_load_note(self) -> str:
        skipped = getattr(self, "_folder_skipped", [])
        note = ""
        if len(self.stack) > 1:
            note = f"{len(self.stack)}-plane stack — step with the z-slider / PgUp-PgDn."
        if skipped:
            note += f"  Skipped {len(skipped)} non-image/old-style .ptu."
        return note

    def load_path(self, path, channel=None, sum_frames=None) -> None:
        """Load a .ptu file (and its numbered z-stack), building/refreshing the view.

        Works as both the initial load and a re-open: on the first load it sets
        fresh gate defaults (t0 → end); afterwards it clamps the existing gates.
        """
        path = Path(path)
        if channel is not None:
            self.channel = channel
        if sum_frames is not None:
            self.sum_frames = sum_frames
        try:
            self.stack = find_stack(path)
            self.z_index = next((i for i, p in enumerate(self.stack) if p == path), 0)
        except Exception as exc:  # noqa: BLE001
            self.statusMessage.emit(f"Could not open {path.name}: {exc}")
            return
        self._decode_async(self.stack[self.z_index], self.channel, self.sum_frames,
                           self._after_initial_load)

    def _after_initial_load(self, cube, err) -> None:
        if err is not None:
            if self.channel != 0:   # the new file may have fewer detector channels
                self.channel = 0
                self._decode_async(self.stack[self.z_index], 0, self.sum_frames,
                                   self._after_initial_load)
                return
            self.statusMessage.emit(f"Could not load: {err}")
            return

        first = self.model is None
        self.model = gating.GatingModel(cube, bin_factor=self.bin_size)
        self._apply_manual_t0()
        self.picks = []
        self.pinned_picks = []
        self._end_hover(redraw=False)
        self._pixel_key = None
        self.select_mask = None
        self._lasso_verts = None
        self.threshold = 0
        self.noise_floor_pp = self.model.auto_noise_floor_pp()
        n = self.model.n_bins
        if first:
            self.gate_lo_bin = self.model.t0_bin
            self.gate_hi_bin = n - 1
            # Gate B is a valid, later gate from the start (the second quarter of
            # the post-pulse axis), so no code path ever sees a degenerate B == A.
            _, (self.gateB_lo_bin, self.gateB_hi_bin) = self._default_lifetime_gates()
            self._lifetime_init = False
        else:
            self.gate_lo_bin = min(self.gate_lo_bin, n - 1)
            self.gate_hi_bin = min(self.gate_hi_bin, n - 1)
            self.gateB_lo_bin = min(self.gateB_lo_bin, n - 1)
            self.gateB_hi_bin = min(self.gateB_hi_bin, n - 1)

        if self.w is not None:
            self.w.set_loaded(True)
            self._refit_ranges()
            self.sync_widgets_from_state()
            self._update_header()
            self._refresh_decay()
            self._refresh_image()
        note = self._folder_load_note()
        if note:
            self.statusMessage.emit(note)
        self._folder_skipped = []


    # --------------------------------------------------------- provenance / IO
    def _metadata(self) -> dict:
        import ptufile
        from .. import __version__ as chronogate_version
        c = self.model.cube
        ny, nx = self.model.intensity.shape
        return {
            "source_file": c.path.name, "source_path": str(c.path), "record_type": c.record_type,
            "resolution_ns": c.resolution_ns, "period_ns": c.period_ns, "n_bins": c.n_bins,
            "n_channels": c.n_channels, "n_frames": c.n_frames, "image_shape": [ny, nx],
            "bin_size": self.model.bin_factor, "t0_bin": self.model.t0_bin, "t0_ns": self.model.t0_ns(),
            "t0_manual": self.manual_t0_ns is not None,
            "total_photons_in_file": c.n_photons,
            "chronogate_version": chronogate_version,
            "ptufile_version": getattr(ptufile, "__version__", "unknown"),
            "numpy_version": np.__version__,
        }

    def _settings(self) -> dict:
        res = self.model.resolution_ns
        lo_ns, hi_ns = gating.gate_bounds_ns(self.gate_lo_bin, self.gate_hi_bin, res)
        blo_ns, bhi_ns = gating.gate_bounds_ns(self.gateB_lo_bin, self.gateB_hi_bin, res)
        return {
            "z_index": self.z_index, "z_file": self.stack[self.z_index].name, "channel": self.channel,
            "sum_frames": self.sum_frames, "gate_lo_bin": self.gate_lo_bin, "gate_hi_bin": self.gate_hi_bin,
            "gate_lo_ns": round(lo_ns, 4), "gate_hi_ns": round(hi_ns, 4), "threshold": self.threshold,
            "noise_floor_per_pixel": round(self.noise_floor_pp, 6),
            "noise_floor_total": round(self.noise_floor_pp * self.model.n_pixels, 4),
            "subtract_floor": self.apply_floor, "bin_size": self.bin_size, "bin_target": self.bin_target,
            "box_size": self.box_size, "smooth_bins": self.smooth_bins, "fit_curve": self.fit_curve,
            "log_scale": self.log_scale, "lock_scale": self.lock_scale, "manual_t0_ns": self.manual_t0_ns,
            "cmap": self.cmap, "mode": self.mode, "edit_target": self.edit_target,
            "gateB_lo_bin": self.gateB_lo_bin, "gateB_hi_bin": self.gateB_hi_bin,
            "gateB_lo_ns": round(blo_ns, 4), "gateB_hi_ns": round(bhi_ns, 4),
            "lifetime_cmap": self.lifetime_cmap, "rld_min_counts": self.rld_min_counts,
            "hsv_lifetime": self.hsv_lifetime, "combine": self.combine,
            "hover_probe": self.hover_probe,
            "pixel_list": self._pixel_list_settings(),
            "picks": [self._pick_recipe(p) for p in self.picks],
            "pinned_picks": [self._pick_recipe(p) for p in self.pinned_picks],
        }

    # ------------------------------------------------- persisting a selection
    def _pick_recipe(self, pick: dict) -> dict:
        """A pick as a small, JSON-safe **recipe** rather than a pixel dump.

        A phasor selection is stored as its lasso polygon -- a few vertices that
        reproduce a 160k-pixel mask exactly -- and a list group as its coordinates.
        Serialising the mask itself would put a quarter of a million booleans in a
        settings file.
        """
        kind = pick["kind"]
        if kind == "pixel":
            return {"kind": "pixel", "r": int(pick["r"]), "c": int(pick["c"])}
        if kind == "roi":
            return {"kind": "roi", **{k: int(pick[k]) for k in ("r0", "r1", "c0", "c1")}}
        if pick.get("verts") is not None:
            return {"kind": "lasso", "verts": np.asarray(pick["verts"], float).tolist(),
                    "label": pick.get("label")}
        rr, cc = np.nonzero(pick["mask"])
        if rr.size > _MAX_SAVED_PIXELS:   # no recipe and too big to list: drop it
            return {"kind": "unsaved", "n_pixels": int(rr.size)}
        return {"kind": "pixels", "coords": np.column_stack([rr, cc]).astype(int).tolist(),
                "label": pick.get("label")}

    def _pick_from_recipe(self, d: dict) -> dict | None:
        """Rebuild a pick from :meth:`_pick_recipe` against the *current* model."""
        ny, nx = self.model.intensity.shape
        kind = d.get("kind")
        if kind == "pixel":
            r, c = int(d["r"]), int(d["c"])
            if not (0 <= r < ny and 0 <= c < nx):
                return None
            return {"kind": "pixel", "r": r, "c": c, "label": f"px({r},{c})"}
        if kind == "roi":
            r0, r1, c0, c1 = (int(d[k]) for k in ("r0", "r1", "c0", "c1"))
            if r1 > ny or c1 > nx or r1 <= r0 or c1 <= c0:
                return None
            return {"kind": "roi", "r0": r0, "r1": r1, "c0": c0, "c1": c1,
                    "label": f"roi[{r0}:{r1},{c0}:{c1}]"}
        if kind == "lasso":
            verts = np.asarray(d.get("verts", []), dtype=float)
            if verts.shape[0] < 3:
                return None
            mask = self._lasso_mask(verts)
            if not mask.any():
                return None
            return {"kind": "mask", "mask": mask, "verts": verts,
                    "label": d.get("label") or f"phasor sel ({int(mask.sum()):,} px)"}
        if kind == "pixels":
            coords = np.asarray(d.get("coords", []), dtype=int)
            if coords.size == 0:
                return None
            keep = ((coords[:, 0] >= 0) & (coords[:, 0] < ny)
                    & (coords[:, 1] >= 0) & (coords[:, 1] < nx))
            coords = coords[keep]
            if coords.size == 0:
                return None
            mask = np.zeros((ny, nx), dtype=bool)
            mask[coords[:, 0], coords[:, 1]] = True
            return {"kind": "mask", "mask": mask,
                    "label": d.get("label") or f"list sel ({int(mask.sum()):,} px)"}
        return None

    def _pixel_list_settings(self) -> dict:
        if self.w is None:
            return {}
        p = self.w.pixels
        return {
            "open": not self.w.pixel_dock.isHidden(),
            "metric": p.current_metric(), "descending": p.desc.isChecked(),
            "limit": p.limit.value(), "vmin": p.fmin.value(), "vmax": p.fmax.value(),
        }

    def _apply_pixel_list_settings(self, s: dict) -> None:
        if self.w is None or not s:
            return
        p = self.w.pixels
        key = s.get("metric")
        keys = [m.key for m in metrics.metrics()]
        with _blocked(p.metric, p.desc, p.limit, p.fmin, p.fmax):
            if key in keys:
                p.metric.setCurrentIndex(keys.index(key))
                self._pixel_key = key      # the saved bounds are for THIS metric
            p.desc.setChecked(bool(s.get("descending", p.desc.isChecked())))
            p.limit.setValue(int(s.get("limit", p.limit.value())))
            if "vmin" in s and "vmax" in s:
                p.fmin.setValue(float(s["vmin"]))
                p.fmax.setValue(float(s["vmax"]))
        self.w.pixel_dock.setVisible(bool(s.get("open", False)))

    def _apply_pick_settings(self, s: dict) -> None:
        """Restore the saved picks, dropping any that no longer fit the model."""
        self.pinned_picks = [p for p in
                             (self._pick_from_recipe(d) for d in s.get("pinned_picks", []))
                             if p is not None]
        self.picks = [p for p in (self._pick_from_recipe(d) for d in s.get("picks", []))
                      if p is not None][:1]
        self.select_mask = None
        self._lasso_verts = None
        for pick in self.picks:
            if pick["kind"] == "mask":
                self.select_mask = pick["mask"]
                self._lasso_verts = pick.get("verts")

    # ------------------------------------------------------- selection export
    def _pick_pixel_mask(self, pick: dict) -> np.ndarray:
        """Every pick kind as a boolean (Y, X) mask."""
        if pick["kind"] == "mask":
            return np.asarray(pick["mask"], dtype=bool)
        r0, r1, c0, c1, _ = self._pick_region(pick)
        m = np.zeros(self.model.intensity.shape, dtype=bool)
        m[r0:r1, c0:c1] = True
        return m

    def _selection_payload(self):
        """Package the current picks so they can leave the program (see
        :class:`chronogate.export.Selection`). ``None`` when nothing is selected."""
        picks = self._shown_picks()
        if self.model is None or not picks:
            return None
        ctx = self._metric_ctx()
        keys = [m.key for m in metrics.metrics()]
        columns = {k: metrics.get(k).compute(ctx) for k in keys}
        n_pinned = len(self.pinned_picks)
        label_map = np.zeros(self.model.intensity.shape, dtype=np.uint16)
        labels, decays, blocks = [], [], []
        for i, pick in enumerate(picks):
            tag = ("pinned " if i < n_pinned else "") + self._pick_tag(pick)
            mask = self._pick_pixel_mask(pick)
            rr, cc = np.nonzero(mask)
            labels.append(tag)
            label_map[mask] = i + 1        # later picks win where they overlap
            decays.append(np.asarray(self._pick_decay(pick), dtype=float))
            blocks.append(np.column_stack(
                [rr, cc] + [columns[k][rr, cc] for k in keys]).astype(float))
        return export_mod.Selection(
            labels=labels, label_map=label_map, time_ns=self.model.cube.time_axis_ns,
            decays=decays, pixel_columns=["row", "col"] + keys, pixel_blocks=blocks)

    def _current_image_for_export(self):
        floor = self._floor_per_pixel() if self.apply_floor else 0.0
        gated = self.model.gate(self.gate_lo_bin, self.gate_hi_bin, floor_per_bin=floor)
        display = gated.astype(float)
        if self.threshold > 0:
            display[self.model.intensity < self.threshold] = np.nan
        vmin, vmax = self._clim_from(display[np.isfinite(display)], 1, 99)
        return display, vmin, vmax

    def export(self, out_dir=None) -> dict:
        """Write the current view's export artefacts; returns the path map."""
        if self.model is None:
            return {}
        out_dir = Path(out_dir) if out_dir else self.model.cube.path.parent / "chronogate_exports"
        stem = self.model.cube.path.stem
        time_ns = self.model.cube.time_axis_ns
        # Whatever is picked (a pixel, an ROI, a phasor cluster, a list group) leaves
        # with the export -- otherwise you can select a population and never get it out.
        selection = self._selection_payload()
        if self.mode == "lifetime":
            tau, rl = self._compute_lifetime_map()
            vmin, vmax = self._clim_from(tau[np.isfinite(tau)], 2, 98, floor_gap=1e-3)
            (alo, ahi), (blo, bhi) = rl["early"], rl["late"]
            base = f"{stem}_ch{self.channel}_RLD_A{alo}-{ahi}_B{blo}-{bhi}"
            paths = export_all(out_dir, base, gated_image=tau, time_ns=time_ns, decay=self.model.decay,
                               cmap=self.lifetime_cmap, vmin=vmin, vmax=vmax, metadata=self._metadata(),
                               settings=self._settings(), colorbar_label="apparent lifetime (ns)",
                               title=f"{self.model.cube.path.name} | RLD τ  (Δt {rl['dt_ns']:.2f} ns)",
                               selection=selection)
        else:
            base = f"{stem}_ch{self.channel}_gate{self.gate_lo_bin}-{self.gate_hi_bin}"
            display, vmin, vmax = self._current_image_for_export()
            paths = export_all(out_dir, base, gated_image=display, time_ns=time_ns, decay=self.model.decay,
                               cmap=self.cmap, vmin=vmin, vmax=vmax, metadata=self._metadata(),
                               settings=self._settings(), selection=selection)
        return paths

    def batch_export(self, out_dir=None) -> int:
        """Apply the current gate/floor/threshold/mode to *every* plane of the
        stack and export each (TIFF/PNG/CSV/provenance). Returns the plane count."""
        if self.model is None or not self.stack:
            return 0
        out_dir = Path(out_dir) if out_dir else self.model.cube.path.parent / "chronogate_exports" / "batch"
        n, saved_z, saved_model = len(self.stack), self.z_index, self.model
        self._set_busy(True, f"batch export ({n} planes)")
        try:
            for i in range(n):
                self.z_index = i
                self.model = self._load_current()  # cache-aware
                self._apply_manual_t0()
                self.export(out_dir)
                if self.w is not None:
                    self.w.set_progress(i + 1, n)
                    QApplication.processEvents()   # repaint only; controls are disabled
        finally:
            self.z_index, self.model = saved_z, saved_model
            self._set_busy(False)
        if self.w is not None:
            self._update_header(); self._refresh_decay(); self._refresh_image()
        self.statusMessage.emit(f"Batch: exported {n} plane(s) → {out_dir}")
        return n

    def _on_batch_export(self) -> None:
        if self.model is None:
            return
        if len(self.stack) <= 1:
            self.statusMessage.emit("Batch export needs a multi-plane stack; use Export for a single file.")
            return
        directory = QFileDialog.getExistingDirectory(
            self.w, "Choose an output folder for the batch export", self._dialog_dir(),
            QFileDialog.Option.ShowDirsOnly | _DLG_OPT)
        if directory:
            self.batch_export(directory)

    def _on_export(self) -> None:
        paths = self.export()
        if not paths:
            return
        out_dir = Path(next(iter(paths.values()))).parent
        msg = f"Exported → {out_dir}  ({', '.join(Path(p).name for p in paths.values())})"
        print(msg)
        self.statusMessage.emit(msg)

    def _on_save(self) -> None:
        if self.model is None:
            return
        path, _ = QFileDialog.getSaveFileName(
            self.w, "Save settings", str(self.model.cube.path.parent / f"{self.model.cube.path.stem}_settings.json"),
            "JSON (*.json)", "", _DLG_OPT)
        if not path:
            return
        save_settings(path, self._settings(), self._metadata())
        self.statusMessage.emit(f"Saved settings → {path}")
        print(f"Saved settings → {path}")

    def _on_load(self) -> None:
        if self.model is None:
            return
        path, _ = QFileDialog.getOpenFileName(
            self.w, "Load settings", self._dialog_dir(), "JSON (*.json);;All files (*)", "", _DLG_OPT)
        if not path:
            return
        self.apply_settings(load_settings(path))
        self.statusMessage.emit(f"Loaded settings ← {path}")
        print(f"Loaded settings ← {path}")

    def apply_settings(self, s: dict) -> None:
        self.bin_size = int(s.get("bin_size", self.bin_size))
        self.bin_target = int(s.get("bin_target", self.bin_target))

        reload_needed = False
        if "channel" in s and s["channel"] != self.channel and s["channel"] < self.model.cube.n_channels:
            self.channel = int(s["channel"])
            reload_needed = True
        if "z_index" in s and 0 <= s["z_index"] < len(self.stack) and s["z_index"] != self.z_index:
            self.z_index = int(s["z_index"])
            reload_needed = True
        if reload_needed:
            self._reload_model_busy()
        elif self.model.bin_factor != self.bin_size:
            self.model = gating.GatingModel(self.model.cube, bin_factor=self.bin_size)

        self.gate_lo_bin = int(s.get("gate_lo_bin", self.gate_lo_bin))
        self.gate_hi_bin = int(s.get("gate_hi_bin", self.gate_hi_bin))
        self.threshold = int(s.get("threshold", self.threshold))
        if "noise_floor_per_pixel" in s:
            self.noise_floor_pp = float(s["noise_floor_per_pixel"])
        elif "noise_floor_total" in s:  # older settings stored the summed floor
            self.noise_floor_pp = float(s["noise_floor_total"]) / max(1, self.model.n_pixels)
        self.apply_floor = bool(s.get("subtract_floor", self.apply_floor))
        self.box_size = int(s.get("box_size", self.box_size))
        self.smooth_bins = int(s.get("smooth_bins", self.smooth_bins))
        self.fit_curve = bool(s.get("fit_curve", self.fit_curve))
        self.log_scale = bool(s.get("log_scale", self.log_scale))
        self.lock_scale = bool(s.get("lock_scale", self.lock_scale))
        mt0 = s.get("manual_t0_ns", None)
        self.manual_t0_ns = float(mt0) if mt0 is not None else None
        self._apply_manual_t0()
        self.cmap = s.get("cmap", self.cmap)

        if "gateB_lo_bin" in s and "gateB_hi_bin" in s:
            self.gateB_lo_bin = int(s["gateB_lo_bin"])
            self.gateB_hi_bin = int(s["gateB_hi_bin"])
            self._lifetime_init = True
        self.lifetime_cmap = s.get("lifetime_cmap", self.lifetime_cmap)
        self.rld_min_counts = float(s.get("rld_min_counts", self.rld_min_counts))
        self.hsv_lifetime = bool(s.get("hsv_lifetime", self.hsv_lifetime))
        self.combine = s.get("combine", self.combine)
        self.hover_probe = bool(s.get("hover_probe", self.hover_probe))
        mode = s.get("mode", self.mode)
        mode = mode if mode in ("lifetime", "phasor") else "intensity"
        self.edit_target = "B" if (mode == "lifetime" and s.get("edit_target") == "B") else "A"

        # The selection last, so a lasso is re-cut against the restored threshold,
        # gate and binning -- exactly the state it was drawn under.
        self._end_hover(redraw=False)
        self._apply_pick_settings(s)

        self._refit_ranges()
        self.sync_widgets_from_state()
        self._apply_pixel_list_settings(s.get("pixel_list", {}))
        self._update_header()
        self._enter_mode(mode)
