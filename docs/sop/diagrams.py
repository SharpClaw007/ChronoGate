"""Generate the SOP's explanatory diagrams with matplotlib (no extra deps).

Two figures:
  * ``workflow.png``   -- the end-to-end operating flow (load -> gate -> analyse
    -> export), with the three analysis branches.
  * ``rld_schematic.png`` -- how two-gate RLD reads a lifetime off the decay.

Re-run with the figures:
    PYTHONPATH=. .venv/bin/python docs/sop/diagrams.py
"""

from __future__ import annotations

from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
from matplotlib.patches import FancyArrowPatch, FancyBboxPatch

IMG = Path(__file__).resolve().parent / "img"

_ACCENT = "#2563EB"
_INK = "#1F2933"
_MUTED = "#6B7280"
_A = "#E8833A"
_B = "#2FA84F"
_PANEL = "#FFFFFF"
_FILL = "#EEF3FE"


def _box(ax, xy, w, h, text, fill=_FILL, edge=_ACCENT, fc=_INK):
    x, y = xy
    ax.add_patch(FancyBboxPatch((x, y), w, h, boxstyle="round,pad=0.02,rounding_size=0.08",
                                linewidth=1.6, edgecolor=edge, facecolor=fill, zorder=2))
    ax.text(x + w / 2, y + h / 2, text, ha="center", va="center",
            fontsize=10.5, color=fc, zorder=3, wrap=True)


def _arrow(ax, p0, p1, color=_MUTED):
    ax.add_patch(FancyArrowPatch(p0, p1, arrowstyle="-|>", mutation_scale=15,
                                 linewidth=1.6, color=color, zorder=1,
                                 shrinkA=2, shrinkB=2))


def workflow() -> None:
    fig = plt.figure(figsize=(9.2, 4.4), dpi=150)
    ax = fig.add_axes([0, 0, 1, 1]); ax.set_xlim(0, 10); ax.set_ylim(0, 5); ax.axis("off")

    _box(ax, (0.2, 3.6), 2.1, 0.9, "1. Load\n.ptu / .sdt")
    _box(ax, (2.7, 3.6), 2.1, 0.9, "2. Set gate\n& t0")
    _box(ax, (5.2, 3.6), 2.1, 0.9, "3. Choose\nanalysis", fill="#FDECEF", edge="#D1495B")

    _box(ax, (7.7, 4.0), 2.1, 0.8, "RLD τ map\n(+ σ)", fill="#FBF0E7", edge=_A)
    _box(ax, (7.7, 3.0), 2.1, 0.8, "Phasor", fill="#EAF6EE", edge=_B)
    _box(ax, (7.7, 2.0), 2.1, 0.8, "IRF fit\n(reconvolution)", fill=_FILL, edge=_ACCENT)

    _box(ax, (3.95, 0.5), 2.1, 0.9, "4. Export /\none-page report", fill="#F2F0FB", edge="#7C3AED")

    _arrow(ax, (2.3, 4.05), (2.7, 4.05))
    _arrow(ax, (4.8, 4.05), (5.2, 4.05))
    _arrow(ax, (7.3, 4.05), (7.7, 4.4))
    _arrow(ax, (7.3, 4.05), (7.7, 3.4))
    _arrow(ax, (7.3, 4.05), (7.7, 2.4))
    for y in (4.4, 3.4, 2.4):
        _arrow(ax, (8.75, y), (8.75, 1.5), color="#C7CDD6")
    _arrow(ax, (8.75, 1.5), (6.05, 0.95))

    ax.text(5.0, 4.92, "ChronoGate operating flow", ha="center", va="top",
            fontsize=12, color=_INK, fontweight="bold")
    fig.savefig(IMG / "workflow.png", facecolor=_PANEL)
    plt.close(fig)


def rld_schematic() -> None:
    fig = plt.figure(figsize=(7.6, 3.6), dpi=150)
    ax = fig.add_subplot(111)
    t = np.linspace(0, 12, 500)
    tau = 2.5
    decay = np.exp(-t / tau)
    ax.plot(t, decay, color=_ACCENT, lw=2, label="fluorescence decay D(t)")

    for (lo, hi, c, name) in [(1.0, 2.6, _A, "gate A (early)"), (4.0, 5.6, _B, "gate B (late)")]:
        m = (t >= lo) & (t <= hi)
        ax.fill_between(t[m], 0, decay[m], color=c, alpha=0.35)
        ax.axvspan(lo, hi, color=c, alpha=0.06)
        ax.text((lo + hi) / 2, np.exp(-((lo + hi) / 2) / tau) + 0.05, name,
                ha="center", color=c, fontsize=9, fontweight="bold")

    ax.annotate("", xy=(4.0, 0.92), xytext=(1.0, 0.92),
                arrowprops=dict(arrowstyle="<->", color=_MUTED, lw=1.3))
    ax.text(2.5, 0.96, "Δt (gate-start separation)", ha="center", color=_MUTED, fontsize=9)

    ax.text(6.6, 0.72, r"$\tau = \dfrac{\Delta t}{\ln(N_A / N_B)}$",
            fontsize=15, color=_INK,
            bbox=dict(boxstyle="round,pad=0.4", fc=_FILL, ec=_ACCENT))
    ax.text(6.6, 0.42, r"$\sigma_\tau = \dfrac{\tau^2}{\Delta t}\sqrt{\frac{1}{N_A}+\frac{1}{N_B}}$",
            fontsize=12, color=_MUTED)

    ax.set_xlabel("microtime (ns)"); ax.set_ylabel("photons (log-ish)")
    ax.set_ylim(0, 1.1); ax.set_xlim(0, 12)
    ax.set_title("Two-gate rapid lifetime determination (RLD)", fontsize=11, color=_INK)
    for s in ("top", "right"):
        ax.spines[s].set_visible(False)
    fig.tight_layout()
    fig.savefig(IMG / "rld_schematic.png", facecolor=_PANEL)
    plt.close(fig)


def build_all() -> list[str]:
    IMG.mkdir(parents=True, exist_ok=True)
    workflow()
    rld_schematic()
    return ["workflow", "rld_schematic"]


if __name__ == "__main__":
    print("generated diagrams:", ", ".join(build_all()))
