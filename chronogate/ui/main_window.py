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
    QFrame, QHBoxLayout, QLabel, QMainWindow, QMessageBox, QPushButton,
    QScrollArea, QSplitter, QStackedWidget, QVBoxLayout, QWidget,
)

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
        self.irf = panels.IrfPanel()

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
            (self.lifetime, self.irf),
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
        return scroll

    def _build_actions(self) -> None:
        self.act_open = QAction("&Open .ptu…", self, shortcut=QKeySequence.Open)
        self.act_open_folder = QAction("Open &folder (stack)…", self,
                                       shortcut=QKeySequence("Ctrl+Shift+O"))
        self.act_load_irf = QAction("Load &IRF…", self)
        self.act_export = QAction("&Export", self, shortcut=QKeySequence("Ctrl+E"))
        self.act_save = QAction("&Save settings", self, shortcut=QKeySequence("Ctrl+S"))
        self.act_load = QAction("&Load settings", self, shortcut=QKeySequence("Ctrl+L"))
        self.act_quit = QAction("&Quit", self, shortcut=QKeySequence.Quit)

        self.act_intensity = QAction("&Intensity", self, checkable=True, shortcut=QKeySequence("I"))
        self.act_lifetime = QAction("Life&time (RLD)", self, checkable=True, shortcut=QKeySequence("T"))
        self.act_intensity.setChecked(True)
        grp = QActionGroup(self)
        grp.setExclusive(True)
        grp.addAction(self.act_intensity)
        grp.addAction(self.act_lifetime)

        self.act_log = QAction("&Log Y axis", self, checkable=True, shortcut=QKeySequence("L"))
        self.act_log.setChecked(True)
        self.act_floor = QAction("Subtract noise &floor", self, checkable=True, shortcut=QKeySequence("F"))
        self.act_floor.setChecked(True)

        self.act_about = QAction("&About ChronoGate", self)
        self.act_open.setToolTip("Open a .ptu file or stack layer")
        self.act_open_folder.setToolTip("Open a folder and load its .ptu stack")
        self.act_export.setToolTip("Export the current view (TIFF + PNG + CSV + provenance)")

    def _build_menus(self) -> None:
        mb = self.menuBar()
        m_file = mb.addMenu("&File")
        m_file.addAction(self.act_open)
        m_file.addAction(self.act_open_folder)
        m_file.addAction(self.act_load_irf)
        m_file.addAction(self.act_export)
        m_file.addSeparator()
        m_file.addAction(self.act_save)
        m_file.addAction(self.act_load)
        m_file.addSeparator()
        m_file.addAction(self.act_quit)

        m_view = mb.addMenu("&View")
        m_view.addAction(self.act_intensity)
        m_view.addAction(self.act_lifetime)
        m_view.addSeparator()
        m_view.addAction(self.act_log)
        m_view.addAction(self.act_floor)

        m_help = mb.addMenu("&Help")
        m_help.addAction(self.act_about)

    def _build_toolbar(self) -> None:
        tb = self.addToolBar("Main")
        tb.setObjectName("MainToolbar")
        tb.setMovable(False)
        tb.addAction(self.act_open)
        tb.addAction(self.act_open_folder)
        tb.addAction(self.act_load_irf)
        tb.addAction(self.act_export)
        tb.addSeparator()
        tb.addAction(self.act_intensity)
        tb.addAction(self.act_lifetime)
        tb.addSeparator()
        tb.addAction(self.act_log)

    def _wire_action_targets(self) -> None:
        # File actions reuse the panel buttons' wiring (connected in bind_view).
        self.act_open.triggered.connect(self.filep.btn_open.click)
        self.act_open_folder.triggered.connect(self.filep.btn_open_folder.click)
        self.act_load_irf.triggered.connect(self.irf.btn_load.click)
        self.act_export.triggered.connect(self.filep.btn_export.click)
        self.act_save.triggered.connect(self.filep.btn_save.click)
        self.act_load.triggered.connect(self.filep.btn_load.click)
        self.act_quit.triggered.connect(self.close)
        self.act_about.triggered.connect(self._show_about)

    def _build_shortcuts(self) -> None:
        c = self.controller
        QShortcut(QKeySequence("C"), self, activated=self.picks.btn_clear.click)
        QShortcut(QKeySequence(Qt.Key_Left), self, activated=lambda: c.nudge_gate(-1, -1))
        QShortcut(QKeySequence(Qt.Key_Right), self, activated=lambda: c.nudge_gate(1, 1))
        QShortcut(QKeySequence("Shift+Left"), self, activated=lambda: c.nudge_gate(0, -1))
        QShortcut(QKeySequence("Shift+Right"), self, activated=lambda: c.nudge_gate(0, 1))
        QShortcut(QKeySequence(Qt.Key_PageUp), self, activated=lambda: c.step_z(1))
        QShortcut(QKeySequence(Qt.Key_PageDown), self, activated=lambda: c.step_z(-1))

    # ------------------------------------------------------------- view hooks
    def set_loaded(self, loaded: bool) -> None:
        """Switch between the welcome screen and the workspace.

        When no file is loaded, the controls dock is hidden and the analysis
        actions are disabled -- only the Open actions stay live.
        """
        self._stack.setCurrentWidget(self._workspace if loaded else self._welcome)
        for a in (self.act_export, self.act_save, self.act_load, self.act_load_irf,
                  self.act_intensity, self.act_lifetime, self.act_log, self.act_floor):
            a.setEnabled(loaded)

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
