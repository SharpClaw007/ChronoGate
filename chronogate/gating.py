"""Core gating maths -- no GUI, no file I/O, easy to test.

The heart of the tool is a **prefix sum** (cumulative sum) along the microtime
axis. Once computed, the number of photons in *any* gate ``[lo, hi]`` is a
single subtraction per pixel, independent of how wide the gate is. That is
what keeps dragging the gate instant even on big files.

Everything else here (nanosecond <-> bin conversion, t0 detection, baseline
estimation, intensity thresholding) is small, pure, and reused by both the
viewer and the test.
"""

from __future__ import annotations

import numpy as np

from .loader import FlimCube


def build_prefix_sum(cube: np.ndarray) -> np.ndarray:
    """Cumulative photon count along the microtime axis, with a leading zero.

    Given ``cube`` of shape (Y, X, H), returns ``prefix`` of shape (Y, X, H+1)
    where ``prefix[..., 0] == 0`` and ``prefix[..., k]`` is the sum of bins
    ``0..k-1``. With that padding, the photons in the inclusive bin range
    ``[lo, hi]`` are exactly ``prefix[..., hi + 1] - prefix[..., lo]``.

    We accumulate in uint32: per-bin counts are uint16, but their running sum
    over the whole axis can exceed 65535 in bright pixels. uint32 (max ~4.3e9)
    is always safe here and uses half the memory of int64.
    """
    ny, nx, nh = cube.shape
    prefix = np.zeros((ny, nx, nh + 1), dtype=np.uint32)
    np.cumsum(cube, axis=-1, dtype=np.uint32, out=prefix[..., 1:])
    return prefix


def gate_image(prefix: np.ndarray, lo_bin: int, hi_bin: int) -> np.ndarray:
    """Photons in the inclusive microtime gate ``[lo_bin, hi_bin]``, per pixel.

    O(Y*X) regardless of gate width -- this is the live-drag workhorse. Returns
    an int64 (Y, X) image (signed so later baseline subtraction can go below
    zero before being clamped).
    """
    lo = int(lo_bin)
    hi = int(hi_bin)
    if hi < lo:
        lo, hi = hi, lo
    lo = max(0, lo)
    hi = min(prefix.shape[-1] - 2, hi)  # prefix has H+1 columns
    # Cast the two (Y, X) planes to int64 before subtracting.
    return prefix[..., hi + 1].astype(np.int64) - prefix[..., lo].astype(np.int64)


def total_decay(cube: np.ndarray) -> np.ndarray:
    """The summed decay curve: total photons per microtime bin over all pixels.

    Shape (H,). This is the curve drawn in the left panel; gating happens on it.
    """
    return cube.sum(axis=(0, 1), dtype=np.int64)


def ns_to_bin(t_ns: float, resolution_ns: float, n_bins: int) -> int:
    """Convert a time in nanoseconds to the nearest microtime bin index."""
    b = int(round(t_ns / resolution_ns))
    return max(0, min(n_bins - 1, b))


def bin_to_ns(bin_index: float, resolution_ns: float) -> float:
    """Convert a (possibly fractional) bin index to its left-edge time in ns."""
    return bin_index * resolution_ns


def detect_t0_bin(decay: np.ndarray) -> int:
    """Estimate t0 (the excitation-pulse position) as the **smoothed** peak bin.

    Rigorous lifetime fitting derives t0 from a measured IRF; for a *gating*
    viewer the peak of the rising edge is the pragmatic, defensible reference. We
    smooth the decay with a short moving average before taking the peak, so a
    single noisy bin on a dim/scattery sample can't drag t0 around. Users can
    still override t0 manually (see :meth:`GatingModel.set_t0`).
    """
    d = np.asarray(decay, dtype=np.float64)
    if d.size >= 5:
        k = max(3, d.size // 100 | 1)          # odd window, ~1% of the axis
        kernel = np.ones(k, dtype=np.float64) / k
        d = np.convolve(d, kernel, mode="same")
    return int(np.argmax(d))


def background_per_bin(cube: np.ndarray, t0_bin: int, guard_bins: int = 3) -> np.ndarray:
    """Estimate a constant background level per pixel, in counts *per bin*.

    Real detectors record a small flat pedestal (dark counts, stray light)
    *before* the laser pulse arrives. We estimate it as the mean counts per bin
    in the pre-pulse region ``[0, t0 - guard]`` for each pixel. Subtracting
    this (scaled by gate width) makes gated intensities honest.

    Returns a float (Y, X) array. If there is no usable pre-pulse region, the
    background is taken as zero (nothing to subtract).
    """
    hi = max(0, t0_bin - guard_bins)
    if hi < 1:
        return np.zeros(cube.shape[:2], dtype=np.float64)
    return cube[..., :hi].mean(axis=-1)


def apply_baseline(
    gated: np.ndarray, bg_per_bin: np.ndarray, gate_width_bins: int
) -> np.ndarray:
    """Subtract ``bg_per_bin * gate_width`` from a gated image, clamped at 0."""
    corrected = gated.astype(np.float64) - bg_per_bin * float(gate_width_bins)
    np.clip(corrected, 0, None, out=corrected)
    return corrected


def gate_bounds_ns(lo_bin: int, hi_bin: int, resolution_ns: float) -> tuple[float, float]:
    """Real-time span (ns) covered by inclusive bins ``[lo_bin, hi_bin]``.

    The gate covers the left edge of ``lo_bin`` up to the right edge of
    ``hi_bin`` (hence ``hi_bin + 1``).
    """
    return lo_bin * resolution_ns, (hi_bin + 1) * resolution_ns


def rld_lifetime(
    na: np.ndarray, nb: np.ndarray, dt_ns: float, min_counts: float = 0.0
) -> np.ndarray:
    """Apparent lifetime from two gates via Rapid Lifetime Determination (RLD).

    For a mono-exponential decay ``D(t) = D0 * exp(-t / tau)``, the photons
    integrated over a gate ``[a, a + G]`` are
    ``N = D0 * tau * exp(-a / tau) * (1 - exp(-G / tau))``. For two gates of
    **equal width** ``G`` whose starts differ by ``dt = a_late - a_early``, the
    width-and-amplitude factors cancel in the ratio, leaving

        N_early / N_late = exp(dt / tau)   =>   tau = dt / ln(N_early / N_late).

    This is the classic two-gate RLD estimator (Ballew & Demas): fit-free, one
    division per pixel, exact for equal-width gates in the mono-exponential tail.

    Parameters
    ----------
    na, nb : np.ndarray
        Per-pixel photons in the **earlier** (``na``) and **later** (``nb``)
        gate. Background should already be removed (so a flat pedestal does not
        bias the ratio toward longer lifetimes).
    dt_ns : float
        Separation between the two gate *start* edges (later minus earlier), ns.
    min_counts : float
        Pixels with ``na`` or ``nb`` at or below this are too photon-starved to
        trust and are returned as NaN.

    Returns
    -------
    np.ndarray
        ``tau`` per pixel (ns), with **NaN** where the estimate is not
        physically meaningful: too few photons, or ``na <= nb`` (no measurable
        decay across the gates -- noise or a rising edge), or ``dt_ns <= 0``.
    """
    na = np.asarray(na, dtype=np.float64)
    nb = np.asarray(nb, dtype=np.float64)
    shape = np.broadcast_shapes(na.shape, nb.shape)
    if dt_ns <= 0:
        return np.full(shape, np.nan)
    # Valid only where both gates have signal and the decay actually decays.
    valid = (na > min_counts) & (nb > min_counts) & (na > nb)
    with np.errstate(divide="ignore", invalid="ignore"):
        tau = dt_ns / np.log(na / nb)
    return np.where(valid, tau, np.nan)


def fit_mono_exponential(
    t_ns: np.ndarray, decay: np.ndarray, t0_ns: float, baseline: float = 0.0
) -> tuple[float, float] | None:
    """Weighted log-linear mono-exponential fit -- a smooth visual guide.

    Fits ``A * exp(-(t - t0) / tau) + baseline`` to the decay over ``t >= t0`` by
    a Poisson-weighted linear regression of ``ln(decay - baseline)`` against time
    (weights ``sqrt(counts)``, since ``Var(ln N) ~ 1/N``). The bright bins just
    after the pulse dominate the fit, so the noisy low-count tail -- where a
    per-pixel decay degenerates into 0/1/2-count "steps" -- is smoothly
    extrapolated rather than fit point-by-point. This is a **display aid**, not a
    rigorous (IRF-deconvolved, multi-exponential) lifetime analysis.

    Parameters
    ----------
    t_ns, decay : np.ndarray
        Microtime axis (ns) and the per-pixel decay to fit (same length).
    t0_ns : float
        Pulse time; only ``t >= t0`` is fit (the decaying part).
    baseline : float
        A flat background subtracted before the log fit (e.g. the noise floor).

    Returns
    -------
    tuple[float, float] | None
        ``(amplitude, tau_ns)``, or ``None`` when the data cannot support a
        decaying fit (too few positive bins, or a non-decreasing trend).
    """
    t = np.asarray(t_ns, dtype=np.float64)
    y = np.asarray(decay, dtype=np.float64) - float(baseline)
    mask = (t >= t0_ns) & (y > 0)
    if int(mask.sum()) < 3:
        return None
    tt = t[mask] - t0_ns
    yy = y[mask]
    try:
        slope, intercept = np.polyfit(tt, np.log(yy), 1, w=np.sqrt(yy))
    except (np.linalg.LinAlgError, ValueError):
        return None
    if not np.isfinite(slope) or not np.isfinite(intercept) or slope >= 0:
        return None
    tau = -1.0 / slope
    if tau > 1e6:            # a ~flat trend (numerically tiny slope) is not a decay
        return None
    return float(np.exp(intercept)), float(tau)


def mono_exponential_curve(
    t_ns: np.ndarray, t0_ns: float, amplitude: float, tau_ns: float,
    baseline: float = 0.0,
) -> np.ndarray:
    """Evaluate ``A*exp(-(t-t0)/tau)+baseline`` (NaN before ``t0``, so it starts
    at the pulse)."""
    t = np.asarray(t_ns, dtype=np.float64)
    y = amplitude * np.exp(-(t - t0_ns) / tau_ns) + baseline
    y[t < t0_ns] = np.nan
    return y


def phasor(counts: np.ndarray, period_bins: float, t0_bin: float = 0.0,
           harmonic: int = 1):
    """Per-pixel phasor coordinates ``(g, s)`` -- fit-free lifetime analysis.

    For each pixel's decay the phasor is the first Fourier harmonic of the
    microtime histogram at the laser rep-rate::

        g = Σ D(t) cos(ω t) / Σ D(t),   s = Σ D(t) sin(ω t) / Σ D(t)

    with ``ω = 2π·harmonic / period`` and ``t`` measured in bins from ``t0``.
    A mono-exponential of lifetime τ lands on the **universal semicircle**
    (centre (0.5, 0), radius 0.5) at ``g = 1/(1+(ωτ)²)``, ``s = ωτ/(1+(ωτ)²)``;
    mixtures fall inside it. No fitting, no IRF -- this is a display/clustering
    tool, so it is *uncalibrated* (a t0/IRF offset rotates the cloud), which is
    why ``t`` is referenced to ``t0``.

    Parameters
    ----------
    counts : (Y, X, H) array
    period_bins : float
        Laser period in microtime bins (``period_ns / resolution_ns``).
    t0_bin : float
        Pulse reference; ``t`` is measured from here.

    Returns
    -------
    (g, s) : two (Y, X) float arrays (NaN where a pixel has no photons).
    """
    counts = np.asarray(counts, dtype=np.float64)
    h = counts.shape[-1]
    t = np.arange(h, dtype=np.float64) - float(t0_bin)
    w = 2.0 * np.pi * harmonic / float(period_bins)
    cosv, sinv = np.cos(w * t), np.sin(w * t)
    total = counts.sum(axis=-1)
    with np.errstate(divide="ignore", invalid="ignore"):
        g = counts @ cosv / total
        s = counts @ sinv / total
    ok = total > 0
    return np.where(ok, g, np.nan), np.where(ok, s, np.nan)


def phasor_semicircle(n: int = 200):
    """The universal semicircle ``(g, s)`` for overlaying on a phasor plot."""
    theta = np.linspace(0.0, np.pi, n)
    return 0.5 + 0.5 * np.cos(theta), 0.5 * np.sin(theta)


def _moving_sum(a: np.ndarray, window: int, axis: int) -> np.ndarray:
    """Centered moving sum of size ``window`` along ``axis`` (edges clamped).

    Computed from a cumulative sum (an "integral image" along one axis), so the
    whole box sum is O(N) regardless of window size. Edge pixels sum only the
    available neighbours. Accumulates in uint32 (photon counts are small).
    """
    a = np.moveaxis(a, axis, 0)
    n = a.shape[0]
    prefix = np.zeros((n + 1,) + a.shape[1:], dtype=np.uint32)
    np.cumsum(a, axis=0, dtype=np.uint32, out=prefix[1:])
    idx = np.arange(n)
    start = np.clip(idx - window // 2, 0, n)
    stop = np.clip(idx - window // 2 + window, 0, n)
    out = prefix[stop] - prefix[start]
    return np.moveaxis(out, 0, axis)


def spatial_bin(counts: np.ndarray, factor: int) -> np.ndarray:
    """Spatially bin a cube by summing each pixel's ``factor`` x ``factor`` box.

    This is *sliding* binning: the image keeps its (Y, X) size and pixel
    coordinates, but every pixel becomes the sum of its neighbourhood, pooling
    photons so per-pixel decays are less shot-noise-limited. Returns uint32.
    Note that because the windows overlap, the *aggregate* total over-counts
    shared photons by ~factor**2 -- per-pixel values remain true pooled sums.
    """
    if factor <= 1:
        return counts
    binned = _moving_sum(counts, factor, axis=0)  # along rows (Y)
    binned = _moving_sum(binned, factor, axis=1)  # along columns (X)
    return binned


def suggest_bin_factor(
    intensity: np.ndarray,
    target_photons: float = 100.0,
    min_intensity: float = 0.0,
    percentile: float = 50.0,
    max_bin: int = 16,
) -> tuple[int, float]:
    """Suggest a bin factor B so signal pixels reach ``target_photons``.

    Lifetime/gating precision is photon-limited (~1/sqrt(N)); binning a B x B box
    multiplies photons-per-pixel by B**2. We take a representative photon count
    ``n0`` (the ``percentile`` of pixels above ``min_intensity`` -- the signal,
    not empty background) and return ``B = ceil(sqrt(target/n0))`` clamped to
    ``max_bin``, plus ``n0`` for reporting.
    """
    signal = intensity[intensity > max(min_intensity, 0)]
    if signal.size == 0:
        return 1, 0.0
    n0 = float(np.percentile(signal, percentile))
    if n0 <= 0:
        return 1, n0
    factor = int(np.ceil(np.sqrt(target_photons / n0)))
    return max(1, min(max_bin, factor)), n0


class GatingModel:
    """Bundles a cube with its derived prefix sum and decay for the viewer.

    Holds everything the GUI needs for one loaded (channel, frame) view so the
    viewer can stay focused on widgets and drawing. Cheap to rebuild when the
    user switches z-slice or channel.
    """

    def __init__(self, cube: FlimCube, bin_factor: int = 1):
        self.cube = cube
        self.bin_factor = max(1, int(bin_factor))
        # All derived quantities (decay, intensity, prefix, background,
        # per-pixel decays) use the spatially binned counts, so binning cleans
        # up per-pixel decays and the gated image consistently. The original
        # cube is kept (self.cube) so we can re-bin without re-reading the file.
        self._counts = spatial_bin(cube.counts, self.bin_factor)
        self.prefix = build_prefix_sum(self._counts)
        self.decay = total_decay(self._counts)
        self.intensity = self._counts.sum(axis=-1, dtype=np.int64)  # per (binned) pixel
        self.t0_bin = detect_t0_bin(self.decay)
        self.bg_per_bin = background_per_bin(self._counts, self.t0_bin)

    @property
    def resolution_ns(self) -> float:
        return self.cube.resolution_ns

    @property
    def n_bins(self) -> int:
        return self.cube.n_bins

    @property
    def n_pixels(self) -> int:
        return int(self.intensity.size)

    def auto_noise_floor_total(self) -> float:
        """Auto background estimate as counts/bin *summed over all pixels*.

        This is the natural starting level for the noise-floor line on the total
        decay: it sits right at the pre-pulse pedestal. Divide by ``n_pixels``
        to get the per-pixel floor used for subtraction.
        """
        return float(self.bg_per_bin.sum())

    def auto_noise_floor_pp(self) -> float:
        """Auto per-pixel floor placed just **above the flat pre-pulse baseline**.

        The bins before the pulse hold only the baseline noise of the first,
        pre-signal photons. We estimate that baseline **robustly**: the *median*
        of the summed pre-pulse decay (which ignores the rising-edge bin(s) that
        creep into the window near ``t0``, where a plain mean+std would be dragged
        far too high), plus ``3`` robust sigma (``1.4826*MAD``, floored at the
        Poisson ``sqrt(median)``). Divided by the pixel count, this sits right
        above the baseline without eating into the signal.
        """
        hi = max(0, self.t0_bin - 3)
        pre = self.decay[:hi].astype(np.float64)
        if pre.size >= 3:
            med = float(np.median(pre))
            mad = float(np.median(np.abs(pre - med)))
            sigma = max(1.4826 * mad, np.sqrt(max(med, 0.0)))
            level = med + 3.0 * sigma
        elif pre.size:
            level = float(np.median(pre))
        else:
            level = float(self.bg_per_bin.sum())
        return level / max(1, self.n_pixels)

    def peak_counts_per_bin(self) -> int:
        """The brightest single-pixel, single-bin count.

        The top of the *per-pixel* noise-floor range: a floor at this level
        subtracts more than any pixel holds in a bin, so the gated image can be
        driven all the way to zero.
        """
        return int(self._counts.max())

    def gate(self, lo_bin: int, hi_bin: int, floor_per_bin: float | np.ndarray = 0.0) -> np.ndarray:
        """Gated intensity image for inclusive bins ``[lo_bin, hi_bin]``.

        Per pixel, this integrates that pixel's decay over the gate (a single
        prefix-sum subtraction). An optional per-pixel **noise floor** --
        ``floor_per_bin * gate_width`` (scalar or per-pixel) -- is then removed
        and the result clamped at 0. With no floor the raw integer image is
        returned unchanged. O(pixels) regardless of gate width.
        """
        img = gate_image(self.prefix, lo_bin, hi_bin)
        floor_on = (floor_per_bin > 0) if np.isscalar(floor_per_bin) else np.any(floor_per_bin)
        if not floor_on:
            return img
        lo, hi = sorted((int(lo_bin), int(hi_bin)))
        out = img.astype(np.float64)
        out -= floor_per_bin * float(hi - lo + 1)
        np.clip(out, 0, None, out=out)
        return out

    def t0_ns(self) -> float:
        return bin_to_ns(self.t0_bin, self.resolution_ns)

    def set_t0(self, bin_index: int) -> None:
        """Override t0 (the pulse reference) and recompute the pre-pulse background."""
        self.t0_bin = int(max(0, min(self.n_bins - 1, bin_index)))
        self.bg_per_bin = background_per_bin(self._counts, self.t0_bin)

    def period_bins(self) -> float:
        """Laser period expressed in microtime bins (falls back to the full axis)."""
        p = self.cube.period_ns
        return (p / self.resolution_ns) if np.isfinite(p) and p > 0 else float(self.n_bins)

    def phasor(self, harmonic: int = 1):
        """Per-pixel phasor ``(g, s)`` on the binned cube (see :func:`phasor`)."""
        return phasor(self._counts, self.period_bins(), t0_bin=self.t0_bin, harmonic=harmonic)

    def pixel_decay(self, r0: int, r1: int, c0: int, c1: int) -> np.ndarray:
        """Mean decay (counts/bin per pixel) over the region rows [r0,r1) cols [c0,c1).

        Returned in *per-pixel* units so a single pixel and a multi-pixel ROI
        share the same y-scale and the same per-pixel noise-floor line.
        """
        region = self._counts[r0:r1, c0:c1, :]
        if region.size == 0:
            return np.zeros(self.n_bins, dtype=float)
        return region.mean(axis=(0, 1), dtype=np.float64)

    def mask_decay(self, mask: np.ndarray) -> np.ndarray:
        """Mean decay (counts/bin per pixel) over an arbitrary boolean pixel mask.

        The mask counterpart of :meth:`pixel_decay`: it lets a population selected
        by *property* rather than location -- e.g. a lasso around a phasor cluster
        -- be pooled into one decay curve, in the same per-pixel units.
        """
        m = np.asarray(mask, dtype=bool)
        if m.shape != self.intensity.shape or not m.any():
            return np.zeros(self.n_bins, dtype=float)
        return self._counts[m].mean(axis=0, dtype=np.float64)

    def rapid_lifetime(
        self,
        gate_a: tuple[int, int],
        gate_b: tuple[int, int],
        floor_per_bin: float | np.ndarray = 0.0,
        min_counts: float = 0.0,
    ) -> dict:
        """Per-pixel apparent-lifetime map from two gates (two-gate RLD).

        Each gate is an inclusive ``(lo_bin, hi_bin)`` pair. They are reordered
        so the earlier one (smaller start bin) is treated as ``na``; the gated
        photons are background-subtracted with the same ``floor_per_bin``
        machinery as the intensity image (so a flat pedestal does not skew the
        ratio). See :func:`rld_lifetime` for the estimator and masking.

        Returns a dict with ``tau`` (Y, X, NaN where invalid), the early/late
        gated images (``na``/``nb``), the start separation ``dt_ns``, the
        ordered ``early``/``late`` gates, and ``equal_width`` (False warns the
        caller that the equal-width RLD assumption is violated).
        """
        alo, ahi = sorted((int(gate_a[0]), int(gate_a[1])))
        blo, bhi = sorted((int(gate_b[0]), int(gate_b[1])))
        if blo < alo:  # ensure gate A is the earlier of the two
            (alo, ahi), (blo, bhi) = (blo, bhi), (alo, ahi)
        na = np.asarray(self.gate(alo, ahi, floor_per_bin), dtype=np.float64)
        nb = np.asarray(self.gate(blo, bhi, floor_per_bin), dtype=np.float64)
        dt_ns = (blo - alo) * self.resolution_ns
        tau = rld_lifetime(na, nb, dt_ns, min_counts=min_counts)
        return {
            "tau": tau,
            "na": na,
            "nb": nb,
            "dt_ns": dt_ns,
            "early": (alo, ahi),
            "late": (blo, bhi),
            "equal_width": (ahi - alo) == (bhi - blo),
        }
