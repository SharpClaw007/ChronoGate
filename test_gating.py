"""Correctness test: prefix-sum gating must equal a direct per-bin sum.

Runs against a real example file (no synthetic data) so it also exercises the
ptufile parsing path. Run directly::

    python test_gating.py

or under pytest::

    pytest test_gating.py

It is also safe to run before the GUI exists -- it imports nothing from the
viewer.
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

from chronogate import gating
from chronogate.loader import FlimCube, find_stack, load_ptu

# Smallest example file = fastest to decode; any .ptu in the folder works.
DATA_DIR = Path(__file__).resolve().parent / "3_FLIM_stack_ptu"


def _example_file() -> Path:
    candidates = sorted(DATA_DIR.rglob("*.ptu"), key=lambda p: p.stat().st_size)
    if not candidates:
        raise FileNotFoundError(
            f"No .ptu files under {DATA_DIR}. Point this test at your data folder."
        )
    return candidates[0]


def test_prefix_sum_matches_direct_sum() -> None:
    cube = load_ptu(_example_file())
    counts = cube.counts
    prefix = gating.build_prefix_sum(counts)
    n = cube.n_bins

    # 1) Parse fidelity: the cube must contain exactly the file's photons for
    #    the selected channel (here, the only channel) -- nothing lost on decode.
    assert counts.sum() == cube.intensity.sum()

    # 2) The whole-range gate must reproduce the per-pixel intensity image.
    full = gating.gate_image(prefix, 0, n - 1)
    assert np.array_equal(full, counts.sum(axis=-1, dtype=np.int64))

    # 3) Random gates: prefix-sum result == direct slice-and-sum, exactly.
    rng = np.random.default_rng(0)
    for _ in range(50):
        lo, hi = sorted(rng.integers(0, n, size=2).tolist())
        fast = gating.gate_image(prefix, lo, hi)
        direct = counts[..., lo : hi + 1].sum(axis=-1, dtype=np.int64)
        assert np.array_equal(fast, direct), f"mismatch for gate [{lo}, {hi}]"

    # 4) Edge cases: a single-bin gate and a swapped (hi < lo) gate.
    one = gating.gate_image(prefix, 10, 10)
    assert np.array_equal(one, counts[..., 10:11].sum(axis=-1, dtype=np.int64))
    swapped = gating.gate_image(prefix, 20, 5)
    assert np.array_equal(swapped, gating.gate_image(prefix, 5, 20))

    print("OK: prefix-sum gating matches direct summation for all tested gates.")


def test_time_axis_is_calibrated() -> None:
    cube = load_ptu(_example_file())
    assert cube.resolution_ns > 0, "microtime resolution must be positive"
    # The decay window (n_bins * bin width) should be on the order of the laser
    # period -- a sanity check that the ns calibration is physically sensible.
    window_ns = cube.n_bins * cube.resolution_ns
    assert 0 < window_ns < 1000, f"decay window {window_ns:.2f} ns looks wrong"
    if np.isfinite(cube.period_ns):
        assert window_ns >= 0.5 * cube.period_ns, "decay window much shorter than laser period"

    decay = gating.total_decay(cube.counts)
    t0 = gating.detect_t0_bin(decay)
    assert 0 <= t0 < cube.n_bins
    print(f"OK: {cube.n_bins} bins @ {cube.resolution_ns*1000:.2f} ps, "
          f"window {window_ns:.2f} ns, t0 at bin {t0} ({gating.bin_to_ns(t0, cube.resolution_ns):.2f} ns).")


def test_spatial_binning_matches_brute_force() -> None:
    # Sliding box-sum binning must equal an explicit neighbourhood sum (with
    # edge clamping), and GatingModel must use the binned counts throughout.
    rng = np.random.default_rng(1)
    a = rng.integers(0, 7, size=(9, 11, 4)).astype(np.uint16)

    def brute_box(counts, B):
        ny, nx, nh = counts.shape
        out = np.zeros((ny, nx, nh), dtype=np.int64)
        h = B // 2
        for y in range(ny):
            for x in range(nx):
                r0, r1 = max(0, y - h), min(ny, y - h + B)
                c0, c1 = max(0, x - h), min(nx, x - h + B)
                out[y, x] = counts[r0:r1, c0:c1, :].sum(axis=(0, 1))
        return out

    for b in (1, 2, 3, 5):
        got = np.asarray(gating.spatial_bin(a, b), dtype=np.int64)
        assert np.array_equal(got, brute_box(a, b)), f"binning mismatch at B={b}"

    # Auto suggestion follows B = ceil(sqrt(target / n0)).
    inten = np.concatenate([np.zeros(50), np.full(50, 25.0)])  # median signal 25
    b, n0 = gating.suggest_bin_factor(inten, target_photons=100)
    assert n0 == 25.0 and b == 2

    cube = load_ptu(_example_file())
    m = gating.GatingModel(cube, bin_factor=3)
    assert m.bin_factor == 3
    assert np.array_equal(m.intensity, gating.spatial_bin(cube.counts, 3).sum(axis=-1, dtype=np.int64))
    print("OK: spatial binning matches brute force; GatingModel uses binned counts.")


def _synthetic_exponential_cube(tau_ns: float, res_ns: float, n_bins: int,
                                t0_bin: int, amp: float) -> np.ndarray:
    """A noise-free (Y, X, H) cube whose every pixel is a mono-exponential.

    Counts before ``t0_bin`` are zero (no pre-pulse signal); from ``t0_bin`` on
    each bin holds ``round(amp * exp(-(t - t0) / tau))`` photons. Large ``amp``
    keeps integer rounding negligible so RLD should recover ``tau`` tightly.
    """
    t = np.arange(n_bins) * res_ns
    decay = np.where(t >= t0_bin * res_ns,
                     amp * np.exp(-(t - t0_bin * res_ns) / tau_ns), 0.0)
    counts = np.rint(decay).astype(np.uint16)
    return np.broadcast_to(counts, (4, 5, n_bins)).copy()


def test_rld_recovers_known_lifetime() -> None:
    # 1) The pure estimator: exact integrals of a known exponential -> exact tau.
    res, n, t0, tau_true = 0.1, 256, 5, 2.5
    G = 20  # equal gate width in bins
    a_lo, a_hi = t0, t0 + G - 1
    b_lo, b_hi = t0 + G, t0 + 2 * G - 1

    def integ(lo, hi):  # exact integral of exp(-(t-t0)/tau) over the gate, in ns
        ta = (lo - t0) * res
        tb = (hi + 1 - t0) * res
        return tau_true * (np.exp(-ta / tau_true) - np.exp(-tb / tau_true))

    dt = (b_lo - a_lo) * res
    tau = float(gating.rld_lifetime(integ(a_lo, a_hi), integ(b_lo, b_hi), dt))
    assert abs(tau - tau_true) < 1e-9, f"pure RLD gave {tau}, expected {tau_true}"

    # 2) Masking: too few photons, no decay (na<=nb), and dt<=0 all give NaN.
    assert np.isnan(gating.rld_lifetime(3.0, 1.0, dt, min_counts=10))   # photon-starved
    assert np.isnan(gating.rld_lifetime(50.0, 60.0, dt))               # not decaying
    assert np.isnan(gating.rld_lifetime(100.0, 10.0, 0.0))             # zero separation

    # 3) End-to-end through GatingModel on a synthetic cube: recover tau per pixel.
    counts = _synthetic_exponential_cube(tau_true, res, n, t0, amp=5000.0)
    cube = FlimCube(counts=counts, resolution_ns=res, period_ns=res * n, n_bins=n,
                    record_type="synthetic", channel=0, n_channels=1,
                    frame_mode="single frame", n_frames=1, n_photons=int(counts.sum()),
                    path=Path("synthetic.ptu"))
    m = gating.GatingModel(cube)
    rl = m.rapid_lifetime((a_lo, a_hi), (b_lo, b_hi), min_counts=10)
    assert rl["equal_width"] and rl["dt_ns"] == dt
    finite = rl["tau"][np.isfinite(rl["tau"])]
    assert finite.size == counts.shape[0] * counts.shape[1], "all pixels should be valid"
    assert abs(float(np.median(finite)) - tau_true) < 0.05, \
        f"recovered tau {np.median(finite):.3f} ns, expected {tau_true}"
    print(f"OK: two-gate RLD recovers tau = {np.median(finite):.3f} ns "
          f"(true {tau_true}); masking and Delta-t=0 guards hold.")


def test_mono_exponential_fit_recovers_tau():
    """The display fit should recover a known tau from a noisy, low-count decay."""
    rng = np.random.default_rng(0)
    res = 25.0 / 264
    t = np.arange(264) * res
    t0, tau_true = 1.0, 2.5
    ideal = np.where(t >= t0, 120.0 * np.exp(-(t - t0) / tau_true), 0.0)
    noisy = rng.poisson(ideal).astype(float)          # Poisson noise -> jagged tail

    fit = gating.fit_mono_exponential(t, noisy, t0)
    assert fit is not None, "fit should succeed on a decaying curve"
    amp, tau = fit
    assert abs(tau - tau_true) < 0.3, f"recovered tau {tau:.3f} not near {tau_true}"

    y = gating.mono_exponential_curve(t, t0, amp, tau)
    assert np.isnan(y[0]) and np.isfinite(y[-1]), "curve starts at t0 (NaN before)"
    assert np.all(np.diff(y[t >= t0]) <= 0), "the fitted curve is smooth & monotonically decaying"

    # Flat / non-decaying data yields no fit (guarded, not a bad curve).
    assert gating.fit_mono_exponential(t, np.ones_like(t) * 5.0, t0) is None
    print(f"OK: mono-exp fit recovers tau ~ {tau:.2f} ns from noisy low counts.")


if __name__ == "__main__":
    try:
        test_prefix_sum_matches_direct_sum()
        test_time_axis_is_calibrated()
        test_spatial_binning_matches_brute_force()
        test_rld_recovers_known_lifetime()
        test_mono_exponential_fit_recovers_tau()
    except AssertionError as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        raise SystemExit(1)
    print("All gating tests passed.")
