# ChronoGate scientific-hardening — design

Date: 2026-07-21
Status: approved (via `/goal`), implementation in progress

Goal: close the gaps that block ChronoGate from being trustworthy **scientific**
analysis software, plus add a second input format. Four independent milestones,
one combined spec, executed in order M1 → M4.

Milestone map:

| M  | Item                         | Rough | Parallelizable |
|----|------------------------------|-------|----------------|
| M1 | Phasor validation (pt 4)     | ~0.5d | yes (subagent) |
| M2 | Format dispatch + `.sdt`     | ~1.5d | yes (subagent) |
| M3 | RLD σ + Monte-Carlo (pt 3)   | ~1.5d | no (solo)      |
| M4 | IRF reconvolution τ-map (pt 2)| ~1–2wk| no (solo)     |

New dependencies: `scipy>=1.10` (M4 solver), `sdtfile>=2023` (M2 reader).
Both already installed in the dev venv (scipy 1.18.0, sdtfile 2026.7.17).

TDD throughout: RED test first, then implement, per milestone. Tests run
directly via the venv python (`.venv/bin/python test_*.py`), no pytest.

---

## M1 — Phasor validation (point 4)

**Finding:** the phasor path is already well tested — `test_phasor_*` cover
semicircle placement (h1), reference-lifetime calibration round-trip, harmonic-2
consistency, identity calibration, degenerate-reference error, and NaN handling.

**Gap:** the **linear-combination law** — the defining phasor property — is
untested. A two-component decay (fractions `f1`, `f2=1−f1`, lifetimes τ1, τ2) has
a phasor equal to the **intensity-weighted mean** of the two component phasors,
and lands strictly *inside* the universal semicircle.

**Deliverable:** add `test_phasor_linear_combination` to `test_gating.py`:
- Build a synthetic cube whose per-pixel decay is `f1·exp(-t/τ1)+f2·exp(-t/τ2)`.
- Assert `phasor(mixture) ≈ w1·P(τ1) + w2·P(τ2)` where the weights are the
  photon fractions (steady-state intensities `fi·τi`, normalized).
- Assert the mixture point is inside the circle: `(g-0.5)²+s² < 0.25`.
- Assert it lies on the chord between `P(τ1)` and `P(τ2)`.
Register in the `_synthetic_tests` runner list. No product-code change expected;
if a discrepancy surfaces, it is a real bug — fix `phasor()` and note it.

Scope note: this is small (one test); it runs as a subagent only to parallelize
with M2. The subagent edits **only** `test_gating.py`.

---

## M2 — Format dispatch + `.sdt`

### (a) Dispatch refactor
Currently `.ptu` is hardwired in four spots: the CLI default-dir glob and
`_resolve_path`/`_first_ptu_under` (`__main__.py`), `find_stack` regex
(`loader.py`), `probe_ptu` (`loader.py`), and the Qt file-dialog filter
(`controller.py:2277`, and the `rglob("*.ptu")` at `controller.py:2296`).

Introduce a reader registry in `loader.py`:
```python
READERS = {".ptu": load_ptu, ".sdt": load_sdt}   # ext -> (path,…) -> FlimCube
def load_flim(path, **kw) -> FlimCube: ...         # dispatch by suffix.lower()
def probe_flim(path) -> str: ...                    # dispatch; ptu keeps old-style probe
def flim_glob_patterns() -> list[str]: ...          # ["*.ptu","*.sdt"] for globs/dialogs
```
- Keep `load_ptu`, `probe_ptu` intact (back-compat; `load_flim`/`probe_flim`
  wrap them). Unknown extension → `UnsupportedFileError`.
- Generalize `find_stack` to match the file's own registered extension (it
  already carries `ext` through — widen the initial regex to any known ext).
- `__main__.py`: `_first_ptu_under` → `_first_flim_under` over registry exts;
  default-dir glob likewise.
- `controller.py`: dialog filter → `"FLIM data (*.ptu *.sdt);;PicoQuant PTU
  (*.ptu);;Becker & Hickl SDT (*.sdt);;All files (*)"`; `rglob` loop over
  registry exts.

### (b) `.sdt` reader — `load_sdt(path, channel=0, frame=None, sum_frames=True)`
Use `sdtfile.SdtFile`:
- `sdt.times` (seconds) → `resolution_ns = mean(diff(times))*1e9`; `n_bins = len(times)`.
- `sdt.data` is a list of blocks (ndarray); `sdt.measure_info` a list of records.
  A FLIM image block reduces to `(Y, X, H)` after picking the channel/routing
  dim and frame (most B&H FLIM images are single-frame; multiple blocks =
  channels/routing). Map block axes robustly (find the time axis by matching
  `len(times)`; the other two large axes are Y,X).
- Period/rep-rate: from `measure_info` if present; else `period_ns = n_bins *
  resolution_ns` (full TAC window) with a provenance note that it's the TAC
  window, not a measured laser period. Resolution fallback if `times` absent:
  `tac_r / tac_g / adc_re`.
- `record_type = "Becker & Hickl SDT"`; `n_channels` = block/routing count;
  `n_photons` = total counts.

### Validation & tests (new file `test_loader_sdt.py`, to avoid colliding with M1)
- **Always-on synthetic mapping test:** construct arrays matching the sdtfile
  data/measure_info shape contract and assert `load_sdt`'s field-mapping math
  (resolution, axis identification, channel pick, photon total) — no real file.
- **Real-`.sdt` test, skip-when-absent:** mirror the ptu data-skip pattern —
  if a sample `.sdt` is present (env var `CHRONOGATE_SDT_SAMPLE` or a known test
  path), assert parse fidelity (positive resolution, sane decay window, photon
  total matches raw). CI must **not** require network or bundled data.
- The subagent may fetch one permissive public `.sdt` to validate locally, but
  must **not** commit sample data unless the license clearly permits; otherwise
  leave the real test skip-when-absent. Log what it validated against.

Subagent edits: `loader.py`, `__main__.py`, `chronogate/ui/controller.py`,
`pyproject.toml` (add `sdtfile`), new `test_loader_sdt.py`. It must **not** touch
`test_gating.py` (M1 owns it) or `gating.py` (M3/M4).

---

## M3 — RLD uncertainty + Monte-Carlo (point 3)

**Analytic σ_τ.** For two-gate RLD `τ = dt / ln(na/nb)` with Poisson counts,
error propagation gives
```
σ_τ = (τ² / dt) · sqrt(1/na + 1/nb)
```
(from `∂τ/∂na = −dt/(L²·na)`, `∂τ/∂nb = dt/(L²·nb)`, `L = ln(na/nb)`,
`Var(na)=na`, `Var(nb)=nb`). New `rld_lifetime_sigma(na, nb, dt_ns, tau=None,
min_counts=0.0)` in `gating.py`, NaN where τ is NaN. Extend
`GatingModel.rapid_lifetime(...)` to also return `sigma_tau` (Y,X) and a scalar
region σ (computed on summed-region na,nb). Caveat recorded in the docstring:
background subtraction's effect on count variance is neglected (shot noise on the
raw gated counts).

**Monte-Carlo validation** (test only, seeded `np.random.default_rng(0)`): for
several `(na, nb, dt)` draw Poisson replicates, compute empirical std of τ, and
assert it matches the analytic σ within ~5–10%. This is the evidence the formula
is right.

**Surface ±:** region τ ± σ appears in the one-page report summary lines, the
pixel-table export, and the UI metric readout. σ_τ map available in export.

Solo work (touches `gating.py`, report/pixel-table in `export.py`/`controller.py`,
and `test_gating.py`) — done after M1/M2 land to avoid file collisions.

---

## M4 — IRF reconvolution, per-pixel τ-map (point 2)

New module `chronogate/reconv.py` (keep `gating.py` lean). Adds `scipy`.

### IRF source
`IRF` object with `.kernel(n_bins, resolution_ns) -> np.ndarray` (unit-area):
- `IRF.from_decay(hist, resolution_ns)` — measured: load an IRF `.ptu/.sdt` via
  `load_flim`, reduce to a 1-D histogram (sum over pixels or a picked point),
  resample onto the data's microtime axis if `resolution_ns`/`n_bins` differ,
  normalize to unit area.
- `IRF.gaussian(center_ns, fwhm_ns, ...)` — parametric fallback when no measured
  IRF; center/width may be fixed or fitted as free parameters.

### Model
`model(t) = shift(IRF) ⊛ Σᵢ aᵢ·exp(−t/τᵢ) + offset`, i = 1..k, k ∈ {1, 2}.
Convolution is **periodic** over the laser period (bins), so incomplete decay /
wrap-around at short periods is handled correctly (FFT-based; cache the IRF FFT).
Free params: `{aᵢ, τᵢ}`, `offset` (flat background), and a sub-bin timing
`shift` (color/timing offset between IRF and decay).

### Estimator
`scipy.optimize.least_squares`. **Default: Poisson-MLE deviance residuals**
(`sign(y−μ)·sqrt(2(μ − y + y·ln(y/μ)))`, the right objective for low-count
TCSPC); **χ²-weighted LSQ** (`(y−μ)/sqrt(max(μ,1))`) available as an option.
Report **reduced χ²**, residuals, and per-parameter σ from the fit covariance
(`JᵀJ` inverse × residual variance). Bound τ > 0, fractions in [0,1].

### Per-pixel map (performance crux)
- **Photon-threshold mask:** only fit pixels with total ≥ N photons; others NaN.
- Optional spatial binning before fitting.
- Per-pixel **seed** from RLD τ (and phasor) for fast, robust convergence.
- **Chunked multiprocessing** (`concurrent.futures.ProcessPoolExecutor` over
  pixel chunks) with a **progress + cancel** callback (reuse the loader's
  `progress(done,total)` pattern). Cache the IRF FFT across pixels.
- Returns `tau_map` (+ `tau2_map`/`fraction_map` for bi-exp), `chi2_map`,
  `sigma_map`, and the aggregate region fit (params + residuals + reduced χ²).

### UI
New action **"IRF lifetime fit…"** → dialog: IRF source (browse measured file /
Gaussian width), model (mono/bi), estimator (MLE/χ²), photon threshold, spatial
bin, **region fit vs full-map**, progress + cancel. Region fit shows params +
reduced χ² + a residuals panel; the τ-map registers as a viewable metric layer
alongside RLD τ and phasor. Export/report include the reconv τ-map (+σ, χ²), fit
params, and residuals, and **label every τ output by method + assumptions**
(this carries the original point-2 honesty caveat: RLD/phasor stay
fit-free/uncalibrated; reconv τ is IRF-deconvolved).

### Validation (scientific-critical — adversarially verified)
- Synthetic `IRF ⊛ known mono/bi-exp + Poisson noise` → fit recovers τ:
  near-exact with no noise, within σ with noise.
- Gaussian-IRF round-trip: recover injected center/width.
- **Cross-engine agreement:** on shared synthetic data, reconv τ ≈ RLD τ ≈
  phasor τ within their uncertainties (ties all three lifetime engines).
- Reduced χ² ≈ 1 on correctly-modeled noisy synthetic data.
- Unit discipline: bins vs ns handled explicitly at every boundary.

Adversarial verification: after implementation, independent skeptic passes
attempt to **refute** the reconvolution math (periodicity, MLE objective, σ from
covariance, IRF normalization/resampling, unit handling) before it is accepted.

---

## Cross-cutting risks / landmines
- **scipy in PyInstaller:** hidden imports + bundle-size jump; verify the frozen
  build and all 6 CI jobs after M4.
- **Per-pixel perf:** without the photon threshold + chunking + cancel, a 512²
  many-bin fit is unusable — this is a correctness-of-UX requirement, not polish.
- **Periodic reconvolution:** incomplete-decay wrap-around must be modeled or
  short-period τ is biased.
- **`.sdt` fields are B&H-software-version dependent:** validate on a real file;
  ship synthetic mapping test + skip-when-absent real test.
- **Public `.sdt` licensing:** do not commit sample data without a clear licence.
- **IRF axis mismatch:** resample the IRF onto the data axis.
- **Determinism:** all MC / noisy-fit tests seeded; small synthetic images so CI
  stays fast.

## Definition of done
All milestones: full suite green on the local venv; version bumped; each
milestone committed (SharpClaw007 identity, no AI trailer); `PLAN.md` updated.
Final: offscreen UI smoke + import/bug sweep across the app; adversarial
verification of M4's science recorded.
