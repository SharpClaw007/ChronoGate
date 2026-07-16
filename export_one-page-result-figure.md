# How to Make a One-Page "Everything" Result Figure

A single-sheet export that carries *all the information a reader needs to trust and
reproduce a result*: the headline number, how it converged, the primary data, a
spatial/structural view, and the provenance to regenerate it.

This started as the NBEAST run report, but the anatomy is discipline-agnostic — the
same layout works for a Monte Carlo neutronics run, a molecular-dynamics trajectory,
a genomics pipeline, an electrophysiology recording, or a climate-model ensemble.
Swap the *content* of each panel; keep the *roles*.

---

## Part 1 — Doing it in NBEAST

**File → Export report…**, pick a folder. You must have a completed run loaded
(run a simulation first; the button is inert otherwise). It writes:

| File | What it is |
|------|-----------|
| `report.png` / `report.pdf` | The one-page figure (the "nice export image") |
| `openmc_deck/` | The reproducible input deck |
| `spectrum.csv` | The raw flux-spectrum numbers behind the plot |
| `flux.vtk`, `flux_panel.png` | The 3-D field, for re-rendering elsewhere |

The PNG's four panels:

- **Top-left — summary text.** k-effective ± σ, materials, parameters, diagnostics,
  and a provenance block (cross-section library hash, seed, threads, engine).
- **Top-right — convergence.** k-effective vs. batch, with Shannon entropy on a
  twin axis and a dashed line marking the end of inactive cycles.
- **Bottom-left — flux energy spectrum.** Log-x, per-lethargy, with a ±1σ band. The
  exact numbers are dumped alongside as `spectrum.csv`.
- **Bottom-right — scalar-flux field.** The off-screen 3-D render, correctly oriented.

For raw mesh arrays instead of a figure, use **File → Export raw data…**
(NumPy / CSV / HDF5, with uncertainties).

---

## Part 2 — The generic recipe (any discipline)

A trustworthy one-pager answers four questions in four panels. Fill each role with
whatever your field's equivalent is.

### The four roles

1. **Identity & provenance** *(top-left, text)* — the headline result with its
   uncertainty, the inputs that produced it, and enough metadata to regenerate it:
   software version, data/reference-library identity (a **content hash**, not just a
   name), random seed, hardware/thread count, date.
   *Neutronics:* k-eff ± σ, materials, XS library hash.
   *MD:* final energy, force field + version, integrator, thermostat, seed.
   *Genomics:* variant count, reference build, aligner version, pipeline commit.
   *Climate:* ensemble mean ± spread, model version, forcing scenario, grid.

2. **Convergence / quality diagnostic** *(top-right, line plot)* — evidence the
   result *settled* and isn't an artifact of too-short a run. Plot the tracked
   quantity vs. iteration; overlay a second diagnostic on a twin axis; mark where
   burn-in/equilibration ends.
   *Neutronics:* k vs. batch + Shannon entropy.
   *MD:* energy/temperature vs. step + RMSD.
   *MCMC:* trace + running R̂.
   *Optimization:* loss + validation metric vs. epoch.

3. **Primary result** *(bottom-left, the money plot)* — the actual scientific
   quantity, **with uncertainty shown** (band, error bars, or CI), and the raw
   numbers exported to a sibling CSV so no one has to trace pixels.
   *Neutronics:* flux spectrum ±1σ. *Spectroscopy:* intensity vs. wavelength.
   *Dose-response:* fitted curve + CI. *Survival:* Kaplan-Meier + band.

4. **Spatial / structural context** *(bottom-right, field or map)* — *where* it
   happens: a rendered field, a heatmap, a structure, a geographic map. Export the
   underlying field (VTK / GeoTIFF / mesh) so it can be re-rendered at higher
   fidelity later.

### Non-negotiables (why the figure is trustworthy)

- **Uncertainty is always visible.** A number without a σ, or a curve without a
  band, is not a result.
- **Provenance is a content hash, not a label.** "Library X" can mean two different
  files; `sha256(index + manifest)` cannot. Same for code: record the commit.
- **The figure and its raw data ship together.** Every plotted curve gets a CSV; every
  field gets a portable file. The image is a view, not the source of truth.
- **Reproducibility is embedded, not remembered.** Seed + versions + the actual input
  deck travel in the export folder. Someone should be able to rerun from the folder
  alone.
- **Headless-safe.** Generate with a non-interactive backend so it works on a cluster
  with no display (see the recipe below).

### Minimal implementation (matplotlib, headless)

The pattern is a `2×2 gridspec` on a landscape page; text panel has its axis turned
off; primary and diagnostic panels use twin axes for the secondary series.

```python
import matplotlib
matplotlib.use("Agg")            # headless: no display needed
import matplotlib.pyplot as plt

fig = plt.figure(figsize=(11, 8.5))       # US-letter landscape
g = fig.add_gridspec(2, 2)

ax_meta = fig.add_subplot(g[0, 0]); ax_meta.axis("off")
ax_meta.text(0, 1, title + "\n\n" + "\n".join(summary_lines),
             va="top", ha="left", family="monospace", fontsize=11)

ax_conv = fig.add_subplot(g[0, 1])        # convergence
ax_conv.plot(iters, tracked, lw=1.2)
ax_conv.axvline(burn_in, ls="--", color="#888")     # mark end of burn-in
ax_twin = ax_conv.twinx(); ax_twin.plot(iters, diagnostic, color="#c0392b")

ax_main = fig.add_subplot(g[1, 0])        # primary result + uncertainty
ax_main.fill_between(x, y - sigma, y + sigma, alpha=0.25, lw=0)
ax_main.plot(x, y, lw=1.2)

ax_map = fig.add_subplot(g[1, 1]); ax_map.axis("off")
ax_map.imshow(plt.imread(field_png))      # or contourf / a rendered field

fig.suptitle("Run report")
fig.tight_layout(rect=(0, 0, 1, 0.97))
fig.savefig("report.pdf")                 # vector, for print
fig.savefig("report.png", dpi=120)        # raster, for sharing
```

Export the numbers next to it (`csv.writer`), and dump the field to a portable
format (VTK, GeoTIFF, `.npz`) so the panel can be regenerated independently.

### Adapting the layout

- **More than four things to say?** Go `2×3` or add a second page — don't crowd.
  Legibility beats completeness on any single sheet.
- **No spatial dimension?** Replace the field panel with a residuals plot,
  a parameter table, or a second diagnostic.
- **Time series, not spatial?** The "field" panel becomes a full-width strip; drop
  to a `3×1` stack.
- **Print vs. screen.** Always emit *both* a vector (PDF/SVG) and a raster (PNG);
  captions and hashes must stay legible at 100% zoom.
