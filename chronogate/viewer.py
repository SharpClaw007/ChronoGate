"""The interactive ChronoGate window (matplotlib).

Two linked panels:

* LEFT  -- the decay curve(s) vs microtime in ns, with a draggable, resizable
  shaded gate (a :class:`~matplotlib.widgets.SpanSelector`), a log/linear y
  toggle, a dashed t0 (pulse) marker, and a dotted **noise-floor** line.
  By default it shows the total (all-pixel) decay; once you pick pixels/regions
  it overlays their per-pixel decays for comparison.
* RIGHT -- the gated intensity image (sum of photons inside the gate, per pixel)
  with a colorbar. It redraws live as you drag the gate.

Supporting controls (kept secondary to the gate): a dim-pixel intensity
threshold, an adjustable **noise floor** subtracted from the gated intensity,
per-pixel/ROI decay picking, channel and z-slice selection, and
export / save-settings / load-settings buttons.

Two coordinate notes that keep the maths honest:
* The noise floor is stored in *total-decay* counts/bin (so its line sits on the
  pre-pulse pedestal of the total decay). The per-pixel floor used for image
  subtraction is ``floor_total / n_pixels``.
* Picked decays are shown as the *mean per pixel* (counts/bin), so a single
  pixel and a multi-pixel ROI share one y-scale and one per-pixel floor line.
"""

from __future__ import annotations

import warnings
from pathlib import Path

import numpy as np
import matplotlib
import matplotlib.pyplot as plt
from matplotlib.widgets import (
    Button, CheckButtons, RadioButtons, RectangleSelector, SpanSelector, Slider, TextBox,
)

from . import gating
from .loader import find_stack, load_ptu
from .export import export_all, load_settings, save_settings

_CMAP = "viridis"
# Distinct colours for overlaid per-pixel/ROI decays (cycled).
_PICK_COLORS = [
    "tab:red", "tab:green", "tab:purple", "tab:brown", "tab:pink",
    "tab:olive", "tab:cyan", "tab:orange", "magenta", "lime",
]
_MAX_PICKS = 10  # cap overlays; oldest drops off (FIFO) beyond this


class GatingViewer:
    """Owns the figure, widgets and current analysis state."""

    def __init__(self, path: str | Path, channel: int = 0, sum_frames: bool = True):
        # The file may be one plane of a numbered z-stack; find its siblings so
        # we can offer a z-slider. Position the slider on the file requested.
        self.stack = find_stack(path)
        self.z_index = next((i for i, p in enumerate(self.stack) if p == Path(path)), 0)
        self.channel = channel
        self.sum_frames = sum_frames

        # Analysis state (mutated by widgets).
        self.log_scale = True          # decays span orders of magnitude -> log default
        self.apply_floor = True        # subtract the noise floor from the gated image
        self.threshold = 0
        self.box_size = 1              # N x N spatial averaging box for single-pixel picks
        self.smooth_bins = 5           # time-axis smoothing of per-pixel decays (display only)
        self.bin_size = 1              # spatial binning factor applied to the whole cube
        self.bin_target = 100          # photons/pixel the Auto-bin button aims for
        self.cmap = _CMAP
        self.picks: list[dict] = []    # accumulated per-pixel / ROI decay selections
        self._pick_lines: list = []    # the Line2D objects currently drawn for picks
        self._gate_fills: list = []    # PolyCollections shading the integrated area
        self._picks_ymax = 1.0
        self._press_xy = None          # for click-vs-drag detection on the image
        # Guard against feedback loops when we programmatically set widgets
        # (TextBox.set_val fires the submit callback).
        self._syncing = False

        self.model = self._load_current()
        # Noise floor (total-decay counts/bin): default to the auto pre-pulse
        # background level, which sits right on the decay's pedestal.
        self.noise_floor_total = self.model.auto_noise_floor_total()
        # Default gate: from the pulse (t0) to the end of the decay window.
        self.gate_lo_bin = self.model.t0_bin
        self.gate_hi_bin = self.model.n_bins - 1

        self._build_figure()
        self._update_filename_label()
        self._refresh_decay()
        self._refresh_image()

    # ----------------------------------------------------------------- loading
    def _load_current(self) -> gating.GatingModel:
        cube = load_ptu(
            self.stack[self.z_index], channel=self.channel, sum_frames=self.sum_frames
        )
        print(cube.summary())
        return gating.GatingModel(cube, bin_factor=self.bin_size)

    def _floor_per_pixel(self) -> float:
        """Noise floor in per-pixel counts/bin (for image subtraction)."""
        return self.noise_floor_total / max(1, self.model.n_pixels)

    # --------------------------------------------------------------- figure UI
    def _build_figure(self) -> None:
        self.fig = plt.figure(figsize=(13, 7.5))
        self.fig.canvas.manager.set_window_title("ChronoGate")

        # Two main panels (figure coordinates), with controls below.
        self.ax_decay = self.fig.add_axes([0.06, 0.55, 0.42, 0.40])
        self.ax_img = self.fig.add_axes([0.55, 0.55, 0.30, 0.40])
        self.cax = self.fig.add_axes([0.865, 0.55, 0.013, 0.40])

        self.ax_decay.set_xlabel("microtime (ns)")
        self.ax_decay.set_ylabel("photons")

        # Total decay line, t0 marker, and the noise-floor line.
        (self.decay_line,) = self.ax_decay.plot(
            [], [], color="#1f77b4", drawstyle="steps-post", label="_nolegend_"
        )
        self.t0_line = self.ax_decay.axvline(0.0, color="0.4", ls="--", lw=1, label="_nolegend_")
        self.floor_line = self.ax_decay.axhline(
            0.0, color="tab:red", ls=":", lw=1.3, visible=False, label="_nolegend_"
        )

        # The gated image + colorbar.
        cmap_obj = matplotlib.colormaps[self.cmap].copy()
        cmap_obj.set_bad(color="black")  # thresholded-out pixels render black
        ny, nx = self.model.intensity.shape
        self.im = self.ax_img.imshow(
            np.zeros((ny, nx)), cmap=cmap_obj, interpolation="nearest", origin="upper"
        )
        self.ax_img.set_xticks([])
        self.ax_img.set_yticks([])
        self.cbar = self.fig.colorbar(self.im, cax=self.cax, label="photons in gate")

        # Draggable, resizable gate over the decay's x-axis (nanoseconds).
        # Calculations (the gated image + the integration highlight) run ONLY
        # when the span is released (onselect -> _on_gate); there is no
        # per-move callback, so dragging the boundaries stays lag-free. During
        # the drag, blitting moves just the orange band cheaply for feedback.
        self._span = SpanSelector(
            self.ax_decay,
            self._on_gate,
            direction="horizontal",
            useblit=True,
            interactive=True,
            drag_from_anywhere=True,
            props=dict(alpha=0.18, facecolor="tab:orange"),
        )

        # Per-pixel decay picking on the image: a rectangle ROI selector plus
        # click handling for single pixels (a click is a near-zero-size drag, so
        # the RectangleSelector's minimum span lets the two coexist).
        self._rect = RectangleSelector(
            self.ax_img,
            self._on_roi,
            useblit=True,
            button=[1],
            minspanx=5,
            minspany=5,
            spancoords="data",
            interactive=False,
            props=dict(facecolor="none", edgecolor="cyan", lw=1.2, ls="--"),
        )
        self.fig.canvas.mpl_connect("button_press_event", self._on_image_press)
        self.fig.canvas.mpl_connect("button_release_event", self._on_image_release)

        self._build_controls()
        self._file_label = self.fig.text(0.06, 0.965, "", fontsize=9, weight="bold")
        self._status = self.fig.text(0.06, 0.01, "", fontsize=8, color="0.3")

    def _build_controls(self) -> None:
        # --- left column: sliders ---
        pos = self.model.intensity[self.model.intensity > 0]
        tmax = int(np.percentile(pos, 99.9)) if pos.size else 1
        ax_thr = self.fig.add_axes([0.20, 0.45, 0.26, 0.022])
        self.s_thresh = Slider(ax_thr, "min photons/px", 0, max(1, tmax), valinit=0, valstep=1)
        self.s_thresh.on_changed(self._on_threshold)

        ax_nf = self.fig.add_axes([0.20, 0.40, 0.26, 0.022])
        self.s_floor = Slider(
            ax_nf, "noise floor\n(cts/bin)", 0, self._floor_slider_max(),
            valinit=min(self.noise_floor_total, self._floor_slider_max()),
        )
        self.s_floor.on_changed(self._on_noise_floor)

        self.s_z = None
        if len(self.stack) > 1:
            ax_z = self.fig.add_axes([0.20, 0.35, 0.26, 0.022])
            self.s_z = Slider(
                ax_z, "z-slice", 0, len(self.stack) - 1, valinit=self.z_index, valstep=1
            )
            self.s_z.on_changed(self._on_zslice)

        # --- left column: exact gate value boxes (ns) ---
        self.fig.text(0.06, 0.285, "Gate (ns):", fontsize=9)
        ax_lo = self.fig.add_axes([0.21, 0.265, 0.08, 0.035])
        self.tb_lo = TextBox(ax_lo, "start ", initial="")
        self.tb_lo.on_submit(self._on_gate_text)
        ax_hi = self.fig.add_axes([0.38, 0.265, 0.08, 0.035])
        self.tb_hi = TextBox(ax_hi, "end ", initial="")
        self.tb_hi.on_submit(self._on_gate_text)

        # --- left column: per-pixel decay picking ---
        self.fig.text(0.06, 0.205,
                      "Per-pixel decay: click a pixel or drag a box on the image"
                      "  (avg = spatial N×N, smooth = time bins)", fontsize=8)
        ax_box = self.fig.add_axes([0.155, 0.16, 0.04, 0.035])
        self.tb_box = TextBox(ax_box, "avg ", initial=str(self.box_size))
        self.tb_box.on_submit(self._on_box_size)
        ax_sm = self.fig.add_axes([0.27, 0.16, 0.04, 0.035])
        self.tb_smooth = TextBox(ax_sm, "smooth ", initial=str(self.smooth_bins))
        self.tb_smooth.on_submit(self._on_smooth)
        ax_clear = self.fig.add_axes([0.36, 0.16, 0.10, 0.04])
        self.b_clear = Button(ax_clear, "Clear picks")
        self.b_clear.on_clicked(lambda _e: self._clear_picks())

        # --- right column: toggles, channel, buttons ---
        ax_chk = self.fig.add_axes([0.55, 0.36, 0.18, 0.12])
        self.c_toggles = CheckButtons(ax_chk, ["log y", "subtract floor"],
                                      [self.log_scale, self.apply_floor])
        self.c_toggles.on_clicked(self._on_toggle)

        self.r_ch = None
        if self.model.cube.n_channels > 1:
            ax_ch = self.fig.add_axes([0.55, 0.18, 0.08, 0.14])
            ax_ch.set_title("channel", fontsize=8)
            self.r_ch = RadioButtons(
                ax_ch, [str(c) for c in range(self.model.cube.n_channels)], active=self.channel
            )
            self.r_ch.on_clicked(self._on_channel)

        ax_exp = self.fig.add_axes([0.72, 0.40, 0.14, 0.05])
        self.b_export = Button(ax_exp, "Export")
        self.b_export.on_clicked(lambda _e: self._on_export())

        ax_save = self.fig.add_axes([0.72, 0.32, 0.066, 0.05])
        self.b_save = Button(ax_save, "Save\nsettings")
        self.b_save.on_clicked(lambda _e: self._on_save())
        ax_load = self.fig.add_axes([0.794, 0.32, 0.066, 0.05])
        self.b_load = Button(ax_load, "Load\nsettings")
        self.b_load.on_clicked(lambda _e: self._on_load())

        ax_open = self.fig.add_axes([0.72, 0.25, 0.14, 0.045])
        self.b_open = Button(ax_open, "Open .ptu file…")
        self.b_open.on_clicked(lambda _e: self._on_open_file())

        # --- right column: spatial binning (pool photons per pixel) ---
        self.fig.text(0.55, 0.165, "Binning (pool photons per pixel):", fontsize=8)
        ax_bin = self.fig.add_axes([0.60, 0.115, 0.04, 0.035])
        self.tb_bin = TextBox(ax_bin, "bin ", initial=str(self.bin_size))
        self.tb_bin.on_submit(self._on_bin_size)
        ax_tgt = self.fig.add_axes([0.71, 0.115, 0.05, 0.035])
        self.tb_target = TextBox(ax_tgt, "target ", initial=str(self.bin_target))
        self.tb_target.on_submit(self._on_bin_target)
        ax_auto = self.fig.add_axes([0.80, 0.11, 0.06, 0.045])
        self.b_auto = Button(ax_auto, "Auto")
        self.b_auto.on_clicked(lambda _e: self._on_auto_bin())

    def _floor_slider_max(self) -> float:
        """A sensible upper bound for the noise-floor slider on this slice."""
        return max(self.model.auto_noise_floor_total() * 5.0, 10.0)

    @staticmethod
    def _smooth(y: np.ndarray, window: int) -> np.ndarray:
        """Centered moving-average over microtime bins (display only).

        A single pixel's decay is photon-starved and looks like static; a small
        moving average reveals the underlying decay shape without touching the
        data used for gating/export. ``window <= 1`` returns the data unchanged.
        """
        w = int(window)
        if w <= 1 or y.size < w:
            return y
        return np.convolve(y, np.ones(w) / w, mode="same")

    def _update_gate_fill(self) -> None:
        """Shade only the area between the decay curve and the noise floor,
        within the gate -- i.e. exactly the quantity being integrated.

        In total-decay view it shades the total curve down to the (total) floor;
        with picks, it shades each per-pixel decay down to the per-pixel floor.
        If the floor isn't being subtracted, the lower bound is 0 (the whole
        area under the curve in the gate is integrated).
        """
        for coll in self._gate_fills:
            coll.remove()
        self._gate_fills = []
        res = self.model.resolution_ns
        lo_ns, hi_ns = gating.gate_bounds_ns(self.gate_lo_bin, self.gate_hi_bin, res)
        x = self.model.cube.time_axis_ns
        in_gate = (x >= lo_ns) & (x < hi_ns)
        floor_on = self.apply_floor and self.noise_floor_total > 0
        if self.picks:
            lower = self._floor_per_pixel() if floor_on else 0.0
            for ln in self._pick_lines:
                y = np.asarray(ln.get_ydata(), dtype=float)
                self._gate_fills.append(self.ax_decay.fill_between(
                    x, lower, y, where=in_gate & (y > lower),
                    step="post", color=ln.get_color(), alpha=0.30, lw=0))
        else:
            lower = self.noise_floor_total if floor_on else 0.0
            y = self.model.decay.astype(float)
            self._gate_fills.append(self.ax_decay.fill_between(
                x, lower, y, where=in_gate & (y > lower),
                step="post", color="tab:orange", alpha=0.35, lw=0))

    # --------------------------------------------------------------- redrawing
    def _redraw_pick_lines(self) -> None:
        """Recreate the overlaid per-pixel/ROI decay lines from ``self.picks``."""
        for ln in self._pick_lines:
            ln.remove()
        self._pick_lines = []
        self._picks_ymax = 0.0
        x = self.model.cube.time_axis_ns
        ny, nx = self.model.intensity.shape
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
            shown = self._smooth(raw, self.smooth_bins)  # display-only smoothing
            # photons-in-gate label uses the *raw* counts (smoothing is cosmetic).
            in_gate = float(raw[self.gate_lo_bin : self.gate_hi_bin + 1].sum())
            color = _PICK_COLORS[i % len(_PICK_COLORS)]
            (ln,) = self.ax_decay.plot(
                x, shown, color=color, lw=1.2, drawstyle="steps-post",
                label=f"{tag}: {in_gate:.1f}/px in gate",
            )
            self._pick_lines.append(ln)
            self._picks_ymax = max(self._picks_ymax, float(shown.max()))

    def _refresh_decay(self) -> None:
        res = self.model.resolution_ns
        x = self.model.cube.time_axis_ns
        self.decay_line.set_data(x, self.model.decay.astype(float))
        self.t0_line.set_xdata([self.model.t0_ns(), self.model.t0_ns()])
        self.ax_decay.set_xlim(0, x[-1] + res)

        picks_mode = bool(self.picks)
        self.decay_line.set_visible(not picks_mode)
        self._redraw_pick_lines()

        # Units & floor-line height depend on which decay(s) are shown.
        if picks_mode:
            self.ax_decay.set_ylabel("photons / pixel (mean)")
            floor_y = self._floor_per_pixel()
            data_max = self._picks_ymax
        else:
            self.ax_decay.set_ylabel("photons")
            floor_y = self.noise_floor_total
            data_max = float(self.model.decay.max())
        ymax = max(data_max, floor_y, 1.0)

        # Log only when there is positive data to show (e.g. picking a dark,
        # zero-count pixel would otherwise warn); fall back to linear. We also
        # silence matplotlib's benign "no positive values" notice that can fire
        # while switching scales before our explicit ylim is applied.
        with warnings.catch_warnings():
            warnings.filterwarnings("ignore", message="Data has no positive values")
            if self.log_scale and data_max > 0:
                self.ax_decay.set_yscale("log")
                bottom = 0.5
                if self.noise_floor_total > 0 and floor_y > 0:
                    bottom = max(1e-4, min(bottom, floor_y * 0.5))
                self.ax_decay.set_ylim(bottom, ymax * 1.5)
            else:
                self.ax_decay.set_yscale("linear")
                self.ax_decay.set_ylim(0, ymax * 1.05)

        if self.noise_floor_total > 0:
            self.floor_line.set_ydata([floor_y, floor_y])
            self.floor_line.set_visible(True)
        else:
            self.floor_line.set_visible(False)

        # Legend only when there are picks to label.
        leg = self.ax_decay.get_legend()
        if picks_mode:
            self.ax_decay.legend(fontsize=7, loc="upper right", framealpha=0.7)
        elif leg is not None:
            leg.remove()

        # Reposition the gate span (clamped to this cube) and sync the boxes.
        self.gate_hi_bin = min(self.gate_hi_bin, self.model.n_bins - 1)
        self.gate_lo_bin = min(self.gate_lo_bin, self.gate_hi_bin)
        self._span.extents = gating.gate_bounds_ns(self.gate_lo_bin, self.gate_hi_bin, res)
        self._sync_gate_textboxes()
        self._update_gate_fill()
        self.fig.canvas.draw_idle()

    def _refresh_image(self) -> None:
        floor = self._floor_per_pixel() if self.apply_floor else 0.0
        gated = self.model.gate(self.gate_lo_bin, self.gate_hi_bin, floor_per_bin=floor)

        # Threshold: blank out pixels too dim to trust (NaN -> 'bad' colour).
        display = gated.astype(float)
        if self.threshold > 0:
            display[self.model.intensity < self.threshold] = np.nan

        finite = display[np.isfinite(display)]
        if finite.size:
            vmin = float(np.percentile(finite, 1))
            vmax = float(np.percentile(finite, 99))
            if vmax <= vmin:
                vmax = vmin + 1.0
        else:
            vmin, vmax = 0.0, 1.0
        self.im.set_data(display)
        self.im.set_clim(vmin, vmax)

        res = self.model.resolution_ns
        lo_ns, hi_ns = gating.gate_bounds_ns(self.gate_lo_bin, self.gate_hi_bin, res)
        t0 = self.model.t0_ns()
        in_gate = int(self.model.decay[self.gate_lo_bin : self.gate_hi_bin + 1].sum())
        floor_note = f"  |  floor {self.noise_floor_total:.0f} cts/bin" if self.apply_floor else ""
        unit = "photons" if self.bin_size == 1 else f"counts ({self.bin_size}×{self.bin_size} binned)"
        self.ax_img.set_title(
            f"gate {lo_ns:.2f}-{hi_ns:.2f} ns  (t0{lo_ns - t0:+.2f} .. t0{hi_ns - t0:+.2f})\n"
            f"{in_gate:,} {unit} in gate{floor_note}",
            fontsize=9,
        )
        self.fig.canvas.draw_idle()

    # ----------------------------------------------------------- gate handlers
    def _apply_gate(self, xmin: float, xmax: float) -> None:
        """Set the gate from a [xmin, xmax] ns span and recompute the gated image
        and the integration highlight. Invoked on span *release* only (there is
        no per-move callback), so dragging the boundaries does not recompute."""
        res, n = self.model.resolution_ns, self.model.n_bins
        self.gate_lo_bin = gating.ns_to_bin(xmin, res, n)
        self.gate_hi_bin = gating.ns_to_bin(xmax, res, n)
        if self.gate_hi_bin < self.gate_lo_bin:
            self.gate_lo_bin, self.gate_hi_bin = self.gate_hi_bin, self.gate_lo_bin
        self._update_gate_fill()
        self._refresh_image()

    def _on_gate(self, xmin: float, xmax: float) -> None:
        """Span released: apply the gate, sync the value boxes and pick readouts."""
        self._apply_gate(xmin, xmax)
        self._sync_gate_textboxes()
        if self.picks:  # refresh per-pick "in gate" labels
            self._refresh_decay()

    def _on_gate_text(self, _text: str) -> None:
        if self._syncing:
            return
        try:
            start_ns = float(self.tb_lo.text)
            end_ns = float(self.tb_hi.text)
        except ValueError:
            self._set_status("Gate values must be numbers in ns; reverting.")
            self._sync_gate_textboxes()
            return
        self.gate_lo_bin, self.gate_hi_bin = self._ns_bounds_to_bins(start_ns, end_ns)
        # _refresh_decay repositions the span, updates the integration highlight
        # and the value boxes; _refresh_image redraws the gated image.
        self._refresh_decay()
        self._refresh_image()

    def _ns_bounds_to_bins(self, start_ns: float, end_ns: float) -> tuple[int, int]:
        """Convert a [start, end] gate in ns to inclusive bin indices (edge-consistent)."""
        res, n = self.model.resolution_ns, self.model.n_bins
        lo = int(round(start_ns / res))
        hi = int(round(end_ns / res)) - 1
        lo = max(0, min(n - 1, lo))
        hi = max(0, min(n - 1, hi))
        if hi < lo:
            lo, hi = hi, lo
        return lo, hi

    def _sync_gate_textboxes(self) -> None:
        if not hasattr(self, "tb_lo"):
            return
        lo_ns, hi_ns = gating.gate_bounds_ns(
            self.gate_lo_bin, self.gate_hi_bin, self.model.resolution_ns
        )
        self._syncing = True
        try:
            self.tb_lo.set_val(f"{lo_ns:.3f}")
            self.tb_hi.set_val(f"{hi_ns:.3f}")
        finally:
            self._syncing = False

    # -------------------------------------------------- per-pixel decay picking
    def _on_image_press(self, event) -> None:
        if event.inaxes is self.ax_img and event.button == 1:
            self._press_xy = (event.x, event.y)
        else:
            self._press_xy = None

    def _on_image_release(self, event) -> None:
        # Treat a release near the press point as a single-pixel *click*; larger
        # drags are rectangles, handled by the RectangleSelector (_on_roi).
        if event.inaxes is not self.ax_img or event.button != 1 or self._press_xy is None:
            self._press_xy = None
            return
        px, py = self._press_xy
        self._press_xy = None
        if abs(event.x - px) > 3 or abs(event.y - py) > 3:
            return
        if event.xdata is None or event.ydata is None:
            return
        self._add_pixel(int(round(event.ydata)), int(round(event.xdata)))

    def _on_roi(self, eclick, erelease) -> None:
        if eclick.xdata is None or erelease.xdata is None:
            return
        ny, nx = self.model.intensity.shape
        c0 = max(0, int(round(min(eclick.xdata, erelease.xdata))))
        c1 = min(nx, int(round(max(eclick.xdata, erelease.xdata))) + 1)
        r0 = max(0, int(round(min(eclick.ydata, erelease.ydata))))
        r1 = min(ny, int(round(max(eclick.ydata, erelease.ydata))) + 1)
        if r1 <= r0 or c1 <= c0:
            return
        self._add_pick({"kind": "roi", "r0": r0, "r1": r1, "c0": c0, "c1": c1,
                        "label": f"roi[{r0}:{r1},{c0}:{c1}]"})

    def _add_pixel(self, r: int, c: int) -> None:
        ny, nx = self.model.intensity.shape
        r = max(0, min(ny - 1, r))
        c = max(0, min(nx - 1, c))
        self._add_pick({"kind": "pixel", "r": r, "c": c, "label": f"px({r},{c})"})

    def _add_pick(self, pick: dict) -> None:
        self.picks.append(pick)
        if len(self.picks) > _MAX_PICKS:
            self.picks.pop(0)  # FIFO: oldest pick drops off
        self._refresh_decay()
        self._set_status(f"{len(self.picks)} pick(s): " + "; ".join(p["label"] for p in self.picks))

    def _clear_picks(self) -> None:
        self.picks = []
        self._refresh_decay()
        self._set_status("Picks cleared; showing total decay.")

    # --------------------------------------------------------- file/layer choice
    def _update_filename_label(self) -> None:
        if hasattr(self, "_file_label"):
            bin_note = f", bin {self.bin_size}×{self.bin_size}" if self.bin_size > 1 else ""
            self._file_label.set_text(
                f"file: {self.model.cube.path.name}   "
                f"(layer z {self.z_index + 1}/{len(self.stack)}, channel {self.channel}{bin_note})"
            )

    def _on_open_file(self) -> None:
        path = self._dialog_open_ptu()
        if path:
            self._load_file(path)

    def _dialog_open_ptu(self) -> str | None:
        """Tk open-file dialog for a .ptu, or None if unavailable/cancelled."""
        try:
            import tkinter as tk
            from tkinter import filedialog

            root = tk.Tk()
            root.withdraw()
            p = filedialog.askopenfilename(
                title="Open a .ptu file / layer",
                initialdir=str(self.model.cube.path.parent),
                filetypes=[("PicoQuant PTU", "*.ptu"), ("All files", "*.*")],
            )
            root.destroy()
            return p or None
        except Exception:
            self._set_status("tkinter unavailable; cannot open a file dialog.")
            return None

    def _load_file(self, path) -> None:
        """Load a different .ptu (and re-detect its numbered stack), rebuilding state."""
        path = Path(path)
        try:
            self.stack = find_stack(path)
            self.z_index = next((i for i, p in enumerate(self.stack) if p == path), 0)
            try:
                self.model = self._load_current()
            except Exception:
                self.channel = 0  # the new file may have fewer detector channels
                self.model = self._load_current()
        except Exception as exc:  # noqa: BLE001 - report cleanly, keep current view
            self._set_status(f"Could not load {path.name}: {exc}")
            return

        # Reset selections tied to the old image and refit slider ranges.
        self.picks = []
        self.noise_floor_total = self.model.auto_noise_floor_total()
        self.gate_lo_bin = min(self.gate_lo_bin, self.model.n_bins - 1)
        self.gate_hi_bin = min(self.gate_hi_bin, self.model.n_bins - 1)
        pos = self.model.intensity[self.model.intensity > 0]
        self.s_thresh.valmax = max(1, int(np.percentile(pos, 99.9)) if pos.size else 1)
        self.s_thresh.ax.set_xlim(self.s_thresh.valmin, self.s_thresh.valmax)
        self.s_floor.valmax = self._floor_slider_max()
        self.s_floor.ax.set_xlim(0, self.s_floor.valmax)
        self._syncing = True
        try:
            self.s_thresh.set_val(0)
            self.s_floor.set_val(min(self.noise_floor_total, self.s_floor.valmax))
            if self.s_z is not None:
                self.s_z.valmax = max(1, len(self.stack) - 1)
                self.s_z.ax.set_xlim(0, self.s_z.valmax)
                self.s_z.set_val(min(self.z_index, self.s_z.valmax))
        finally:
            self._syncing = False
        self.threshold = 0
        if self.s_z is None and len(self.stack) > 1:
            self._set_status(f"Loaded a {len(self.stack)}-plane stack; relaunch for a z-slider.")
        self._update_filename_label()
        self._refresh_decay()
        self._refresh_image()
        print(self.model.cube.summary())

    # ----------------------------------------------------- other event handlers
    def _on_threshold(self, val: float) -> None:
        if self._syncing:
            return
        self.threshold = int(val)
        self._refresh_image()

    def _on_noise_floor(self, val: float) -> None:
        if self._syncing:
            return
        self.noise_floor_total = float(val)
        self._refresh_decay()   # move the floor line
        self._refresh_image()   # re-subtract from the gated image

    def _on_box_size(self, text: str) -> None:
        if self._syncing:
            return
        try:
            b = max(1, int(float(text)))
        except ValueError:
            self._set_status("Averaging box must be a positive integer; reverting.")
            self._syncing = True
            try:
                self.tb_box.set_val(str(self.box_size))
            finally:
                self._syncing = False
            return
        self.box_size = b
        if self.picks:
            self._refresh_decay()

    def _on_smooth(self, text: str) -> None:
        if self._syncing:
            return
        try:
            w = max(1, int(float(text)))
        except ValueError:
            self._set_status("Smoothing must be a positive integer (bins); reverting.")
            self._syncing = True
            try:
                self.tb_smooth.set_val(str(self.smooth_bins))
            finally:
                self._syncing = False
            return
        self.smooth_bins = w
        if self.picks:
            self._refresh_decay()

    def _on_bin_size(self, text: str) -> None:
        if self._syncing:
            return
        try:
            b = max(1, int(float(text)))
        except ValueError:
            self._set_status("Bin size must be a positive integer; reverting.")
            self._syncing = True
            try:
                self.tb_bin.set_val(str(self.bin_size))
            finally:
                self._syncing = False
            return
        self.bin_size = b
        self._rebuild_binned_model()
        self._set_status(
            f"Binning {b}×{b}: each pixel pools its {b}×{b} neighborhood "
            f"(≈{b * b}× more photons/pixel)." if b > 1 else "Binning off (1×1)."
        )

    def _on_bin_target(self, text: str) -> None:
        if self._syncing:
            return
        try:
            self.bin_target = max(1, int(float(text)))
        except ValueError:
            self._set_status("Target photons must be a positive integer; reverting.")
            self._syncing = True
            try:
                self.tb_target.set_val(str(self.bin_target))
            finally:
                self._syncing = False

    def _on_auto_bin(self) -> None:
        # Decide from the UNBINNED per-pixel photon distribution above the
        # current threshold (the pixels that matter), not the binned counts.
        b, n0 = gating.suggest_bin_factor(
            self.model.cube.intensity, target_photons=self.bin_target,
            min_intensity=self.threshold,
        )
        self.bin_size = b
        self._syncing = True
        try:
            self.tb_bin.set_val(str(b))
        finally:
            self._syncing = False
        self._rebuild_binned_model()
        self._set_status(
            f"Auto-bin: median signal pixel ≈ {n0:.0f} photons → {b}×{b} "
            f"(≈{n0 * b * b:.0f} photons/px, target {self.bin_target})."
        )

    def _rebuild_binned_model(self) -> None:
        """Re-bin the current cube (no disk read) and refit dependent ranges."""
        self.model = gating.GatingModel(self.model.cube, bin_factor=self.bin_size)
        pos = self.model.intensity[self.model.intensity > 0]
        self.s_thresh.valmax = max(1, int(np.percentile(pos, 99.9)) if pos.size else 1)
        self.s_thresh.ax.set_xlim(self.s_thresh.valmin, self.s_thresh.valmax)
        # Binning changes the count scale; re-default the noise floor to the new auto.
        self.noise_floor_total = self.model.auto_noise_floor_total()
        self.s_floor.valmax = self._floor_slider_max()
        self.s_floor.ax.set_xlim(0, self.s_floor.valmax)
        self._syncing = True
        try:
            self.s_floor.set_val(min(self.noise_floor_total, self.s_floor.valmax))
        finally:
            self._syncing = False
        self._update_filename_label()
        self._refresh_decay()
        self._refresh_image()

    def _on_toggle(self, label: str) -> None:
        if self._syncing:  # ignore toggles we trigger ourselves via set_active
            return
        if label == "log y":
            self.log_scale = not self.log_scale
            self._refresh_decay()
        elif label == "subtract floor":
            self.apply_floor = not self.apply_floor
            self._refresh_decay()   # the highlight's lower bound switches (floor <-> 0)
            self._refresh_image()

    def _on_channel(self, label: str) -> None:
        self.channel = int(label)
        self.model = self._load_current()
        self._update_filename_label()
        self._refresh_decay()
        self._refresh_image()

    def _on_zslice(self, val: float) -> None:
        if self._syncing:  # ignore programmatic set_val (the cube is loaded elsewhere)
            return
        self.z_index = int(val)
        self.model = self._load_current()
        # Slider ranges depend on this slice; keep current values, refit bounds.
        pos = self.model.intensity[self.model.intensity > 0]
        self.s_thresh.valmax = max(1, int(np.percentile(pos, 99.9)) if pos.size else 1)
        self.s_thresh.ax.set_xlim(self.s_thresh.valmin, self.s_thresh.valmax)
        self.s_floor.valmax = self._floor_slider_max()
        self.s_floor.ax.set_xlim(0, self.s_floor.valmax)
        self._update_filename_label()
        self._refresh_decay()
        self._refresh_image()

    # --------------------------------------------------------- provenance / IO
    def _metadata(self) -> dict:
        c = self.model.cube
        ny, nx = self.model.intensity.shape
        return {
            "source_file": c.path.name,
            "source_path": str(c.path),
            "record_type": c.record_type,
            "resolution_ns": c.resolution_ns,
            "period_ns": c.period_ns,
            "n_bins": c.n_bins,
            "n_channels": c.n_channels,
            "n_frames": c.n_frames,
            "image_shape": [ny, nx],
            "bin_size": self.model.bin_factor,
            "t0_bin": self.model.t0_bin,
            "t0_ns": self.model.t0_ns(),
            "total_photons_in_file": c.n_photons,
        }

    def _settings(self) -> dict:
        res = self.model.resolution_ns
        lo_ns, hi_ns = gating.gate_bounds_ns(self.gate_lo_bin, self.gate_hi_bin, res)
        return {
            "z_index": self.z_index,
            "z_file": self.stack[self.z_index].name,
            "channel": self.channel,
            "sum_frames": self.sum_frames,
            "gate_lo_bin": self.gate_lo_bin,
            "gate_hi_bin": self.gate_hi_bin,
            "gate_lo_ns": round(lo_ns, 4),
            "gate_hi_ns": round(hi_ns, 4),
            "threshold": self.threshold,
            "noise_floor_total": round(self.noise_floor_total, 4),
            "noise_floor_per_pixel": self._floor_per_pixel(),
            "subtract_floor": self.apply_floor,
            "bin_size": self.bin_size,
            "bin_target": self.bin_target,
            "box_size": self.box_size,
            "smooth_bins": self.smooth_bins,
            "log_scale": self.log_scale,
            "cmap": self.cmap,
        }

    def _current_image_for_export(self) -> tuple[np.ndarray, float, float]:
        """Recompute exactly what's on screen (gated, floor, threshold)."""
        floor = self._floor_per_pixel() if self.apply_floor else 0.0
        gated = self.model.gate(self.gate_lo_bin, self.gate_hi_bin, floor_per_bin=floor)
        display = gated.astype(float)
        if self.threshold > 0:
            display[self.model.intensity < self.threshold] = np.nan
        finite = display[np.isfinite(display)]
        if finite.size:
            vmin, vmax = float(np.percentile(finite, 1)), float(np.percentile(finite, 99))
            if vmax <= vmin:
                vmax = vmin + 1.0
        else:
            vmin, vmax = 0.0, 1.0
        return display, vmin, vmax

    def _on_export(self) -> None:
        out_dir = self.model.cube.path.parent / "chronogate_exports"
        stem = self.model.cube.path.stem
        base = f"{stem}_ch{self.channel}_gate{self.gate_lo_bin}-{self.gate_hi_bin}"
        display, vmin, vmax = self._current_image_for_export()
        paths = export_all(
            out_dir,
            base,
            gated_image=display,
            time_ns=self.model.cube.time_axis_ns,
            decay=self.model.decay,
            cmap=self.cmap,
            vmin=vmin,
            vmax=vmax,
            metadata=self._metadata(),
            settings=self._settings(),
        )
        msg = f"Exported -> {out_dir}  ({', '.join(Path(p).name for p in paths.values())})"
        print(msg)
        self._set_status(msg)

    def _on_save(self) -> None:
        path = self._dialog_path(save=True, default_name=f"{self.model.cube.path.stem}_settings.json")
        if not path:
            return
        save_settings(path, self._settings(), self._metadata())
        self._set_status(f"Saved settings -> {path}")
        print(f"Saved settings -> {path}")

    def _on_load(self) -> None:
        path = self._dialog_path(save=False)
        if not path:
            return
        self._apply_settings(load_settings(path))
        self._set_status(f"Loaded settings <- {path}")
        print(f"Loaded settings <- {path}")

    def _apply_settings(self, s: dict) -> None:
        # Binning factor first, so any cube reload below uses the right factor.
        self.bin_size = int(s.get("bin_size", self.bin_size))
        self.bin_target = int(s.get("bin_target", self.bin_target))

        # Switch channel/z-slice next (these reload the cube), then gate/view.
        reload_needed = False
        if "channel" in s and s["channel"] != self.channel and s["channel"] < self.model.cube.n_channels:
            self.channel = int(s["channel"])
            reload_needed = True
        if "z_index" in s and 0 <= s["z_index"] < len(self.stack) and s["z_index"] != self.z_index:
            self.z_index = int(s["z_index"])
            reload_needed = True
        if reload_needed:
            self.model = self._load_current()
        elif self.model.bin_factor != self.bin_size:
            # No file reload, but the bin factor changed -> re-bin in place.
            self.model = gating.GatingModel(self.model.cube, bin_factor=self.bin_size)

        self.gate_lo_bin = int(s.get("gate_lo_bin", self.gate_lo_bin))
        self.gate_hi_bin = int(s.get("gate_hi_bin", self.gate_hi_bin))
        self.threshold = int(s.get("threshold", self.threshold))
        self.noise_floor_total = float(s.get("noise_floor_total", self.noise_floor_total))
        self.apply_floor = bool(s.get("subtract_floor", self.apply_floor))
        self.box_size = int(s.get("box_size", self.box_size))
        self.smooth_bins = int(s.get("smooth_bins", self.smooth_bins))
        self.log_scale = bool(s.get("log_scale", self.log_scale))

        # Refit slider ranges to the (possibly re-binned / reloaded) scale.
        pos = self.model.intensity[self.model.intensity > 0]
        self.s_thresh.valmax = max(1, int(np.percentile(pos, 99.9)) if pos.size else 1)
        self.s_thresh.ax.set_xlim(self.s_thresh.valmin, self.s_thresh.valmax)
        self.s_floor.valmax = self._floor_slider_max()
        self.s_floor.ax.set_xlim(0, self.s_floor.valmax)

        # Sync widget visuals (guarded so set_val/set_active don't recurse badly).
        self._syncing = True
        try:
            self.s_thresh.set_val(self.threshold)
            self.s_floor.set_val(min(self.noise_floor_total, self.s_floor.valmax))
            self.tb_box.set_val(str(self.box_size))
            self.tb_smooth.set_val(str(self.smooth_bins))
            self.tb_bin.set_val(str(self.bin_size))
            self.tb_target.set_val(str(self.bin_target))
            if self.s_z is not None:
                self.s_z.set_val(min(self.z_index, self.s_z.valmax))
            if self.c_toggles.get_status()[0] != self.log_scale:
                self.c_toggles.set_active(0)
            if self.c_toggles.get_status()[1] != self.apply_floor:
                self.c_toggles.set_active(1)
        finally:
            self._syncing = False
        self._update_filename_label()
        self._refresh_decay()
        self._refresh_image()

    def _dialog_path(self, save: bool, default_name: str = "") -> str | None:
        """Open a Tk file dialog if available; else fall back to a default path."""
        try:
            import tkinter as tk
            from tkinter import filedialog

            root = tk.Tk()
            root.withdraw()
            initial = str(self.model.cube.path.parent)
            if save:
                p = filedialog.asksaveasfilename(
                    initialdir=initial, initialfile=default_name, defaultextension=".json",
                    filetypes=[("JSON", "*.json")],
                )
            else:
                p = filedialog.askopenfilename(
                    initialdir=initial, filetypes=[("JSON", "*.json"), ("All", "*.*")]
                )
            root.destroy()
            return p or None
        except Exception:
            fallback = self.model.cube.path.parent / "chronogate_exports"
            fallback.mkdir(parents=True, exist_ok=True)
            if save:
                self._set_status("tkinter unavailable; saving to default path.")
                return str(fallback / (default_name or "settings.json"))
            self._set_status("tkinter unavailable; cannot open a load dialog.")
            return None

    def _set_status(self, text: str) -> None:
        self._status.set_text(text)
        self.fig.canvas.draw_idle()

    def show(self) -> None:
        plt.show()
