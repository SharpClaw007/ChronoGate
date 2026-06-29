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
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from ptufile import PtuFile

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

        # --- decode the photon stream into (T, Y, X, C, H) ---
        ptu.use_xarray = False  # we want a plain numpy array back
        arr = np.asarray(ptu.decode_image())
        arr = _to_canonical(arr, tuple(ptu.dims))
        n_frames = arr.shape[0]

        # Pick the detector channel -> (T, Y, X, H).
        chan = arr[:, :, :, channel, :]

        # Reduce the frame axis. T==1 is the common case (one frame per file).
        if sum_frames or n_frames == 1:
            cube = chan.sum(axis=0)
            frame_mode = "sum" if n_frames > 1 else "single frame"
        else:
            if frame is None or not (0 <= frame < n_frames):
                raise UnsupportedFileError(
                    f"requested frame {frame} but file has {n_frames} frame(s)."
                )
            cube = chan[frame]
            frame_mode = f"frame {frame}"

        # Keep counts compact (uint16 is plenty per bin) for memory; the
        # prefix-sum step widens to uint32 where accumulation could overflow.
        cube = np.ascontiguousarray(cube, dtype=np.uint16)
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


def find_stack(path: str | Path) -> list[Path]:
    """Find sibling files that form a numbered stack (e.g. a z-series).

    Many acquisitions save one ``.ptu`` per z-plane: ``FLIM_stack_z1.ptu`` ...
    ``FLIM_stack_z65.ptu``. We detect the trailing number, group files that
    share the same non-numeric prefix, and return them ordered by that number.
    Returns ``[path]`` if no numbered series is found.
    """
    path = Path(path)
    m = re.match(r"^(.*?)(\d+)(\.ptu)$", path.name, re.IGNORECASE)
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
