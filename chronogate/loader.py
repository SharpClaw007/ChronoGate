"""Load a PicoQuant ``.ptu`` file and build a FLIM data cube.

This module is the only place that talks to the ``ptufile`` library (BSD-3,
Christoph Gohlke). It deliberately does NO analysis -- it just turns a raw
photon stream into a clean, well-described numpy cube plus the metadata we
need to put real nanosecond units on the time axis.

Design choices worth knowing:

* The **record type is read from the file**, never assumed. PicoQuant stores
  one of several TTTR record layouts (PicoHarp / TimeHarp / MultiHarp ...);
  ``ptufile`` detects it. If it ever hits a layout it cannot decode we surface
  a clear error naming exactly what was found.
* ptufile decodes an image file into a 5-D array with named axes
  ``(T, Y, X, C, H)`` = (frame, row, column, detector-channel, microtime-bin).
  We reduce that to a 3-D cube ``(Y, X, H)`` by picking one detector channel
  and either picking one frame or summing all frames.
"""

from __future__ import annotations

import re
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import sdtfile
from ptufile import PtuFile


class FrameCache:
    """A byte-bounded LRU cache of decoded cubes, so revisiting a z-plane (or a
    channel) is instant instead of re-reading the file.

    Keys are opaque (the caller uses ``(path, channel, sum_frames)``); values are
    :class:`FlimCube`. Eviction is least-recently-used until the cached cubes fit
    under ``max_bytes`` (measured by ``counts.nbytes``). Single huge cubes are
    still stored (an item larger than the cap simply lives alone).
    """

    def __init__(self, max_bytes: int = 1_500_000_000):
        self.max_bytes = int(max_bytes)
        self._store: "OrderedDict[object, FlimCube]" = OrderedDict()
        self._bytes = 0

    def get(self, key):
        cube = self._store.get(key)
        if cube is not None:
            self._store.move_to_end(key)
        return cube

    def put(self, key, cube: "FlimCube") -> None:
        if key in self._store:
            self._bytes -= int(self._store.pop(key).counts.nbytes)
        self._store[key] = cube
        self._bytes += int(cube.counts.nbytes)
        while self._bytes > self.max_bytes and len(self._store) > 1:
            _, old = self._store.popitem(last=False)
            self._bytes -= int(old.counts.nbytes)

    def clear(self) -> None:
        self._store.clear()
        self._bytes = 0

# Canonical axis order ptufile uses for a decoded image. We never hardcode the
# *positions*; we look the names up in ``ptu.dims`` so odd files still work.
_CANON_DIMS = ("T", "Y", "X", "C", "H")


@dataclass
class FlimCube:
    """A loaded FLIM measurement, reduced to one channel and one frame view.

    Attributes
    ----------
    counts : np.ndarray (Y, X, H), uint16
        Per-pixel microtime histogram (the cube). ``counts[y, x, h]`` is the
        number of photons that landed in microtime bin ``h`` at pixel (y, x).
    resolution_ns : float
        Width of one microtime bin in nanoseconds (the TCSPC resolution).
    period_ns : float
        Laser period (1 / repetition rate) in nanoseconds -- the usable decay
        window length.
    n_bins : int
        Number of microtime bins (the H dimension of ``counts``).
    record_type : str
        Human-readable record-type name read from the file (e.g. "PicoHarpT3").
    channel : int
        Which detector channel this cube came from.
    n_channels : int
        Number of active detector channels in the file.
    frame_mode : str
        "sum" if all frames were summed, or "frame <i>" if one was picked.
    n_frames : int
        Number of frames/z-planes stored *inside this file*.
    n_photons : int
        Total photons recorded in the file (all channels/frames), from header.
    path : Path
        Source file.
    """

    counts: np.ndarray
    resolution_ns: float
    period_ns: float
    n_bins: int
    record_type: str
    channel: int
    n_channels: int
    frame_mode: str
    n_frames: int
    n_photons: int
    path: Path

    @property
    def time_axis_ns(self) -> np.ndarray:
        """Left edge of each microtime bin, in nanoseconds."""
        return np.arange(self.n_bins) * self.resolution_ns

    @property
    def intensity(self) -> np.ndarray:
        """Total photons per pixel (sum over all microtime bins), (Y, X)."""
        # int64 so a very bright pixel can never overflow.
        return self.counts.sum(axis=-1, dtype=np.int64)

    def summary(self) -> str:
        ny, nx, _ = self.counts.shape
        return (
            f"{self.path.name}: {self.record_type}, {nx}x{ny} px, "
            f"{self.n_bins} microtime bins @ {self.resolution_ns*1000:.2f} ps "
            f"(period {self.period_ns:.2f} ns), channel {self.channel}"
            f"/{self.n_channels}, {self.frame_mode}, "
            f"{int(self.counts.sum()):,} photons in cube"
        )


class UnsupportedFileError(RuntimeError):
    """Raised when a .ptu file is not an image we know how to turn into a cube."""


def _to_canonical(arr: np.ndarray, dims: tuple[str, ...]) -> np.ndarray:
    """Reorder/expand a decoded array to canonical ``(T, Y, X, C, H)``.

    ptufile reports the meaning of each axis in ``dims``. We transpose the
    axes that are present into canonical relative order, then insert size-1
    axes for any canonical dimension the file omitted -- so downstream code
    can always assume a 5-D ``(T, Y, X, C, H)`` shape.
    """
    present = [d for d in _CANON_DIMS if d in dims]
    missing = [d for d in _CANON_DIMS if d not in dims]
    if not {"Y", "X", "H"}.issubset(dims):
        raise UnsupportedFileError(
            f"file does not look like a FLIM image: decoded axes were {dims!r}, "
            f"but X, Y and a microtime (H) axis are required (missing {missing})."
        )
    # Transpose present axes into their canonical order.
    arr = np.transpose(arr, [dims.index(d) for d in present])
    # Insert singletons for any missing dimension, in increasing canonical
    # index order so earlier insertions don't shift later target positions.
    for i, d in enumerate(_CANON_DIMS):
        if d not in dims:
            arr = np.expand_dims(arr, i)
    return arr


def load_ptu(
    path: str | Path,
    channel: int = 0,
    frame: int | None = None,
    sum_frames: bool = True,
    progress=None,
) -> FlimCube:
    """Read a .ptu file into a :class:`FlimCube`.

    Parameters
    ----------
    path : str or Path
        The .ptu file.
    channel : int
        Detector channel to extract (0-based). FLIM files often have one; this
        lets you choose when there are several (spectral/polarisation/FRET).
    frame : int or None
        If ``sum_frames`` is False, the 0-based frame/z-plane to extract.
    sum_frames : bool
        If True (default), sum all frames in the file into one cube. If False,
        extract the single ``frame``.
    progress : callable or None
        Optional ``progress(done, total)`` callback invoked once per frame while
        summing a multi-frame file (lets a GUI show/cancel a long decode).

    Notes
    -----
    Frames are decoded **one at a time** (``decode_image(frame=t, channel=c)``)
    and accumulated, so a long time-series never materializes the full
    ``(T, Y, X, C, H)`` cube -- peak memory is one frame plus the result.

    Raises
    ------
    UnsupportedFileError
        If the file is not a decodable FLIM image, with a message naming what
        was actually found (defensive parsing, as requested).
    """
    path = Path(path)
    try:
        ptu = PtuFile(str(path))
    except Exception as exc:  # noqa: BLE001 - we re-raise with context
        raise UnsupportedFileError(f"could not open {path.name!r} as a PTU file: {exc}") from exc

    try:
        if not ptu.is_image:
            raise UnsupportedFileError(
                f"{path.name!r} is a {ptu.record_type!r} measurement but not an "
                f"image (measurement submode {ptu.measurement_submode}); "
                f"ChronoGate only handles imaging FLIM data."
            )

        # --- metadata, all read from the file header (nothing hardcoded) ---
        record_type = getattr(ptu.record_type, "name", str(ptu.record_type))
        resolution_ns = float(ptu.tcspc_resolution) * 1e9  # seconds -> ns
        freq = float(ptu.frequency) if ptu.frequency else 0.0
        period_ns = (1.0 / freq * 1e9) if freq else float("nan")
        n_channels = int(ptu.number_channels)
        n_photons = int(ptu.number_photons)

        if resolution_ns <= 0:
            raise UnsupportedFileError(
                f"{path.name!r}: microtime resolution read as {resolution_ns} ns; "
                f"cannot calibrate the time axis."
            )
        if not (0 <= channel < n_channels):
            raise UnsupportedFileError(
                f"requested channel {channel} but file has {n_channels} "
                f"channel(s) (valid: 0..{n_channels - 1})."
            )

        # --- decode the photon stream one frame at a time ---
        ptu.use_xarray = False  # we want a plain numpy array back
        dims = tuple(ptu.dims)
        shape = tuple(ptu.shape)
        n_frames = shape[dims.index("T")] if "T" in dims else 1

        def _decode_frame(t: int) -> np.ndarray:
            """Decode one frame of the chosen channel as a ``(Y, X, H)`` array.

            Only this frame/channel is materialized (memory-safe for long
            time-series). ``decode_image`` keeps size-1 T and C axes; we
            canonicalize to ``(T, Y, X, C, H)`` and drop them.
            """
            kw = {}
            if "T" in dims:
                kw["frame"] = t
            if "C" in dims:
                kw["channel"] = channel
            raw = np.asarray(ptu.decode_image(**kw))
            raw = _to_canonical(raw, dims)
            return raw[0, :, :, 0, :]

        try:
            if not sum_frames and n_frames > 1:
                if frame is None or not (0 <= frame < n_frames):
                    raise UnsupportedFileError(
                        f"requested frame {frame} but file has {n_frames} frame(s)."
                    )
                cube = _decode_frame(frame)
                frame_mode = f"frame {frame}"
            elif n_frames == 1:
                cube = _decode_frame(0)
                frame_mode = "single frame"
            else:
                # Sum all frames, accumulating in uint32 (a bright pixel/bin can
                # exceed uint16 once summed over many frames).
                cube = _decode_frame(0).astype(np.uint32)
                if progress is not None:
                    progress(1, n_frames)
                for t in range(1, n_frames):
                    cube += _decode_frame(t)
                    if progress is not None:
                        progress(t + 1, n_frames)
                frame_mode = "sum"
        except UnsupportedFileError:
            raise
        except Exception as exc:  # noqa: BLE001 - surface ptufile decode failures cleanly
            raise UnsupportedFileError(
                f"{path.name!r}: could not reconstruct the image ({exc}). Some older "
                f"PicoQuant 'old-style' image files are not supported by the decoder."
            ) from exc

        # Keep counts compact: uint16 if every bin fits, else uint32 (multi-frame
        # sums can exceed 65535, where the old uint16 cast would have wrapped).
        cube = np.asarray(cube)
        if cube.dtype != np.uint16 and int(cube.max(initial=0)) <= 65535:
            cube = cube.astype(np.uint16)
        cube = np.ascontiguousarray(cube)
    finally:
        ptu.close()

    return FlimCube(
        counts=cube,
        resolution_ns=resolution_ns,
        period_ns=period_ns,
        n_bins=cube.shape[-1],
        record_type=record_type,
        channel=channel,
        n_channels=n_channels,
        frame_mode=frame_mode,
        n_frames=n_frames,
        n_photons=n_photons,
        path=path,
    )


def probe_ptu(path: str | Path) -> str:
    """Quickly classify a ``.ptu`` for ChronoGate without a full load.

    Returns one of ``"image"`` (a decodable FLIM image), ``"point"`` (a
    point/histogram measurement -- FCS, antibunching, an IRF), ``"old-style"``
    (an image the decoder can't reconstruct), or ``"error"``. Header metadata is
    cheap, but an *old-style* image only reveals itself on a decode attempt, so
    image-typed files get a guarded single-frame decode.
    """
    path = Path(path)
    try:
        ptu = PtuFile(str(path))
    except Exception:  # noqa: BLE001
        return "error"
    try:
        dims = ptu.dims
        d = dict(zip(dims, (int(s) for s in ptu.shape)))
        is_image = "X" in dims and "Y" in dims and d.get("X", 0) > 1 and d.get("Y", 0) > 1
    except Exception:  # noqa: BLE001
        return "error"
    finally:
        ptu.close()
    if not is_image:
        return "point"
    try:
        load_ptu(path, channel=0, frame=0, sum_frames=False)   # old-style fails fast
        return "image"
    except UnsupportedFileError as exc:
        return "old-style" if "old-style" in str(exc).lower() else "error"
    except Exception:  # noqa: BLE001
        return "error"


def load_sdt(
    path: str | Path,
    channel: int = 0,
    frame: int | None = None,
    sum_frames: bool = True,
    progress=None,
) -> FlimCube:
    """Read a Becker & Hickl ``.sdt`` file into the SAME :class:`FlimCube`.

    This is the only place that talks to the ``sdtfile`` library (BSD, Christoph
    Gohlke). An ``.sdt`` stores one or more *data blocks* (one per detector
    channel / routing window) plus a *measurement description* per block. Each
    imaging block decodes to ``(Y, X, H)`` (rows, columns, microtime bins) and
    carries its own time axis in seconds via ``sdt.times[block]``.

    Mapping to :class:`FlimCube`:

    * ``resolution_ns`` <- mean spacing of ``sdt.times[channel]`` (seconds) x 1e9;
      falls back to the measurement description ``tac_r / (tac_g * adc_re)`` if the
      time axis is missing/degenerate.
    * ``n_bins`` <- ``len(sdt.times[channel])``; the block's time axis is located
      by matching that length (last matching axis wins, the B&H convention), the
      remaining two large axes are ``Y, X``.
    * ``channel`` selects the data block; ``n_channels`` is the block count.
    * ``period_ns`` <- ``n_bins * resolution_ns`` -- the full TAC window, NOT a
      measured laser period (the SDT header carries no reliable rep-rate).
    * ``record_type = "Becker & Hickl SDT"``; ``n_photons`` = total counts across
      all blocks (mirrors ``load_ptu``'s all-channels header count).

    Parameters mirror :func:`load_ptu`. Multi-frame/mosaic blocks (>2 spatial
    axes) are summed by default, or one ``frame`` is picked when ``sum_frames``
    is False.

    Raises
    ------
    UnsupportedFileError
        If the file cannot be opened or mapped to a FLIM cube, naming what was
        actually found (defensive parsing, exactly like :func:`load_ptu`).
    """
    path = Path(path)
    try:
        sdt = sdtfile.SdtFile(str(path))
    except Exception as exc:  # noqa: BLE001 - re-raise with context
        raise UnsupportedFileError(
            f"could not open {path.name!r} as a Becker & Hickl SDT file: {exc}"
        ) from exc

    try:
        data_blocks = list(sdt.data)
        times_list = list(sdt.times)
        n_channels = len(data_blocks)
        if n_channels == 0:
            raise UnsupportedFileError(f"{path.name!r}: SDT file contains no data blocks.")
        if not (0 <= channel < n_channels):
            raise UnsupportedFileError(
                f"requested channel {channel} but {path.name!r} has {n_channels} "
                f"data block(s)/channel(s) (valid: 0..{n_channels - 1})."
            )

        block = np.asarray(data_blocks[channel])
        times = np.asarray(times_list[channel], dtype=np.float64)
        n_bins = int(times.shape[0]) if times.ndim else 0
        if n_bins <= 0 or block.size == 0:
            raise UnsupportedFileError(
                f"{path.name!r}: channel {channel} has an empty decay "
                f"(times={times.shape}, data={block.shape})."
            )

        # --- microtime resolution (ns per bin) ---
        # sdt.times are in SECONDS; the bin width is their mean spacing.
        resolution_ns = 0.0
        if times.size >= 2:
            resolution_ns = float(np.mean(np.diff(times))) * 1e9
        if not (resolution_ns > 0):
            # Fallback: TAC range / (gain x ADC bins) -- the same formula sdtfile
            # uses to build `times`, read straight from the measurement record.
            try:
                mi = sdt.measure_info[channel if channel < len(sdt.measure_info) else 0]
                tac_r = float(mi.tac_r)
                tac_g = float(mi.tac_g)
                adc_re = int(mi.adc_re) or n_bins
                if tac_g and adc_re:
                    resolution_ns = (tac_r / (tac_g * adc_re)) * 1e9
            except (AttributeError, IndexError, TypeError, ValueError, ZeroDivisionError):
                pass
        if not (resolution_ns > 0):
            raise UnsupportedFileError(
                f"{path.name!r}: microtime resolution read as {resolution_ns} ns; "
                f"cannot calibrate the time axis."
            )

        # --- identify the microtime (H) axis and reduce to (Y, X, H) ---
        # The time axis is the one whose length matches len(times). If several
        # axes share that size (e.g. a square image), the microtime axis is by
        # B&H convention the LAST one, so we take the highest-index match.
        time_axes = [ax for ax, s in enumerate(block.shape) if s == n_bins]
        if not time_axes:
            raise UnsupportedFileError(
                f"{path.name!r}: no data axis matches the {n_bins}-bin time axis "
                f"(block shape {block.shape})."
            )
        block = np.moveaxis(block, time_axes[-1], -1)  # -> (..., H)
        lead = block.shape[:-1]

        n_frames = 1
        frame_mode = "single frame"
        if len(lead) == 0:
            # A pure decay / point measurement -> a 1x1 image.
            cube = block.reshape(1, 1, n_bins)
        elif len(lead) == 1:
            # A single spatial axis (line scan / point set) -> (1, N, H).
            cube = block.reshape(1, int(lead[0]), n_bins)
        elif len(lead) == 2:
            cube = block  # already (Y, X, H)
        else:
            # Extra leading axes are frames / mosaic tiles: (frames..., Y, X, H).
            # Y, X are the last two spatial axes; collapse the rest into frames.
            n_frames = int(np.prod(lead[:-2]))
            stacked = block.reshape(n_frames, int(lead[-2]), int(lead[-1]), n_bins)
            if not sum_frames and n_frames > 1:
                if frame is None or not (0 <= frame < n_frames):
                    raise UnsupportedFileError(
                        f"requested frame {frame} but {path.name!r} has {n_frames} frame(s)."
                    )
                cube = stacked[frame]
                frame_mode = f"frame {frame}"
            else:
                cube = stacked.sum(axis=0, dtype=np.uint32)
                frame_mode = "sum" if n_frames > 1 else "single frame"

        # Keep counts compact: uint16 if every bin fits, else uint32 (a summed
        # multi-frame bin can exceed 65535, where a uint16 cast would wrap).
        cube = np.ascontiguousarray(cube)
        if cube.dtype not in (np.uint16, np.uint32):
            cube = cube.astype(np.uint32 if int(cube.max(initial=0)) > 65535 else np.uint16)
        elif cube.dtype == np.uint32 and int(cube.max(initial=0)) <= 65535:
            cube = cube.astype(np.uint16)

        if progress is not None:
            progress(n_frames, n_frames)

        # Period: the SDT header has no reliable measured laser rep-rate, so use
        # the full TAC window (n_bins x bin width). This is the digitiser's time
        # window, NOT a measured laser period -- labelled as such downstream.
        period_ns = n_bins * resolution_ns

        # Total photons across ALL blocks/channels (mirrors load_ptu's header count).
        n_photons = int(sum(int(np.asarray(b).sum(dtype=np.int64)) for b in data_blocks))
    finally:
        sdt.close()

    return FlimCube(
        counts=cube,
        resolution_ns=resolution_ns,
        period_ns=period_ns,
        n_bins=int(cube.shape[-1]),
        record_type="Becker & Hickl SDT",
        channel=channel,
        n_channels=n_channels,
        frame_mode=frame_mode,
        n_frames=n_frames,
        n_photons=n_photons,
        path=path,
    )


# --------------------------------------------------------------------------- #
# Reader registry + format dispatch
#
# One place maps a file extension to the function that turns it into a FlimCube.
# Everything else (the CLI, the Qt dialogs, folder globs) asks the registry what
# it supports instead of hardwiring ``.ptu``. Adding a format = one entry here.
# --------------------------------------------------------------------------- #
READERS = {".ptu": load_ptu, ".sdt": load_sdt}  # ext -> (path, ...) -> FlimCube


def load_flim(path: str | Path, **kw) -> FlimCube:
    """Load any registered FLIM format into a :class:`FlimCube`, by extension.

    Dispatches on ``Path(path).suffix.lower()``. An unknown extension raises
    :class:`UnsupportedFileError` naming the offending extension. ``**kw`` is
    forwarded verbatim to the chosen reader (``channel``, ``frame``,
    ``sum_frames``, ``progress`` -- the readers share that signature).
    """
    ext = Path(path).suffix.lower()
    reader = READERS.get(ext)
    if reader is None:
        known = ", ".join(sorted(READERS))
        raise UnsupportedFileError(
            f"unsupported file type {ext!r} for {Path(path).name!r}; "
            f"ChronoGate reads: {known}."
        )
    return reader(path, **kw)


def probe_flim(path: str | Path) -> str:
    """Classify a FLIM file for the folder-open picker, dispatching by extension.

    ``.ptu`` keeps the existing :func:`probe_ptu` behaviour ("image" / "point" /
    "old-style" / "error"). ``.sdt`` gets a lightweight decode: "image" if it maps
    to a ``(Y, X, H)`` cube with ``Y > 1`` and ``X > 1``, else "point"; "error" on
    any failure. An unknown extension returns "error".
    """
    ext = Path(path).suffix.lower()
    if ext == ".ptu":
        return probe_ptu(path)
    if ext == ".sdt":
        try:
            cube = load_sdt(path)
        except Exception:  # noqa: BLE001 - a probe must never raise
            return "error"
        ny, nx = cube.counts.shape[0], cube.counts.shape[1]
        return "image" if ny > 1 and nx > 1 else "point"
    return "error"


def flim_glob_patterns() -> list[str]:
    """Glob patterns for every registered format, e.g. ``["*.ptu", "*.sdt"]``.

    Derived from :data:`READERS` so globs and file dialogs stay in sync with the
    reader registry automatically.
    """
    return [f"*{ext}" for ext in READERS]


def find_stack(path: str | Path) -> list[Path]:
    """Find sibling files that form a numbered stack (e.g. a z-series).

    Many acquisitions save one file per z-plane: ``FLIM_stack_z1.ptu`` ...
    ``FLIM_stack_z65.ptu`` (or ``.sdt``). We detect the trailing number, group
    files that share the same non-numeric prefix *and* the same registered
    extension, and return them ordered by that number. Returns ``[path]`` if no
    numbered series is found.
    """
    path = Path(path)
    # Match any registered format extension, not just ``.ptu`` (from READERS).
    ext_alt = "|".join(re.escape(e) for e in READERS)
    m = re.match(rf"^(.*?)(\d+)({ext_alt})$", path.name, re.IGNORECASE)
    if not m:
        return [path]
    prefix, _, ext = m.groups()
    members: list[tuple[int, Path]] = []
    for sibling in path.parent.glob(f"*{ext}"):
        sm = re.match(rf"^(.*?)(\d+){re.escape(ext)}$", sibling.name, re.IGNORECASE)
        if sm and sm.group(1) == prefix:
            members.append((int(sm.group(2)), sibling))
    members.sort()
    return [p for _, p in members] or [path]
