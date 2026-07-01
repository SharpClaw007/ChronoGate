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


def _synthetic_irf_cube(a_scatter: float):
    """A (Y, X, H) cube of identical pixels: a_scatter*IRF + convolved fluorescence."""
    n, res = 400, 0.016
    t = np.arange(n)
    irf = np.exp(-0.5 * ((t - 60) / 5.0) ** 2)
    irf /= irf.sum()  # unit area
    decay = np.where(t >= 0, np.exp(-t / 120.0), 0.0)
    fluor = np.convolve(decay, irf)[:n]
    fluor *= 4000.0 / fluor.max()
    pix = a_scatter * irf + fluor
    counts = np.round(np.broadcast_to(pix, (10, 10, n))).astype(np.uint16).copy()
    cube = FlimCube(counts=counts, resolution_ns=res, period_ns=res * n, n_bins=n,
                    record_type="synthetic", channel=0, n_channels=1,
                    frame_mode="single frame", n_frames=1, n_photons=int(counts.sum()),
                    path=Path("synthetic.ptu"))
    return cube, irf


def test_irf_isolation_and_subtraction() -> None:
    cube, irf = _synthetic_irf_cube(2500.0)
    m = gating.GatingModel(cube)
    m.set_irf(irf)

    # 1) t0 + the instrument window come from the IRF (rigorous t0).
    assert m.t0_bin == int(np.argmax(irf))
    lo, hi = m.instrument_window
    assert lo <= int(np.argmax(irf)) <= hi, "instrument window must bracket the IRF peak"

    # 2) the IRF-subtracted gate equals a direct per-bin subtraction (clamped) --
    #    the prefix-sum form is exact and stays O(pixels).
    m.irf_subtract = True
    a = m.irf_amplitude()
    g = m.gate(40, 120)
    raw = m._counts[:, :, 40:121].sum(-1).astype(float)
    direct = np.clip(raw - a * float(irf[40:121].sum()), 0, None)
    assert np.allclose(g, direct), "prefix-sum IRF subtraction must match direct"

    # 3) at scale=1 the subtraction removes exactly the prompt-window signal,
    #    so the instrument-window gate of the residual is ~0 (well-defined op).
    win = m.gate(lo, hi)
    assert np.allclose(win, 0.0), "scale=1 must remove the whole instrument window"

    # 4) brighter pixels get a proportionally larger amplitude (intensity-anchored).
    cube2, irf2 = _synthetic_irf_cube(2500.0)
    cube2.counts[0, 0] = (cube2.counts[0, 0].astype(np.int64) * 3).clip(0, 65535).astype(np.uint16)
    m2 = gating.GatingModel(cube2); m2.set_irf(irf2)
    amp = m2.irf_amplitude()
    assert amp[0, 0] > 1.5 * float(np.median(amp)), "amplitude must scale with prompt intensity"
    print("OK: IRF t0/window, exact prefix-sum subtraction, well-defined scale, intensity-anchored.")


if __name__ == "__main__":
    try:
        test_prefix_sum_matches_direct_sum()
        test_time_axis_is_calibrated()
        test_spatial_binning_matches_brute_force()
        test_rld_recovers_known_lifetime()
        test_irf_isolation_and_subtraction()
    except AssertionError as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        raise SystemExit(1)
    print("All gating tests passed.")
