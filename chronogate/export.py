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

import csv
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
import tifffile


@dataclass(frozen=True)
class ExportOptions:
    """Which artefacts an export writes. Provenance is not optional: whatever
    subset the user picks, the sidecar records the choices (and the omissions),
    or the export is not reproducible."""

    raw_tiff: bool = True       # raw-value raster (intensity or τ), for ImageJ
    color_png: bool = True      # colormapped figure with colorbar
    decay_csv: bool = True      # summed decay curve
    selection: bool = True      # label-map TIFF + pooled-decay CSV (if any picks)
    pixel_table: bool = True    # per-pixel metric CSV (can be tens of MB)
    # The one-page report (PNG + PDF + its panel CSVs, see export_report). Off
    # by default so a plain export() keeps writing exactly the classic file set;
    # the orchestration (controller) acts on it, not export_all.
    report: bool = False


@dataclass
class Selection:
    """The pixels the user has selected, in a form that can leave the program.

    A selection (a picked pixel, an ROI, a phasor-lasso cluster, or a multi-select
    in the pixel list) is the *result* of an analysis, so it has to be exportable
    or the feature is a dead end. Three artefacts:

    * a **label map** -- 0 where unselected, *k* for the k-th selection, so the
      regions can be re-used as masks in ImageJ/Python;
    * the **pooled decay** of each selection, in counts/bin per pixel;
    * a **pixel table** -- one row per selected pixel with every registered metric,
      which is what actually goes into a statistics package.
    """

    labels: list[str]                                  # label k is labels[k - 1]
    label_map: np.ndarray                              # (Y, X) uint8/uint16
    time_ns: np.ndarray
    decays: list[np.ndarray]                           # per label, (H,)
    pixel_columns: list[str]                           # e.g. row, col, in_gate, tau...
    pixel_blocks: list[np.ndarray] = field(default_factory=list)   # per label, (N, C)
    # Per label: {metric key: {"mean", "median", "std", "n"}}, JSON-safe (no NaN --
    # undefined stats travel as None). These are the numbers a paper quotes, so
    # they ride along in the provenance even when the pixel table is omitted.
    aggregates: list[dict] | None = None

    def counts(self) -> list[int]:
        return [int(b.shape[0]) for b in self.pixel_blocks]


def _write_selection_mask(path: Path, sel: Selection, metadata: dict[str, Any]) -> None:
    """The label map: 0 = unselected, k = the k-th selection."""
    m = sel.label_map
    out = m.astype(np.uint8) if int(m.max(initial=0)) <= 255 else m.astype(np.uint16)
    tifffile.imwrite(str(path), out, photometric="minisblack",
                     description=json.dumps(metadata, default=str))


def _write_selection_decay_csv(path: Path, sel: Selection) -> None:
    """One column per selection: its pooled decay, in counts/bin per pixel.

    Written through ``csv`` rather than ``np.savetxt`` because the column headers
    are user-facing labels that legitimately contain commas ("phasor sel (22,575
    px)") -- they need quoting, not mangling.
    """
    table = np.column_stack([sel.time_ns] + [np.asarray(d, float) for d in sel.decays])
    with open(path, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["time_ns"] + list(sel.labels))
        for row in table:
            w.writerow([f"{row[0]:.6f}"] + [f"{v:.6g}" for v in row[1:]])


def _write_selection_pixels_csv(path: Path, sel: Selection) -> None:
    """One row per selected pixel, with every metric -- the table you do stats on."""
    with open(path, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["selection"] + sel.pixel_columns)
        for label, block in zip(sel.labels, sel.pixel_blocks):
            for row in np.asarray(block):
                w.writerow([label] + [f"{v:.6g}" for v in row])


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
    selection: Selection | None = None,
    options: ExportOptions | None = None,
) -> dict[str, str]:
    """Write the export artefacts. Returns a map of role -> file path.

    ``colorbar_label`` and ``title`` let a caller relabel the PNG for a
    non-intensity raster (e.g. a lifetime map). ``title`` defaults to the
    source-file-plus-gate string built from ``settings``.

    When ``selection`` is given, three more files carry the selected pixels out
    (label-map TIFF, pooled-decay CSV, per-pixel metric CSV) and the provenance
    records what each selection was.

    ``options`` chooses the subset of artefacts to write (default: everything).
    The provenance JSON is always written, and lists every artefact skipped by
    choice under ``"omitted"`` instead of staying silent -- ``pixel_table=False``
    in particular skips the per-pixel CSV (a 160k-pixel selection is ~10 MB).
    """
    opts = options or ExportOptions()
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

    paths: dict[str, str] = {}
    omitted: list[str] = []
    raw_dtype: str | None = None
    if opts.raw_tiff:
        raw_dtype = _write_raw_tiff(raw_path, gated_image, {**metadata, "settings": settings})
        paths["raw_tiff"] = str(raw_path)
    else:
        omitted.append("raw_tiff")
    if opts.color_png:
        _write_color_png(
            png_path,
            gated_image,
            cmap=cmap,
            vmin=vmin,
            vmax=vmax,
            title=title,
            colorbar_label=colorbar_label,
        )
        paths["color_png"] = str(png_path)
    else:
        omitted.append("color_png")
    if opts.decay_csv:
        _write_decay_csv(csv_path, time_ns, decay)
        paths["decay_csv"] = str(csv_path)
    else:
        omitted.append("decay_csv")

    files = {k: Path(v).name for k, v in paths.items()}
    sel_block: dict[str, Any] | str | None = None

    if selection is not None and selection.labels and not opts.selection:
        omitted.append("selection")
        sel_block = "omitted (user choice)"
    elif selection is not None and selection.labels:
        mask_path = out_dir / f"{base}_selection_mask.tif"
        sdec_path = out_dir / f"{base}_selection_decay.csv"
        _write_selection_mask(mask_path, selection, {**metadata, "settings": settings})
        _write_selection_decay_csv(sdec_path, selection)
        paths |= {"selection_mask": str(mask_path), "selection_decay": str(sdec_path)}
        files |= {"selection_mask": mask_path.name, "selection_decay": sdec_path.name}
        sel_block = {
            "labels": selection.labels,
            "pixel_counts": selection.counts(),
            "pixel_columns": selection.pixel_columns,
            "label_map_values": "0 = unselected; k = the k-th label above",
            "aggregates": selection.aggregates,
        }
        if opts.pixel_table:
            spix_path = out_dir / f"{base}_selection_pixels.csv"
            _write_selection_pixels_csv(spix_path, selection)
            paths["selection_pixels"] = str(spix_path)
            files["selection_pixels"] = spix_path.name
        else:
            sel_block["pixel_table"] = "omitted (large selection; user choice)"
            omitted.append("selection_pixels")

    provenance = {
        "tool": "ChronoGate",
        "metadata": metadata,
        "settings": settings,
        "raw_tiff_dtype": raw_dtype,
        "selection": sel_block,
        "files": files,
        "omitted": omitted,
    }
    json_path.write_text(json.dumps(provenance, indent=2, default=str))
    paths["provenance"] = str(json_path)
    return paths


def _build_report_figure(
    *,
    title: str,
    summary_lines: list[str],
    time_ns: np.ndarray,
    decay: np.ndarray,
    t0_ns: float | None,
    gates_ns: list[tuple[float, float, str]],
    primary: dict[str, Any],
    image: np.ndarray,
    cmap: str,
    vmin: float,
    vmax: float,
    image_label: str,
    select_mask: np.ndarray | None = None,
    select_color: str = "#E91E63",
):
    """The one-page "everything" figure: a 2x2 gridspec on a landscape page.

    Four roles (see export_one-page-result-figure.md): identity & provenance
    (text, axis off), a convergence/quality diagnostic (the decay with gates and
    t0 marked, cumulative photon fraction on a twin axis), the primary result
    with its uncertainty visible, and the spatial field. Built on the Agg
    canvas directly so it renders headless (a cluster with no display).
    """
    import matplotlib
    from matplotlib.figure import Figure
    from matplotlib.backends.backend_agg import FigureCanvasAgg

    fig = Figure(figsize=(11, 8.5), dpi=150)          # US-letter landscape
    FigureCanvasAgg(fig)
    g = fig.add_gridspec(2, 2)

    # -- role 1: identity & provenance (top-left, text) ----------------------
    ax_meta = fig.add_subplot(g[0, 0])
    ax_meta.axis("off")
    # The title lives in the suptitle; this panel is the summary only.
    ax_meta.text(0, 1, "\n".join(summary_lines),
                 va="top", ha="left", family="monospace", fontsize=8.5,
                 transform=ax_meta.transAxes, wrap=True)

    # -- role 2: quality diagnostic (top-right, decay + gates + t0) ----------
    ax_conv = fig.add_subplot(g[0, 1])
    ax_conv.plot(time_ns, decay, lw=1.2, color="#3949ab")
    ax_conv.set_yscale("log")
    ax_conv.set_xlabel("time (ns)")
    ax_conv.set_ylabel("photons / bin (log)")
    ax_conv.set_title("summed decay — the photon statistics behind the result",
                      fontsize=9)
    if t0_ns is not None:
        ax_conv.axvline(t0_ns, ls="--", color="#888", lw=1)
        ax_conv.annotate("t0", (t0_ns, ax_conv.get_ylim()[1]),
                         fontsize=8, color="#888", ha="left", va="top")
    for i, (lo, hi, label) in enumerate(gates_ns):
        ax_conv.axvspan(lo, hi, alpha=0.15, lw=0,
                        color=["#3949ab", "#e53935"][i % 2], label=label)
    if gates_ns:
        ax_conv.legend(fontsize=8, loc="center right", framealpha=0.9)
    ax_twin = ax_conv.twinx()
    total = float(np.sum(decay))
    if total > 0:
        ax_twin.plot(time_ns, np.cumsum(decay) / total, color="#c0392b", lw=1)
    ax_twin.set_ylabel("cumulative photon fraction", color="#c0392b", fontsize=8)
    ax_twin.set_ylim(0, 1.05)

    # -- role 3: primary result, uncertainty visible (bottom-left) -----------
    ax_main = fig.add_subplot(g[1, 0])
    if primary["kind"] == "hist":
        values = np.asarray(primary["values"], float)
        values = values[np.isfinite(values)]
        bins = int(primary.get("bins", 40))
        rng = primary.get("range")
        counts, edges = np.histogram(values, bins=bins, range=rng)
        ax_main.bar(edges[:-1], counts, width=np.diff(edges), align="edge",
                    color="#3949ab", alpha=0.8, lw=0)
        sel = primary.get("selection")
        if sel is not None:
            sel = np.asarray(sel, float)
            sel = sel[np.isfinite(sel)]
            scounts, _ = np.histogram(sel, bins=edges)
            ax_main.bar(edges[:-1], scounts, width=np.diff(edges), align="edge",
                        color=select_color, alpha=0.85, lw=0, zorder=3,
                        label="selection")
            ax_main.legend(fontsize=8)
        if values.size:                       # median + IQR: the uncertainty
            q1, med, q3 = np.percentile(values, [25, 50, 75])
            ax_main.axvspan(q1, q3, color="#3949ab", alpha=0.12, lw=0)
            ax_main.axvline(med, color="#1a237e", lw=1.2, ls="-")
            ax_main.annotate(f"median {med:.3g}  (IQR {q1:.3g}–{q3:.3g})",
                             (0.02, 0.98), xycoords="axes fraction",
                             fontsize=8, va="top")
        ax_main.set_xlabel(primary.get("xlabel", ""))
        ax_main.set_ylabel("pixels")
    elif primary["kind"] == "phasor":
        gg = np.asarray(primary["g"], float).ravel()
        ss = np.asarray(primary["s"], float).ravel()
        ok = np.isfinite(gg) & np.isfinite(ss)
        ax_main.hexbin(gg[ok], ss[ok], gridsize=60, cmap="viridis",
                       mincnt=1, lw=0.1)
        th = np.linspace(0, np.pi, 200)       # universal semicircle
        ax_main.plot(0.5 + 0.5 * np.cos(th), 0.5 * np.sin(th),
                     color="#888", lw=1, ls="--")
        ax_main.set_xlabel("g")
        ax_main.set_ylabel("s")
        ax_main.set_xlim(-0.05, 1.05)
        ax_main.set_ylim(-0.02, 0.65)
        ax_main.set_aspect("equal")
    else:                                      # pragma: no cover - caller bug
        raise ValueError(f"unknown primary kind: {primary['kind']!r}")
    ax_main.set_title(primary.get("label", "primary result"), fontsize=9)

    # -- role 4: spatial context (bottom-right, the field) -------------------
    ax_map = fig.add_subplot(g[1, 1])
    cmap_obj = matplotlib.colormaps[cmap].copy()
    cmap_obj.set_bad(color="black")
    im = ax_map.imshow(image, cmap=cmap_obj, vmin=vmin, vmax=vmax,
                       interpolation="nearest")
    if select_mask is not None and select_mask.any():
        ax_map.contour(select_mask.astype(float), levels=[0.5],
                       colors=[select_color], linewidths=1.0)
    ax_map.set_xticks([])
    ax_map.set_yticks([])
    fig.colorbar(im, ax=ax_map, label=image_label, fraction=0.046, pad=0.04)

    fig.suptitle(title, fontsize=11)
    fig.tight_layout(rect=(0, 0, 1, 0.96))
    return fig


def export_report(
    out_dir: str | Path,
    base: str,
    *,
    metadata: dict[str, Any],
    settings: dict[str, Any],
    **fig_kwargs: Any,
) -> dict[str, str]:
    """Write the one-page report and its raw-data siblings; returns role -> path.

    The figure is a *view*, never the source of truth: the decay and the primary
    plot each ship their exact numbers as CSV, the field ships as a raw TIFF,
    and the provenance JSON (also a loadable settings file) records every file
    written -- someone should be able to regenerate the page from the folder
    alone. Both a raster (PNG, for sharing) and a vector (PDF, for print) are
    emitted.
    """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    fig = _build_report_figure(**fig_kwargs)
    png_path = out_dir / f"{base}_report.png"
    pdf_path = out_dir / f"{base}_report.pdf"
    fig.savefig(str(png_path), dpi=150)
    fig.savefig(str(pdf_path))

    decay_path = out_dir / f"{base}_decay.csv"
    _write_decay_csv(decay_path, fig_kwargs["time_ns"], fig_kwargs["decay"])

    primary = fig_kwargs["primary"]
    primary_path = out_dir / f"{base}_primary.csv"
    with open(primary_path, "w", newline="") as fh:
        w = csv.writer(fh)
        if primary["kind"] == "hist":
            values = np.asarray(primary["values"], float)
            values = values[np.isfinite(values)]
            counts, edges = np.histogram(values, bins=int(primary.get("bins", 40)),
                                         range=primary.get("range"))
            sel = primary.get("selection")
            if sel is not None:
                sel = np.asarray(sel, float)
                scounts, _ = np.histogram(sel[np.isfinite(sel)], bins=edges)
            else:
                scounts = np.zeros_like(counts)
            w.writerow(["bin_lo", "bin_hi", "count", "selection_count"])
            for lo, hi, n, ns in zip(edges[:-1], edges[1:], counts, scounts):
                w.writerow([f"{lo:.6g}", f"{hi:.6g}", int(n), int(ns)])
        else:
            gg = np.asarray(primary["g"], float).ravel()
            ss = np.asarray(primary["s"], float).ravel()
            w.writerow(["g", "s"])
            for a, b in zip(gg, ss):
                w.writerow([f"{a:.6g}", f"{b:.6g}"])

    tiff_path = out_dir / f"{base}_gated_raw.tif"
    raw_dtype = _write_raw_tiff(tiff_path, fig_kwargs["image"],
                                {**metadata, "settings": settings})

    paths = {
        "report_png": str(png_path),
        "report_pdf": str(pdf_path),
        "decay_csv": str(decay_path),
        "primary_csv": str(primary_path),
        "raw_tiff": str(tiff_path),
    }
    provenance = {
        "tool": "ChronoGate",
        "report": "one-page result figure; the CSVs/TIFF are the source data "
                  "behind each panel",
        "metadata": metadata,
        "settings": settings,
        "raw_tiff_dtype": raw_dtype,
        "files": {k: Path(v).name for k, v in paths.items()},
    }
    json_path = out_dir / f"{base}_provenance.json"
    json_path.write_text(json.dumps(provenance, indent=2, default=str))
    paths["provenance"] = str(json_path)
    return paths


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
