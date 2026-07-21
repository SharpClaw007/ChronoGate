"""IRF reconvolution lifetime fitting (rigorous, fit-based τ).

Where :mod:`chronogate.gating` gives *fit-free* lifetime contrast (two-gate RLD)
and :mod:`chronogate.gating.phasor` gives fit-free phasor coordinates, this
module provides the rigorous, instrument-response-deconvolved lifetime: it fits

    μ(t) = offset + IRF ⊛ Σ_i a_i · exp(-t / τ_i)

to a measured TCSPC decay, where ``IRF`` is the measured (or modelled) instrument
response and ``⊛`` is **periodic** (circular) convolution over the laser period.

Why periodic convolution -- the one subtle point
------------------------------------------------
The excitation is a *periodic* pulse train, so photons from a slow decay that
have not died out by the end of the microtime window wrap into the start of the
next period ("incomplete decay"). The physically correct model input is the
periodic steady-state exponential

    pe[k] = Σ_{m≥0} exp(-(k + m·n)·Δt / τ) = exp(-k·Δt/τ) / (1 - exp(-n·Δt/τ)),

a single-period exponential scaled by a **k-independent** constant
``1/(1-exp(-n·Δt/τ))``. That constant is absorbed into the amplitude ``a_i`` and
therefore does NOT bias τ -- so all the incomplete-decay physics is captured
simply by using **circular** convolution (which treats both signals as
``n``-periodic) of the unit-area IRF with the plain single-period exponential
``exp(-k·Δt/τ)``. Using linear convolution instead would drop the wrap-around and
bias short-period lifetimes. This is the correctness lynchpin of the module.

The model assumes the microtime window spans one laser period (``n_bins`` bins),
which is the usual PicoQuant/B&H FLIM acquisition and matches how
``gating.phasor`` is used.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

# scipy is a hard dependency of this module only (imported lazily so the rest of
# ChronoGate -- loader/gating/export -- stays scipy-free and light to import).


# --------------------------------------------------------------------------- #
# Instrument response function (IRF)
# --------------------------------------------------------------------------- #
@dataclass
class IRF:
    """A normalised (unit-area) instrument-response kernel on a microtime axis.

    Construct either from a *measured* IRF histogram (:meth:`from_decay`) or as a
    parametric *Gaussian* model (:meth:`gaussian`). :meth:`kernel` returns the
    unit-area kernel resampled onto a requested ``(n_bins, resolution_ns)`` axis,
    so an IRF measured at a different TCSPC resolution than the data is handled.
    """

    # The kernel is stored on its own axis; kernel() resamples to the data axis.
    _hist: np.ndarray
    _resolution_ns: float
    kind: str = "measured"
    meta: dict = field(default_factory=dict)

    @staticmethod
    def _normalise(h: np.ndarray) -> np.ndarray:
        h = np.asarray(h, dtype=np.float64)
        h = np.clip(h, 0.0, None)          # an IRF is a non-negative histogram
        total = h.sum()
        if not np.isfinite(total) or total <= 0:
            raise ValueError("IRF histogram is empty or non-positive; cannot normalise.")
        return h / total

    @classmethod
    def from_decay(cls, hist: np.ndarray, resolution_ns: float, **meta) -> "IRF":
        """A measured IRF from a 1-D histogram sampled at ``resolution_ns`` ns/bin."""
        hist = np.asarray(hist, dtype=np.float64).ravel()
        if hist.size < 2:
            raise ValueError("measured IRF needs at least 2 bins.")
        if resolution_ns <= 0:
            raise ValueError("IRF resolution_ns must be positive.")
        return cls(cls._normalise(hist), float(resolution_ns), kind="measured", meta=dict(meta))

    @classmethod
    def gaussian(cls, center_ns: float, fwhm_ns: float, n_bins: int,
                 resolution_ns: float, **meta) -> "IRF":
        """A Gaussian IRF of given centre and FWHM, sampled on ``n_bins`` bins."""
        if fwhm_ns <= 0 or resolution_ns <= 0 or n_bins < 2:
            raise ValueError("Gaussian IRF needs fwhm_ns>0, resolution_ns>0, n_bins>=2.")
        sigma = float(fwhm_ns) / (2.0 * np.sqrt(2.0 * np.log(2.0)))
        t = np.arange(int(n_bins)) * float(resolution_ns)
        h = np.exp(-0.5 * ((t - float(center_ns)) / sigma) ** 2)
        m = dict(meta); m.update(center_ns=float(center_ns), fwhm_ns=float(fwhm_ns))
        return cls(cls._normalise(h), float(resolution_ns), kind="gaussian", meta=m)

    def kernel(self, n_bins: int, resolution_ns: float) -> np.ndarray:
        """Unit-area kernel resampled onto ``(n_bins, resolution_ns)``.

        If the IRF was sampled at the same resolution and length, it is returned
        (re-normalised) unchanged. Otherwise it is linearly interpolated onto the
        target time axis -- an IRF measured at a finer/coarser TCSPC resolution or
        a different number of bins still yields a valid kernel on the data axis.
        """
        n_bins = int(n_bins)
        if (abs(resolution_ns - self._resolution_ns) < 1e-12
                and self._hist.size == n_bins):
            return self._normalise(self._hist)
        src_t = np.arange(self._hist.size) * self._resolution_ns
        dst_t = np.arange(n_bins) * float(resolution_ns)
        # Interpolate; outside the measured support the IRF is zero.
        resampled = np.interp(dst_t, src_t, self._hist, left=0.0, right=0.0)
        return self._normalise(resampled)


def irf_from_cube_counts(counts: np.ndarray, resolution_ns: float) -> IRF:
    """Build a measured :class:`IRF` from a loaded IRF measurement cube.

    ``counts`` is a ``(Y, X, H)`` FLIM cube (e.g. from :func:`loader.load_flim`
    on a scatter/reflection IRF acquisition); the IRF histogram is the sum over
    all pixels (the total instrument response), normalised to unit area.
    """
    counts = np.asarray(counts, dtype=np.float64)
    hist = counts.reshape(-1, counts.shape[-1]).sum(axis=0)
    return IRF.from_decay(hist, resolution_ns, source="cube-sum")


# --------------------------------------------------------------------------- #
# Forward model
# --------------------------------------------------------------------------- #
def _shifted_kernel_fft(kernel: np.ndarray, shift_bins: float, n: int) -> np.ndarray:
    """rFFT of the IRF kernel, sub-bin **circularly** shifted by ``shift_bins``.

    A positive shift delays the IRF (moves the decay onset later). The shift uses
    the Fourier shift theorem, so it is periodic -- consistent with the circular
    convolution used by :func:`decay_model`.
    """
    freqs = np.fft.rfftfreq(n)                      # cycles per bin
    return np.fft.rfft(kernel, n) * np.exp(-2j * np.pi * freqs * float(shift_bins))


def decay_model(taus_ns, amps, offset: float, shift_bins: float,
                kernel: np.ndarray, resolution_ns: float, n: int) -> np.ndarray:
    """Periodic reconvolution model μ[k] on ``n`` bins (see module docstring).

    ``taus_ns``/``amps`` are equal-length sequences of component lifetimes (ns)
    and amplitudes. Uses **circular** convolution of the unit-area ``kernel`` with
    ``Σ_i amps_i · exp(-k·Δt/τ_i)``; adds a flat ``offset``.
    """
    t = np.arange(n) * float(resolution_ns)                     # ns, per bin
    comp = np.zeros(n, dtype=np.float64)
    for tau, a in zip(taus_ns, amps):
        comp += float(a) * np.exp(-t / float(tau))
    H = _shifted_kernel_fft(kernel, shift_bins, n)
    conv = np.fft.irfft(H * np.fft.rfft(comp, n), n)            # circular conv
    return float(offset) + conv


# --------------------------------------------------------------------------- #
# Residuals / objective
# --------------------------------------------------------------------------- #
def _residuals(y: np.ndarray, mu: np.ndarray, objective: str) -> np.ndarray:
    """Per-bin residuals whose sum of squares is the fit objective.

    ``"mle"`` (default): signed Poisson **deviance** residuals -- the maximum-
    likelihood objective for photon-counting (low-count TCSPC) data, where
    Σ r² equals the Poisson deviance. ``"chi2"``: Pearson/Neyman weighted least
    squares, ``(y-μ)/sqrt(max(μ,1))`` (the classic weighted χ²).
    """
    mu = np.clip(mu, 1e-12, None)          # μ must be positive for logs / weights
    if objective == "chi2":
        return (y - mu) / np.sqrt(np.clip(mu, 1.0, None))
    # Poisson deviance: 2[ μ - y + y·ln(y/μ) ]; the y·ln(y/μ) term -> 0 as y -> 0.
    with np.errstate(divide="ignore", invalid="ignore"):
        ylog = np.where(y > 0, y * np.log(y / mu), 0.0)
    dev = 2.0 * (mu - y + ylog)
    dev = np.clip(dev, 0.0, None)          # guard tiny negatives from round-off
    return np.sign(y - mu) * np.sqrt(dev)


def reduced_chi_square(y: np.ndarray, mu: np.ndarray, n_params: int) -> float:
    """Pearson reduced χ² = Σ (y-μ)²/max(μ,1) / (n_bins - n_params).

    Reported as goodness-of-fit regardless of the fitting objective; ≈ 1 for a
    correct model on Poisson data. The ``max(μ,1)`` clip stabilises the classic
    Pearson blow-up when many bins have μ<1, at the cost of pulling the reported
    value a few percent low in very low-count regimes -- so read it as a
    clip-stabilised (slightly conservative) Pearson statistic, not a bare χ².
    """
    mu = np.clip(mu, 1.0, None)
    chi2 = float(np.sum((y - mu) ** 2 / mu))
    dof = max(int(y.size) - int(n_params), 1)
    return chi2 / dof


# --------------------------------------------------------------------------- #
# Single-decay fit
# --------------------------------------------------------------------------- #
@dataclass
class FitResult:
    taus_ns: np.ndarray          # fitted lifetimes (ns), longest-first
    amps: np.ndarray             # fitted amplitudes (same order)
    offset: float
    shift_bins: float
    tau_mean_ns: float           # amplitude-weighted mean lifetime
    sigma_tau_ns: np.ndarray     # 1σ on each τ from the fit covariance
    reduced_chi2: float
    success: bool
    n_params: int
    model: np.ndarray            # the fitted μ[k]
    objective: str
    kind: str                    # "mono" or "bi"

    @property
    def tau_ns(self) -> float:
        """The (longest) principal lifetime -- convenience for the mono case."""
        return float(self.taus_ns[0])

    @property
    def amp_fractions(self) -> np.ndarray:
        s = float(np.sum(self.amps))
        return self.amps / s if s else self.amps * np.nan

    @property
    def intensity_fractions(self) -> np.ndarray:
        """Fractional *intensity* contributions f_i = a_i·τ_i / Σ a_j·τ_j."""
        w = self.amps * self.taus_ns
        s = float(np.sum(w))
        return w / s if s else w * np.nan


# Any eigen-direction of JᵀJ this many times weaker than the strongest is treated
# as unidentifiable: its parameter variance is enormous (→ ∞ at exact degeneracy),
# so we report NaN rather than a numerically-garbage, often deceptively *small* σ.
# (A plain ``np.linalg.inv`` at cond≈1e12 returns meaningless values; ``pinv``
# would be worse -- it drops the ill-determined direction and *under*-reports it.)
_SIGMA_COND_FLOOR = 1e-9


def _param_sigma(res, n_params: int) -> np.ndarray:
    """Per-parameter 1σ from the least_squares Jacobian, conditioning-aware.

    For the Poisson-deviance / weighted-χ² residuals used here the residuals are
    ~unit-variance at the optimum, so ``(JᵀJ)⁻¹`` is the parameter covariance and
    ``sqrt(diag)`` its 1σ (verified well-calibrated for a well-posed fit across a
    500× photon range). But near-degenerate models (e.g. a bi-exponential whose
    two lifetimes nearly coincide) make ``JᵀJ`` numerically singular, where a
    naive inverse yields a **false, often too-small** σ. We diagonalise ``JᵀJ``
    and, when an eigenvalue falls below ``_SIGMA_COND_FLOOR × λ_max``, treat that
    direction as having infinite variance -- so every parameter projecting onto it
    is honestly reported as **NaN** (unidentifiable) instead of confidently wrong.
    """
    try:
        J = np.asarray(res.jac, dtype=np.float64)
        JTJ = J.T @ J
        w, V = np.linalg.eigh(JTJ)                 # symmetric PSD -> real eigenpairs
        wmax = float(w.max()) if w.size else 0.0
        if not np.isfinite(wmax) or wmax <= 0:
            return np.full(JTJ.shape[0], np.nan)
        floor = wmax * _SIGMA_COND_FLOOR
        inv_w = np.where(w > floor, 1.0 / w, np.inf)   # ill-conditioned -> ∞ variance
        cov_diag = (V ** 2) @ inv_w                     # diag(V·diag(inv_w)·Vᵀ)
        sigma = np.sqrt(cov_diag)                       # ∞ where unidentifiable
        return np.where(np.isfinite(sigma), sigma, np.nan)
    except (np.linalg.LinAlgError, ValueError):
        return np.full(n_params, np.nan)


def fit_decay(
    y: np.ndarray,
    irf: IRF,
    resolution_ns: float,
    model: str = "mono",
    objective: str = "mle",
    seed_tau_ns: float | None = None,
    fit_shift: bool = True,
    max_tau_ns: float | None = None,
) -> FitResult:
    """Fit one measured decay ``y`` by IRF reconvolution.

    Parameters
    ----------
    y : (n,) array
        The measured microtime histogram (photon counts per bin).
    irf : IRF
        Instrument response; resampled onto ``y``'s axis internally.
    resolution_ns : float
        TCSPC bin width (ns).
    model : {"mono","bi"}
        One- or two-exponential model.
    objective : {"mle","chi2"}
        Poisson maximum-likelihood (default) or weighted least squares.
    seed_tau_ns : float or None
        Initial lifetime guess (ns); a robust default is derived if None.
    fit_shift : bool
        Fit a sub-bin IRF timing shift (recommended). If False, shift is 0.
    max_tau_ns : float or None
        Upper τ bound; defaults to the full window ``n·Δt``.
    """
    from scipy.optimize import least_squares

    y = np.asarray(y, dtype=np.float64).ravel()
    n = y.size
    dt = float(resolution_ns)
    kernel = irf.kernel(n, dt)
    window_ns = n * dt
    hi_tau = float(max_tau_ns) if max_tau_ns else window_ns
    peak = float(np.max(y)) if y.size else 1.0
    base = float(np.min(y))
    if seed_tau_ns is None or not np.isfinite(seed_tau_ns) or seed_tau_ns <= 0:
        seed_tau_ns = max(0.1 * window_ns, 5.0 * dt)
    seed_tau_ns = float(np.clip(seed_tau_ns, 2.0 * dt, 0.9 * hi_tau))

    tiny = 1e-12
    if model == "mono":
        #        a1                tau1            offset   shift
        p0 = [max(peak, 1.0), seed_tau_ns, max(base, 0.0), 0.0]
        lo = [0.0, 2.0 * dt, 0.0, -n / 4.0]
        hi = [np.inf, hi_tau, max(peak, 1.0), n / 4.0]
    elif model == "bi":
        p0 = [max(peak, 1.0), seed_tau_ns, 0.3 * max(peak, 1.0), seed_tau_ns / 3.0,
              max(base, 0.0), 0.0]
        lo = [0.0, 2.0 * dt, 0.0, 2.0 * dt, 0.0, -n / 4.0]
        hi = [np.inf, hi_tau, np.inf, hi_tau, max(peak, 1.0), n / 4.0]
    else:
        raise ValueError(f"unknown model {model!r} (expected 'mono' or 'bi').")

    if not fit_shift:                       # freeze shift at 0 by pinning bounds
        p0[-1] = 0.0
        lo[-1], hi[-1] = -tiny, tiny

    def unpack(p):
        if model == "mono":
            a1, tau1, offset, shift = p
            return [tau1], [a1], offset, shift
        a1, tau1, a2, tau2, offset, shift = p
        return [tau1, tau2], [a1, a2], offset, shift

    def resid(p):
        taus, amps, offset, shift = unpack(p)
        mu = decay_model(taus, amps, offset, shift, kernel, dt, n)
        return _residuals(y, mu, objective)

    res = least_squares(resid, p0, bounds=(lo, hi), method="trf", max_nfev=2000)
    taus, amps, offset, shift = unpack(res.x)
    taus = np.asarray(taus, float); amps = np.asarray(amps, float)
    mu = decay_model(taus, amps, offset, shift, kernel, dt, n)
    n_params = len(res.x) - (1 if not fit_shift else 0)
    sigma_all = _param_sigma(res, len(res.x))
    # amplitude-weighted mean lifetime, and σ on each τ (τ params are odd indices)
    tau_idx = [1] if model == "mono" else [1, 3]
    sigma_tau = np.asarray([sigma_all[i] for i in tau_idx], float)
    wsum = float(np.sum(amps))
    tau_mean = float(np.sum(amps * taus) / wsum) if wsum else float("nan")

    # Report components longest-τ first for a stable, comparable ordering.
    order = np.argsort(taus)[::-1]
    return FitResult(
        taus_ns=taus[order], amps=amps[order], offset=float(offset),
        shift_bins=float(shift), tau_mean_ns=tau_mean,
        sigma_tau_ns=sigma_tau[order], reduced_chi2=reduced_chi_square(y, mu, n_params),
        success=bool(res.success), n_params=n_params, model=mu,
        objective=objective, kind=model,
    )


# --------------------------------------------------------------------------- #
# Per-pixel τ-map
# --------------------------------------------------------------------------- #
def fit_map(
    counts: np.ndarray,
    irf: IRF,
    resolution_ns: float,
    model: str = "mono",
    objective: str = "mle",
    photon_threshold: float = 100.0,
    seed_tau_map: np.ndarray | None = None,
    seed_tau_ns: float | None = None,
    fit_shift: bool = True,
    progress=None,
    cancel=None,
) -> dict:
    """Per-pixel reconvolution τ-map over a ``(Y, X, H)`` cube.

    Only pixels whose total photons ≥ ``photon_threshold`` are fitted (the rest
    are NaN) -- this is what makes a per-pixel nonlinear fit tractable on a real
    image. Each pixel is seeded from ``seed_tau_map`` (e.g. an RLD τ map) when
    given, else ``seed_tau_ns``. ``progress(done, total)`` is called as fits
    complete; if ``cancel()`` returns True the run stops early (already-fitted
    pixels are kept, the rest stay NaN).

    Returns a dict of ``(Y, X)`` maps: ``tau`` (amplitude-weighted mean lifetime),
    ``tau1`` (principal τ), ``sigma_tau`` (1σ on the principal τ), ``chi2``
    (reduced χ²), plus ``fitted`` (bool mask) and ``n_fitted``.
    """
    counts = np.asarray(counts, dtype=np.float64)
    ny, nx, nh = counts.shape
    total = counts.sum(axis=-1)
    mask = total >= float(photon_threshold)

    tau = np.full((ny, nx), np.nan)
    tau1 = np.full((ny, nx), np.nan)
    sigma = np.full((ny, nx), np.nan)
    chi2 = np.full((ny, nx), np.nan)

    idx = np.argwhere(mask)
    n_total = int(idx.shape[0])
    done = 0
    for (yy, xx) in idx:
        if cancel is not None and cancel():
            break
        seed = None
        if seed_tau_map is not None:
            s = seed_tau_map[yy, xx]
            seed = float(s) if np.isfinite(s) else seed_tau_ns
        else:
            seed = seed_tau_ns
        try:
            fr = fit_decay(counts[yy, xx], irf, resolution_ns, model=model,
                           objective=objective, seed_tau_ns=seed, fit_shift=fit_shift)
            tau[yy, xx] = fr.tau_mean_ns
            tau1[yy, xx] = fr.tau_ns
            sigma[yy, xx] = float(fr.sigma_tau_ns[0])
            chi2[yy, xx] = fr.reduced_chi2
        except Exception:      # noqa: BLE001 - a bad pixel stays NaN, never aborts the map
            pass
        done += 1
        if progress is not None:
            progress(done, n_total)

    return {
        "tau": tau, "tau1": tau1, "sigma_tau": sigma, "chi2": chi2,
        "fitted": mask & np.isfinite(tau1), "n_fitted": int(np.isfinite(tau1).sum()),
        "photon_threshold": float(photon_threshold), "model": model,
        "objective": objective,
    }
