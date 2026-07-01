"""The ChronoGate app/window icon.

A small logomark: a fluorescence-decay stroke (accent blue) sweeping under two
translucent gate bands (early orange, late green) -- literally "a gate on a
decay". Loaded from the shipped SVG, with a hand-painted ``QPixmap`` fallback so
the icon still appears if the Qt SVG image plugin is unavailable.
"""

from __future__ import annotations

from pathlib import Path

_SVG = Path(__file__).resolve().parent / "assets" / "chronogate.svg"


def app_icon():
    """Return a :class:`QIcon` for the application and main window."""
    from PySide6.QtGui import QIcon

    if _SVG.exists():
        icon = QIcon(str(_SVG))
        if not icon.isNull():
            return icon
    return _painted_icon()


def _painted_icon():
    from PySide6.QtCore import Qt, QRectF
    from PySide6.QtGui import QIcon, QPixmap, QPainter, QColor, QPen, QPainterPath

    from . import theme

    pm = QPixmap(64, 64)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)

    # Rounded card.
    p.setBrush(QColor(theme.PANEL))
    p.setPen(QPen(QColor(theme.BORDER), 2))
    p.drawRoundedRect(QRectF(2, 2, 60, 60), 14, 14)

    # Gate bands.
    p.setPen(Qt.NoPen)
    p.setBrush(QColor(232, 131, 58, 56))   # gate A, ~22% alpha
    p.drawRect(19, 12, 12, 38)
    p.setBrush(QColor(47, 168, 79, 46))    # gate B, ~18% alpha
    p.drawRect(31, 12, 12, 38)

    # Decay curve.
    path = QPainterPath()
    path.moveTo(10, 15)
    path.cubicTo(19, 15, 21, 47, 54, 49)
    p.setBrush(Qt.NoBrush)
    p.setPen(QPen(QColor(theme.ACCENT), 4, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
    p.drawPath(path)
    p.end()

    return QIcon(pm)
