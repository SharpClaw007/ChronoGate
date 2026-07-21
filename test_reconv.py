"""Adversarial validation of the IRF-reconvolution lifetime engine (reconv.py).

These tests are deliberately hostile to the implementation: the "ground truth"
decays are built by an **independent** method (an explicit multi-pulse excitation
train convolved *linearly* with a long exponential, then folded into one period)
so that a bug shared between the model's forward and inverse paths cannot hide.
They also pin the periodicity lynchpin (incomplete-decay wrap-around) and cross-
check the recovered lifetime against the two other, independent engines (two-gate
RLD and phasor).
"""

from __future__ import annotations

import sys

# Match the app: never let a τ/φ/→ print crash a cp1252 console.
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError, OSError):
        pass

import numpy as np

from chronogate import gating, reconv


# --------------------------------------------------------------------------- #
# Independent ground-truth builder (uses only linear convolution + an explicit
# pulse train -- shares no code path with reconv's rfft circular model).
# --------------------------------------------------------------------------- #
def _periodic_measured_decay(kernel, tau_bins, n, n_periods=60, amp=1.0):
    """One steady-state period of an IRF-convolved periodic mono-exponential.

    Excitation = deltas at 0, n, 2n, ...; single-pulse response = exp(-j/tau);
    both convolved **linearly** over many periods, then the last (steady-state)
    period is returned. This is the physically correct incomplete-decay shape,
    constructed without any FFT / circular trick.
    """
    L = n * (n_periods + 1)
    j = np.arange(L)
    single = amp * np.exp(-j / float(tau_bins))          # response to one pulse
    train = np.zeros(L)
    train[: n_periods * n : n] = 1.0                     # a delta every period
    decay_full = np.convolve(train, single)[:L]          # linear superposition
    measured = np.convolve(decay_full, kernel)[:L]       # linear IRF convolution
    return measured[n_periods * n - n: n_periods * n]    # a fully steady period


def _assert(cond, msg):
    if not cond:
        raise AssertionError(msg)


# --------------------------------------------------------------------------- #
def test_model_matches_independent_periodic_construction():
    """reconv's circular-conv model must equal the independent periodic decay.

    If this passes for a lifetime comparable to the window (heavy incomplete
    decay), the circular-convolution periodicity handling is correct -- a linear
    convolution would visibly mismatch at the window edges.
    """
    n, dt = 256, 0.05
    tau_bins = 120.0                     # ~half the window: strong wrap-around
    tau_ns = tau_bins * dt
    kernel = reconv.IRF.gaussian(0.4, 0.25, n, dt).kernel(n, dt)

    truth = _periodic_measured_decay(kernel, tau_bins, n)
    # reconv model with amplitude/offset matched to the same construction.
    model = reconv.decay_model([tau_ns], [1.0], 0.0, 0.0, kernel, dt, n)
    # Compare shapes after normalising to unit sum (amplitude is a free param).
    a = truth / truth.sum()
    b = model / model.sum()
    rel = np.max(np.abs(a - b)) / np.max(a)
    _assert(rel < 5e-3, f"circular model vs independent periodic decay: max rel err {rel:.2e}")
    print(f"OK: circular reconvolution matches the independent periodic decay "
          f"(max rel err {rel:.1e}, tau={tau_ns:.2f} ns, ~half-window).")


def test_recover_lifetime_noiseless():
    """Fitting the independent ground-truth decay recovers τ to <0.5%."""
    n, dt = 512, 0.05
    kernel_irf = reconv.IRF.gaussian(0.5, 0.3, n, dt)
    kernel = kernel_irf.kernel(n, dt)
    for tau_bins in (30.0, 80.0, 160.0):
        tau_ns = tau_bins * dt
        y = _periodic_measured_decay(kernel, tau_bins, n, amp=5000.0)
        fr = reconv.fit_decay(y, kernel_irf, dt, model="mono", objective="chi2")
        err = abs(fr.tau_ns - tau_ns) / tau_ns
        _assert(fr.success, f"fit failed for tau={tau_ns}")
        _assert(err < 5e-3, f"tau={tau_ns:.3f} recovered {fr.tau_ns:.3f} (err {err:.2%})")
    print("OK: noiseless reconvolution recovers tau to <0.5% across 3 lifetimes "
          "(including a long, incomplete-decay lifetime).")


def test_delta_irf_agrees_with_rld_and_phasor():
    """With a δ IRF, reconv τ must agree with the two independent engines.

    A delta instrument response with the decay starting at the first bin reduces
    the measured signal to a pure sampled exponential -- the exact common ground
    where two-gate RLD and phasor are also exact -- so all three lifetime engines
    must land on the same τ. Ties the rigorous fit to the fit-free tools. (A
    delayed onset / finite IRF is covered separately by ``test_irf_shift_is_fit``;
    a delta IRF physically implies zero electronics delay, hence onset at bin 0.)
    """
    n, dt = 400, 0.05
    tau_ns = 2.5
    t = np.arange(n)
    decay = np.exp(-t * dt / tau_ns) * 8000.0        # pure exponential from bin 0
    y = decay.copy()

    delta = np.zeros(n); delta[0] = 1.0
    irf = reconv.IRF.from_decay(delta, dt)
    fr = reconv.fit_decay(y, irf, dt, model="mono", objective="chi2")

    # RLD from two equal-width gates (geometric sums cancel -> exact for a
    # sampled exponential).
    G, gap = 20, 40
    a_lo, a_hi = 5, 5 + G
    b_lo, b_hi = a_lo + gap, a_hi + gap
    na = decay[a_lo:a_hi + 1].sum()
    nb = decay[b_lo:b_hi + 1].sum()
    tau_rld = float(gating.rld_lifetime(na, nb, (b_lo - a_lo) * dt))

    # Phasor τ from the semicircle: tan(phi) = ω τ  ->  τ = s/g / ω.
    g, s = gating.phasor(decay[None, None, :], float(n), t0_bin=0.0)
    w = 2 * np.pi / n
    tau_phasor = (s[0, 0] / g[0, 0]) / w * dt

    for name, val in (("RLD", tau_rld), ("phasor", tau_phasor)):
        err = abs(fr.tau_ns - val) / tau_ns
        _assert(err < 0.06, f"reconv {fr.tau_ns:.3f} vs {name} {val:.3f} (err {err:.2%})")
    print(f"OK: delta-IRF reconv tau={fr.tau_ns:.3f} agrees with RLD={tau_rld:.3f} "
          f"and phasor={tau_phasor:.3f} ns (all within 6%).")


def test_poisson_noise_bias_and_reported_sigma():
    """Under Poisson noise the fit is ~unbiased and its reported σ tracks scatter.

    Fits many noisy realisations of the same decay: the mean τ must sit near
    truth, and the *median reported* σ_τ (from the fit covariance) must be within
    a factor ~2 of the empirical standard deviation of the fitted τ.
    """
    n, dt = 400, 0.05
    tau_ns = 2.0
    kernel_irf = reconv.IRF.gaussian(0.4, 0.25, n, dt)
    kernel = kernel_irf.kernel(n, dt)
    clean = _periodic_measured_decay(kernel, tau_ns / dt, n, amp=800.0)
    clean = np.clip(clean, 0.0, None)

    rng = np.random.default_rng(0)
    taus, sigmas = [], []
    for _ in range(60):
        y = rng.poisson(clean).astype(float)
        fr = reconv.fit_decay(y, kernel_irf, dt, model="mono", objective="mle",
                              seed_tau_ns=tau_ns)
        if fr.success and np.isfinite(fr.tau_ns):
            taus.append(fr.tau_ns); sigmas.append(fr.sigma_tau_ns[0])
    taus = np.asarray(taus); sigmas = np.asarray(sigmas[: len(taus)])
    mean_tau = float(np.mean(taus)); emp_sigma = float(np.std(taus))
    bias = abs(mean_tau - tau_ns) / tau_ns
    _assert(len(taus) >= 50, f"too many fits failed ({len(taus)}/60)")
    _assert(bias < 0.05, f"mean tau {mean_tau:.3f} biased {bias:.2%} from {tau_ns}")
    med_sig = float(np.nanmedian(sigmas))
    ratio = med_sig / emp_sigma if emp_sigma else np.inf
    _assert(0.4 < ratio < 2.5, f"reported σ {med_sig:.3f} vs empirical {emp_sigma:.3f} (ratio {ratio:.2f})")
    print(f"OK: Poisson fits ~unbiased (mean tau {mean_tau:.3f}, bias {bias:.2%}); "
          f"reported σ {med_sig:.3f} ≈ empirical {emp_sigma:.3f} (ratio {ratio:.2f}).")


def test_reduced_chi2_near_one_on_correct_model():
    """A correctly-modelled noisy decay yields reduced χ² ≈ 1 (averaged)."""
    n, dt = 400, 0.05
    tau_ns = 2.0
    kernel_irf = reconv.IRF.gaussian(0.4, 0.25, n, dt)
    kernel = kernel_irf.kernel(n, dt)
    clean = np.clip(_periodic_measured_decay(kernel, tau_ns / dt, n, amp=1500.0), 0.0, None)
    rng = np.random.default_rng(1)
    chis = []
    for _ in range(40):
        y = rng.poisson(clean).astype(float)
        fr = reconv.fit_decay(y, kernel_irf, dt, model="mono", objective="mle",
                              seed_tau_ns=tau_ns)
        if fr.success:
            chis.append(fr.reduced_chi2)
    mean_chi = float(np.mean(chis))
    _assert(0.8 < mean_chi < 1.3, f"mean reduced chi2 {mean_chi:.3f} not ≈ 1")
    print(f"OK: reduced chi2 ≈ 1 on the correct model (mean {mean_chi:.3f} over 40 draws).")


def test_bi_exponential_recovers_two_lifetimes():
    """A two-component decay recovers both lifetimes (well-separated, noiseless)."""
    n, dt = 512, 0.05
    tau1_ns, tau2_ns = 4.0, 0.8            # 5x separation
    kernel_irf = reconv.IRF.gaussian(0.4, 0.25, n, dt)
    kernel = kernel_irf.kernel(n, dt)
    y = (_periodic_measured_decay(kernel, tau1_ns / dt, n, amp=6000.0)
         + _periodic_measured_decay(kernel, tau2_ns / dt, n, amp=6000.0))
    fr = reconv.fit_decay(y, kernel_irf, dt, model="bi", objective="chi2",
                          seed_tau_ns=2.0)
    got = np.sort(fr.taus_ns)[::-1]        # longest first
    e1 = abs(got[0] - tau1_ns) / tau1_ns
    e2 = abs(got[1] - tau2_ns) / tau2_ns
    _assert(e1 < 0.1 and e2 < 0.15, f"bi-exp recovered {got} vs ({tau1_ns},{tau2_ns})")
    print(f"OK: bi-exponential recovers tau1={got[0]:.2f}, tau2={got[1]:.2f} ns "
          f"(truth {tau1_ns}, {tau2_ns}).")


def test_irf_shift_is_fit():
    """A timing offset between IRF and decay is absorbed by the shift param."""
    n, dt = 400, 0.05
    tau_ns = 2.0
    kernel_irf = reconv.IRF.gaussian(0.5, 0.3, n, dt)
    kernel = kernel_irf.kernel(n, dt)
    y = _periodic_measured_decay(kernel, tau_ns / dt, n, amp=5000.0)
    y = np.roll(y, 7)                      # decay is 7 bins late vs the IRF
    fr = reconv.fit_decay(y, kernel_irf, dt, model="mono", objective="chi2",
                          seed_tau_ns=tau_ns, fit_shift=True)
    err = abs(fr.tau_ns - tau_ns) / tau_ns
    _assert(abs(fr.shift_bins - 7) < 1.0, f"shift {fr.shift_bins:.2f} not ~7 bins")
    _assert(err < 0.03, f"tau {fr.tau_ns:.3f} off {err:.2%} despite fitted shift")
    print(f"OK: fitted IRF shift {fr.shift_bins:.2f}≈7 bins; tau recovered ({fr.tau_ns:.3f}).")


def test_fit_map_thresholds_and_recovers():
    """Per-pixel map: only above-threshold pixels are fitted, and τ is recovered."""
    n, dt = 300, 0.05
    tau_ns = 2.0
    kernel_irf = reconv.IRF.gaussian(0.4, 0.25, n, dt)
    kernel = kernel_irf.kernel(n, dt)
    bright = np.clip(_periodic_measured_decay(kernel, tau_ns / dt, n, amp=4000.0), 0.0, None)
    dim = bright / bright.sum() * 20.0     # only ~20 photons: below the threshold
    cube = np.zeros((2, 2, n))
    cube[0, 0] = bright; cube[0, 1] = bright
    cube[1, 0] = dim; cube[1, 1] = dim

    seen = []
    res = reconv.fit_map(cube, kernel_irf, dt, model="mono", objective="chi2",
                         photon_threshold=100.0, seed_tau_ns=tau_ns,
                         progress=lambda d, t: seen.append((d, t)))
    _assert(res["n_fitted"] == 2, f"expected 2 fitted pixels, got {res['n_fitted']}")
    _assert(np.isfinite(res["tau1"][0, 0]) and np.isnan(res["tau1"][1, 0]),
            "threshold mask wrong: bright fitted, dim NaN")
    err = abs(res["tau1"][0, 0] - tau_ns) / tau_ns
    _assert(err < 0.02, f"map tau {res['tau1'][0,0]:.3f} off {err:.2%}")
    _assert(seen and seen[-1][0] == seen[-1][1], "progress did not reach total")
    print(f"OK: fit_map fitted {res['n_fitted']}/4 pixels (threshold honoured), "
          f"tau {res['tau1'][0,0]:.3f} ns, progress reached {seen[-1]}.")


def test_cancel_stops_the_map_early():
    """A cancel callback stops the per-pixel run without fitting every pixel."""
    n, dt = 200, 0.05
    kernel_irf = reconv.IRF.gaussian(0.4, 0.25, n, dt)
    kernel = kernel_irf.kernel(n, dt)
    bright = np.clip(_periodic_measured_decay(kernel, 2.0 / dt, n, amp=4000.0), 0.0, None)
    cube = np.broadcast_to(bright, (3, 3, n)).copy()   # 9 fittable pixels
    calls = {"n": 0}

    def cancel():
        calls["n"] += 1
        return calls["n"] > 2                            # bail after ~2 pixels
    res = reconv.fit_map(cube, kernel_irf, dt, seed_tau_ns=2.0, cancel=cancel)
    _assert(res["n_fitted"] < 9, f"cancel ignored: fitted {res['n_fitted']}/9")
    print(f"OK: cancel stopped the map early ({res['n_fitted']}/9 pixels fitted).")


if __name__ == "__main__":
    tests = [
        test_model_matches_independent_periodic_construction,
        test_recover_lifetime_noiseless,
        test_delta_irf_agrees_with_rld_and_phasor,
        test_poisson_noise_bias_and_reported_sigma,
        test_reduced_chi2_near_one_on_correct_model,
        test_bi_exponential_recovers_two_lifetimes,
        test_irf_shift_is_fit,
        test_fit_map_thresholds_and_recovers,
        test_cancel_stops_the_map_early,
    ]
    try:
        for t in tests:
            t()
    except AssertionError as exc:
        import traceback
        traceback.print_exc()
        print(f"FAIL: {exc}", file=sys.stderr)
        raise SystemExit(1)
    print("All reconvolution tests passed.")
