# ChronoGate — plan & handoff

**State at the end of the last session: v0.14.0, working tree clean,
52 tests green (14 in `test_gating.py`, 38 in `test_ui_smoke.py`).**

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

Nothing is in progress. v0.14 added **restrict-to-selection exports**
(`ExportOptions.restrict_to_selection`, dialog checkbox, enabled only with a
selection): rasters keep full-frame geometry but go NaN outside the selection
(so coordinates stay valid and the TIFF becomes float32), the decay CSV
becomes the selection's pooled counts (`mask_decay × n`, rinted), the report
page is fully scoped (masked field, pooled decay, selection-only
primary/headline, "scope selection only — N px of M" line, `_sel` basename
suffix, histogram range=None so a percentile clim can't clip a handful of
pixels out of their own histogram). The provenance records
`restricted_to_selection` as what *actually happened* — the flag without a
selection quietly exports the full frame and records false.

v0.13 added the **one-page report** (File ▸ Export
report… / Ctrl+R), modelled on `export_one-page-result-figure.md` (a
user-provided recipe; keep following it for report changes). Four roles on a
2×2 landscape page: identity & provenance text (headline number with IQR
first, sha256 content hash of the source .ptu, acquisition/analysis settings,
versions, date), the summed decay with gate spans + t0 + cumulative-fraction
twin axis, a mode-aware primary plot (τ histogram with median/IQR band and
selection overlay · phasor hexbin + semicircle · gated-intensity histogram),
and the field with colorbar + selection outline. Written headless (Agg) as
PNG **and** PDF by `export._build_report_figure` / `export.export_report`
(data-only args, matplotlib imported lazily); every panel's numbers ship
alongside (decay CSV, primary CSV, raw TIFF, provenance JSON that doubles as
a settings file and records the full hash). Controller assembly:
`_report_summary_lines`, `_source_sha256` (cached per path), `export_report`,
`_on_export_report`. The export dialog offers it too (`chk_report`,
`ExportOptions.report`, default **off** so a plain `export()` keeps the
classic file set); in a batch it writes one page per plane.

v0.12 added **reopen-last-at-launch**
(`chronogate/ui/prefs.py`): a Preferences dialog (File ▸ Preferences…, macOS
app menu via `PreferencesRole`) with a checkbox that makes the app open the
last used .ptu/stack — the last *viewed plane*, recorded on every successful
decode (`_after_initial_load`, `_after_reload`, `_reload_model_busy`; batch
export's direct `_load_current` deliberately does not move it). QSettings-
backed; `CHRONOGATE_PREFS_INI` redirects to an ini so the test suite never
touches real user config (set globally at the top of `test_ui_smoke.py`).
Startup resolution lives in `app._startup_path`: CLI path > recorded path (if
the pref is on and the file still exists) > welcome screen — `MainWindow`
itself stays dumb, so tests constructing `MainWindow(None)` are unaffected by
the user's real preference.

v0.10.1 fixed a startup crash (Qt-virtual shadowing —
see Landmines). v0.11 added the **export dialog**
(`chronogate/ui/export_dialog.py`): File ▸ Export and File ▸ Export all planes
both open one dialog choosing the artefacts (raster TIFF / colormapped PNG /
decay CSV / selection files / per-pixel table with its size announced), the
output folder, and single-plane vs batch; the choices land in
`export.ExportOptions` (frozen dataclass, Qt-free), which
`controller.export` / `batch_export` and `export_all` all take. Provenance is
always written and records skipped artefacts under `"omitted"`.

Earlier: v0.9 closed the original seven-item improvement list (selection
stats, group compare, big-CSV ask, batch selection re-cut, phasor calibration,
`_lifetime_init` defused, gridlines-under-hexbin). v0.10 closed **all six
candidates from the v0.9 handoff**, one commit each, then passed an
adversarial diff review (no findings) and an offscreen full-feature launch
smoke:

1. **Shared τ-map cache.** `MetricContext.tau_fn` (the `phasor_fn` pattern) +
   `controller._tau_map()`, keyed on (model-weakref, RLD gates, floor, min
   counts). The pick legend, the pixel list, `mask_stats` and the exports all
   read one map instead of re-running RLD each.
2. **Aggregates in provenance.** Each exported selection label carries its
   `mask_stats` block (mean/median/std/valid-n per metric), JSON-safe (NaN →
   null via `_json_safe_stats`), present even when the pixel table is omitted.
3. **τref marker + τ ruler.** A calibrated phasor marks the reference position
   (cross + label) and labelled τ ticks (0.5–16 ns) along the semicircle — the
   reference cloud must sit on its cross, so a calibration is visibly checkable.
   Only drawn when calibrated; meaningless on a raw phasor.
4. **Second harmonic** (View ▸ Phasor 2nd harmonic). `self.harmonic` (1|2) keys
   the (g, s) cache, lasso, metrics, ruler and title; **each harmonic keeps its
   own calibration** (`phasor_cals: {harmonic: cal}`). Settings persist both;
   v0.9 single-cal files land on harmonic 1.
5. **Selection overlay on the τ histogram.** The inset overlays the active
   selection's τ distribution (same 40 display-range bins, accent colour).
6. **Undo for selections** (Ctrl+Z / View ▸ Undo selection change). A 20-deep
   history of complete pick states; pixel-cursor walks coalesce into one entry
   so holding an arrow key cannot flush a lasso out of the bounded history;
   pin/clear/settings-load are always their own step; restored states are
   revalidated against the current model shape.

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
(model-weakref, t0, harmonic, (φ, mod)), feeds the plot, the lasso and the g/s
metrics, so they can never disagree about which (g, s) space they are in.
`calibrate_phasor` always measures the **raw** maps — recalibrating replaces the
correction instead of compounding it. Saved lasso polygons live in calibrated
space, which is why `apply_settings` restores the harmonic and calibrations
*before* the picks. The τ map has the same shape of cache (`_tau_map`), injected
into `MetricContext.tau_fn`.

**Pick history** (`_snapshot_picks` / `undo_pick`). Snapshots are complete
tuples (picks, pins, spotlight mask, lasso verts); the lists are copied because
`_on_pin` mutates `pinned_picks` in place, the pick dicts are shared because
they are never mutated. Consecutive *trivial* states (mask-less, all-pixel)
coalesce; mutations that deserve their own step pass `force=True`.

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
- **A widget attribute must never share a name with a Qt virtual.** PySide6
  dispatches C++ virtual calls through Python attribute lookup, so
  `self.metric = QComboBox()` made Qt's own DPI probe die with
  `TypeError: Error calling Python override of QWidget::metric(): ... not
  callable` — instant crash at startup (`dock.setFloating(True)`), and
  **environment-dependent**: the same tree passed the offscreen smoke one day
  and crashed the next (display/OS config decides whether Qt calls `metric()`).
  Guarded by `test_no_qt_virtual_shadowing`, which sweeps every widget for
  non-callable attributes named after commonly-queried virtuals (`metric`,
  `event`, `sizeHint`, `paintEngine`, …).

---

## Next candidates (nothing committed to; ranked as I'd do them)

1. **Export the phasor plot.** Largely covered by the v0.13 one-page report
   (phasor mode: hexbin + semicircle panel, (g, s) rows as CSV). Still missing:
   the calibration ruler/lasso on the report panel, and the (g, s) maps as
   TIFFs for people who compute their own fractions.
2. **Redo.** Undo exists; a redo stack is its natural complement (undo currently
   discards the popped state).
3. **A visible calibration readout.** The calibration lives only in the phasor
   title and a status message; a persistent readout (φ, mod, τref, harmonic —
   e.g. a Stats row in phasor mode) would make state obvious after a reload.
4. **Two-component fractions.** With a calibrated phasor, the fraction along the
   chord between two chosen semicircle points is quantitative composition — the
   standard next analysis after calibration (pick τ₁/τ₂, colour pixels by
   fractional position).
5. **Arbitrary harmonic (spin box).** The plumbing is harmonic-generic already;
   the UI exposes 1|2. Cheap if ever needed; do with a use case in hand.
6. **IRF-aware analysis.** Loading a measured IRF (deconvolved fitting, exact
   t0) is the step from "gating viewer" toward "fitting tool" — big, and it
   changes the numpy-only constraint conversation (still doable with numpy).

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
  Since v0.11 the "ask" is the export dialog's pixel-table checkbox starting
  unchecked (row count + ~MB in its label) above `_PIXEL_TABLE_WARN_ROWS`
  (100k rows ≈ several MB) — one click re-opts in.
- **The provenance JSON is not a dialog checkbox.** Whatever artefact subset is
  picked, the sidecar is written and lists the skipped artefacts under
  `"omitted"` — an export you cannot reproduce is not an export.
- **Batch export re-cuts lassos per plane** (a lifetime signature means something
  on every plane; the drawing plane's pixel mask does not). Located picks —
  pixels, ROIs, coordinate groups — carry across unchanged.
- **Calibration measures the raw phasor, always.** Building the factor from
  calibrated maps would compound corrections on every recalibrate.
- **Calibration is per harmonic.** The same reference measurement yields a
  different complex factor at each ω; one shared factor would be silently wrong
  on the other harmonic. `phasor_cal` (the property) is "the current harmonic's
  calibration or None".
- **Undo coalesces pixel walks, and only pixel walks.** Every mask/ROI/pin/clear
  state is preserved; without coalescing, holding an arrow key flushes the lasso
  out of the 20-deep history — the exact loss undo exists to prevent.
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
