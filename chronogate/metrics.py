"""Per-pixel metrics and ranking -- the data behind the pixel list.

A **registry** of named per-pixel quantities (photons in gate, total photons,
apparent lifetime, phasor coordinates, ...), each a pure function of a
:class:`MetricContext` returning a ``(Y, X)`` float array (``NaN`` where the
value is undefined for that pixel). :func:`rank` then filters and sorts the
pixels by one of them.

**Adding a metric is one function.** Decorate it with :func:`register` and it
shows up as a sortable, filterable column in the UI automatically -- no changes
to the panel, the controller, or the table::

    @register("peak_bin", "peak bin", fmt="{:.0f}")
    def _peak_bin(ctx):
        return ctx.model._counts.argmax(axis=-1).astype(float)

No Qt and no matplotlib here: this is numpy-only and unit-testable, like the
rest of the analysis layer.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

import numpy as np


@dataclass(frozen=True)
class MetricContext:
    """Everything a metric may need: the model plus the current analysis settings.

    ``phasor_fn`` lets the caller inject a *cached* ``() -> (g, s)`` supplier --
    the phasor is a full Fourier pass over the cube, so recomputing it per metric
    would dominate the cost.
    """

    model: object                       # gating.GatingModel
    gate_a: tuple[int, int]             # the gate the intensity image integrates
    gate_b: tuple[int, int] = (0, 0)    # the late RLD gate
    rld_gate_a: tuple[int, int] | None = None   # the early RLD gate (defaults to gate_a)
    floor_per_bin: float = 0.0
    rld_min_counts: float = 0.0
    phasor_fn: Callable[[], tuple] | None = field(default=None, repr=False)

    @property
    def rld_gates(self) -> tuple[tuple[int, int], tuple[int, int]]:
        """The (early, late) pair for lifetime metrics. The early gate can differ from
        ``gate_a`` -- e.g. the caller split one wide gate into two equal halves."""
        return (self.rld_gate_a or self.gate_a), self.gate_b

    def phasor_maps(self):
        return self.phasor_fn() if self.phasor_fn is not None else self.model.phasor()


@dataclass(frozen=True)
class PixelMetric:
    key: str
    label: str
    fmt: str
    compute: Callable[[MetricContext], np.ndarray]
    descending: bool          # the natural "most interesting first" direction

    def format(self, value: float) -> str:
        if value is None or not np.isfinite(value):
            return "—"
        return self.fmt.format(value)


_REGISTRY: dict[str, PixelMetric] = {}


def register(key: str, label: str, fmt: str = "{:,.0f}", descending: bool = True):
    """Register a per-pixel metric. The function takes a MetricContext and returns
    a ``(Y, X)`` float array (NaN where undefined)."""
    def decorate(fn: Callable[[MetricContext], np.ndarray]) -> Callable:
        _REGISTRY[key] = PixelMetric(key, label, fmt, fn, descending)
        return fn
    return decorate


def metrics() -> list[PixelMetric]:
    """Every registered metric, in registration order (the column order)."""
    return list(_REGISTRY.values())


def get(key: str) -> PixelMetric:
    return _REGISTRY[key]


# --------------------------------------------------------------- the built-ins
@register("in_gate", "in gate")
def _in_gate(ctx: MetricContext) -> np.ndarray:
    """Photons the pixel contributes to the current gate (floor subtracted)."""
    return np.asarray(ctx.model.gate(*ctx.gate_a, floor_per_bin=ctx.floor_per_bin),
                      dtype=np.float64)


@register("total", "total")
def _total(ctx: MetricContext) -> np.ndarray:
    """All photons in the pixel, across the whole microtime axis."""
    return ctx.model.intensity.astype(np.float64)


@register("tau", "τ (ns)", fmt="{:.2f}", descending=True)
def _tau(ctx: MetricContext) -> np.ndarray:
    """Apparent lifetime from the two RLD gates (NaN where photon-starved)."""
    early, late = ctx.rld_gates
    rl = ctx.model.rapid_lifetime(early, late,
                                  floor_per_bin=ctx.floor_per_bin,
                                  min_counts=ctx.rld_min_counts)
    return np.asarray(rl["tau"], dtype=np.float64)


@register("g", "phasor g", fmt="{:.3f}", descending=False)
def _phasor_g(ctx: MetricContext) -> np.ndarray:
    return np.asarray(ctx.phasor_maps()[0], dtype=np.float64)


@register("s", "phasor s", fmt="{:.3f}", descending=True)
def _phasor_s(ctx: MetricContext) -> np.ndarray:
    return np.asarray(ctx.phasor_maps()[1], dtype=np.float64)


# ------------------------------------------------------------------- ranking
@dataclass
class PixelTable:
    """The rows the pixel list shows: the top ``limit`` matches, already sorted."""

    keys: list[str]                     # metric keys, in column order
    rows: list[tuple[int, int]]         # (row, col) per table row
    values: dict[str, np.ndarray]       # key -> that column's values for `rows`
    sort_key: str
    n_matched: int                      # pixels passing the filter
    n_total: int                        # pixels in the image
    truncated: bool                     # True when n_matched > len(rows)


def rank(
    ctx: MetricContext,
    sort_key: str,
    *,
    vmin: float | None = None,
    vmax: float | None = None,
    limit: int = 200,
    descending: bool | None = None,
    keys: list[str] | None = None,
) -> PixelTable:
    """Filter pixels by ``sort_key`` in ``[vmin, vmax]`` and return the top ``limit``.

    Pixels whose sort metric is NaN (undefined -- e.g. a lifetime with too few
    photons) are excluded rather than sorted to one end, so the list only ever
    shows pixels the metric is actually meaningful for.
    """
    keys = keys or [m.key for m in metrics()]
    if sort_key not in keys:
        keys = [sort_key] + keys
    columns = {k: get(k).compute(ctx).ravel() for k in keys}
    v = columns[sort_key]

    keep = np.isfinite(v)
    if vmin is not None:
        keep &= v >= vmin
    if vmax is not None:
        keep &= v <= vmax
    idx = np.flatnonzero(keep)
    n_matched = int(idx.size)

    if descending is None:
        descending = get(sort_key).descending
    if n_matched:
        # Sort the *negated* values rather than reversing an ascending sort: reversing
        # would also reverse the ties, so the "brightest pixel" would be the last of
        # the tied maxima rather than the first. Stable, so ties stay in image order.
        order = np.argsort(-v[idx] if descending else v[idx], kind="stable")
        idx = idx[order[: max(0, int(limit))]]

    shape = ctx.model.intensity.shape
    rr, cc = np.unravel_index(idx, shape)
    return PixelTable(
        keys=keys,
        rows=list(zip(rr.tolist(), cc.tolist())),
        values={k: columns[k][idx] for k in keys},
        sort_key=sort_key,
        n_matched=n_matched,
        n_total=int(v.size),
        truncated=n_matched > idx.size,
    )


def value_range(ctx: MetricContext, key: str) -> tuple[float, float]:
    """The finite ``(min, max)`` of a metric -- the sensible bounds for its filter."""
    v = get(key).compute(ctx)
    finite = v[np.isfinite(v)]
    if finite.size == 0:
        return 0.0, 0.0
    return float(finite.min()), float(finite.max())
