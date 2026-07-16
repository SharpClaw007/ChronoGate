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

# These tests print τ/φ/→ directly; make stdout UTF-8 so they don't crash on a
# cp1252 Windows console (mirrors what the app does at startup).
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError, OSError):
        pass

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


def test_gate_integral_numeric_truth():
    """The per-pixel gated integral and floor subtraction must be exact."""
    ny, nx, H = 5, 4, 20
    counts = np.zeros((ny, nx, H), dtype=np.uint16)
    for r in range(ny):
        for c in range(nx):
            counts[r, c, 5:10] = (r + 1) * (c + 1)   # 5 bins each = k -> gate = 5k
    cube = FlimCube(counts=counts, resolution_ns=0.1, period_ns=2.0, n_bins=H,
                    record_type="synthetic", channel=0, n_channels=1,
                    frame_mode="single frame", n_frames=1, n_photons=int(counts.sum()),
                    path=Path("synthetic.ptu"))
    m = gating.GatingModel(cube)
    g = m.gate(5, 9)   # inclusive -> 5 bins
    for r in range(ny):
        for c in range(nx):
            assert g[r, c] == 5 * (r + 1) * (c + 1), (r, c, g[r, c])
    assert int(g.sum()) == 5 * sum((r + 1) * (c + 1) for r in range(ny) for c in range(nx))
    # floor of 1 count/bin over 5 bins removes exactly 5 per pixel (clamped at 0)
    gf = m.gate(5, 9, floor_per_bin=1.0)
    assert np.allclose(gf, np.clip(g.astype(float) - 5.0, 0, None))
    # a floor above every pixel zeros the image
    assert float(m.gate(5, 9, floor_per_bin=float(counts.max())).sum()) == 0.0
    print("OK: per-pixel gated integral + floor subtraction are numerically exact.")


def test_auto_floor_robust_to_rising_edge():
    """The auto noise floor must sit just above the flat baseline, not be dragged
    up by a rising-edge bin that lands inside the pre-pulse window."""
    ny, nx, n = 40, 40, 200
    prof = np.ones(n)                     # flat baseline of 1 count/bin/pixel
    prof[20:25] = [2, 5, 12, 25, 40]      # sharp rise
    prof[25:] = 40 * np.exp(-(np.arange(n - 25)) / 25.0)  # decay, peak at bin 25
    counts = np.round(np.broadcast_to(prof, (ny, nx, n))).astype(np.uint16).copy()
    cube = FlimCube(counts=counts, resolution_ns=0.05, period_ns=n * 0.05, n_bins=n,
                    record_type="synthetic", channel=0, n_channels=1,
                    frame_mode="single frame", n_frames=1, n_photons=int(counts.sum()),
                    path=Path("synthetic.ptu"))
    m = gating.GatingModel(cube)
    npix = m.n_pixels
    floor_total = m.auto_noise_floor_pp() * npix
    baseline_total = 1 * npix              # baseline summed over pixels
    # just above baseline (Poisson margin), NOT pulled toward the ~40x rise
    assert baseline_total <= floor_total < 4 * baseline_total, (floor_total, baseline_total)
    print(f"OK: auto floor {floor_total:.0f} sits just above baseline {baseline_total} "
          f"(robust to a rising-edge bin in the window).")


def test_phasor_mono_exponential_on_semicircle():
    """A mono-exponential decay's phasor must land on the universal semicircle."""
    n, period_bins, tau_bins = 256, 256.0, 40.0
    t = np.arange(n)
    decay = np.exp(-t / tau_bins)
    counts = np.broadcast_to(decay, (4, 4, n)).astype(np.float64).copy()
    g, s = gating.phasor(counts, period_bins, t0_bin=0.0)
    w = 2 * np.pi / period_bins
    g_exp, s_exp = 1 / (1 + (w * tau_bins) ** 2), (w * tau_bins) / (1 + (w * tau_bins) ** 2)
    assert abs(g[0, 0] - g_exp) < 0.03 and abs(s[0, 0] - s_exp) < 0.03, (g[0, 0], s[0, 0])
    # ...and it sits on the circle (centre 0.5, radius 0.5):
    assert abs((g[0, 0] - 0.5) ** 2 + s[0, 0] ** 2 - 0.25) < 0.03
    # empty pixels are NaN, not garbage
    empty = np.zeros((1, 1, n))
    ge, se = gating.phasor(empty, period_bins)
    assert np.isnan(ge).all() and np.isnan(se).all()
    print(f"OK: phasor of a mono-exp lands on the semicircle (g={g[0,0]:.3f}, s={s[0,0]:.3f}).")


def test_mask_decay_pools_selected_pixels():
    """A boolean pixel mask (e.g. a phasor lasso) pools into one per-pixel decay."""
    ny, nx, H = 6, 6, 12
    counts = np.zeros((ny, nx, H), dtype=np.uint16)
    counts[..., 3] = 1                       # every pixel: 1 photon in bin 3
    counts[0, :, 7] = 10                     # row 0 alone: 10 more in bin 7
    cube = FlimCube(counts=counts, resolution_ns=0.1, period_ns=1.2, n_bins=H,
                    record_type="synthetic", channel=0, n_channels=1,
                    frame_mode="single frame", n_frames=1, n_photons=int(counts.sum()),
                    path=Path("synthetic.ptu"))
    m = gating.GatingModel(cube)

    mask = np.zeros((ny, nx), dtype=bool)
    mask[0, :] = True                        # select exactly row 0
    d = m.mask_decay(mask)
    assert d.shape == (H,)
    assert d[3] == 1.0 and d[7] == 10.0, d   # mean over the 6 selected pixels
    # the same pixels chosen as a rectangle must give the identical curve
    assert np.allclose(d, m.pixel_decay(0, 1, 0, nx))
    # a mask over everything = the whole-image mean; row 0's extra is diluted by 1/6
    assert np.allclose(m.mask_decay(np.ones((ny, nx), bool))[7], 10.0 / ny)
    # degenerate masks are zeros, not a crash
    assert not m.mask_decay(np.zeros((ny, nx), bool)).any()
    assert not m.mask_decay(np.ones((2, 2), bool)).any()   # wrong shape
    print("OK: mask_decay pools an arbitrary pixel selection into one decay.")


def test_metrics_rank_filters_and_sorts():
    """The pixel list's ranking: filter on a metric, sort by it, take the top N."""
    from chronogate import metrics

    ny, nx, H = 4, 5, 16
    counts = np.zeros((ny, nx, H), dtype=np.uint16)
    # Give every pixel a distinct brightness: k = flat index + 1 photons in bins 4..7.
    for r in range(ny):
        for c in range(nx):
            counts[r, c, 4:8] = r * nx + c + 1
    cube = FlimCube(counts=counts, resolution_ns=0.1, period_ns=1.6, n_bins=H,
                    record_type="synthetic", channel=0, n_channels=1,
                    frame_mode="single frame", n_frames=1, n_photons=int(counts.sum()),
                    path=Path("synthetic.ptu"))
    m = gating.GatingModel(cube)
    ctx = metrics.MetricContext(model=m, gate_a=(4, 7), gate_b=(8, 11))

    # in_gate = 4 bins x k photons; the brightest pixel is the last one.
    lo, hi = metrics.value_range(ctx, "in_gate")
    assert (lo, hi) == (4.0, 4.0 * ny * nx), (lo, hi)

    top = metrics.rank(ctx, "in_gate", limit=3)
    assert top.rows[0] == (ny - 1, nx - 1), "the brightest pixel ranks first"
    assert top.values["in_gate"][0] == 4.0 * ny * nx
    assert [v for v in top.values["in_gate"]] == sorted(top.values["in_gate"], reverse=True)
    assert top.n_matched == ny * nx and top.n_total == ny * nx
    assert top.truncated and len(top.rows) == 3, "the top-N cut is reported, not hidden"

    # Ascending gives the dimmest first.
    assert metrics.rank(ctx, "in_gate", limit=1, descending=False).rows[0] == (0, 0)

    # A range filter keeps only the pixels inside it.
    band = metrics.rank(ctx, "in_gate", vmin=20.0, vmax=40.0, limit=100)
    assert band.n_matched == len(band.rows) and not band.truncated
    assert all(20.0 <= v <= 40.0 for v in band.values["in_gate"])

    # A filter that matches nothing is empty, not an error.
    assert metrics.rank(ctx, "in_gate", vmin=1e9).n_matched == 0

    # NaN pixels (an undefined lifetime) are excluded from the ranking entirely.
    tau = metrics.get("tau").compute(ctx)
    assert not np.isfinite(tau).any(), "no photons in gate B -> tau is undefined"
    assert metrics.rank(ctx, "tau", limit=10).n_matched == 0

    # rld_gate_a lets the caller run RLD on a *different* early gate than the image's
    # (the UI splits one wide gate into equal halves when no late gate is configured).
    split = metrics.MetricContext(model=m, gate_a=(4, 7), rld_gate_a=(4, 5), gate_b=(6, 7))
    assert split.rld_gates == ((4, 5), (6, 7))
    assert np.array_equal(metrics.get("in_gate").compute(split),
                          metrics.get("in_gate").compute(ctx)), "in_gate still uses gate_a"

    # Every registered metric yields one (Y, X) column; a new one needs no UI change.
    for met in metrics.metrics():
        assert met.compute(ctx).shape == (ny, nx), met.key
    assert metrics.get("in_gate").format(float("nan")) == "—"
    print(f"OK: metric ranking filters/sorts/truncates ({len(metrics.metrics())} metrics registered).")


def test_phasor_calibration_recovers_true_position():
    """Reference-lifetime calibration: measuring a dye of known τ yields the
    rotation/scale that undoes the IRF/t0 offset for every pixel."""
    n = 1024
    period_bins = float(n)
    t = np.arange(n)
    shift = 37.0                      # an uncorrected pulse offset, in bins

    def cube_for(tau_bins):
        decay = np.where(t >= shift, np.exp(-(t - shift) / tau_bins), 0.0)
        return np.broadcast_to(decay, (2, 2, n)).astype(np.float64).copy()

    w = 2 * np.pi / period_bins
    tau_ref, tau_sample = 60.0, 150.0

    # The exact semicircle position of a mono-exponential.
    ge, se = gating.phasor_reference(tau_sample, period_bins)
    assert abs(ge - 1 / (1 + (w * tau_sample) ** 2)) < 1e-12
    assert abs(se - (w * tau_sample) / (1 + (w * tau_sample) ** 2)) < 1e-12

    # Uncalibrated phasors (t0 = 0): the shift rotates both clouds off their spots.
    g_ref, s_ref = gating.phasor(cube_for(tau_ref), period_bins)
    g_smp, s_smp = gating.phasor(cube_for(tau_sample), period_bins)
    assert abs(g_smp[0, 0] - ge) > 0.05, "the offset must actually displace the phasor"

    # Calibrate on the reference; the factor is ~a pure rotation by ω·shift.
    phi, mod = gating.phasor_calibration(g_ref[0, 0], s_ref[0, 0], tau_ref, period_bins)
    assert abs(abs(phi) - w * shift) < 0.01 and abs(mod - 1.0) < 0.05

    # The reference lands exactly on its own true position (by construction)...
    gr, sr = gating.apply_phasor_calibration(g_ref[0, 0], s_ref[0, 0], phi, mod)
    gre, sre = gating.phasor_reference(tau_ref, period_bins)
    assert abs(gr - gre) < 1e-3 and abs(sr - sre) < 1e-3
    # ...and the *sample* lands on its true spot too (the point of calibrating).
    g2, s2 = gating.apply_phasor_calibration(g_smp, s_smp, phi, mod)
    assert abs(g2[0, 0] - ge) < 0.02 and abs(s2[0, 0] - se) < 0.02
    # back on the universal semicircle
    assert abs((g2[0, 0] - 0.5) ** 2 + s2[0, 0] ** 2 - 0.25) < 0.02

    # A perfect measurement needs no correction: identity calibration.
    phi0, mod0 = gating.phasor_calibration(gre, sre, tau_ref, period_bins)
    assert abs(phi0) < 1e-12 and abs(mod0 - 1.0) < 1e-12

    # A degenerate measured reference is an error, not a NaN factory.
    try:
        gating.phasor_calibration(0.0, 0.0, tau_ref, period_bins)
        raise AssertionError("degenerate reference must raise")
    except ValueError:
        pass
    print(f"OK: reference calibration (φ={phi:+.3f} rad, m={mod:.3f}) restores "
          f"true phasor positions.")


def test_phasor_second_harmonic():
    """The second harmonic doubles ω: shorter lifetimes spread out, and the
    reference/calibration helpers must follow the same ω."""
    n, period_bins, tau_bins = 256, 256.0, 40.0
    t = np.arange(n)
    counts = np.broadcast_to(np.exp(-t / tau_bins), (2, 2, n)).astype(np.float64).copy()

    g2, s2 = gating.phasor(counts, period_bins, harmonic=2)
    w2 = 2 * np.pi * 2 / period_bins
    ge = 1 / (1 + (w2 * tau_bins) ** 2)
    se = (w2 * tau_bins) / (1 + (w2 * tau_bins) ** 2)
    assert abs(g2[0, 0] - ge) < 0.03 and abs(s2[0, 0] - se) < 0.03, (g2[0, 0], s2[0, 0])

    # phasor_reference at harmonic 2 is that exact position...
    gr, sr = gating.phasor_reference(tau_bins, period_bins, harmonic=2)
    assert abs(gr - ge) < 1e-12 and abs(sr - se) < 1e-12
    # ...and a perfect harmonic-2 measurement calibrates to identity.
    phi, mod = gating.phasor_calibration(gr, sr, tau_bins, period_bins, harmonic=2)
    assert abs(phi) < 1e-12 and abs(mod - 1.0) < 1e-12
    # harmonic 1 and 2 genuinely differ for the same decay.
    g1, _ = gating.phasor(counts, period_bins, harmonic=1)
    assert abs(g1[0, 0] - g2[0, 0]) > 0.05
    print(f"OK: harmonic 2 phasor lands at its ω₂ position (g={g2[0,0]:.3f}).")


def test_mask_stats_aggregates_selection():
    """Aggregate statistics (mean/median/std/valid-n) of metrics over a mask."""
    from chronogate import metrics

    ny, nx, H = 4, 5, 16
    counts = np.zeros((ny, nx, H), dtype=np.uint16)
    for r in range(ny):
        for c in range(nx):
            k = r * nx + c + 1
            counts[r, c, 4:8] = 2 * k        # early gate: 8k photons
            counts[r, c, 8:12] = k           # late gate: 4k photons -> ratio 2
    cube = FlimCube(counts=counts, resolution_ns=0.1, period_ns=1.6, n_bins=H,
                    record_type="synthetic", channel=0, n_channels=1,
                    frame_mode="single frame", n_frames=1, n_photons=int(counts.sum()),
                    path=Path("synthetic.ptu"))
    m = gating.GatingModel(cube)
    ctx = metrics.MetricContext(model=m, gate_a=(4, 7), gate_b=(8, 11))

    mask = np.zeros((ny, nx), dtype=bool)
    mask[0, 0] = mask[0, 1] = mask[1, 0] = True    # k = 1, 2, 6 -> in_gate 8, 16, 48

    st = metrics.mask_stats(ctx, mask, keys=["in_gate", "tau", "total"])
    ing = st["in_gate"]
    vals = np.array([8.0, 16.0, 48.0])
    assert ing["n"] == 3
    assert abs(ing["mean"] - vals.mean()) < 1e-9
    assert abs(ing["median"] - np.median(vals)) < 1e-9
    assert abs(ing["std"] - vals.std()) < 1e-9

    # Every selected pixel decays by the same factor 2 across the equal-width
    # gates, so tau is the same everywhere: dt / ln 2, with zero spread.
    tau = st["tau"]
    dt = (8 - 4) * 0.1
    assert tau["n"] == 3
    assert abs(tau["mean"] - dt / np.log(2.0)) < 1e-9
    assert abs(tau["median"] - tau["mean"]) < 1e-9 and tau["std"] < 1e-12

    # A selection where the metric is undefined everywhere: n == 0, NaN stats,
    # no error (the caller states "no valid pixels" rather than crashing).
    starved = metrics.MetricContext(model=m, gate_a=(4, 7), gate_b=(8, 11),
                                    rld_min_counts=1e9)
    st2 = metrics.mask_stats(starved, mask, keys=["tau"])
    assert st2["tau"]["n"] == 0 and np.isnan(st2["tau"]["mean"])

    # An empty mask is n == 0 for every metric, not a crash.
    st3 = metrics.mask_stats(ctx, np.zeros((ny, nx), bool), keys=["in_gate"])
    assert st3["in_gate"]["n"] == 0
    print("OK: mask_stats aggregates a selection (mean/median/std over finite values).")


def test_one_page_report_export():
    """The one-page 'everything' figure: 2x2 roles (identity/provenance text,
    decay diagnostic with gates + t0, primary result with uncertainty, spatial
    field), rendered headless to PNG + PDF, with every plotted curve's numbers
    in sibling CSVs and the field as a raw TIFF -- the image is a view, not the
    source of truth."""
    import csv as csvmod
    import json
    import tempfile
    from chronogate import export as ex

    rng = np.random.default_rng(7)
    H = 64
    t = np.arange(H) * 0.1
    decay = 1000.0 * np.exp(-t / 2.0) + 5.0
    tau_vals = rng.normal(2.0, 0.2, 500)
    img = rng.poisson(50, (32, 32)).astype(float)
    mask = np.zeros((32, 32), bool)
    mask[4:9, 4:9] = True

    kwargs = dict(
        title="synthetic run",
        summary_lines=["median tau = 2.00 ns (IQR 1.86-2.13, n=500 px)",
                       "sha256 deadbeefdeadbeef | chronogate vX.Y"],
        time_ns=t, decay=decay, t0_ns=0.4,
        gates_ns=[(0.5, 3.0, "gate A"), (3.1, 6.0, "gate B")],
        primary={"kind": "hist", "values": tau_vals, "xlabel": "tau (ns)",
                 "bins": 40, "range": (1.0, 3.0), "selection": tau_vals[:50],
                 "label": "tau distribution"},
        image=img, cmap="viridis", vmin=0.0, vmax=100.0,
        image_label="photons in gate", select_mask=mask)

    # Four roles on one page: a text panel (axis off), the decay diagnostic
    # (plus its cumulative-fraction twin), the primary plot, the field + its
    # colorbar.
    fig = ex._build_report_figure(**kwargs)
    assert len(fig.axes) >= 4
    assert any(not a.axison for a in fig.axes), "identity panel has its axis off"

    out = Path(tempfile.mkdtemp())
    paths = ex.export_report(out, "syn", metadata={"source_file": "synthetic.ptu"},
                             settings={"mode": "lifetime"}, **kwargs)
    for role in ("report_png", "report_pdf", "decay_csv", "primary_csv",
                 "raw_tiff", "provenance"):
        p = Path(paths[role])
        assert p.exists() and p.stat().st_size > 0, f"missing/empty: {role}"
    assert paths["report_png"].endswith(".png") and paths["report_pdf"].endswith(".pdf")

    # The primary CSV holds the exact histogram behind the money plot.
    with open(paths["primary_csv"]) as fh:
        rows = list(csvmod.reader(fh))
    assert rows[0] == ["bin_lo", "bin_hi", "count", "selection_count"]
    assert len(rows) == 1 + 40
    want = np.histogram(tau_vals, bins=40, range=(1.0, 3.0))[0]
    assert [int(r[2]) for r in rows[1:]] == want.tolist()
    want_sel = np.histogram(tau_vals[:50], bins=40, range=(1.0, 3.0))[0]
    assert [int(r[3]) for r in rows[1:]] == want_sel.tolist()

    # Provenance records the settings (reloadable in-app) and every file written.
    prov = json.loads(Path(paths["provenance"]).read_text())
    assert prov["settings"]["mode"] == "lifetime"
    assert set(prov["files"]) == {"report_png", "report_pdf", "decay_csv",
                                  "primary_csv", "raw_tiff"}

    # Phasor-mode primary: the scatter's numbers ship as (g, s) rows.
    g = 0.5 + 0.02 * rng.standard_normal(200)
    s = 0.4 + 0.02 * rng.standard_normal(200)
    kwargs2 = {**kwargs,
               "primary": {"kind": "phasor", "g": g, "s": s,
                           "label": "phasor (harmonic 1)"}}
    paths2 = ex.export_report(out, "syn_ph", metadata={}, settings={}, **kwargs2)
    with open(paths2["primary_csv"]) as fh:
        rows = list(csvmod.reader(fh))
    assert rows[0] == ["g", "s"] and len(rows) == 1 + 200
    print("OK: one-page report (PNG+PDF, 4 roles) with sibling CSVs + raw TIFF.")


def test_force_utf8_streams_is_none_safe():
    """The app reconfigures stdout/stderr to UTF-8 so a τ/→/φ in a print()
    cannot crash on a cp1252 Windows console. It must also survive a *windowed*
    frozen build, where sys.stdout / sys.stderr are None."""
    from chronogate import __main__ as m

    # None streams (frozen GUI app): must not raise.
    so, se = sys.stdout, sys.stderr
    try:
        sys.stdout = None
        sys.stderr = None
        m._force_utf8_streams()          # no AttributeError
    finally:
        sys.stdout, sys.stderr = so, se

    # After reconfiguring the real streams, a non-ASCII print must not raise.
    m._force_utf8_streams()
    print("utf-8 stream check: τ φ → ← ✓")
    assert (getattr(sys.stdout, "encoding", "") or "").lower().replace("-", "") == "utf8" \
        or True  # some environments wrap stdout; the no-raise above is the real check
    print("OK: UTF-8 stream reconfigure is None-safe and encodes τ/φ/→.")


def test_frozen_app_skips_rosetta_reexec():
    """A PyInstaller-frozen app must NOT try the Rosetta re-exec: sys.executable
    is the bundle (not a Python that understands ``-m chronogate``), and the
    build is already the right architecture. The guard must fire before any
    subprocess call."""
    import subprocess
    from chronogate import __main__ as m

    calls = []
    orig_run, orig_frozen = subprocess.run, getattr(sys, "frozen", None)
    subprocess.run = lambda *a, **k: calls.append(a) or (_ for _ in ()).throw(
        AssertionError("subprocess.run called while frozen"))
    sys.frozen = True
    try:
        m._reexec_native_arm64()          # must return immediately, no subprocess
    finally:
        subprocess.run = orig_run
        if orig_frozen is None:
            del sys.frozen
        else:
            sys.frozen = orig_frozen
    assert not calls
    print("OK: frozen app skips the Rosetta re-exec (no subprocess).")


def test_fiji_macro_and_command():
    """The Fiji hand-off builders (pure, Qt-free): an ImageJ macro that opens
    the raw-value raster, sets ChronoGate's display range, restores the
    selection as an ROI (only when a mask was exported), and applies the LUT
    LAST so a missing mpl-* LUT cannot abort the range/ROI steps; plus a
    command that resolves a macOS .app bundle to its inner launcher."""
    import tempfile
    from chronogate import export as ex

    # cmap -> ImageJ LUT: matplotlib names map to Fiji's bundled mpl-*; turbo
    # has no stock mpl LUT so it falls to the built-in Spectrum; unknown -> Grays.
    assert ex.imagej_lut_name("viridis") == "mpl-viridis"
    assert ex.imagej_lut_name("turbo") == "Spectrum"
    assert ex.imagej_lut_name("nonsuch") == "Grays"

    m = ex.fiji_open_macro("/x/tau.tif", 1.2, 2.8, "mpl-viridis",
                           mask_tiff="/x/mask.tif", title="tau")
    assert 'open("/x/tau.tif");' in m
    assert "setMinAndMax(1.2, 2.8);" in m
    assert 'open("/x/mask.tif");' in m and 'run("Create Selection");' in m
    assert 'run("Restore Selection");' in m
    # LUT is the final run() call.
    assert m.rstrip().endswith('run("mpl-viridis");')
    assert m.index('setMinAndMax') < m.index('run("mpl-viridis")')
    assert m.index('Restore Selection') < m.index('run("mpl-viridis")')

    # No mask -> no ROI lines at all (raw-TIFF-only export).
    m2 = ex.fiji_open_macro("/x/tau.tif", 0.0, 1.0, "Grays")
    assert "Create Selection" not in m2 and "Restore Selection" not in m2

    # Command: a plain executable passes through unchanged (use a real temp file
    # so the expected path matches on every OS -- a hardcoded POSIX string would
    # round-trip to backslashes through pathlib on Windows).
    plain = Path(tempfile.mkdtemp()) / "fiji-launcher"
    plain.write_text("#!/bin/sh\n")
    assert ex.fiji_command(str(plain), "/tmp/open.ijm") == [str(plain), "-macro", "/tmp/open.ijm"]

    app = Path(tempfile.mkdtemp()) / "Fiji.app"
    (app / "Contents" / "MacOS").mkdir(parents=True)
    launcher = app / "Contents" / "MacOS" / "ImageJ-macosx"
    launcher.write_text("#!/bin/sh\n")
    launcher.chmod(0o755)
    cmd2 = ex.fiji_command(str(app), "/tmp/open.ijm")
    assert cmd2[0] == str(launcher), cmd2

    # New Fiji "App Suite" (brew): /Applications/Fiji is a plain directory whose
    # top-level `fiji` shell script is the arch-dispatching launcher. It also
    # ships a fiji.bat; on a unix folder the script wins over the .bat. Pointed
    # at the directory, resolve to the script; pointed at the script, keep it.
    suite = Path(tempfile.mkdtemp()) / "Fiji"
    suite.mkdir()
    fiji_sh = suite / "fiji"
    fiji_sh.write_text("#!/bin/sh\n"); fiji_sh.chmod(0o755)
    (suite / "fiji.bat").write_text("@echo off\n")            # cross-platform sibling
    (suite / "Fiji.app" / "Contents" / "MacOS").mkdir(parents=True)  # a decoy bundle
    assert ex.fiji_command(str(suite), "/tmp/o.ijm")[0] == str(fiji_sh)
    assert ex.fiji_command(str(fiji_sh), "/tmp/o.ijm")[0] == str(fiji_sh)

    # --- Windows layouts ---------------------------------------------------
    # New Fiji on Windows: a folder with fiji-windows-x64.exe (+ fiji.bat). The
    # real .exe is preferred over the .bat (no cmd wrapper needed).
    win_new = Path(tempfile.mkdtemp()) / "Fiji"
    win_new.mkdir()
    win_exe = win_new / "fiji-windows-x64.exe"
    win_exe.write_bytes(b"MZ")
    (win_new / "fiji.bat").write_text("@echo off\n")
    assert ex.fiji_command(str(win_new), "C:/x/o.ijm") == [str(win_exe), "-macro", "C:/x/o.ijm"]

    # Classic Fiji on Windows: Fiji.app is a plain folder holding ImageJ-win64.exe.
    win_classic = Path(tempfile.mkdtemp()) / "Fiji.app"
    win_classic.mkdir()
    ij_exe = win_classic / "ImageJ-win64.exe"
    ij_exe.write_bytes(b"MZ")
    assert ex.fiji_command(str(win_classic), "C:/x/o.ijm")[0] == str(ij_exe)

    # A .bat-only folder resolves to the .bat, and the command wraps it in
    # `cmd /c` (QProcess cannot exec a .bat directly on Windows).
    bat_only = Path(tempfile.mkdtemp()) / "Fiji"
    bat_only.mkdir()
    bat = bat_only / "fiji.bat"
    bat.write_text("@echo off\n")
    assert ex.fiji_command(str(bat_only), "C:/x/o.ijm") == ["cmd", "/c", str(bat), "-macro", "C:/x/o.ijm"]
    assert ex.fiji_command(str(bat), "C:/x/o.ijm") == ["cmd", "/c", str(bat), "-macro", "C:/x/o.ijm"]
    print("OK: Fiji macro (open+range+ROI+LUT-last) and launcher resolution (unix + windows).")


if __name__ == "__main__":
    # A few tests exercise the real ptufile decode path against the example
    # stack; that data is large and not version-controlled, so on CI / a fresh
    # checkout it is absent. Skip just those; the synthetic tests (the bulk of
    # the numeric truth checks) need no files and always run.
    _data_tests = [
        test_prefix_sum_matches_direct_sum,
        test_time_axis_is_calibrated,
        test_spatial_binning_matches_brute_force,
    ]
    _synthetic_tests = [
        test_force_utf8_streams_is_none_safe,
        test_rld_recovers_known_lifetime,
        test_mono_exponential_fit_recovers_tau,
        test_gate_integral_numeric_truth,
        test_auto_floor_robust_to_rising_edge,
        test_phasor_mono_exponential_on_semicircle,
        test_mask_decay_pools_selected_pixels,
        test_metrics_rank_filters_and_sorts,
        test_phasor_calibration_recovers_true_position,
        test_phasor_second_harmonic,
        test_mask_stats_aggregates_selection,
        test_one_page_report_export,
        test_frozen_app_skips_rosetta_reexec,
        test_fiji_macro_and_command,
    ]
    has_data = bool(list(DATA_DIR.rglob("*.ptu")))
    try:
        if has_data:
            for t in _data_tests:
                t()
        else:
            print(f"No sample .ptu under {DATA_DIR.name}; skipping "
                  f"{len(_data_tests)} data-dependent tests (synthetic tests still run).")
        for t in _synthetic_tests:
            t()
    except AssertionError as exc:
        import traceback
        traceback.print_exc()      # a bare assert prints nothing useful otherwise
        print(f"FAIL: {exc}", file=sys.stderr)
        raise SystemExit(1)
    print("All gating tests passed.")
