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
from contextlib import contextmanager
from pathlib import Path

import numpy as np
import matplotlib
from matplotlib.widgets import SpanSelector
from matplotlib.patches import Rectangle

from PySide6.QtCore import QObject, Qt, Signal, QSignalBlocker
from PySide6.QtWidgets import QApplication, QFileDialog

from .. import gating
from ..loader import find_stack, load_ptu
from ..export import export_all, load_settings, save_settings
from . import theme

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

        # Analysis state (the authoritative values; widgets render these).
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
        self._lifetime_init = False
        self.picks: list[dict] = []
        self._pick_lines: list = []
        self._gate_fills: list = []
        self._gate_bands: list = []
        self._picks_ymax = 1.0
        self._press_xy = None
        self._press_data = None

        # Filled in when a file is loaded (see load_path).
        self.model = None
        self.noise_floor_pp = 0.0   # noise floor in counts/bin per pixel
        self.gate_lo_bin = 0
        self.gate_hi_bin = 0
        self.gateB_lo_bin = 0
        self.gateB_hi_bin = 0

        self._build_artists()

    # ----------------------------------------------------------------- loading
    def _load_current(self) -> gating.GatingModel:
        # Decode synchronously (no QProgressDialog/processEvents): re-entering the
        # Qt event loop mid-decode is a known crash source, especially on macOS
        # under Rosetta. A busy cursor is shown by the callers instead.
        cube = load_ptu(self.stack[self.z_index], channel=self.channel,
                        sum_frames=self.sum_frames)
        print(cube.summary())
        return gating.GatingModel(cube, bin_factor=self.bin_size)

    def _floor_per_pixel(self) -> float:
        """The floor actually subtracted from each pixel (counts/bin per pixel)."""
        return self.noise_floor_pp

    def _floor_slider_range(self) -> tuple[int, int]:
        """Slider bounds for the (per-pixel) noise floor: 0 up to the brightest
        single-pixel bin, so the floor can be pushed high enough to zero even the
        brightest pixel -- not just the average level."""
        return 0, self.model.peak_counts_per_bin()

    # --------------------------------------------------------------- artists
    def _build_artists(self) -> None:
        ax = self.dc.ax
        (self.decay_line,) = ax.plot([], [], color=theme.ACCENT, drawstyle="steps-post",
                                     lw=1.4, label="_nolegend_")
        self.t0_line = ax.axvline(0.0, color=theme.MUTED, ls="--", lw=1, label="_nolegend_")
        self.floor_line = ax.axhline(0.0, color=theme.FLOOR, ls=":", lw=1.4,
                                     visible=False, label="_nolegend_")

        # A placeholder image until a file is loaded; load_path sets real data.
        self.im = self.ic.ax.imshow(np.zeros((2, 2)), cmap=self._image_cmap("intensity"),
                                    interpolation="nearest", origin="upper")
        self.cbar = self.ic.fig.colorbar(self.im, ax=self.ic.ax, fraction=0.046, pad=0.02,
                                         label="photons in gate")
        self.cbar.outline.set_edgecolor(theme.BORDER_HI)

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
        w.lifetime.radio_a.toggled.connect(self._on_edit_radio)
        w.lifetime.min_cts.valueChanged.connect(self._on_min_counts)
        w.lifetime.cmap_life.currentTextChanged.connect(self._on_lifetime_cmap)
        w.picks.avg.valueChanged.connect(self._on_box_size)
        w.picks.smooth.valueChanged.connect(self._on_smooth)
        w.picks.fit.toggled.connect(self._on_fit_curve)
        w.picks.btn_clear.clicked.connect(self._clear_picks)
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
        w.gate.spin_lo.setRange(0.0, self.model.cube.period_ns if np.isfinite(self.model.cube.period_ns) else 1e6)
        w.gate.spin_hi.setRange(0.0, self.model.cube.period_ns if np.isfinite(self.model.cube.period_ns) else 1e6)
        w.filep.z.setRange(0, max(0, len(self.stack) - 1))
        w.filep.z.setEnabled(len(self.stack) > 1)
        with _blocked(w.filep.channel):
            w.filep.channel.clear()
            w.filep.channel.addItems([str(c) for c in range(self.model.cube.n_channels)])
            w.filep.channel.setCurrentIndex(self.channel)
        w.filep.channel.setEnabled(self.model.cube.n_channels > 1)

    def sync_widgets_from_state(self) -> None:
        """Push the authoritative state into every widget (signals blocked)."""
        w = self.w
        res = self.model.resolution_ns
        lo_ns, hi_ns = gating.gate_bounds_ns(*self._get_gate(self.edit_target), res)
        widgets = [w.gate.spin_lo, w.gate.spin_hi, w.display.thr, w.display.floor,
                   w.display.cmap, w.lifetime.radio_a, w.lifetime.radio_b,
                   w.lifetime.min_cts, w.lifetime.cmap_life, w.picks.avg, w.picks.smooth, w.picks.fit,
                   w.binning.bin, w.binning.target, w.filep.z, w.filep.channel,
                   w.act_intensity, w.act_lifetime, w.act_log, w.act_floor]
        with _blocked(*widgets):
            w.gate.spin_lo.setValue(lo_ns)
            w.gate.spin_hi.setValue(hi_ns)
            w.display.thr.setValue(self.threshold)
            w.display.floor.setValue(min(self.noise_floor_pp, w.display.floor.maximum()))
            w.display.cmap.setCurrentText(self.cmap)
            w.lifetime.radio_a.setChecked(self.edit_target == "A")
            w.lifetime.radio_b.setChecked(self.edit_target == "B")
            w.lifetime.min_cts.setValue(int(self.rld_min_counts))
            w.lifetime.cmap_life.setCurrentText(self.lifetime_cmap)
            w.picks.avg.setValue(self.box_size)
            w.picks.smooth.setValue(self.smooth_bins)
            w.picks.fit.setChecked(self.fit_curve)
            w.binning.bin.setValue(self.bin_size)
            w.binning.target.setValue(self.bin_target)
            w.filep.z.setValue(min(self.z_index, w.filep.z.maximum()))
            w.filep.channel.setCurrentIndex(self.channel)
            w.act_intensity.setChecked(self.mode == "intensity")
            w.act_lifetime.setChecked(self.mode == "lifetime")
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
            if self.picks:
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
        for i, pick in enumerate(self.picks):
            if pick["kind"] == "pixel":
                r, c, b = pick["r"], pick["c"], max(1, self.box_size)
                half = b // 2
                r0, r1 = max(0, r - half), min(ny, r + half + 1)
                c0, c1 = max(0, c - half), min(nx, c + half + 1)
                tag = f"px({r},{c})" + (f"·{b}²" if b > 1 else "")
            else:
                r0, r1, c0, c1 = pick["r0"], pick["r1"], pick["c0"], pick["c1"]
                tag = f"roi[{r0}:{r1},{c0}:{c1}]"
            raw = self.model.pixel_decay(r0, r1, c0, c1)
            shown = self._smooth(raw, self.smooth_bins)
            seg = raw[self.gate_lo_bin: self.gate_hi_bin + 1] - floor_pp
            in_gate = float(np.clip(seg, 0, None).sum())
            color = _PICK_COLORS[i % len(_PICK_COLORS)]

            # Optional mono-exponential fit overlay (a smooth visual guide).
            fit = (gating.fit_mono_exponential(x, raw, self.model.t0_ns(), floor_pp)
                   if self.fit_curve else None)
            tau_note = f"  τ≈{fit[1]:.2f} ns" if fit else ""
            # When fitting, fade the jagged raw steps so the smooth curve reads clearly.
            (ln,) = self.dc.ax.plot(x, shown, color=color, lw=1.2, drawstyle="steps-post",
                                    alpha=0.35 if fit else 1.0,
                                    label=f"{tag}: {in_gate:.1f}/px in gate{tau_note}")
            self._pick_lines.append(ln)
            self._picks_ymax = max(self._picks_ymax, float(shown.max()))
            if fit is not None:
                yfit = gating.mono_exponential_curve(x, self.model.t0_ns(), fit[0], fit[1], floor_pp)
                (fl,) = self.dc.ax.plot(x, yfit, color=color, lw=1.8, ls="--",
                                        label="_nolegend_")
                self._pick_lines.append(fl)
                self._picks_ymax = max(self._picks_ymax, float(np.nanmax(yfit)))
            list_items.append((f"{tag} — {in_gate:.1f}/px in gate{tau_note}", color))
        if self.w is not None:
            self.w.picks.set_items(list_items)

    # ---------------------------------------------------------------- refresh
    def _refresh_decay(self) -> None:
        if self.model is None:
            return
        res = self.model.resolution_ns
        x = self.model.cube.time_axis_ns
        ax = self.dc.ax
        self.decay_line.set_data(x, self.model.decay.astype(float))
        self.t0_line.set_xdata([self.model.t0_ns(), self.model.t0_ns()])
        ax.set_xlim(0, x[-1] + res)

        picks_mode = bool(self.picks)
        self.decay_line.set_visible(not picks_mode)
        self._redraw_pick_lines()

        if picks_mode:
            ax.set_ylabel("photons / pixel (mean)")
            floor_y = self._floor_per_pixel()
            data_max = self._picks_ymax
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
        if picks_mode:
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

    def _refresh_image(self) -> None:
        if self.model is None:
            return
        if self.mode == "lifetime":
            self._refresh_lifetime_image()
        else:
            self._refresh_intensity_image()

    def _refresh_intensity_image(self) -> None:
        res = self.model.resolution_ns
        # Per pixel: integrate that pixel's decay over the gate, minus the floor.
        floor = self._floor_per_pixel() if self.apply_floor else 0.0
        gated = self.model.gate(self.gate_lo_bin, self.gate_hi_bin, floor_per_bin=floor)
        lo_ns, hi_ns = gating.gate_bounds_ns(self.gate_lo_bin, self.gate_hi_bin, res)
        t0 = self.model.t0_ns()
        in_gate = int(gated.sum())
        unit = "photons" if self.bin_size == 1 else f"cts ·{self.bin_size}×{self.bin_size}"
        title = (f"gate {lo_ns:.2f}–{hi_ns:.2f} ns  (t0{lo_ns - t0:+.1f}…{hi_ns - t0:+.1f})\n"
                 f"{in_gate:,} {unit} in gate")

        display = gated.astype(float)
        if self.threshold > 0:
            display[self.model.intensity < self.threshold] = np.nan
        vmin, vmax = self._clim_from(display[np.isfinite(display)], 1, 99)
        self.im.set_cmap(self._image_cmap("intensity"))
        self.im.set_data(display)
        self.im.set_clim(vmin, vmax)
        self.cbar.set_label("photons in gate")
        self.ic.ax.set_title(title, fontsize=9)
        self.ic.draw_idle()
        self._update_stats(gated, lo_ns, hi_ns)

    def _update_stats(self, gated, lo_ns: float, hi_ns: float) -> None:
        """Push gated-image statistics into the Stats panel (intensity mode)."""
        if self.w is None:
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

    def _compute_lifetime_map(self):
        floor = self._floor_per_pixel() if self.apply_floor else 0.0
        rl = self.model.rapid_lifetime(self._get_gate("A"), self._get_gate("B"),
                                       floor_per_bin=floor, min_counts=self.rld_min_counts)
        tau = np.asarray(rl["tau"], dtype=float).copy()
        if self.threshold > 0:
            tau[self.model.intensity < self.threshold] = np.nan
        return tau, rl

    def _refresh_lifetime_image(self) -> None:
        tau, rl = self._compute_lifetime_map()
        finite = tau[np.isfinite(tau)]
        if finite.size == 0:
            self.statusMessage.emit(
                f"No pixels reached N ≥ {self.rld_min_counts:.0f} photons in BOTH gates — "
                f"increase binning (Auto) or lower 'min cts' to get a lifetime map.")
        vmin, vmax = self._clim_from(finite, 2, 98, floor_gap=1e-3)
        self.im.set_cmap(self._image_cmap("lifetime"))
        self.im.set_data(tau)
        self.im.set_clim(vmin, vmax)
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
        if self.picks:
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

    def _init_lifetime_gates(self) -> None:
        n, t0 = self.model.n_bins, self.model.t0_bin
        width = max(2, (n - 1 - t0) // 4)
        self.gate_lo_bin = min(t0, n - 1)
        self.gate_hi_bin = min(t0 + width - 1, n - 1)
        self.gateB_lo_bin = min(t0 + width, n - 1)
        self.gateB_hi_bin = min(t0 + 2 * width - 1, n - 1)

    def _enter_mode(self, mode: str) -> None:
        if self.model is None:
            return
        self.mode = "lifetime" if mode == "lifetime" else "intensity"
        if self.mode == "lifetime" and not self._lifetime_init:
            self._init_lifetime_gates()
            self._lifetime_init = True
        if self.mode == "intensity":
            self.edit_target = "A"
        if self.w is not None:
            self.w.set_lifetime_enabled(self.mode == "lifetime")
            with _blocked(self.w.lifetime.radio_a, self.w.lifetime.radio_b,
                          self.w.act_intensity, self.w.act_lifetime):
                self.w.lifetime.radio_a.setChecked(self.edit_target == "A")
                self.w.lifetime.radio_b.setChecked(self.edit_target == "B")
                self.w.act_intensity.setChecked(self.mode == "intensity")
                self.w.act_lifetime.setChecked(self.mode == "lifetime")
            self.w.gate.set_active_label(self.edit_target, self.mode)
        if self.mode == "lifetime":
            self.statusMessage.emit("Lifetime mode: two-gate RLD  τ = Δt / ln(N_A/N_B). "
                                    "Keep gates equal width; edit A/B with the radio or the ns boxes.")
        else:
            self.statusMessage.emit("Intensity mode: single gate, photons summed per pixel.")
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
        # A new pick *replaces* the current one (single decay at a time), rather
        # than accumulating a cumulative overlay.
        self.picks = [pick]
        self._refresh_decay()
        self.statusMessage.emit(f"Showing decay for {pick['label']}.")

    def _clear_picks(self) -> None:
        self.picks = []
        self._refresh_decay()
        self.statusMessage.emit("Picks cleared; showing total decay.")

    # ----------------------------------------------------- other event handlers
    def _on_threshold(self, val) -> None:
        self.threshold = int(val)
        self._refresh_image()

    def _on_noise_floor(self, val) -> None:
        self.noise_floor_pp = float(val)
        self._refresh_decay()
        self._refresh_image()

    def _on_cmap(self, name) -> None:
        self.cmap = name
        if self.mode == "intensity":
            self._refresh_image()

    def _on_box_size(self, val) -> None:
        self.box_size = max(1, int(val))
        if self.picks:
            self._refresh_decay()

    def _on_smooth(self, val) -> None:
        self.smooth_bins = max(1, int(val))
        if self.picks:
            self._refresh_decay()

    def _on_fit_curve(self, checked) -> None:
        self.fit_curve = bool(checked)
        if self.picks:
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
        # Binning changes the per-pixel scale, so reset the floor to its default.
        self.noise_floor_pp = self.model.auto_noise_floor_pp()
        self._refit_ranges()
        with _blocked(self.w.display.floor):
            self.w.display.floor.setValue(min(self.noise_floor_pp, self.w.display.floor.maximum()))
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

    def _on_channel(self, idx) -> None:
        if idx < 0:
            return
        self.channel = int(idx)
        self._reload_model_busy()
        self._update_header()
        self._refresh_decay()
        self._refresh_image()

    def _on_zslice(self, val) -> None:
        self.z_index = int(val)
        self._reload_model_busy()
        self._refit_ranges()
        self._update_header()
        self._refresh_decay()
        self._refresh_image()

    def _reload_model_busy(self) -> None:
        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            self.model = self._load_current()
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
        """Load the .ptu stack found under ``directory`` (z-series across files)."""
        directory = Path(directory)
        ptus = sorted(directory.rglob("*.ptu"))
        if not ptus:
            self.statusMessage.emit(f"No .ptu files found under {directory.name}.")
            return
        # find_stack (in load_path) groups the numbered siblings into the z-stack.
        self.load_path(ptus[0])
        if len(self.stack) > 1:
            self.statusMessage.emit(
                f"Loaded a {len(self.stack)}-plane stack from {directory.name} — "
                f"step planes with the z-slice slider or PgUp/PgDn.")

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

        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            self.stack = find_stack(path)
            self.z_index = next((i for i, p in enumerate(self.stack) if p == path), 0)
            try:
                model = self._load_current()
            except Exception:
                self.channel = 0  # the new file may have fewer detector channels
                model = self._load_current()
        except Exception as exc:  # noqa: BLE001 - report cleanly, keep current view
            QApplication.restoreOverrideCursor()
            self.statusMessage.emit(f"Could not load {path.name}: {exc}")
            return
        QApplication.restoreOverrideCursor()

        first = self.model is None
        self.model = model
        self.picks = []
        self.threshold = 0
        self.noise_floor_pp = model.auto_noise_floor_pp()
        if first:
            self.gate_lo_bin = model.t0_bin
            self.gate_hi_bin = model.n_bins - 1
            self.gateB_lo_bin = self.gate_lo_bin
            self.gateB_hi_bin = self.gate_hi_bin
            self._lifetime_init = False
        else:
            self.gate_lo_bin = min(self.gate_lo_bin, model.n_bins - 1)
            self.gate_hi_bin = min(self.gate_hi_bin, model.n_bins - 1)
            self.gateB_lo_bin = min(self.gateB_lo_bin, model.n_bins - 1)
            self.gateB_hi_bin = min(self.gateB_hi_bin, model.n_bins - 1)

        if self.w is not None:
            self.w.set_loaded(True)
            self._refit_ranges()
            self.sync_widgets_from_state()
            self._update_header()
            self._refresh_decay()
            self._refresh_image()


    # --------------------------------------------------------- provenance / IO
    def _metadata(self) -> dict:
        c = self.model.cube
        ny, nx = self.model.intensity.shape
        return {
            "source_file": c.path.name, "source_path": str(c.path), "record_type": c.record_type,
            "resolution_ns": c.resolution_ns, "period_ns": c.period_ns, "n_bins": c.n_bins,
            "n_channels": c.n_channels, "n_frames": c.n_frames, "image_shape": [ny, nx],
            "bin_size": self.model.bin_factor, "t0_bin": self.model.t0_bin, "t0_ns": self.model.t0_ns(),
            "total_photons_in_file": c.n_photons,
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
            "log_scale": self.log_scale,
            "cmap": self.cmap, "mode": self.mode, "edit_target": self.edit_target,
            "gateB_lo_bin": self.gateB_lo_bin, "gateB_hi_bin": self.gateB_hi_bin,
            "gateB_lo_ns": round(blo_ns, 4), "gateB_hi_ns": round(bhi_ns, 4),
            "lifetime_cmap": self.lifetime_cmap, "rld_min_counts": self.rld_min_counts,
        }

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
        if self.mode == "lifetime":
            tau, rl = self._compute_lifetime_map()
            vmin, vmax = self._clim_from(tau[np.isfinite(tau)], 2, 98, floor_gap=1e-3)
            (alo, ahi), (blo, bhi) = rl["early"], rl["late"]
            base = f"{stem}_ch{self.channel}_RLD_A{alo}-{ahi}_B{blo}-{bhi}"
            paths = export_all(out_dir, base, gated_image=tau, time_ns=time_ns, decay=self.model.decay,
                               cmap=self.lifetime_cmap, vmin=vmin, vmax=vmax, metadata=self._metadata(),
                               settings=self._settings(), colorbar_label="apparent lifetime (ns)",
                               title=f"{self.model.cube.path.name} | RLD τ  (Δt {rl['dt_ns']:.2f} ns)")
        else:
            base = f"{stem}_ch{self.channel}_gate{self.gate_lo_bin}-{self.gate_hi_bin}"
            display, vmin, vmax = self._current_image_for_export()
            paths = export_all(out_dir, base, gated_image=display, time_ns=time_ns, decay=self.model.decay,
                               cmap=self.cmap, vmin=vmin, vmax=vmax, metadata=self._metadata(),
                               settings=self._settings())
        return paths

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
        self.cmap = s.get("cmap", self.cmap)

        if "gateB_lo_bin" in s and "gateB_hi_bin" in s:
            self.gateB_lo_bin = int(s["gateB_lo_bin"])
            self.gateB_hi_bin = int(s["gateB_hi_bin"])
            self._lifetime_init = True
        self.lifetime_cmap = s.get("lifetime_cmap", self.lifetime_cmap)
        self.rld_min_counts = float(s.get("rld_min_counts", self.rld_min_counts))
        mode = "lifetime" if s.get("mode", self.mode) == "lifetime" else "intensity"
        self.edit_target = "B" if (mode == "lifetime" and s.get("edit_target") == "B") else "A"

        self._refit_ranges()
        self.sync_widgets_from_state()
        self._update_header()
        self._enter_mode(mode)
