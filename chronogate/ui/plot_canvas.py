"""Embedded matplotlib canvases for the decay curve and the gated image.

Each plot lives on its own :class:`~matplotlib.figure.Figure` hosted by a
``FigureCanvasQTAgg`` so Qt can lay them out and resize them. We build the
``Figure`` directly (no pyplot) to avoid spinning up a second GUI backend, use
``layout="constrained"`` with tight padding so axes/labels/colorbar track
resizing without wasting margin, and keep modest minimum sizes so the plots stay
compact and leave room for the controls.
"""

from __future__ import annotations

from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg
from matplotlib.figure import Figure
from PySide6.QtWidgets import QSizePolicy


class MplCanvas(FigureCanvasQTAgg):
    """A single-axes matplotlib canvas embeddable in a Qt layout."""

    def __init__(self, width: float = 4.4, height: float = 3.4,
                 min_w: int = 340, min_h: int = 260) -> None:
        self.fig = Figure(figsize=(width, height), layout="constrained")
        super().__init__(self.fig)
        self.ax = self.fig.add_subplot(111)
        # Trim the constrained-layout padding so plots don't float in whitespace.
        try:
            self.fig.get_layout_engine().set(w_pad=0.015, h_pad=0.015, wspace=0.0, hspace=0.0)
        except Exception:
            pass
        self.setMinimumSize(min_w, min_h)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)


class DecayCanvas(MplCanvas):
    """Left panel: the fluorescence decay vs microtime (ns)."""

    def __init__(self) -> None:
        super().__init__(width=5.4, height=3.0, min_w=380, min_h=200)
        self.ax.set_xlabel("microtime (ns)")
        self.ax.set_ylabel("photons")


class ImageCanvas(MplCanvas):
    """Right panel: the gated intensity image or the lifetime map."""

    def __init__(self) -> None:
        super().__init__(width=3.4, height=3.0, min_w=300, min_h=200)
        self.ax.set_xticks([])
        self.ax.set_yticks([])
