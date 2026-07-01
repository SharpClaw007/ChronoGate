"""The native-Qt control panels (one QGroupBox each), shown in the Controls dock.

Panels are *dumb views*: they build and expose their widgets as public
attributes, emit Qt signals on user edits, and offer setters the controller calls
to push state back. All analysis logic lives in ``controller.py``. Layouts are
kept compact so the six panels fit in two columns without scrolling.
"""

from __future__ import annotations

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
    """A horizontal QSlider linked to a QSpinBox (drag *or* type the value).

    Programmatic :meth:`setValue`/:meth:`setRange` are silent (no
    ``valueChanged``); only user interaction emits, so the controller can sync it
    without feedback loops.
    """

    valueChanged = Signal(float)

    def __init__(self, minimum: int = 0, maximum: int = 100, value: int = 0, suffix: str = ""):
        super().__init__()
        self.slider = QSlider(Qt.Horizontal)
        self.spin = QSpinBox()
        if suffix:
            self.spin.setSuffix(suffix)
        for w in (self.slider, self.spin):
            w.setRange(minimum, maximum)
            w.setValue(value)
        self.spin.setButtonSymbols(QSpinBox.NoButtons)
        self.spin.setMaximumWidth(78)
        self.slider.setMinimumWidth(60)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(6)
        lay.addWidget(self.slider, 1)
        lay.addWidget(self.spin)
        self.slider.valueChanged.connect(self._from_slider)
        self.spin.valueChanged.connect(self._from_spin)

    def _from_slider(self, v: int) -> None:
        if v != self.spin.value():
            with QSignalBlocker(self.spin):
                self.spin.setValue(v)
        self.valueChanged.emit(float(v))

    def _from_spin(self, v: int) -> None:
        if v != self.slider.value():
            with QSignalBlocker(self.slider):
                self.slider.setValue(v)
        self.valueChanged.emit(float(v))

    def setRange(self, lo, hi) -> None:
        lo, hi = int(lo), max(int(lo), int(hi))
        with QSignalBlocker(self.slider), QSignalBlocker(self.spin):
            self.slider.setRange(lo, hi)
            self.spin.setRange(lo, hi)

    def setValue(self, v) -> None:
        v = int(round(v))
        with QSignalBlocker(self.slider), QSignalBlocker(self.spin):
            self.slider.setValue(v)
            self.spin.setValue(v)

    def value(self) -> int:
        return self.spin.value()

    def maximum(self) -> int:
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
        self.active_label = _muted("single gate")
        form = _form()
        form.addRow("start", self.spin_lo)
        form.addRow("end", self.spin_hi)
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
        self.floor = SliderSpin(0, 100, 0, suffix=" cts")
        self.floor.setToolTip("Noise floor in counts/bin, subtracted from the gated intensity (× gate width).")
        self.cmap = QComboBox()
        self.cmap.addItems(INTENSITY_CMAPS)
        self.cmap.setToolTip("Colormap for the gated intensity image.")
        form = _form()
        form.addRow("min photons/px", self.thr)
        form.addRow("noise floor", self.floor)
        form.addRow("colormap", self.cmap)
        _col(self).addLayout(form)


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
        self.btn_clear = QPushButton("Clear")
        self.list = QListWidget()
        self.list.setToolTip("Picked pixels / regions and their photons-in-gate.")
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
        row.addStretch(1)
        lay.addLayout(row)
        lay.addWidget(self.list)
        self.btn_clear.setMaximumWidth(90)
        clear_row = QHBoxLayout()
        clear_row.addStretch(1)
        clear_row.addWidget(self.btn_clear)
        lay.addLayout(clear_row)

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


class IrfPanel(QGroupBox):
    """Instrument-response controls: load an IRF, view Sample/Instrument, subtract."""

    def __init__(self):
        super().__init__("IRF (instrument response)")
        self.btn_load = QPushButton("Load IRF…")
        self.btn_clear = QPushButton("Clear")
        self.btn_clear.setMaximumWidth(64)
        self.name_label = _muted("none loaded")
        self.channel = QComboBox()
        self.channel.setToolTip("Detector channel within the IRF file.")
        self.radio_sample = QRadioButton("sample")
        self.radio_instrument = QRadioButton("instrument")
        self.radio_sample.setChecked(True)
        self.radio_sample.setToolTip("Image the fluorescence after the IRF window.")
        self.radio_instrument.setToolTip("Image the photons inside the IRF (prompt) window.")
        self._grp = QButtonGroup(self)
        self._grp.addButton(self.radio_sample)
        self._grp.addButton(self.radio_instrument)
        self.subtract = QCheckBox("subtract scatter (approx)")
        self.subtract.setToolTip("Subtract an IRF-shaped fraction of the prompt-window signal per pixel.")
        self.scale = SliderSpin(0, 200, 100, suffix=" %")
        self.scale.setToolTip("Fraction of the prompt-window signal removed (100% = the whole window).")

        lay = _col(self)
        top = QHBoxLayout()
        top.setSpacing(6)
        top.addWidget(self.btn_load)
        top.addWidget(self.btn_clear)
        lay.addLayout(top)
        lay.addWidget(self.name_label)
        form = _form()
        form.addRow("IRF channel", self.channel)
        lay.addLayout(form)
        view = QHBoxLayout()
        view.setSpacing(12)
        view.addWidget(_muted("View:"))
        view.addWidget(self.radio_sample)
        view.addWidget(self.radio_instrument)
        view.addStretch(1)
        lay.addLayout(view)
        lay.addWidget(self.subtract)
        sform = _form()
        sform.addRow("scatter", self.scale)
        lay.addLayout(sform)
        self.set_irf_controls_enabled(False)

    def set_loaded_name(self, text: str) -> None:
        self.name_label.setText(text)

    def set_irf_controls_enabled(self, on: bool) -> None:
        for w in (self.btn_clear, self.channel, self.radio_sample, self.radio_instrument,
                  self.subtract, self.scale):
            w.setEnabled(on)


class FilePanel(QGroupBox):
    def __init__(self):
        super().__init__("File / layer")
        self.z = SliderSpin(0, 0, 0)
        self.z.setToolTip("Step through z-planes of a numbered .ptu stack (PgUp/PgDn).")
        self.channel = QComboBox()
        self.channel.setToolTip("Detector channel.")
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
