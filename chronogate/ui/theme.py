"""ChronoGate's visual identity: one source of truth for the light clinical theme.

Both layers read these colours so the app reads as a single system:

* **Qt widgets** -- via :func:`CLINICAL_QSS` (a stylesheet) and :func:`apply_qt_palette`.
* **The embedded matplotlib figures** -- via :func:`apply_matplotlib_theme`, which
  also makes exported PNGs inherit the same restrained, publication-friendly look
  (white facecolor, muted ticks, no top/right spines).

Palette: a near-white workspace, white panel cards, one blue accent for primary
actions, and the two gate colours (orange = early, green = late) reused from the
analysis so the controls and the plots agree.
"""

from __future__ import annotations

# --- palette ---------------------------------------------------------------
BG = "#F7F8FA"          # window background
PANEL = "#FFFFFF"       # cards / plot canvases
BORDER = "#E3E7EC"      # card borders
BORDER_HI = "#D7DCE3"   # control borders
TEXT = "#1F2933"        # primary text
MUTED = "#6B7280"       # secondary text / plot ticks
ACCENT = "#2563EB"      # primary actions, focus ring, decay line
ACCENT_HI = "#1D4FD7"   # accent pressed/hover
ACCENT_BG = "#EEF3FE"   # subtle accent fill (hover)
DISABLED = "#AEB6C0"    # text/controls in an inactive panel
GATE_A = "#E8833A"      # early gate
GATE_B = "#2FA84F"      # late gate
FLOOR = "#D1495B"       # noise-floor line (restrained alert tone)
GRID = "#EEF1F4"        # plot gridlines


def CLINICAL_QSS() -> str:
    """The Qt stylesheet implementing the light clinical theme."""
    return f"""
    QWidget {{ background: {BG}; color: {TEXT}; font-size: 12px; }}
    QMainWindow, QDialog {{ background: {BG}; }}
    QLabel {{ background: transparent; }}
    QLabel#Muted {{ color: {MUTED}; }}
    QLabel#Header {{ color: {TEXT}; font-weight: 600; }}
    QLabel#Welcome {{ color: {TEXT}; font-size: 26px; font-weight: 700; }}
    QLabel#WelcomeSub {{ color: {MUTED}; font-size: 13px; }}

    QGroupBox {{
        background: {PANEL}; border: 1px solid {BORDER}; border-radius: 8px;
        margin-top: 13px; padding: 6px 8px 6px 8px; font-weight: 600;
    }}
    QGroupBox::title {{
        subcontrol-origin: margin; subcontrol-position: top left;
        left: 10px; top: 2px; padding: 0 4px; color: {MUTED};
        font-size: 11px; font-weight: 700;
    }}
    QGroupBox:disabled {{ border-color: {BORDER}; }}
    QGroupBox:disabled::title {{ color: {DISABLED}; }}
    QFrame#Card {{ background: {PANEL}; border: 1px solid {BORDER}; border-radius: 8px; }}

    QLabel:disabled, QLabel#Muted:disabled {{ color: {DISABLED}; }}
    QRadioButton:disabled, QCheckBox:disabled {{ color: {DISABLED}; }}

    QPushButton {{
        background: {PANEL}; border: 1px solid {BORDER_HI}; border-radius: 6px;
        padding: 5px 12px; color: {TEXT};
    }}
    QPushButton:hover {{ background: {ACCENT_BG}; border-color: {ACCENT}; }}
    QPushButton:pressed {{ background: #E5EAF1; }}
    QPushButton:disabled {{ color: {MUTED}; background: {BG}; border-color: {BORDER}; }}
    QPushButton[accent="true"] {{
        background: {ACCENT}; border: 1px solid {ACCENT}; color: white; font-weight: 600;
    }}
    QPushButton[accent="true"]:hover {{ background: {ACCENT_HI}; border-color: {ACCENT_HI}; }}
    QPushButton[accent="true"]:pressed {{ background: {ACCENT_HI}; }}

    QSpinBox, QDoubleSpinBox, QComboBox {{
        background: {PANEL}; border: 1px solid {BORDER_HI}; border-radius: 6px;
        padding: 3px 6px; min-height: 20px; selection-background-color: {ACCENT};
        selection-color: white;
    }}
    QSpinBox:focus, QDoubleSpinBox:focus, QComboBox:focus {{ border: 1px solid {ACCENT}; }}
    QSpinBox:disabled, QDoubleSpinBox:disabled, QComboBox:disabled {{
        color: {DISABLED}; background: {BG}; border-color: {BORDER};
    }}
    QComboBox::drop-down {{ border: none; width: 18px; }}
    QComboBox QAbstractItemView {{
        background: {PANEL}; border: 1px solid {BORDER}; selection-background-color: {ACCENT};
        selection-color: white;
    }}

    QSlider::groove:horizontal {{ height: 4px; background: {BORDER}; border-radius: 2px; }}
    QSlider::sub-page:horizontal {{ background: {ACCENT}; border-radius: 2px; }}
    QSlider::handle:horizontal {{
        background: {ACCENT}; width: 14px; height: 14px; margin: -6px 0; border-radius: 7px;
    }}
    QSlider::handle:horizontal:hover {{ background: {ACCENT_HI}; }}
    QSlider:disabled {{ }}
    QSlider::sub-page:horizontal:disabled {{ background: {BORDER_HI}; }}
    QSlider::handle:horizontal:disabled {{ background: {BORDER_HI}; }}

    QCheckBox, QRadioButton {{ spacing: 6px; background: transparent; }}
    QCheckBox::indicator, QRadioButton::indicator {{ width: 15px; height: 15px; }}
    QCheckBox::indicator {{ border: 1px solid {BORDER_HI}; border-radius: 4px; background: {PANEL}; }}
    QRadioButton::indicator {{ border: 1px solid {BORDER_HI}; border-radius: 8px; background: {PANEL}; }}
    QCheckBox::indicator:checked, QRadioButton::indicator:checked {{
        background: {ACCENT}; border-color: {ACCENT};
    }}
    QCheckBox::indicator:disabled, QRadioButton::indicator:disabled {{ border-color: {BORDER}; }}

    QToolBar {{ background: {PANEL}; border-bottom: 1px solid {BORDER}; spacing: 4px; padding: 4px 6px; }}
    QToolBar::separator {{ background: {BORDER}; width: 1px; margin: 4px 6px; }}
    QToolButton {{ background: transparent; border: 1px solid transparent; border-radius: 6px; padding: 4px 9px; color: {TEXT}; }}
    QToolButton:hover {{ background: {ACCENT_BG}; border-color: {BORDER_HI}; }}
    QToolButton:checked {{ background: {ACCENT_BG}; border-color: {ACCENT}; color: {ACCENT}; }}

    QMenuBar {{ background: {PANEL}; border-bottom: 1px solid {BORDER}; }}
    QMenuBar::item {{ padding: 4px 10px; background: transparent; }}
    QMenuBar::item:selected {{ background: {ACCENT_BG}; border-radius: 4px; }}
    QMenu {{ background: {PANEL}; border: 1px solid {BORDER}; padding: 4px; }}
    QMenu::item {{ padding: 5px 24px 5px 12px; border-radius: 4px; }}
    QMenu::item:selected {{ background: {ACCENT}; color: white; }}
    QMenu::separator {{ height: 1px; background: {BORDER}; margin: 4px 6px; }}

    QStatusBar {{ background: {PANEL}; border-top: 1px solid {BORDER}; color: {MUTED}; }}
    QStatusBar::item {{ border: none; }}
    QDockWidget {{ color: {MUTED}; titlebar-close-icon: none; titlebar-normal-icon: none; }}
    QDockWidget::title {{
        background: {PANEL}; padding: 6px 10px; border-bottom: 1px solid {BORDER};
        color: {MUTED}; font-weight: 700;
    }}
    QTabBar::tab {{
        background: {BG}; color: {MUTED}; padding: 6px 12px; border: 1px solid {BORDER};
        border-bottom: none; border-top-left-radius: 6px; border-top-right-radius: 6px;
    }}
    QTabBar::tab:selected {{ background: {PANEL}; color: {TEXT}; }}
    QToolTip {{ background: {TEXT}; color: white; border: none; padding: 4px 7px; border-radius: 4px; }}
    QListWidget {{ background: {PANEL}; border: 1px solid {BORDER_HI}; border-radius: 6px; padding: 2px; }}
    QSplitter::handle {{ background: {BG}; }}
    QScrollArea {{ background: {BG}; border: none; }}
    """


def apply_qt_palette(app) -> None:
    """Set a QPalette matching the QSS, so Fusion-drawn chrome blends in."""
    from PySide6.QtGui import QPalette, QColor

    pal = QPalette()
    pal.setColor(QPalette.Window, QColor(BG))
    pal.setColor(QPalette.Base, QColor(PANEL))
    pal.setColor(QPalette.AlternateBase, QColor(BG))
    pal.setColor(QPalette.Text, QColor(TEXT))
    pal.setColor(QPalette.WindowText, QColor(TEXT))
    pal.setColor(QPalette.Button, QColor(PANEL))
    pal.setColor(QPalette.ButtonText, QColor(TEXT))
    pal.setColor(QPalette.Highlight, QColor(ACCENT))
    pal.setColor(QPalette.HighlightedText, QColor("#FFFFFF"))
    pal.setColor(QPalette.ToolTipBase, QColor(TEXT))
    pal.setColor(QPalette.ToolTipText, QColor("#FFFFFF"))
    pal.setColor(QPalette.PlaceholderText, QColor(MUTED))
    app.setPalette(pal)


def apply_matplotlib_theme() -> None:
    """Tune matplotlib rcParams so the embedded plots and exports match the UI.

    Call once, before any Figure is built. ``export.py`` constructs its own
    Figure at call time and so inherits these settings automatically -- exported
    PNGs come out on a white facecolor with the same fonts and muted ticks.
    """
    import matplotlib as mpl

    mpl.rcParams.update({
        "figure.facecolor": PANEL,
        "axes.facecolor": PANEL,
        "savefig.facecolor": PANEL,
        "savefig.edgecolor": PANEL,
        "axes.edgecolor": BORDER_HI,
        "axes.linewidth": 0.8,
        "axes.labelcolor": TEXT,
        "axes.titlecolor": TEXT,
        "axes.spines.top": False,
        "axes.spines.right": False,
        "xtick.color": MUTED,
        "ytick.color": MUTED,
        "xtick.labelcolor": TEXT,
        "ytick.labelcolor": TEXT,
        "text.color": TEXT,
        "axes.grid": True,
        "grid.color": GRID,
        "grid.linewidth": 0.8,
        "font.size": 9,
        "axes.titlesize": 9,
        "legend.fontsize": 7,
        "legend.framealpha": 0.85,
        "figure.dpi": 100,
    })
