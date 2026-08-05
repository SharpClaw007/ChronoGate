"""Tests for the Becker & Hickl ``.sdt`` reader and the format-dispatch registry.

Two layers, mirroring the ptu tests' data-skip pattern:

* **Always-on synthetic tests** fabricate stand-in ``sdtfile.SdtFile`` objects
  matching the real library's data/times/measure_info contract (confirmed against
  sdtfile 2026.7.17) and assert ``load_sdt``'s field-mapping math -- resolution
  from ``times``, time-axis identification, channel pick, photon total, period.
  No real file is needed, so these run on CI.
* A **real-`.sdt` test** that SKIPS when absent: it reads a sample path from the
  ``CHRONOGATE_SDT_SAMPLE`` environment variable and asserts parse fidelity only
  if that file exists. It never needs the network or bundled data.

Run directly::

    python test_loader_sdt.py

or under pytest::

    pytest test_loader_sdt.py
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from types import SimpleNamespace

# These tests print τ/φ/→ and unit glyphs directly; make stdout UTF-8 so they
# don't crash on a cp1252 Windows console (mirrors what the app does at startup).
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError, OSError):
        pass

import numpy as np

import sdtfile

from chronogate import loader
from chronogate.loader import (
    READERS,
    UnsupportedFileError,
    find_stack,
    flim_glob_patterns,
    load_flim,
    load_sdt,
    probe_flim,
)


# --------------------------------------------------------------------------- #
# A stand-in for sdtfile.SdtFile matching the attributes load_sdt consumes:
#   .data          list[ndarray]   one block per detector channel / routing
#   .times         list[ndarray]   per-block time axis, in SECONDS
#   .measure_info  list[record]    per-block measurement description
#   .close()
# --------------------------------------------------------------------------- #
class _FakeSdt:
    def __init__(self, data, times, measure_info):
        self.data = data
        self.times = times
        self.measure_info = measure_info
        self.closed = False

    def close(self):
        self.closed = True


class _patch_sdtfile:
    """Context manager: make ``sdtfile.SdtFile(path)`` return a preset instance.

    ``load_sdt`` looks up ``sdtfile.SdtFile`` at call time, so patching the module
    attribute is enough -- no real file is opened.
    """

    def __init__(self, instance):
        self._instance = instance
        self._saved = None

    def __enter__(self):
        self._saved = sdtfile.SdtFile
        sdtfile.SdtFile = lambda _path, *a, **k: self._instance
        return self._instance

    def __exit__(self, *exc):
        sdtfile.SdtFile = self._saved
        return False


def _linear_times(n_bins: int, dt_s: float) -> np.ndarray:
    """A B&H-style time axis in seconds: arange(n) * bin width."""
    return np.arange(n_bins, dtype=np.float64) * dt_s


# --------------------------------------------------------------------------- #
# Always-on synthetic tests
# --------------------------------------------------------------------------- #
def test_sdt_resolution_and_photon_total_from_times():
    """resolution_ns = mean(diff(times))*1e9; counts.sum() == injected total."""
    ny, nx, n_bins = 4, 5, 16
    dt_s = 5e-11  # 50 ps per bin -> 0.05 ns
    rng = np.random.default_rng(0)
    block = rng.integers(0, 40, size=(ny, nx, n_bins), dtype=np.uint16)
    injected_total = int(block.sum())
    fake = _FakeSdt(
        data=[block],
        times=[_linear_times(n_bins, dt_s)],
        measure_info=[SimpleNamespace(tac_r=5e-8, tac_g=1, adc_re=n_bins)],
    )
    with _patch_sdtfile(fake):
        cube = load_sdt("phantom.sdt")

    assert cube.counts.shape == (ny, nx, n_bins), cube.counts.shape
    assert np.isclose(cube.resolution_ns, dt_s * 1e9), cube.resolution_ns  # 0.05 ns
    assert cube.n_bins == n_bins
    assert int(cube.counts.sum()) == injected_total, (cube.counts.sum(), injected_total)
    assert cube.record_type == "Becker & Hickl SDT"
    # period is the full TAC window (n_bins * bin width), not a laser period.
    assert np.isclose(cube.period_ns, n_bins * cube.resolution_ns)
    assert 0 < cube.period_ns < 1000
    assert fake.closed, "load_sdt must close the SdtFile handle"
    print(f"OK: .sdt maps times→resolution ({cube.resolution_ns*1000:.1f} ps/bin), "
          f"photon total exact ({injected_total:,}).")


def test_sdt_time_axis_identified_when_not_last():
    """The H axis is found by matching len(times), even if it is not last."""
    ny, nx, n_bins = 3, 7, 20
    dt_s = 1e-10  # 0.1 ns/bin
    rng = np.random.default_rng(1)
    # Build (Y, X, H) truth, then store it as (H, Y, X) to force axis relocation.
    truth = rng.integers(0, 25, size=(ny, nx, n_bins), dtype=np.uint16)
    block_hyx = np.moveaxis(truth, -1, 0)  # (H, Y, X)
    assert block_hyx.shape == (n_bins, ny, nx)
    fake = _FakeSdt(
        data=[block_hyx],
        times=[_linear_times(n_bins, dt_s)],
        measure_info=[SimpleNamespace(tac_r=1e-7, tac_g=1, adc_re=n_bins)],
    )
    with _patch_sdtfile(fake):
        cube = load_sdt("phantom.sdt")

    assert cube.counts.shape == (ny, nx, n_bins), cube.counts.shape
    # Relocation preserves the per-pixel decays exactly.
    assert np.array_equal(cube.counts, truth), "H-axis relocation must be lossless"
    assert np.isclose(cube.resolution_ns, dt_s * 1e9)
    print(f"OK: time axis identified by len(times)={n_bins} even when stored first; "
          f"cube reshaped to {cube.counts.shape} losslessly.")


def test_sdt_channel_pick_and_n_channels():
    """channel selects the data block; n_channels = block count; total = all blocks."""
    n_bins = 12
    dt_s = 8e-11
    block0 = np.full((2, 3, n_bins), 1, dtype=np.uint16)   # total = 2*3*12  = 72
    block1 = np.full((2, 3, n_bins), 5, dtype=np.uint16)   # total = 2*3*12*5 = 360
    times = _linear_times(n_bins, dt_s)
    mi = SimpleNamespace(tac_r=4e-8, tac_g=1, adc_re=n_bins)
    fake = _FakeSdt(data=[block0, block1], times=[times, times], measure_info=[mi, mi])

    with _patch_sdtfile(fake):
        cube1 = load_sdt("phantom.sdt", channel=1)
    assert cube1.channel == 1
    assert cube1.n_channels == 2
    assert int(cube1.counts.sum()) == int(block1.sum()) == 360, cube1.counts.sum()
    # n_photons spans ALL blocks (mirrors load_ptu's all-channels header count).
    assert cube1.n_photons == int(block0.sum() + block1.sum()) == 432, cube1.n_photons

    # Picking channel 0 gives that block's total instead.
    fake2 = _FakeSdt(data=[block0, block1], times=[times, times], measure_info=[mi, mi])
    with _patch_sdtfile(fake2):
        cube0 = load_sdt("phantom.sdt", channel=0)
    assert int(cube0.counts.sum()) == int(block0.sum()) == 72

    # Out-of-range channel is rejected with a clear message.
    fake3 = _FakeSdt(data=[block0, block1], times=[times, times], measure_info=[mi, mi])
    with _patch_sdtfile(fake3):
        try:
            load_sdt("phantom.sdt", channel=2)
        except UnsupportedFileError as exc:
            assert "channel 2" in str(exc), str(exc)
        else:
            raise AssertionError("channel 2 of a 2-channel file must raise")
    print("OK: channel picks the block, n_channels=2, n_photons spans all blocks, "
          "out-of-range channel rejected.")


def test_sdt_resolution_fallback_to_measure_info():
    """When `times` spacing is degenerate, resolution falls back to tac_r/tac_g/adc_re."""
    ny, nx, n_bins = 2, 2, 16
    block = np.ones((ny, nx, n_bins), dtype=np.uint16)
    # times all-zero -> mean(diff)=0 -> primary path yields 0 -> fallback engages.
    degenerate_times = np.zeros(n_bins, dtype=np.float64)
    tac_r, tac_g = 5e-8, 1.0  # 50 ns TAC range, gain 1 -> 50/16 ns per bin
    mi = SimpleNamespace(tac_r=tac_r, tac_g=tac_g, adc_re=n_bins)
    fake = _FakeSdt(data=[block], times=[degenerate_times], measure_info=[mi])
    with _patch_sdtfile(fake):
        cube = load_sdt("phantom.sdt")
    expected = (tac_r / (tac_g * n_bins)) * 1e9  # 3.125 ns
    assert np.isclose(cube.resolution_ns, expected), (cube.resolution_ns, expected)
    assert cube.resolution_ns > 0
    print(f"OK: resolution fallback to tac_r/(tac_g·adc_re) = {cube.resolution_ns:.3f} ns/bin.")


def test_sdt_bad_file_raises_unsupported():
    """A failure opening the file is wrapped in UnsupportedFileError, naming the file."""
    def _boom(_path, *a, **k):
        raise ValueError("invalid SDT file header")
    saved = sdtfile.SdtFile
    sdtfile.SdtFile = _boom
    try:
        try:
            load_sdt("broken.sdt")
        except UnsupportedFileError as exc:
            assert "broken.sdt" in str(exc), str(exc)
            assert "SDT" in str(exc)
        else:
            raise AssertionError("a broken .sdt must raise UnsupportedFileError")
    finally:
        sdtfile.SdtFile = saved
    print("OK: unreadable .sdt raises UnsupportedFileError naming the file.")


def test_registry_and_dispatch():
    """READERS, load_flim, probe_flim, flim_glob_patterns wire the two formats."""
    assert set(READERS) == {".ptu", ".sdt"}, READERS
    assert READERS[".sdt"] is load_sdt
    assert flim_glob_patterns() == ["*.ptu", "*.sdt"], flim_glob_patterns()

    # load_flim dispatches .sdt to load_sdt and forwards kwargs.
    n_bins = 10
    block = np.ones((3, 3, n_bins), dtype=np.uint16)
    fake = _FakeSdt(
        data=[block],
        times=[_linear_times(n_bins, 1e-10)],
        measure_info=[SimpleNamespace(tac_r=1e-8, tac_g=1, adc_re=n_bins)],
    )
    with _patch_sdtfile(fake):
        cube = load_flim("phantom.sdt", channel=0)
    assert cube.record_type == "Becker & Hickl SDT"

    # Unknown extension names the offending suffix.
    try:
        load_flim("mystery.xyz")
    except UnsupportedFileError as exc:
        assert ".xyz" in str(exc), str(exc)
    else:
        raise AssertionError("unknown extension must raise UnsupportedFileError")

    # probe_flim classifies a (Y>1, X>1) sdt cube as an image, a 1-D decay as a point.
    with _patch_sdtfile(fake):
        assert probe_flim("phantom.sdt") == "image"
    point = _FakeSdt(
        data=[np.ones(n_bins, dtype=np.uint16)],   # bare decay -> (1, 1, H)
        times=[_linear_times(n_bins, 1e-10)],
        measure_info=[SimpleNamespace(tac_r=1e-8, tac_g=1, adc_re=n_bins)],
    )
    with _patch_sdtfile(point):
        assert probe_flim("phantom.sdt") == "point"
    assert probe_flim("mystery.xyz") == "error"
    print("OK: registry dispatch (load_flim/probe_flim/flim_glob_patterns) covers .ptu+.sdt.")


def test_find_stack_matches_sdt_extension():
    """find_stack groups a numbered .sdt series, not only .ptu."""
    import tempfile
    d = Path(tempfile.mkdtemp())
    made = []
    for i in (1, 2, 10):
        p = d / f"scan_z{i}.sdt"
        p.write_bytes(b"")  # find_stack only inspects names, never opens them
        made.append(p)
    # A lone .ptu with a different prefix must NOT join the .sdt group.
    (d / "other_z1.ptu").write_bytes(b"")
    ordered = find_stack(d / "scan_z2.sdt")
    assert [p.name for p in ordered] == ["scan_z1.sdt", "scan_z2.sdt", "scan_z10.sdt"], \
        [p.name for p in ordered]
    print("OK: find_stack groups a numbered .sdt z-series (mixed-extension safe).")


# --------------------------------------------------------------------------- #
# Real-.sdt fidelity test -- skips when no sample is present
# --------------------------------------------------------------------------- #
def test_real_sdt_sample_if_present():
    """Parse a real .sdt if CHRONOGATE_SDT_SAMPLE points at one; else skip.

    Never requires the network or bundled data -- exactly the ptu data-skip
    pattern. Asserts positive resolution, a sane decay window, and a real
    photon count.
    """
    sample = os.environ.get("CHRONOGATE_SDT_SAMPLE")
    if not sample or not Path(sample).exists():
        print("SKIP: no real .sdt sample (set CHRONOGATE_SDT_SAMPLE to a file to run).")
        return
    cube = load_sdt(sample)
    assert cube.resolution_ns > 0, cube.resolution_ns
    window = cube.n_bins * cube.resolution_ns
    assert 0 < window < 1000, f"decay window {window} ns out of sane range"
    assert int(cube.counts.sum()) > 0, "real .sdt must contain photons"
    # load_flim must route the same file to the same result.
    assert load_flim(sample).record_type == "Becker & Hickl SDT"
    print(f"OK (real): {Path(sample).name} -> {cube.summary()}")


def test_non_image_ptu_message_is_plain_english() -> None:
    """A point/FCS .ptu must be refused in words a microscopist can act on.

    Real SymPhoTime workspaces are full of point measurements (FCS/FCCS), and
    ChronoGate rightly cannot map them to pixels. The refusal is therefore a
    normal, frequent path -- not an edge case -- so it must name the measurement
    type and say what is needed instead, without leaking a Python repr like
    ``<PtuRecordType.GenericT3: 66311>`` at a scientist.
    """
    msg = loader.not_an_image_message("LSM_1.ptu", record_type_name="GenericT3", submode=1)

    assert "<" not in msg and ">" not in msg, f"leaks a Python repr: {msg}"
    assert "66311" not in msg, f"leaks a raw enum value: {msg}"
    assert "point" in msg.lower(), f"does not name the measurement type: {msg}"
    assert "LSM_1.ptu" in msg, f"does not name the file: {msg}"
    # It must say what to do, not merely what went wrong.
    assert "image" in msg.lower(), f"does not say what ChronoGate needs: {msg}"

    # Unknown submodes must degrade gracefully rather than assert a wrong label.
    other = loader.not_an_image_message("x.ptu", record_type_name="GenericT3", submode=7)
    assert "7" in other, f"unknown submode should still be reported: {other}"
    assert "<" not in other

    print(f"OK: non-image message is plain English -> {msg}")


if __name__ == "__main__":
    _synthetic_tests = [
        test_non_image_ptu_message_is_plain_english,
        test_sdt_resolution_and_photon_total_from_times,
        test_sdt_time_axis_identified_when_not_last,
        test_sdt_channel_pick_and_n_channels,
        test_sdt_resolution_fallback_to_measure_info,
        test_sdt_bad_file_raises_unsupported,
        test_registry_and_dispatch,
        test_find_stack_matches_sdt_extension,
    ]
    try:
        for t in _synthetic_tests:
            t()
        test_real_sdt_sample_if_present()  # self-skips when no sample is present
    except AssertionError as exc:
        import traceback
        traceback.print_exc()      # a bare assert prints nothing useful otherwise
        print(f"FAIL: {exc}", file=sys.stderr)
        raise SystemExit(1)
    print("All .sdt loader tests passed.")
