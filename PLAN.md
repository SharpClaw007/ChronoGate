# ChronoGate — plan & handoff

**State at the end of the last session: v0.9.0, working tree clean,
38 tests green (12 in `test_gating.py`, 26 in `test_ui_smoke.py`).**

Run both suites with the project venv (there is no pytest installed; the files are
runnable directly):

```bash
.venv/bin/python test_gating.py
.venv/bin/python test_ui_smoke.py
```

Guiding constraints, unchanged: **numpy-only** (no scipy); the analysis layer
(`loader` / `gating` / `metrics` / `export`) stays **Qt-free and matplotlib-free**;
every change ships with a test and a green run of both suites.

---

## Where we left off

Nothing is in progress. The last session (v0.9) closed out **all seven candidates**
from the previous handoff, one commit each:

1. **Selection statistics.** `metrics.mask_stats` aggregates any registered metric
   over a pixel mask (mean/median/std over *finite* values, plus the valid count);
   the Stats panel now speaks for the active pick — pixel, ROI, lasso or list
   group — and reverts to whole-image stats on clear.
2. **Compare selections.** Each shown pick (pinned or live) states its aggregate
   `med τ x.xx ns` in the legend and pick list, from one shared τ map — so two
   phasor clusters or top-N/bottom-N groups compare by numbers, not just shape.
3. **Big pixel-table warning.** A >100k-pixel selection announces its row count and
   ~CSV size and asks before writing the per-pixel table (`_confirm_pixel_table` /
   `export(..., include_pixel_table=)`). Never capped; the label map and pooled
   decays are written either way; an omission is recorded in the provenance.
4. **Batch export carries selections.** Picks travel as *recipes*: a lasso polygon
   is re-cut against each plane's own phasor, located picks carry across, and the
   live picks are restored untouched afterwards.
5. **Phasor calibration** (View ▸ Calibrate phasor). The median **raw** phasor of
   the selection (or thresholded image) maps onto the semicircle position of a
   known reference τ; the rotation/modulation applies to plot, lasso and g/s
   metrics alike, persists through settings/provenance, and clears cleanly.
   Numeric layer: `gating.phasor_reference/phasor_calibration/apply_phasor_calibration`.
6. **`_lifetime_init` defused.** Gate B is a valid later gate from load, and
   `_rld_gates` is keyed on the *mode*: lifetime mode uses the A/B pair verbatim
   (τ column == τ map), every other mode splits the current gate into equal
   halves. The flag now only picks the first-entry gate layout.
7. **Phasor gridlines** draw under the hexbin (`set_axisbelow(True)`).

## Architecture a newcomer needs to know

**`chronogate/metrics.py` — the extension point.** A registry of per-pixel
quantities. Each is a pure function of a `MetricContext` returning a `(Y, X)` float
array, `NaN` where undefined. Adding one is **a single decorated function**; it
becomes a pixel-list column, a sort key, a filter, a column in the exported pixel
table, *and* an aggregate in `mask_stats`, with no change to the panel, the
controller or the table:

```python
@register("peak_bin", "peak bin", fmt="{:.0f}")
def _peak_bin(ctx):
    return ctx.model.counts.argmax(axis=-1).astype(float)
```

**Picks are one of three kinds** (`controller.py`): `pixel`, `roi`, `mask`. A
`mask` covers both the phasor lasso and a pixel-list multi-select, so anything that
selects many pixels reuses one code path — pooled decay, spotlight overlay,
combined readout, export, aggregate stats. `self.select_mask` is the mask
currently spotlighted.

**Picks serialise as *recipes*, not pixel dumps** (`_pick_recipe`). A phasor
selection stores its lasso **polygon**, so a 95,000-pixel selection round-trips
through a few vertices and is re-cut against the restored gate/threshold/binning —
and, since v0.9, against **each plane's phasor** during a batch export
(`_picks_for_current_model`).

**The phasor calibration lives in `_phasor_maps`.** One cache, keyed on
(model-weakref, t0, (φ, mod)), feeds the plot, the lasso and the g/s metrics, so
they can never disagree about which (g, s) space they are in. `calibrate_phasor`
always measures the **raw** maps — recalibrating replaces the correction instead
of compounding it. Saved lasso polygons live in calibrated space, which is why
`apply_settings` restores the calibration *before* the picks.

**The hover blit** (`_hover_begin` / `_hover_draw` / `_on_decay_draw`). The y-axis
is *frozen* for a hover session — required (a blit background bakes the axis in)
and desirable (a brighter pixel visibly is brighter, rather than the axis rescaling
to hide it). Hover artists carry `animated=True` so ordinary draws skip them.

---

## Landmines (all of these bit us; do not re-learn them)

- **`AxesImage.set_data()` does not update the extent.** You must call
  `set_extent()` too, or the image renders into the 2×2 placeholder corner and the
  panel looks blank — and mouse picking silently maps into that corner as well.
- **matplotlib's font (DejaVu Sans) has no emoji.** 📌 in a plot label is a tofu
  box plus a `UserWarning` per redraw. Qt widgets render emoji fine; the plot does
  not. (`_PIN_MARK_PLOT` / `_PIN_MARK_LIST`.) Combining glyphs (τ̃) are equally
  risky — v0.9 spells it `med τ` instead.
- **matplotlib's default `axisbelow='line'` draws the grid at z 1.5 — over a
  hexbin collection (z 1).** `set_axisbelow(True)` for any axes that hold
  collections.
- **Reversing an ascending sort also reverses the ties.** `metrics.rank` sorts the
  *negated* values, or "the brightest pixel" is the last tied maximum, not the first.
- **A spin box rounds, and a rounded filter bound excludes the extreme.** Seeded
  filter bounds are rounded **outward**, or a `vmax` of 48.947 silently drops the
  true maximum of 48.9474 — the pixel you opened the list to find.
- **`ptufile` metadata lies about old-style files**; they only fail at
  `decode_image`. Always probe with a real frame-0 decode (`loader.probe_ptu`).
- **A Qt widget in a never-shown window reports `isVisible() == False`** (docks
  also skip `visibilityChanged`). Guard on `isHidden()` (user intent); show the
  window in tests — or filter on `isHidden()` there too.
- **Component-wise medians do not commute with rotation.** The median of a
  calibrated cloud is *not* the calibrated median of the raw cloud; the
  calibration tests verify through the exact (φ, mod) factor, not cloud medians.
- **A wide gate split in half starves the late half.** With the default
  full-tail gate, split-half RLD τ is NaN almost everywhere on real data (the
  late half is pure tail). Tests that need τ narrow the gate to the decay first.
- **BSD `sed` has no `\b`.** A `sed -i '' 's/\bfoo\b/bar/g'` silently does nothing.
- **macOS:** run native arm64, never Rosetta; `QFileDialog` must use
  `DontUseNativeDialog`; join the decode `QThread` on close or you get a SIGABRT.

---

## Next candidates (nothing committed to; ranked as I'd do them)

1. **Mark the reference point on the calibrated phasor.** After calibrating, draw
   the τref position (and maybe a τ ruler along the semicircle: 1, 2, 4, 8 ns
   ticks). Cheap, and it makes the calibration visibly verifiable.
2. **Selection aggregates into the provenance.** `mask_stats` output (mean/median
   τ, photons) recorded per selection in the export JSON — the numbers a paper
   quotes should leave the program with the data.
3. **Second harmonic.** `phasor()` already takes `harmonic`; the UI pins it to 1.
   A toggle plus per-harmonic calibration disambiguates multi-component mixtures.
4. **τ-histogram inset for the selection.** The lifetime histogram currently shows
   the whole image; overlaying the selection's τ distribution (same bins, accent
   colour) would make "is this cluster different?" answerable at a glance.
5. **Cache the pick-legend τ map.** `_redraw_pick_lines` recomputes the τ map per
   decay refresh while picks are shown (~ms, fine, but it is the same map the
   pixel list computes — one keyed cache would serve both).
6. **Undo for picks.** Losing a carefully drawn lasso to a stray click on the
   image is the sharpest UX edge left.

## Non-obvious decisions worth not re-litigating

- **A raw pixel list is useless** — 262,144 rows. The list is *always* ranked and
  truncated, and the truncation is stated in the summary rather than hidden.
- **A multi-pixel selection is pooled into one curve**, not overlaid as N curves.
  Two hundred individual decays are unreadable; Pin covers comparing a few.
- **The τ metric's gates follow the mode.** In lifetime mode the τ column must
  match the τ map (user's A/B verbatim); anywhere else it splits the *current*
  gate, because quoting τ for gates the view no longer shows is worse than a
  slightly different estimate. Do not "unify" these.
- **The big pixel table asks; it never caps.** It is the data the user asked for.
  The ask threshold is `_PIXEL_TABLE_WARN_ROWS` (100k rows ≈ several MB).
- **Batch export re-cuts lassos per plane** (a lifetime signature means something
  on every plane; the drawing plane's pixel mask does not). Located picks —
  pixels, ROIs, coordinate groups — carry across unchanged.
- **Calibration measures the raw phasor, always.** Building the factor from
  calibrated maps would compound corrections on every recalibrate.
- **A tint alone cannot mark a selection** — a green wash over viridis reads as
  just another colormap value. The overlay veils the *unselected* pixels and tints
  the selected in magenta (`theme.SELECT`, a hue no colormap here produces). Small
  groups (≤500 px) also get a ring per pixel, or they are invisible single dots.
- **Arrow keys are scoped per canvas** (image = pixel cursor, decay = gate nudge,
  Alt+arrow = gate from anywhere). A window-wide binding would swallow Up/Down from
  every spin box in the app, and two window-wide bindings on one key is an
  ambiguous-shortcut overload.
- The hover readout lives on the **decay panel and the status bar**, not the image
  title: those repaint for free, the image title cannot repaint at cursor speed.
