"""Screenshot annotation engine for the ChronoGate SOP.

Draws SOP-style callouts on a captured Qt widget: highlight boxes around named
controls plus numbered badges, and optional arrows. Annotations target *live
widgets* -- their positions are read from the widget geometry at capture time --
so when the UI layout changes, re-running the capture re-annotates correctly
without hand-editing pixel coordinates. Pure PySide6 (QPainter); no extra deps.
"""

from __future__ import annotations

from PySide6.QtCore import QPoint, QRect, Qt
from PySide6.QtGui import QBrush, QColor, QFont, QPainter, QPen, QPixmap, QPolygon

# Match the app's palette so the SOP looks of-a-piece with the tool.
_ACCENT = QColor("#2563EB")   # highlight box
_BADGE = QColor("#F0189B")    # numbered badge (the app's SELECT pink)
_WHITE = QColor("#FFFFFF")


def rect_of(child, root) -> QRect:
    """The rectangle of ``child`` in the coordinate space of ``root`` (the grabbed
    widget), so a highlight box lands exactly on the control."""
    top_left = child.mapTo(root, QPoint(0, 0))
    return QRect(top_left, child.size())


def grab(widget) -> QPixmap:
    """Render a widget to a pixmap (offscreen-safe)."""
    return widget.grab()


def annotate(pixmap: QPixmap, marks, pad: int = 5) -> QPixmap:
    """Return a copy of ``pixmap`` with ``marks`` drawn on top.

    Each mark is a dict:
      * ``rect``  -- QRect to highlight (usually ``rect_of(child, root)``)
      * ``n``     -- optional badge number
      * ``box``   -- draw the highlight rectangle (default True)
      * ``badge`` -- badge corner: "tl" (default), "tr", "bl", "br"
    """
    pm = pixmap.copy()
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    for m in marks:
        r = m["rect"].adjusted(-pad, -pad, pad, pad)
        if m.get("box", True):
            p.setPen(QPen(_ACCENT, 3))
            p.setBrush(Qt.NoBrush)
            p.drawRoundedRect(r, 7, 7)
        n = m.get("n")
        if n is not None:
            d = 28
            corner = m.get("badge", "tl")
            cx = r.left() if corner in ("tl", "bl") else r.right()
            cy = r.top() if corner in ("tl", "tr") else r.bottom()
            badge = QRect(cx - d // 2, cy - d // 2, d, d)
            p.setPen(QPen(_WHITE, 2))
            p.setBrush(QBrush(_BADGE))
            p.drawEllipse(badge)
            p.setPen(_WHITE)
            f = QFont()
            f.setBold(True)
            f.setPointSize(13)
            p.setFont(f)
            p.drawText(badge, Qt.AlignCenter, str(n))
    p.end()
    return pm


def save(pixmap: QPixmap, path) -> None:
    path = str(path)
    if not pixmap.save(path):
        raise RuntimeError(f"failed to write {path}")
