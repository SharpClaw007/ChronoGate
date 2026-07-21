"""The native-Qt control panels (one QGroupBox each), shown in the Controls dock.

Panels are *dumb views*: they build and expose their widgets as public
attributes, emit Qt signals on user edits, and offer setters the controller calls
to push state back. All analysis logic lives in ``controller.py``. Layouts are
kept compact so the six panels fit in two columns without scrolling.
"""

from __future__ import annotations

import math

from PySide6.QtCore import (
    QItemSelection, QItemSelectionModel, Qt, QTimer, Signal, QSignalBlocker,
)
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QButtonGroup, QCheckBox, QComboBox, QDoubleSpinBox, QFormLayout, QGroupBox,
    QHBoxLayout, QLabel, QListWidget, QListWidgetItem, QPushButton, QRadioButton,
    QSlider, QSpinBox, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
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

    def minimum(self):
        return self.spin.minimum()


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
        self.btn_t0_reset = QPushButton("↺")
        self.btn_t0_reset.setMaximumWidth(28)
        self.btn_t0_reset.setToolTip("Reset t0 to the default (0 ns, the start of the window).")
        self.active_label = _muted("single gate")
        form = _form()
        form.addRow("start", self.spin_lo)
        form.addRow("end", self.spin_hi)
        t0row = QHBoxLayout()
        t0row.setSpacing(6)
        t0row.addWidget(self.t0, 1)
        t0row.addWidget(self.btn_t0_auto)
        t0row.addWidget(self.btn_t0_reset)
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
        self.btn_floor_auto = QPushButton("auto")
        self.btn_floor_auto.setMaximumWidth(52)
        self.btn_floor_auto.setToolTip("Reset the noise floor to the auto value "
                                       "(just above the flat pre-pulse baseline).")
        self.btn_floor_reset = QPushButton("↺")
        self.btn_floor_reset.setMaximumWidth(28)
        self.btn_floor_reset.setToolTip("Reset the noise floor to the default (0 — no subtraction).")
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
        floor_row = QHBoxLayout()
        floor_row.setSpacing(6)
        floor_row.addWidget(self.floor, 1)
        floor_row.addWidget(self.btn_floor_auto)
        floor_row.addWidget(self.btn_floor_reset)
        form.addRow("noise floor", floor_row)
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
    """Pixel inspection: hover to preview, click to lock, arrows to step, or type
    exact coordinates. The list shows the locked/pinned picks and their counts."""

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
        self.hover = QCheckBox("hover")
        self.hover.setChecked(True)
        self.hover.setToolTip("Preview the decay of the pixel under the cursor as you move "
                              "over the image (no click). Click to lock it in.")

        # Exact coordinate entry -- a 512×512 image can't be clicked pixel-accurately.
        self.row = QSpinBox()
        self.col = QSpinBox()
        for s in (self.row, self.col):
            s.setRange(0, 0)
            s.setMaximumWidth(66)
            s.setToolTip("Jump to an exact pixel. Arrow keys step the selected pixel "
                         "on the image (Shift = ×10).")
        self.btn_go = QPushButton("Go")
        self.btn_go.setMaximumWidth(44)
        self.btn_go.setToolTip("Select the pixel at (row, col).")

        self.btn_pin = QPushButton("Pin")
        self.btn_pin.setToolTip("Freeze the current decay so the next click can be compared against it.")
        self.btn_clear = QPushButton("Clear")
        self.list = QListWidget()
        self.list.setToolTip("Picked pixels / regions (📌 = pinned) and their photons-in-gate.")
        self.list.setMaximumHeight(76)

        lay = _col(self)
        lay.addWidget(_muted("Hover to preview · click to lock · drag a box for an ROI · "
                             "arrows step (Shift ×10)."))
        row = QHBoxLayout()
        row.setSpacing(8)
        row.addWidget(QLabel("avg N×N"))
        row.addWidget(self.avg)
        row.addSpacing(10)
        row.addWidget(QLabel("smooth"))
        row.addWidget(self.smooth)
        row.addStretch(1)
        row.addWidget(self.fit)
        row.addWidget(self.hover)
        lay.addLayout(row)
        # The jump-to and the pin/clear actions share one row: the panel has to fit
        # its column in the controls rack without a scrollbar.
        self.btn_pin.setMaximumWidth(58)
        self.btn_clear.setMaximumWidth(58)
        go_row = QHBoxLayout()
        go_row.setSpacing(6)
        go_row.addWidget(QLabel("row"))
        go_row.addWidget(self.row)
        go_row.addWidget(QLabel("col"))
        go_row.addWidget(self.col)
        go_row.addWidget(self.btn_go)
        go_row.addStretch(1)
        go_row.addWidget(self.btn_pin)
        go_row.addWidget(self.btn_clear)
        lay.addLayout(go_row)
        lay.addWidget(self.list)

    def set_coords(self, r: int, c: int) -> None:
        """Push the selected pixel into the row/col boxes (no signal)."""
        with QSignalBlocker(self.row), QSignalBlocker(self.col):
            self.row.setValue(int(r))
            self.col.setValue(int(c))

    def set_items(self, items) -> None:
        self.list.clear()
        for text, color in items:
            it = QListWidgetItem(text)
            it.setForeground(QColor(color))
            self.list.addItem(it)


class PixelListPanel(QWidget):
    """A ranked, filterable table of individual pixels.

    262,144 rows is not a list anyone can use, so this is never a raw dump: pick a
    metric, bound it, and see the top N pixels by it. Selecting a row selects that
    pixel (its decay, crosshair and readout follow), and the arrow keys walk the
    ranking -- which is the point: stepping through the *brightest* or
    *longest-lived* pixels, not through arbitrary coordinates.

    The columns come from :mod:`chronogate.metrics`, so a new metric registered
    there appears here with no change to this file.
    """

    pixelsChosen = Signal(list)         # [(row, col), ...] -- may be many
    refreshRequested = Signal()

    def __init__(self, metrics: list):
        super().__init__()
        self._metrics = metrics
        self._rows: list[tuple[int, int]] = []
        # A rubber-band drag over the table emits a selection change per row it
        # crosses; coalesce them so one drag costs one refresh, not two hundred.
        self._sel_timer = QTimer(self)
        self._sel_timer.setSingleShot(True)
        self._sel_timer.setInterval(60)
        self._sel_timer.timeout.connect(self._emit_selection)

        # NOT self.metric: QWidget has a virtual metric() (DPI queries), and
        # PySide6 routes C++ virtual calls through Python attribute lookup — a
        # QComboBox stored under that name crashes the first devicePixelRatio
        # probe (e.g. the dock going floating on some display configs).
        self.metric_box = QComboBox()
        for m in metrics:
            self.metric_box.addItem(m.label, m.key)
        self.metric_box.setToolTip("Rank and filter the pixels by this quantity.")
        self.desc = QCheckBox("high→low")
        self.desc.setChecked(True)
        self.desc.setToolTip("Sort direction.")
        self.fmin = QDoubleSpinBox()
        self.fmax = QDoubleSpinBox()
        for s in (self.fmin, self.fmax):
            s.setDecimals(3)
            s.setRange(-1e12, 1e12)
            s.setButtonSymbols(QDoubleSpinBox.NoButtons)
            s.setMaximumWidth(90)
            s.setToolTip("Keep only pixels whose value falls in this range "
                         "(defaults to the metric's full range = no filter).")
        self.limit = QSpinBox()
        self.limit.setRange(1, 20000)
        self.limit.setValue(200)
        self.limit.setMaximumWidth(74)
        self.limit.setToolTip("How many top-ranked pixels to list.")
        self.btn_refresh = QPushButton("Refresh")
        self.btn_refresh.setToolTip("Recompute the ranking for the current gate.")

        self.table = QTableWidget(0, 0)
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        # Finder/Explorer-style multi-select: Ctrl (Cmd on macOS) toggles a row,
        # Shift extends a range, Ctrl+A takes the lot. Qt maps the platform's
        # modifiers for us. Several rows = one pooled group of pixels.
        self.table.setSelectionMode(QTableWidget.ExtendedSelection)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        self.table.setAlternatingRowColors(True)
        self.table.setToolTip(
            "Click a row (or walk it with ↑/↓) to select that pixel.\n"
            "Ctrl/⌘-click to add one, Shift-click for a range, Ctrl+A for all — "
            "several rows are pooled into one group (combined decay, all highlighted).")
        self.table.itemSelectionChanged.connect(self._sel_timer.start)

        self.summary = _muted("")

        lay = QVBoxLayout(self)
        lay.setContentsMargins(8, 8, 8, 8)
        lay.setSpacing(6)
        top = QHBoxLayout()
        top.setSpacing(6)
        top.addWidget(QLabel("rank by"))
        top.addWidget(self.metric_box, 1)
        top.addWidget(self.desc)
        lay.addLayout(top)
        rng = QHBoxLayout()
        rng.setSpacing(6)
        rng.addWidget(QLabel("from"))
        rng.addWidget(self.fmin)
        rng.addWidget(QLabel("to"))
        rng.addWidget(self.fmax)
        rng.addSpacing(8)
        rng.addWidget(QLabel("top"))
        rng.addWidget(self.limit)
        rng.addWidget(self.btn_refresh)
        rng.addStretch(1)
        lay.addLayout(rng)
        lay.addWidget(self.table, 1)
        lay.addWidget(self.summary)

        self.btn_refresh.clicked.connect(self.refreshRequested)

    def current_metric(self) -> str:
        return self.metric_box.currentData()

    def set_filter_bounds(self, lo: float, hi: float) -> None:
        """Reset the range boxes to a metric's full span (i.e. filter nothing).

        Rounded **outward** to the boxes' displayed precision: a spin box holding
        48.947 for a true maximum of 48.9474 would otherwise quietly filter out the
        very brightest pixel -- the one you opened the list to find.
        """
        step = 10.0 ** -self.fmin.decimals()
        with QSignalBlocker(self.fmin), QSignalBlocker(self.fmax):
            self.fmin.setValue(math.floor(lo / step) * step)
            self.fmax.setValue(math.ceil(hi / step) * step)

    def set_table(self, table, metric_by_key: dict) -> None:
        """Render a :class:`chronogate.metrics.PixelTable`."""
        self._rows = list(table.rows)
        headers = ["row", "col"] + [metric_by_key[k].label for k in table.keys]
        self.table.clear()
        self.table.setColumnCount(len(headers))
        self.table.setRowCount(len(table.rows))
        self.table.setHorizontalHeaderLabels(headers)
        with QSignalBlocker(self.table):
            for i, (r, c) in enumerate(table.rows):
                self.table.setItem(i, 0, QTableWidgetItem(str(r)))
                self.table.setItem(i, 1, QTableWidgetItem(str(c)))
                for j, key in enumerate(table.keys):
                    text = metric_by_key[key].format(float(table.values[key][i]))
                    item = QTableWidgetItem(text)
                    item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
                    self.table.setItem(i, 2 + j, item)
        self.table.resizeColumnsToContents()
        shown = len(table.rows)
        if table.n_matched == 0:
            # Say why it is empty. "0 of 0" on its own reads like a bug.
            label = metric_by_key[table.sort_key].label
            self.summary.setText(
                f"No pixel has a usable “{label}” in this range — widen the from/to "
                f"bounds, or pool photons (Binning ▸ Auto) if the value is undefined.")
            return
        note = f"{shown:,} shown of {table.n_matched:,} matched · {table.n_total:,} px"
        if table.truncated:
            note += f"  (truncated to the top {shown:,})"
        self.summary.setText(note)

    def select_matching(self, is_selected) -> int:
        """Highlight every listed row whose pixel satisfies ``is_selected(r, c)``.

        Used to *reflect* the current pick back into the table (after a rebuild, or
        when the pixel was chosen on the image), so it must not emit -- otherwise
        showing a selection would immediately re-apply it.
        """
        self._sel_timer.stop()
        sm = self.table.selectionModel()
        if sm is None:
            return 0
        model, last = self.table.model(), self.table.columnCount() - 1
        chosen = QItemSelection()
        first, n = None, 0
        for i, (r, c) in enumerate(self._rows):
            if is_selected(r, c):
                chosen.select(model.index(i, 0), model.index(i, max(0, last)))
                n += 1
                if first is None:
                    first = i
        with QSignalBlocker(self.table), QSignalBlocker(sm):
            sm.select(chosen, QItemSelectionModel.ClearAndSelect | QItemSelectionModel.Rows)
        if first is not None:
            self.table.scrollToItem(self.table.item(first, 0))
        return n

    def select_pixel(self, r: int, c: int) -> bool:
        """Highlight the row for pixel (r, c) if it is listed. No signal."""
        return self.select_matching(lambda rr, cc: (rr, cc) == (int(r), int(c))) > 0

    def selected_pixels(self) -> list[tuple[int, int]]:
        model = self.table.selectionModel()
        if model is None:
            return []
        return [self._rows[i.row()] for i in sorted(model.selectedRows(), key=lambda x: x.row())
                if 0 <= i.row() < len(self._rows)]

    def _emit_selection(self) -> None:
        picked = self.selected_pixels()
        if picked:
            self.pixelsChosen.emit(picked)


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
        self.btn_bin_reset = QPushButton("↺")
        self.btn_bin_reset.setMaximumWidth(28)
        self.btn_bin_reset.setToolTip("Reset binning to the default (1×, off).")

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
        row.addWidget(self.btn_bin_reset)
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
        self.btn_export.setToolTip("Choose what to export (TIFF / PNG / CSV / selection), "
                                   "any parameters, and the output folder.")
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
