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
from chronogate.loader import find_stack, load_ptu

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


if __name__ == "__main__":
    try:
        test_prefix_sum_matches_direct_sum()
        test_time_axis_is_calibrated()
        test_spatial_binning_matches_brute_force()
    except AssertionError as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        raise SystemExit(1)
    print("All gating tests passed.")
