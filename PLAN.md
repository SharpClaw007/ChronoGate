# ChronoGate — plan & handoff

**State at the end of the last session: v0.8.0, `c164e8d`, working tree clean,
29 tests green (10 in `test_gating.py`, 19 in `test_ui_smoke.py`).**

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

Nothing is in progress. The last four sessions closed out two themes:

**v0.5 — the fourteen-item improvement plan (all landed).** Frame cache, lock
colour scale, robust t0, threaded decode, phasor plot, multi-channel combine,
intensity-weighted (HSV) lifetime, τ histogram, pin decay, old-style `.ptu`
probing, batch export, provenance versioning, packaging + CI, numeric-truth tests.

**v0.6 → v0.8 — inspecting individual pixels.** The driving problem: a 512×512
image in a ~400 px panel is ~1.3 data pixels per screen pixel, so *clicking cannot
land on a chosen pixel*. What shipped:

- **Hover probe** (`v0.6`, made truly live in `v0.7`). The pixel under the cursor
  is drawn continuously by **blitting** — the static parts of the decay panel are
  rendered once and cached as a bitmap, and each frame repaints only the
  `animated` hover artists over it. **6.2 ms/frame (~160 fps)** against ~100 ms for
  the full redraw it replaced.
- **Pixel list** (`Ctrl+P`, floating dock): a ranked, filterable table of
  individual pixels, driven by a metrics registry. Finder-style multi-select
  (Ctrl/⌘, Shift, Ctrl+A).
- **Arrow-key pixel cursor**, **go-to (row, col)**, and **crosshair markers** on
  the image (a picked pixel is otherwise sub-pixel on screen and invisible).
- **Phasor lasso**: select pixels by *lifetime signature* rather than by location.
- **Selections export** and **persist** (`v0.8`), plus five smaller fixes.

---

## Architecture a newcomer needs to know

**`chronogate/metrics.py` — the extension point.** A registry of per-pixel
quantities. Each is a pure function of a `MetricContext` returning a `(Y, X)` float
array, `NaN` where undefined. Adding one is **a single decorated function**; it
becomes a pixel-list column, a sort key, a filter, *and* a column in the exported
pixel table, with no change to the panel, the controller or the table:

```python
@register("peak_bin", "peak bin", fmt="{:.0f}")
def _peak_bin(ctx):
    return ctx.model.counts.argmax(axis=-1).astype(float)
```

**Picks are one of three kinds** (`controller.py`): `pixel`, `roi`, `mask`. A
`mask` covers both the phasor lasso and a pixel-list multi-select, so anything that
selects many pixels reuses one code path — pooled decay, spotlight overlay,
combined readout, export. `self.select_mask` is the mask currently spotlighted.

**Picks serialise as *recipes*, not pixel dumps** (`_pick_recipe`). A phasor
selection stores its lasso **polygon**, so a 95,000-pixel selection round-trips
through a few vertices and is re-cut against the restored gate/threshold/binning.

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
  not. (`_PIN_MARK_PLOT` / `_PIN_MARK_LIST`.)
- **Reversing an ascending sort also reverses the ties.** `metrics.rank` sorts the
  *negated* values, or "the brightest pixel" is the last tied maximum, not the first.
- **A spin box rounds, and a rounded filter bound excludes the extreme.** Seeded
  filter bounds are rounded **outward**, or a `vmax` of 48.947 silently drops the
  true maximum of 48.9474 — the pixel you opened the list to find.
- **`ptufile` metadata lies about old-style files**; they only fail at
  `decode_image`. Always probe with a real frame-0 decode (`loader.probe_ptu`).
- **A Qt dock in a never-shown window reports `isVisible() == False`** and does not
  emit `visibilityChanged`. Guard on `isHidden()` (user intent); show the window in
  tests.
- **BSD `sed` has no `\b`.** A `sed -i '' 's/\bfoo\b/bar/g'` silently does nothing.
- **macOS:** run native arm64, never Rosetta; `QFileDialog` must use
  `DontUseNativeDialog`; join the decode `QThread` on close or you get a SIGABRT.

---

## Next candidates (nothing committed to; ranked as I'd do them)

1. **Selection statistics.** The pixel table exports fine, but the app itself never
   states the *aggregate* for a selection — mean/median τ, mean photons, spread.
   The Stats panel has four rows and currently ignores the selection entirely. This
   is the obvious next thing a user asks for after selecting a population.
2. **Compare selections.** Pin currently freezes one decay. Pinning *groups* (two
   phasor clusters, or top-100 vs bottom-100 by τ) and showing their decays and
   stats side by side is the natural analysis, and the machinery is all there.
3. **A big pixel table is a big CSV** — ~10 MB for a 160k-pixel lasso. Not capped
   (it is the data the user asked for), but worth an explicit warning, or a
   "selected pixels only, not every metric" option.
4. **Batch export ignores selections.** `batch_export` walks the stack applying the
   gate; it does not carry a selection across planes. Whether it *should* is a real
   question (is a lasso meaningful on a different z-plane?) — probably re-cut per
   plane from the polygon, which the recipe format already allows.
5. **The phasor is uncalibrated** (t0-referenced). A reference-lifetime calibration
   (measure a known dye, rotate/scale the cloud) is the standard next step, and is
   what makes the phasor quantitative rather than a clustering aid.
6. **`_lifetime_init` is load-bearing and subtle.** The RLD gate B is only
   configured on entering lifetime mode, which is why the τ metric needed a
   fallback (`_rld_gates` splits the current gate in half). Worth making the two
   gates always-valid at load instead of carrying the fallback.
7. Cosmetic: the phasor axes' gridlines draw over the hexbin.

## Non-obvious decisions worth not re-litigating

- **A raw pixel list is useless** — 262,144 rows. The list is *always* ranked and
  truncated, and the truncation is stated in the summary rather than hidden.
- **A multi-pixel selection is pooled into one curve**, not overlaid as N curves.
  Two hundred individual decays are unreadable; Pin covers comparing a few.
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
