"""Reproducible exports and settings persistence.

The academic convention this follows: keep the **raw numeric layer** separate
from the **presentation layer**, and write a sidecar that records every choice
needed to regenerate the figure.

Each export produces, for a base name ``<base>``:

* ``<base>_gated_raw.tif``   -- the gated intensity values, no colormap, no
  rescaling (16-bit if they fit, else 32-bit float / uint32). For ImageJ etc.
* ``<base>_gated_color.png`` -- the colormapped image *with its colorbar*, for
  figures.
* ``<base>_decay.csv``       -- the total decay curve (time_ns, counts).
* ``<base>_provenance.json`` -- source file, header parameters, and the exact
  gate / channel / threshold / baseline settings used.

The provenance JSON is also a valid *settings* file: ``save_settings`` /
``load_settings`` round-trip the same ``settings`` block so an analysis is
exactly restartable.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import numpy as np
import tifffile


def _write_raw_tiff(path: Path, image: np.ndarray, metadata: dict[str, Any]) -> str:
    """Write the gated image preserving raw values; pick the smallest safe dtype.

    Returns the dtype name actually written, so the caller can log it.
    """
    # NaNs mark thresholded-out pixels; store them as 0 in integer rasters.
    has_nan = np.isnan(image).any()
    finite_max = np.nanmax(image) if image.size else 0
    is_integral = np.all(np.isfinite(image) & (image == np.floor(image)))

    if is_integral and not has_nan and finite_max <= 65535:
        out = image.astype(np.uint16)
        dtype = "uint16"
    elif is_integral and not has_nan and finite_max <= 4294967295:
        # Brighter than 16-bit can hold: keep raw integers, widen to 32-bit.
        out = image.astype(np.uint32)
        dtype = "uint32"
    else:
        # Baseline-subtracted (fractional) or thresholded (NaN) -> float keeps
        # exact values and lets NaN mark excluded pixels.
        out = image.astype(np.float32)
        dtype = "float32"

    # ImageJ-readable tags; description carries our provenance for traceability.
    tifffile.imwrite(
        str(path),
        out,
        photometric="minisblack",
        description=json.dumps(metadata, default=str),
    )
    return dtype


def _write_color_png(
    path: Path,
    image: np.ndarray,
    *,
    cmap: str,
    vmin: float,
    vmax: float,
    title: str,
    colorbar_label: str,
) -> None:
    """Write a colormapped PNG with axes off and a colorbar, for figures."""
    import matplotlib

    # Use a non-interactive backend for the saved figure so export works even
    # if called headless; does not disturb the live viewer's backend.
    from matplotlib.figure import Figure
    from matplotlib.backends.backend_agg import FigureCanvasAgg

    fig = Figure(figsize=(6, 5), dpi=150)
    FigureCanvasAgg(fig)
    ax = fig.add_subplot(111)
    cmap_obj = matplotlib.colormaps[cmap].copy()
    cmap_obj.set_bad(color="black")  # thresholded (NaN) pixels render black
    im = ax.imshow(image, cmap=cmap_obj, vmin=vmin, vmax=vmax, interpolation="nearest")
    ax.set_title(title, fontsize=9)
    ax.set_xticks([])
    ax.set_yticks([])
    fig.colorbar(im, ax=ax, label=colorbar_label, fraction=0.046, pad=0.04)
    fig.savefig(str(path), bbox_inches="tight")


def _write_decay_csv(path: Path, time_ns: np.ndarray, decay: np.ndarray) -> None:
    """Write the summed decay curve as a two-column CSV value table."""
    table = np.column_stack([time_ns, decay])
    np.savetxt(
        str(path),
        table,
        delimiter=",",
        header="time_ns,counts",
        comments="",
        fmt=["%.6f", "%d"],
    )


def export_all(
    out_dir: str | Path,
    base: str,
    *,
    gated_image: np.ndarray,
    time_ns: np.ndarray,
    decay: np.ndarray,
    cmap: str,
    vmin: float,
    vmax: float,
    metadata: dict[str, Any],
    settings: dict[str, Any],
    colorbar_label: str = "photons in gate",
    title: str | None = None,
) -> dict[str, str]:
    """Write all four export artefacts. Returns a map of role -> file path.

    ``colorbar_label`` and ``title`` let a caller relabel the PNG for a
    non-intensity raster (e.g. a lifetime map). ``title`` defaults to the
    source-file-plus-gate string built from ``settings``.
    """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    raw_path = out_dir / f"{base}_gated_raw.tif"
    png_path = out_dir / f"{base}_gated_color.png"
    csv_path = out_dir / f"{base}_decay.csv"
    json_path = out_dir / f"{base}_provenance.json"

    if title is None:
        title = (
            f"{metadata.get('source_file', base)} | gate "
            f"{settings.get('gate_lo_ns', '?')}-{settings.get('gate_hi_ns', '?')} ns"
        )

    raw_dtype = _write_raw_tiff(raw_path, gated_image, {**metadata, "settings": settings})
    _write_color_png(
        png_path,
        gated_image,
        cmap=cmap,
        vmin=vmin,
        vmax=vmax,
        title=title,
        colorbar_label=colorbar_label,
    )
    _write_decay_csv(csv_path, time_ns, decay)

    provenance = {
        "tool": "ChronoGate",
        "metadata": metadata,
        "settings": settings,
        "raw_tiff_dtype": raw_dtype,
        "files": {
            "raw_tiff": raw_path.name,
            "color_png": png_path.name,
            "decay_csv": csv_path.name,
        },
    }
    json_path.write_text(json.dumps(provenance, indent=2, default=str))

    return {
        "raw_tiff": str(raw_path),
        "color_png": str(png_path),
        "decay_csv": str(csv_path),
        "provenance": str(json_path),
    }


def save_settings(path: str | Path, settings: dict[str, Any], metadata: dict[str, Any]) -> None:
    """Persist gate/threshold/etc. settings (plus metadata) to a JSON file."""
    Path(path).write_text(
        json.dumps({"tool": "ChronoGate", "metadata": metadata, "settings": settings}, indent=2, default=str)
    )


def load_settings(path: str | Path) -> dict[str, Any]:
    """Load a settings/provenance JSON and return its ``settings`` block."""
    data = json.loads(Path(path).read_text())
    if "settings" not in data:
        raise ValueError(f"{path}: not a ChronoGate settings file (no 'settings' block).")
    return data["settings"]
