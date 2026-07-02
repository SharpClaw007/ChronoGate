"""The native-Qt control panels (one QGroupBox each), shown in the Controls dock.

Panels are *dumb views*: they build and expose their widgets as public
attributes, emit Qt signals on user edits, and offer setters the controller calls
to push state back. All analysis logic lives in ``controller.py``. Layouts are
kept compact so the six panels fit in two columns without scrolling.
"""

from __future__ import annotations

import math

from PySide6.QtCore import Qt, Signal, QSignalBlocker
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QButtonGroup, QCheckBox, QComboBox, QDoubleSpinBox, QFormLayout, QGroupBox,
    QHBoxLayout, QLabel, QListWidget, QListWidgetItem, QPushButton, QRadioButton,
    QSlider, QSpinBox, QVBoxLayout, QWidget,
)

INTENSITY_CMAPS = ["viridis", "gray", "magma", "inferno", "cividis"]
LIFETIME_CMAPS = ["turbo", "viridis", "plasma", "inferno", "cividis"]


def _muted(text: str) -> QLabel:
    lab = QLabel(text)
    lab.setObjectName("Muted")
    lab.setWordWrap(True)
    return lab


def _col(box, margins=(10, 4, 10, 8), spacing=5) -> QVBoxLayout:
    lay = QVBoxLayout(box)
    lay.setContentsMargins(*margins)
    lay.setSpacing(spacing)
    return lay


def _form() -> QFormLayout:
    form = QFormLayout()
    form.setContentsMargins(0, 0, 0, 0)
    form.setVerticalSpacing(4)
    form.setHorizontalSpacing(8)
    form.setLabelAlignment(Qt.AlignRight | Qt.AlignVCenter)
    return form


class SliderSpin(QWidget):
    """A horizontal QSlider linked to a spin box (drag *or* type the value).

    The spin box holds the value; the slider is a position that maps to it either
    **linearly** (default) or **logarithmically** (``log=True`` / :meth:`setScale`)
    so the control can match a log-scaled plot axis while the typed value stays
    exact. Pass ``decimals > 0`` for a fractional value (a ``QDoubleSpinBox``) --
    needed when the meaningful range dips below 1. Programmatic
    :meth:`setValue`/:meth:`setRange`/:meth:`setScale` are silent (no
    ``valueChanged``); only user interaction emits, so the controller can sync it
    without feedback loops.
    """

    valueChanged = Signal(float)
    _STEPS = 1000  # slider resolution when position is mapped (log or fractional)

    def __init__(self, minimum=0, maximum=100, value=0, suffix: str = "",
                 log: bool = False, decimals: int = 0):
        super().__init__()
        self._decimals = int(decimals)
        self._log = bool(log)
        self._min = float(minimum)
        self._max = max(self._min, float(maximum))
        self.slider = QSlider(Qt.Horizontal)
        self.spin = QDoubleSpinBox() if self._decimals else QSpinBox()
        if self._decimals:
            self.spin.setDecimals(self._decimals)
            self.spin.setSingleStep(10.0 ** -self._decimals)
        if suffix:
            self.spin.setSuffix(suffix)
        self.spin.setButtonSymbols(type(self.spin).NoButtons)
        self.spin.setMaximumWidth(92 if self._decimals else 78)
        self.slider.setMinimumWidth(60)
        with QSignalBlocker(self.spin):
            self.spin.setRange(self._min, self._max)
            self.spin.setValue(self._round(value))
        self._config_slider()
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(6)
        lay.addWidget(self.slider, 1)
        lay.addWidget(self.spin)
        self.slider.valueChanged.connect(self._from_slider)
        self.spin.valueChanged.connect(self._from_spin)

    # -- slider position <-> value mapping (identity when plain-linear-int) ---
    @property
    def _mapped(self) -> bool:
        return self._log or self._decimals > 0

    def _round(self, v):
        return round(float(v), self._decimals) if self._decimals else int(round(v))

    def _config_slider(self) -> None:
        with QSignalBlocker(self.slider):
            if self._mapped:
                self.slider.setRange(0, self._STEPS)
            else:
                self.slider.setRange(int(self._min), int(self._max))
            self.slider.setValue(self._pos_from_value(self.spin.value()))

    def _bounds(self):
        lo, hi = self._min, self._max
        step = 10.0 ** -self._decimals if self._decimals else 1.0
        if self._log:
            lo = max(lo, step)              # log needs a positive origin
            hi = max(hi, lo * (1.0 + 1e-9))
        else:
            hi = max(hi, lo + step)
        return lo, hi

    def _value_from_pos(self, p: int):
        if not self._mapped:
            return int(p)
        lo, hi = self._bounds()
        frac = p / self._STEPS
        v = lo * (hi / lo) ** frac if self._log else lo + (hi - lo) * frac
        return self._round(v)

    def _pos_from_value(self, v) -> int:
        if not self._mapped:
            return int(v)
        lo, hi = self._bounds()
        v = min(max(float(v), lo), hi)
        frac = (math.log(v / lo) / math.log(hi / lo)) if self._log else (v - lo) / (hi - lo)
        return int(round(frac * self._STEPS))

    def _from_slider(self, p: int) -> None:
        v = self._value_from_pos(p)
        if v == self.spin.value():
            return
        with QSignalBlocker(self.spin):
            self.spin.setValue(v)
        self.valueChanged.emit(float(v))

    def _from_spin(self, v) -> None:
        pos = self._pos_from_value(v)
        if pos != self.slider.value():
            with QSignalBlocker(self.slider):
                self.slider.setValue(pos)
        self.valueChanged.emit(float(v))

    def setRange(self, lo, hi) -> None:
        self._min = float(lo)
        self._max = max(self._min, float(hi))
        with QSignalBlocker(self.spin):
            self.spin.setRange(self._min, self._max)
        self._config_slider()

    def setValue(self, v) -> None:
        with QSignalBlocker(self.spin):
            self.spin.setValue(self._round(v))
        with QSignalBlocker(self.slider):
            self.slider.setValue(self._pos_from_value(self.spin.value()))

    def setScale(self, log: bool) -> None:
        """Switch slider mapping between linear and log, preserving the value."""
        if bool(log) == self._log:
            return
        self._log = bool(log)
        self._config_slider()

    def value(self):
        return self.spin.value()

    def maximum(self):
        return self.spin.maximum()


class GatePanel(QGroupBox):
    def __init__(self):
        super().__init__("Gate")
        self.spin_lo = QDoubleSpinBox()
        self.spin_hi = QDoubleSpinBox()
        for s in (self.spin_lo, self.spin_hi):
            s.setDecimals(3)
            s.setSuffix(" ns")
            s.setRange(0.0, 1e6)
            s.setSingleStep(0.1)
        self.spin_lo.setToolTip("Gate start (ns). Or drag the shaded span on the decay.")
        self.spin_hi.setToolTip("Gate end (ns). Or drag the shaded span on the decay.")
        self.t0 = QDoubleSpinBox()
        self.t0.setDecimals(3)
        self.t0.setSuffix(" ns")
        self.t0.setRange(0.0, 1e6)
        self.t0.setSingleStep(0.05)
        self.t0.setToolTip("Pulse reference t0. Auto = smoothed decay peak; edit to override.")
        self.btn_t0_auto = QPushButton("auto")
        self.btn_t0_auto.setMaximumWidth(52)
        self.btn_t0_auto.setToolTip("Reset t0 to the auto (smoothed-peak) estimate.")
        self.active_label = _muted("single gate")
        form = _form()
        form.addRow("start", self.spin_lo)
        form.addRow("end", self.spin_hi)
        t0row = QHBoxLayout()
        t0row.setSpacing(6)
        t0row.addWidget(self.t0, 1)
        t0row.addWidget(self.btn_t0_auto)
        form.addRow("t0", t0row)
        lay = _col(self)
        lay.addLayout(form)
        lay.addWidget(self.active_label)

    def set_active_label(self, which: str, mode: str) -> None:
        self.active_label.setText(
            f"editing gate {which}" if mode == "lifetime" else "single gate")


class DisplayPanel(QGroupBox):
    def __init__(self):
        super().__init__("Display")
        self.thr = SliderSpin(0, 100, 0)
        self.thr.setToolTip("Blank pixels whose total photons are below this (dim-pixel mask).")
        self.floor = SliderSpin(0, 100, 0, suffix=" cts", decimals=0)
        self.floor.setToolTip("Noise-floor level, read on the summed decay (counts/bin). "
                              "Ranges over the whole curve (lowest→highest recorded value); "
                              "subtracted from every pixel × gate width.")
        self.cmap = QComboBox()
        self.cmap.addItems(INTENSITY_CMAPS)
        self.cmap.setToolTip("Colormap for the gated intensity image.")
        self.lock = QCheckBox("lock scale")
        self.lock.setToolTip("Freeze the colour range so z-planes/frames are directly "
                             "comparable (off = auto-scaled per frame).")
        self.vmin = QDoubleSpinBox()
        self.vmax = QDoubleSpinBox()
        for s in (self.vmin, self.vmax):
            s.setRange(0.0, 1e12)
            s.setDecimals(2)
            s.setButtonSymbols(QDoubleSpinBox.NoButtons)
            s.setMaximumWidth(96)
            s.setEnabled(False)
            s.setToolTip("Locked colour-range limit (editable while 'lock scale' is on).")
        form = _form()
        form.addRow("min photons/px", self.thr)
        form.addRow("noise floor", self.floor)
        form.addRow("colormap", self.cmap)
        lockrow = QHBoxLayout()
        lockrow.setSpacing(6)
        lockrow.addWidget(self.lock)
        lockrow.addStretch(1)
        lockrow.addWidget(QLabel("min"))
        lockrow.addWidget(self.vmin)
        lockrow.addWidget(QLabel("max"))
        lockrow.addWidget(self.vmax)
        col = _col(self)
        col.addLayout(form)
        col.addLayout(lockrow)


class LifetimePanel(QGroupBox):
    def __init__(self):
        super().__init__("Lifetime (two-gate RLD)")
        self.radio_a = QRadioButton("A (early)")
        self.radio_b = QRadioButton("B (late)")
        self.radio_a.setChecked(True)
        self._grp = QButtonGroup(self)
        self._grp.addButton(self.radio_a)
        self._grp.addButton(self.radio_b)
        self.min_cts = QSpinBox()
        self.min_cts.setRange(0, 1_000_000)
        self.min_cts.setValue(10)
        self.min_cts.setMaximumWidth(80)
        self.min_cts.setToolTip("Pixels with fewer photons than this in either gate are left blank.")
        self.cmap_life = QComboBox()
        self.cmap_life.addItems(LIFETIME_CMAPS)
        self.cmap_life.setToolTip("Colormap for the apparent-lifetime map.")
        self.hsv = QCheckBox("intensity-weighted (HSV)")
        self.hsv.setToolTip("Modulate the τ image brightness by photon count, so dim "
                            "noisy pixels don't show a false lifetime.")

        lay = _col(self)
        edit_row = QHBoxLayout()
        edit_row.setSpacing(12)
        edit_row.addWidget(_muted("Edit:"))
        edit_row.addWidget(self.radio_a)
        edit_row.addWidget(self.radio_b)
        edit_row.addStretch(1)
        lay.addLayout(edit_row)
        form = _form()
        form.addRow("min photons", self.min_cts)
        form.addRow("τ colormap", self.cmap_life)
        lay.addLayout(form)
        lay.addWidget(self.hsv)
        lay.addWidget(_muted("τ = Δt / ln(N_A / N_B)  ·  keep gates equal width"))


class PicksPanel(QGroupBox):
    def __init__(self):
        super().__init__("Per-pixel decay")
        self.avg = QSpinBox()
        self.avg.setRange(1, 99)
        self.avg.setMaximumWidth(60)
        self.avg.setToolTip("Spatial N×N averaging box for single-pixel picks (display only).")
        self.smooth = QSpinBox()
        self.smooth.setRange(1, 99)
        self.smooth.setValue(5)
        self.smooth.setMaximumWidth(60)
        self.smooth.setToolTip("Time-bin moving-average smoothing of the picked decay (display only).")
        self.fit = QCheckBox("exp fit")
        self.fit.setToolTip("Overlay a mono-exponential fit on each picked decay -- a smooth "
                            "visual guide through low-count noise; shows the apparent τ.")
        self.btn_pin = QPushButton("Pin")
        self.btn_pin.setToolTip("Freeze the current decay so the next click can be compared against it.")
        self.btn_clear = QPushButton("Clear")
        self.list = QListWidget()
        self.list.setToolTip("Picked pixels / regions (📌 = pinned) and their photons-in-gate.")
        self.list.setMaximumHeight(76)

        lay = _col(self)
        lay.addWidget(_muted("Click a pixel or drag a box on the image."))
        row = QHBoxLayout()
        row.setSpacing(8)
        row.addWidget(QLabel("avg N×N"))
        row.addWidget(self.avg)
        row.addSpacing(10)
        row.addWidget(QLabel("smooth"))
        row.addWidget(self.smooth)
        row.addSpacing(10)
        row.addWidget(self.fit)
        row.addStretch(1)
        lay.addLayout(row)
        lay.addWidget(self.list)
        self.btn_pin.setMaximumWidth(70)
        self.btn_clear.setMaximumWidth(70)
        btn_row = QHBoxLayout()
        btn_row.addStretch(1)
        btn_row.addWidget(self.btn_pin)
        btn_row.addWidget(self.btn_clear)
        lay.addLayout(btn_row)

    def set_items(self, items) -> None:
        self.list.clear()
        for text, color in items:
            it = QListWidgetItem(text)
            it.setForeground(QColor(color))
            self.list.addItem(it)


class BinningPanel(QGroupBox):
    def __init__(self):
        super().__init__("Binning")
        self.bin = QSpinBox()
        self.bin.setRange(1, 16)
        self.bin.setMaximumWidth(56)
        self.bin.setToolTip("Sum each pixel's B×B neighbourhood to pool photons (B×B more per pixel).")
        self.target = QSpinBox()
        self.target.setRange(1, 1_000_000)
        self.target.setValue(100)
        self.target.setMaximumWidth(72)
        self.target.setToolTip("Photons/pixel the Auto button aims a representative signal pixel at.")
        self.btn_auto = QPushButton("Auto")
        self.btn_auto.setToolTip("Suggest a bin factor from the photon statistics.")

        lay = _col(self)
        lay.addWidget(_muted("Pool photons per pixel for cleaner decays/lifetimes."))
        row = QHBoxLayout()
        row.setSpacing(8)
        row.addWidget(QLabel("bin"))
        row.addWidget(self.bin)
        row.addSpacing(12)
        row.addWidget(QLabel("target"))
        row.addWidget(self.target)
        row.addStretch(1)
        self.btn_auto.setMaximumWidth(72)
        row.addWidget(self.btn_auto)
        lay.addLayout(row)


class StatsPanel(QGroupBox):
    """Read-only statistics for the current gated image, refreshed on every gate
    change. The controller pushes a small ``{label: value}`` map via
    :meth:`set_stats` (4 rows: intensity-gate stats, or the τ-map stats in
    lifetime mode)."""

    _ROWS = 4

    def __init__(self):
        super().__init__("Stats")
        self._keys = [_muted("") for _ in range(self._ROWS)]
        self._vals = [QLabel("") for _ in range(self._ROWS)]
        form = _form()
        for k, v in zip(self._keys, self._vals):
            v.setWordWrap(True)
            form.addRow(k, v)
        lay = _col(self)
        lay.addLayout(form)
        self.set_stats({"": "load a file to see stats"})

    def set_stats(self, stats: dict) -> None:
        items = list(stats.items())
        for i in range(self._ROWS):
            has = i < len(items)
            self._keys[i].setVisible(has)
            self._vals[i].setVisible(has)
            if has:
                key, val = items[i]
                self._keys[i].setText(key)
                self._vals[i].setText(str(val))


class FilePanel(QGroupBox):
    def __init__(self):
        super().__init__("File / layer")
        self.z = SliderSpin(0, 0, 0)
        self.z.setToolTip("Step through z-planes of a numbered .ptu stack (PgUp/PgDn).")
        self.channel = QComboBox()
        self.channel.setToolTip("Detector channel (the primary channel A).")
        self.combine = QComboBox()
        self.combine.addItems(["single", "ratio A/B", "merge RGB"])
        self.combine.setToolTip("Combine two detector channels (intensity mode): single channel, "
                                "ratio A/B (e.g. FRET), or a red/green false-colour merge.")
        self.btn_open = QPushButton("Open .ptu…")
        self.btn_open_folder = QPushButton("Open folder…")
        self.btn_open_folder.setToolTip("Open a folder and load its .ptu stack (z-series).")
        self.btn_export = QPushButton("Export")
        self.btn_export.setProperty("accent", True)
        self.btn_export.setToolTip("Write TIFF + colormapped PNG + decay CSV + provenance JSON.")
        self.btn_save = QPushButton("Save settings")
        self.btn_load = QPushButton("Load settings")

        form = _form()
        form.addRow("z-slice", self.z)
        form.addRow("channel", self.channel)
        form.addRow("combine", self.combine)
        lay = _col(self)
        lay.addLayout(form)
        open_row = QHBoxLayout()
        open_row.setSpacing(6)
        open_row.addWidget(self.btn_open)
        open_row.addWidget(self.btn_open_folder)
        lay.addLayout(open_row)
        lay.addWidget(self.btn_export)
        row = QHBoxLayout()
        row.setSpacing(6)
        row.addWidget(self.btn_save)
        row.addWidget(self.btn_load)
        lay.addLayout(row)
