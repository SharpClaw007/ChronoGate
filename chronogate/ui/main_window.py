"""The ChronoGate main window: native Qt chrome around the two embedded plots.

A central `QStackedWidget` flips between a **welcome** screen (shown until a file
is opened) and the **workspace** (a splitter with the decay canvas left and the
gated-image / lifetime-map canvas right). A right-side **Controls** dock holds the
six panel cards in two compact columns; a menubar + toolbar of shared `QAction`s
(with shortcuts and tooltips) and a status bar complete the chrome. All analysis
behaviour is delegated to `ViewerController`.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QAction, QActionGroup, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QDockWidget, QFrame, QHBoxLayout, QLabel, QMainWindow, QMessageBox,
    QProgressBar, QPushButton, QScrollArea, QSplitter, QStackedWidget,
    QVBoxLayout, QWidget,
)

from .. import metrics
from . import panels
from .controller import ViewerController
from .icon import app_icon
from .plot_canvas import DecayCanvas, ImageCanvas


def _card(widget: QWidget) -> QFrame:
    frame = QFrame()
    frame.setObjectName("Card")
    lay = QVBoxLayout(frame)
    lay.setContentsMargins(6, 6, 6, 6)
    lay.addWidget(widget)
    return frame


class WelcomeWidget(QWidget):
    """The empty-state landing screen: logomark + the two open actions."""

    def __init__(self, on_open_file, on_open_folder):
        super().__init__()
        outer = QVBoxLayout(self)
        outer.addStretch(2)
        logo = QLabel()
        logo.setPixmap(app_icon().pixmap(96, 96))
        logo.setAlignment(Qt.AlignCenter)
        title = QLabel("ChronoGate")
        title.setObjectName("Welcome")
        title.setAlignment(Qt.AlignCenter)
        sub = QLabel("Interactive time-gating & rapid-lifetime viewer for FLIM photon data")
        sub.setObjectName("WelcomeSub")
        sub.setAlignment(Qt.AlignCenter)

        btn_file = QPushButton("Open .ptu file…")
        btn_file.setProperty("accent", True)
        btn_file.clicked.connect(lambda: on_open_file())
        btn_folder = QPushButton("Open folder (stack)…")
        btn_folder.clicked.connect(lambda: on_open_folder())
        for b in (btn_file, btn_folder):
            b.setMinimumWidth(180)
        btn_row = QHBoxLayout()
        btn_row.addStretch(1)
        btn_row.addWidget(btn_file)
        btn_row.addWidget(btn_folder)
        btn_row.addStretch(1)

        for w in (logo, title, sub):
            outer.addWidget(w)
        outer.addSpacing(18)
        outer.addLayout(btn_row)
        outer.addStretch(3)


class MainWindow(QMainWindow):
    def __init__(self, path=None, channel: int = 0, sum_frames: bool = True, open_dir=None):
        super().__init__()
        self.setWindowIcon(app_icon())
        self.setWindowTitle("ChronoGate")
        self.resize(1500, 880)

        # --- control panels (built first; referenced by the controller) ---
        self.gate = panels.GatePanel()
        self.display = panels.DisplayPanel()
        self.lifetime = panels.LifetimePanel()
        self.picks = panels.PicksPanel()
        self.binning = panels.BinningPanel()
        self.filep = panels.FilePanel()
        self.stats = panels.StatsPanel()
        self.pixels = panels.PixelListPanel(metrics.metrics())

        self._build_pixel_dock()   # before the actions: one of them is its toggle
        self._build_actions()

        # --- workspace: plots (top) over a controls rack (bottom), both resizable ---
        self.decay_canvas = DecayCanvas()
        self.image_canvas = ImageCanvas()
        plots = QSplitter(Qt.Horizontal)
        plots.addWidget(_card(self.decay_canvas))     # decay -> wide / landscape
        plots.addWidget(_card(self.image_canvas))      # image -> near-square area
        plots.setStretchFactor(0, 3)
        plots.setStretchFactor(1, 2)
        plots.setSizes([940, 500])
        plots.setChildrenCollapsible(False)

        self._workspace = QSplitter(Qt.Vertical)
        self._workspace.addWidget(plots)
        self._workspace.addWidget(self._build_controls_rack())
        self._workspace.setStretchFactor(0, 3)
        self._workspace.setStretchFactor(1, 2)
        self._workspace.setSizes([420, 380])
        self._workspace.setChildrenCollapsible(False)

        self._welcome = WelcomeWidget(self.act_open.trigger, self.act_open_folder.trigger)
        self._stack = QStackedWidget()
        self._stack.addWidget(self._welcome)
        self._stack.addWidget(self._workspace)
        self.setCentralWidget(self._stack)

        self._build_menus()
        self._build_toolbar()
        self.header_label = QLabel("")
        self.header_label.setObjectName("Header")
        # Live readout for the pixel under the cursor. It lives in the status bar
        # because updating a QLabel is free, while repainting the plot is not --
        # so the probe can track the mouse at full rate.
        self.probe_label = QLabel("")
        self.probe_label.setObjectName("Header")
        self.progress = QProgressBar()
        self.progress.setMaximumWidth(200)
        self.progress.setVisible(False)
        self.statusBar().addWidget(self.progress)
        self.statusBar().addPermanentWidget(self.probe_label)
        self.statusBar().addPermanentWidget(self.header_label)

        # --- controller: owns the model, artists and all logic ---
        self.controller = ViewerController(
            self.decay_canvas, self.image_canvas,
            channel=channel, sum_frames=sum_frames, open_dir=open_dir)
        self.controller.statusMessage.connect(self.statusBar().showMessage)
        self.controller.headerChanged.connect(self.header_label.setText)
        self.controller.titleChanged.connect(self.setWindowTitle)

        self._wire_action_targets()
        self.controller.bind_view(self)   # shows the welcome state
        self._build_shortcuts()

        if path is not None:
            self.controller.load_path(path)

    # ------------------------------------------------------------------ build
    def _build_controls_rack(self) -> QScrollArea:
        """The bottom controls rack: the panels arranged in balanced columns."""
        container = QWidget()
        row = QHBoxLayout(container)
        row.setContentsMargins(8, 8, 8, 8)
        row.setSpacing(8)
        # Grouped into 4 columns, pairing tall panels with short ones so the
        # column heights stay even (no column overflows the rack).
        columns = [
            (self.gate, self.picks),
            (self.display, self.binning),
            (self.lifetime, self.stats),
            (self.filep,),
        ]
        for group in columns:
            col = QVBoxLayout()
            col.setSpacing(8)
            for p in group:
                col.addWidget(p)
            col.addStretch(1)
            row.addLayout(col, 1)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(container)
        scroll.setFrameShape(QFrame.NoFrame)
        self._controls_rack = scroll
        return scroll

    def _build_pixel_dock(self) -> QDockWidget:
        """The pixel list lives in a dock: a table needs vertical room the bottom
        controls rack does not have, and it is an occasional tool, not a constant one."""
        dock = QDockWidget("Pixel list", self)
        dock.setObjectName("PixelDock")
        dock.setAllowedAreas(Qt.LeftDockWidgetArea | Qt.RightDockWidgetArea)
        dock.setWidget(self.pixels)
        self.addDockWidget(Qt.RightDockWidgetArea, dock)
        # Floating by default: docked, the table steals ~350 px from the workspace and
        # forces the controls rack into a horizontal scrollbar. Drag it to an edge to
        # dock it if you would rather have it inline.
        dock.setFloating(True)
        dock.resize(560, 620)
        dock.hide()                       # opt-in (View ▸ Pixel list, or Ctrl+P)
        self.pixel_dock = dock
        return dock

    def _build_actions(self) -> None:
        self.act_open = QAction("&Open .ptu…", self, shortcut=QKeySequence.Open)
        self.act_open_folder = QAction("Open &folder (stack)…", self,
                                       shortcut=QKeySequence("Ctrl+Shift+O"))
        self.act_export = QAction("&Export", self, shortcut=QKeySequence("Ctrl+E"))
        self.act_batch = QAction("Export &all planes (batch)…", self,
                                 shortcut=QKeySequence("Ctrl+Shift+E"))
        self.act_save = QAction("&Save settings", self, shortcut=QKeySequence("Ctrl+S"))
        self.act_load = QAction("&Load settings", self, shortcut=QKeySequence("Ctrl+L"))
        self.act_quit = QAction("&Quit", self, shortcut=QKeySequence.Quit)

        self.act_intensity = QAction("&Intensity", self, checkable=True, shortcut=QKeySequence("I"))
        self.act_lifetime = QAction("Life&time (RLD)", self, checkable=True, shortcut=QKeySequence("T"))
        self.act_phasor = QAction("&Phasor", self, checkable=True, shortcut=QKeySequence("P"))
        self.act_intensity.setChecked(True)
        grp = QActionGroup(self)
        grp.setExclusive(True)
        grp.addAction(self.act_intensity)
        grp.addAction(self.act_lifetime)
        grp.addAction(self.act_phasor)

        self.act_log = QAction("&Log Y axis", self, checkable=True, shortcut=QKeySequence("L"))
        self.act_log.setChecked(True)
        self.act_floor = QAction("Subtract noise &floor", self, checkable=True, shortcut=QKeySequence("F"))
        self.act_floor.setChecked(True)
        self.act_pixels = self.pixel_dock.toggleViewAction()
        self.act_pixels.setText("Pi&xel list")
        self.act_pixels.setShortcut(QKeySequence("Ctrl+P"))
        self.act_pixels.setToolTip("A ranked, filterable table of individual pixels")

        self.act_phasor_cal = QAction("&Calibrate phasor from reference…", self)
        self.act_phasor_cal.setToolTip(
            "Make the phasor quantitative: map the median phasor of the current "
            "selection (or the whole image) onto a known reference lifetime.")
        self.act_phasor_cal_clear = QAction("Clear phasor calibration", self)
        self.act_harmonic2 = QAction("Phasor 2nd &harmonic", self, checkable=True)
        self.act_harmonic2.setToolTip(
            "Compute the phasor at twice the laser frequency (ω₂ = 2ω): spreads "
            "short lifetimes apart and helps disambiguate multi-component mixtures. "
            "Each harmonic keeps its own reference calibration.")

        self.act_undo_pick = QAction("&Undo selection change", self,
                                     shortcut=QKeySequence.Undo)
        self.act_undo_pick.setToolTip(
            "Restore the previous selection -- a stray click must not cost a lasso.")

        self.act_about = QAction("&About ChronoGate", self)
        self.act_open.setToolTip("Open a .ptu file or stack layer")
        self.act_open_folder.setToolTip("Open a folder and load its .ptu stack")
        self.act_export.setToolTip("Export the current view (TIFF + PNG + CSV + provenance)")

    def _build_menus(self) -> None:
        mb = self.menuBar()
        m_file = mb.addMenu("&File")
        m_file.addAction(self.act_open)
        m_file.addAction(self.act_open_folder)
        m_file.addAction(self.act_export)
        m_file.addAction(self.act_batch)
        m_file.addSeparator()
        m_file.addAction(self.act_save)
        m_file.addAction(self.act_load)
        m_file.addSeparator()
        m_file.addAction(self.act_quit)

        m_view = mb.addMenu("&View")
        m_view.addAction(self.act_intensity)
        m_view.addAction(self.act_lifetime)
        m_view.addAction(self.act_phasor)
        m_view.addAction(self.act_harmonic2)
        m_view.addAction(self.act_phasor_cal)
        m_view.addAction(self.act_phasor_cal_clear)
        m_view.addSeparator()
        m_view.addAction(self.act_pixels)
        m_view.addAction(self.act_undo_pick)
        m_view.addSeparator()
        m_view.addAction(self.act_log)
        m_view.addAction(self.act_floor)

        m_help = mb.addMenu("&Help")
        m_help.addAction(self.act_about)

    def _build_toolbar(self) -> None:
        tb = self.addToolBar("Main")
        self._toolbar = tb
        tb.setObjectName("MainToolbar")
        tb.setMovable(False)
        tb.addAction(self.act_open)
        tb.addAction(self.act_open_folder)
        tb.addAction(self.act_export)
        tb.addSeparator()
        tb.addAction(self.act_intensity)
        tb.addAction(self.act_lifetime)
        tb.addAction(self.act_phasor)
        tb.addSeparator()
        tb.addAction(self.act_pixels)
        tb.addAction(self.act_log)

    def _wire_action_targets(self) -> None:
        # File actions reuse the panel buttons' wiring (connected in bind_view).
        self.act_open.triggered.connect(self.filep.btn_open.click)
        self.act_open_folder.triggered.connect(self.filep.btn_open_folder.click)
        self.act_export.triggered.connect(self.filep.btn_export.click)
        self.act_batch.triggered.connect(self.controller._on_batch_export)
        self.act_phasor_cal.triggered.connect(self.controller._on_phasor_calibrate)
        self.act_phasor_cal_clear.triggered.connect(self.controller.clear_phasor_calibration)
        self.act_harmonic2.toggled.connect(self.controller._on_harmonic2)
        self.act_undo_pick.triggered.connect(self.controller.undo_pick)
        self.act_save.triggered.connect(self.filep.btn_save.click)
        self.act_load.triggered.connect(self.filep.btn_load.click)
        self.act_quit.triggered.connect(self.close)
        self.act_about.triggered.connect(self._show_about)

    @staticmethod
    def _scoped(key: str, widget: QWidget, slot) -> QShortcut:
        """A shortcut that only fires while ``widget`` (or a child) has focus.

        Arrow keys mean different things depending on which plot you are working
        in -- step a pixel on the image, nudge the gate on the decay -- and they
        must not be swallowed window-wide, or every spin box would lose its
        Up/Down. Scoping each binding to its canvas keeps both true and avoids an
        ambiguous-shortcut overload.
        """
        sc = QShortcut(QKeySequence(key), widget, activated=slot)
        sc.setContext(Qt.WidgetWithChildrenShortcut)
        return sc

    def _build_shortcuts(self) -> None:
        c = self.controller
        QShortcut(QKeySequence("C"), self, activated=self.picks.btn_clear.click)
        QShortcut(QKeySequence(Qt.Key_PageUp), self, activated=lambda: c.step_z(1))
        QShortcut(QKeySequence(Qt.Key_PageDown), self, activated=lambda: c.step_z(-1))

        # Image canvas: the arrow keys are a pixel cursor (Shift = 10-px strides).
        img = self.image_canvas
        for key, (dr, dc) in {
            "Left": (0, -1), "Right": (0, 1), "Up": (-1, 0), "Down": (1, 0),
            "Shift+Left": (0, -10), "Shift+Right": (0, 10),
            "Shift+Up": (-10, 0), "Shift+Down": (10, 0),
        }.items():
            self._scoped(key, img, lambda dr=dr, dc=dc: c.nudge_pixel(dr, dc))

        # Decay canvas: the arrow keys move/resize the active gate.
        dec = self.decay_canvas
        self._scoped("Left", dec, lambda: c.nudge_gate(-1, -1))
        self._scoped("Right", dec, lambda: c.nudge_gate(1, 1))
        self._scoped("Shift+Left", dec, lambda: c.nudge_gate(0, -1))
        self._scoped("Shift+Right", dec, lambda: c.nudge_gate(0, 1))

        # ...and from anywhere in the window, with Alt held.
        QShortcut(QKeySequence("Alt+Left"), self, activated=lambda: c.nudge_gate(-1, -1))
        QShortcut(QKeySequence("Alt+Right"), self, activated=lambda: c.nudge_gate(1, 1))
        QShortcut(QKeySequence("Alt+Shift+Left"), self, activated=lambda: c.nudge_gate(0, -1))
        QShortcut(QKeySequence("Alt+Shift+Right"), self, activated=lambda: c.nudge_gate(0, 1))

    def closeEvent(self, event) -> None:
        self.controller.stop_decode()   # never leave a QThread running at exit
        super().closeEvent(event)

    # ------------------------------------------------------------- view hooks
    def set_busy(self, on: bool, name: str = "") -> None:
        """Disable interaction and show the progress bar during a background
        decode, so the window stays responsive without letting a second load
        start on top of the first."""
        self._controls_rack.setEnabled(not on)
        self._toolbar.setEnabled(not on)
        self.progress.setVisible(on)
        if on:
            self.progress.setRange(0, 0)   # indeterminate until the first frame
            self.statusBar().showMessage(f"Loading {name}…")
        else:
            self.progress.setRange(0, 1)
            self.progress.reset()
            self.statusBar().clearMessage()

    def set_progress(self, done: int, total: int) -> None:
        self.progress.setRange(0, total)
        self.progress.setValue(done)

    def set_probe(self, text: str) -> None:
        """The hovered pixel's coordinates and counts (empty when off the image)."""
        self.probe_label.setText(text)

    def set_loaded(self, loaded: bool) -> None:
        """Switch between the welcome screen and the workspace.

        When no file is loaded, the controls dock is hidden and the analysis
        actions are disabled -- only the Open actions stay live.
        """
        self._stack.setCurrentWidget(self._workspace if loaded else self._welcome)
        for a in (self.act_export, self.act_batch, self.act_save, self.act_load,
                  self.act_intensity, self.act_lifetime, self.act_phasor, self.act_log,
                  self.act_floor, self.act_pixels, self.act_phasor_cal,
                  self.act_phasor_cal_clear, self.act_harmonic2, self.act_undo_pick):
            a.setEnabled(loaded)
        if not loaded:
            self.pixel_dock.hide()

    def set_lifetime_enabled(self, enabled: bool) -> None:
        """Enable the Lifetime panel only in lifetime mode (mirrors the old gating)."""
        self.lifetime.setEnabled(enabled)

    def _show_about(self) -> None:
        from .. import __version__
        box = QMessageBox(self)
        box.setWindowTitle("About ChronoGate")
        box.setIconPixmap(app_icon().pixmap(64, 64))
        box.setText(f"<b>ChronoGate</b> {__version__}")
        box.setInformativeText(
            "Interactive time-gating & rapid-lifetime viewer for FLIM photon data.\n\n"
            "MIT licensed · built on numpy, matplotlib, ptufile, tifffile, PySide6.")
        box.exec()
