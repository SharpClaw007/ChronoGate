# ChronoGate — improvement plan (v0.4 → v0.5)

Fourteen improvements, grouped and ordered by impact. Status is checked off as
each lands. Guiding constraints: stay numpy-only where possible; keep the
analysis layer (`loader`/`gating`/`export`) Qt-free; every change ships with a
test and a green run of both suites.

## Wave A — core usability (fastest payoff)

- [x] **1. Frame cache for z-stacks.** `_on_zslice` re-decodes the `.ptu` from
  disk on every plane change. Add a bounded LRU cache of decoded `GatingModel`s
  keyed by `(path, channel, sum_frames, bin_size)` so stepping a stack is
  instant. Evict by count/bytes to bound memory.
- [x] **2. Lock color range.** `_clim_from` recomputes percentiles per frame, so
  z-stepping remaps the colormap and planes can't be compared. Add a **"lock
  scale"** toggle (freeze vmin/vmax) plus optional manual min/max entry, applied
  in both intensity and lifetime modes.
- [x] **3. Robust t0.** `t0 = argmax(decay)` is fragile on dim/scattery data.
  Smooth the summed decay before argmax and take the leading-edge rise; expose a
  **manual t0** override (a small control) and show t0 prominently.

## Wave B — responsiveness

- [x] **4. Threaded decode + progress.** Move `load_ptu` onto a `QThread`
  worker (object-lives-in-thread pattern, kept referenced, `quit()`+`wait()` on
  teardown) with a non-modal progress bar and a busy-guard so the UI stays alive
  on big multi-frame files. **Decision:** safe now that the launcher runs native
  arm64 (the earlier SIGABRT was x86/Rosetta + a modal dialog re-entering the
  loop); we avoid both.

## Wave C — FLIM features

- [x] **5. Phasor plot.** Per-pixel `(g, s)` from the DFT of each decay at the
  laser rep-rate (from the header period); a phasor scatter view with the
  universal semicircle. New **Phasor** view mode alongside Intensity/Lifetime.
  Pure cosine/sine transform of the existing cube (numpy-only).
- [x] **6. Multi-channel.** Files carry 2–3 channels; add a channel-combine
  control: single channel, **ratio** (chA/chB), or **merged** false-colour, so
  FRET donor/acceptor data is usable.
- [x] **7. Intensity-weighted lifetime (HSV).** Standard FLIM display: hue = τ,
  value = photon count, so dim pixels don't shout false lifetimes. A toggle in
  lifetime mode.
- [x] **8. τ histogram.** Histogram of the lifetime map (and gated-intensity),
  drawn under/near the image, with click-drag to restrict the displayed τ range.
- [x] **9. Pin decay.** Single-pick replaced comparison; add a **Pin** so one
  decay can be frozen while probing others (pinned + live overlaid).

## Wave D — IO & rigor

- [x] **10. Old-style `.ptu` labelling.** Decode-probe files up front (folder
  scan) and mark un-openable/old-style ones in the UI instead of failing on
  click.
- [x] **11. Batch export.** Apply the current gate/floor/threshold/mode across a
  whole stack and export every plane (TIFF/PNG/CSV/provenance) with progress.
- [x] **12. Provenance versioning.** Record `chronogate.__version__`, the
  `ptufile` version, and the resolved `t0_bin` in the provenance JSON so a run is
  actually reproducible.

## Wave E — distribution & tests

- [x] **13. Packaging + CI.** `pyproject.toml` with a `chronogate` entry point
  (pip-installable), and a GitHub Actions workflow running both suites headless
  (offscreen Qt) on push.
- [x] **14. Numeric-truth tests.** Assert the *stats numbers* and gated/lifetime
  arithmetic against a synthetic cube with a known answer; run the loader against
  a synthetic old-style-style failure; pin the phasor of a mono-exponential.

## Notes / decisions
- Keep everything numpy-only (no scipy). Phasor, HSV, histograms are all doable.
- New view modes (Phasor) join the Intensity/Lifetime action group.
- The frame cache is memory-bounded; big single frames (GUVs, 4096 bins) count
  their bytes so the cache never blows past a cap.
