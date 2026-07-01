# ChronoGate

**An interactive time-gating viewer for FLIM photon data.**

ChronoGate loads a PicoQuant `.ptu` file (the raw, photon-by-photon TTTR stream
written by SymPhoTime), reconstructs a per-pixel histogram of photon *arrival
delays*, and lets you **drag a time "gate" across the fluorescence decay while
watching the gated intensity image update live**. The time axis is calibrated in
real nanoseconds from the file header, so every gate edge is physically
meaningful.

It is deliberately focused: a fast, defensible *gating* tool, not a fitting
suite. For exponential decay fitting, phasor analysis, FRET, anisotropy, or
batch/global fitting, use [FLIMfit](https://flimfit.org) (which ChronoGate was
studied against, conceptually, but shares no code with — see *Licensing*).

---

## A 60-second FLIM primer

A pulsed laser fires repeatedly. For every detected photon, the hardware records
*when* it arrived relative to the most recent pulse — the **microtime**. Photons
from short-lived molecular states arrive soon after the pulse; long-lived states
arrive later. The per-pixel histogram of microtimes is the **fluorescence
decay**.

**Time gating** = keep only the photons whose microtime falls in a chosen window
and sum them per pixel to make an image. Sliding that window changes which
lifetime population you emphasise — a quick, fit-free way to get lifetime
*contrast*. ChronoGate makes that window a draggable span and renders the image
in real time.

---

## What it does (v1)

- **Loads `.ptu`** from a CLI argument, a folder, or a file dialog (defaults to
  `3_FLIM_stack_ptu/`). The **record type is read from the file**, never assumed.
- **Builds an X×Y×T cube** (per-pixel microtime histogram), with **channel
  selection** and **frame handling** (sum all frames, or pick one). Frames are
  decoded **one at a time** and accumulated, so long time-series (hundreds of
  frames) load without materializing the whole `(T,Y,X,C,H)` array — a progress
  bar shows during a big decode.
- **Two linked panels:**
  - *Left* — the decay curve vs microtime (ns), with a **draggable, resizable
    gate** (also settable by typing exact ns values), a **log/linear** y-toggle,
    a dashed **t0** (pulse) marker, and a dotted **noise-floor** line. The gate's
    **shaded highlight is the area between the curve and the noise floor** — i.e.
    exactly the quantity being integrated into the gated image.
  - *Right* — the **gated intensity image**, redrawn live as you drag, with a
    colorbar. The title shows the gate bounds (ns, absolute and relative to t0)
    and the total photons inside the gate.
- **Rapid-lifetime (two-gate RLD) mode:** switch *View* to **lifetime** and a
  second gate (B, green) joins the first (A, orange). The right panel becomes a
  per-pixel **apparent-lifetime map** in nanoseconds — computed fit-free from the
  ratio of the two gated sums by **Rapid Lifetime Determination**,
  τ = Δt / ln(N_A / N_B) (see *Rapid lifetime* below). One `SpanSelector` edits
  whichever gate the **Edit gate: A / B** radio selects (the other shows as a
  static band); both are also typeable in the ns boxes. Background is removed
  per gate before the ratio, dim/non-decaying pixels are masked (honest map), and
  the map exports just like the intensity image (a float **τ TIFF**, a colormapped
  PNG with an ns colorbar, and provenance recording both gates).
- **Instant dragging** via a precomputed **prefix sum** along the microtime axis:
  each gate update is O(number of pixels), independent of gate width.
- **Per-pixel & ROI decays:** **click** any pixel or **drag a box** on the image
  to overlay that pixel/region's decay on the left panel. Single-pixel decays are
  photon-starved, so the display is **smoothed** (a `smooth` time-bin window) and
  can be spatially averaged (an `avg` *N×N* box) for a clean curve — both
  display-only (gating/export use the raw counts). Picks accumulate as a
  colour-coded list (each labelled with its photons-in-gate); **Clear picks** resets.
- **Spatial binning (pool photons):** a **bin** factor sums each pixel's *B×B*
  neighborhood (sliding, so the image keeps its size and coordinates), giving
  ≈B²× more photons per pixel — the standard fix for photon-starved single-pixel
  decays in point-scanning FLIM. **Auto** suggests B from the photon statistics:
  precision scales as ~1/√N, so it sizes B so a representative signal pixel
  reaches a **target** photon count (default 100 ≈ 10%). Reported in the status
  line, e.g. *"median signal pixel ≈ 35 photons → 2×2 (≈140/px)."*
- **Honest images:**
  - a **dim-pixel intensity threshold** (mask pixels whose *total* photons are low), and
  - an adjustable **noise floor** — a background level (counts/bin), drawn as a
    line on the decay and subtracted (× gate width) from each pixel's gated
    intensity, clamped at 0. It defaults to the auto pre-pulse estimate and is
    on by default; toggle **subtract floor** off for raw counts.
- **Layer / file selection:** **Open .ptu file…** loads any file (re-detecting
  its numbered stack); for a numbered series (`..._z1.ptu` … `..._z65.ptu`) a
  **z-slice** slider steps through the planes. The current file/layer is shown
  at the top.
- **Reproducible export** (one click): a raw-value **16-bit TIFF**, a colormapped
  **PNG with colorbar**, a **decay CSV**, and a **provenance JSON** logging the
  source file, header parameters, and the exact gate/threshold/noise-floor/channel used.
- **Save / load settings** so a figure can be regenerated identically.

### IRF (instrument response) support
Load an **IRF** (a point-mode `.ptu`, read as its microtime histogram) to separate
the instrument's prompt response from the sample fluorescence:

- **Rigorous t0** — taken from the IRF peak (replacing the decay-peak heuristic),
  and the IRF is overlaid on the decay with its **instrument window** (the prompt
  support) shaded.
- **Gating split (robust, default)** — the main gate auto-places *after* the
  instrument window, so the image shows true **sample** fluorescence; a *View*
  toggle shows the **instrument** image (photons inside the prompt window). No
  fitting — just time-gating.
- **Approximate scatter subtraction (optional)** — a *subtract scatter* toggle
  removes an IRF-shaped, per-pixel fraction (the **scatter %** slider) of the
  prompt-window signal. It is deliberately a *tunable operation*, not an automatic
  scatter/fluorescence split: the two genuinely overlap at the prompt, so a clean
  separation needs deconvolution (below).

### Deliberately out of scope (use FLIMfit)
Exponential/global lifetime fitting and **IRF reconvolution/deconvolution**,
phasor analysis, FRET, fluorescence anisotropy, reference-lifetime calibration,
and OMERO/plate batch processing.

### Rapid lifetime (two-gate RLD)
For a mono-exponential decay `D(t) = D0·exp(−t/τ)`, the photons in a gate of
width *G* starting at *a* are `N = D0·τ·exp(−a/τ)·(1 − exp(−G/τ))`. For two gates
of **equal width** whose starts differ by `Δt`, the width and amplitude factors
cancel in the ratio, leaving a closed form — no fitting, one division per pixel:

```
N_early / N_late = exp(Δt / τ)      ⟹      τ = Δt / ln(N_early / N_late)
```

This is the classic Ballew–Demas estimator. ChronoGate computes it per pixel over
the prefix-summed cube, so the lifetime map updates as fast as the intensity
image. Caveats it makes honest rather than hides:

- **Apparent, not fitted.** A two-gate ratio is a fast lifetime *estimate*; it is
  exact only for a single-exponential tail. For multi-exponential decays, IRF
  reconvolution, or rigorous τ, use FLIMfit.
- **Equal width matters.** The closed form assumes the two gates share a width;
  if they don't, the title flags it (the result is then approximate).
- **Photon-limited.** Single pixels in point-scanning FLIM are often too starved
  for a stable ratio, so pixels below a per-gate **min cts** floor (or the dim
  threshold), or where the decay doesn't actually fall (`N_early ≤ N_late`), are
  left blank. Use **binning** (the *Auto* button) to pool photons first — the map
  fills in as the counts rise.

---

## Setup

Requires **Python 3.9+**. Nothing is assumed to be installed globally — use a
virtual environment:

```bash
cd ChronoGate
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install --upgrade pip
pip install -r requirements.txt
```

All dependencies are permissively licensed (BSD / MIT / PSF / LGPL): `ptufile`,
`numpy`, `matplotlib`, `tifffile`, and **`PySide6`** (the Qt UI). PySide6 ships
`abi3` wheels, so it installs on current CPython (including 3.14); it is a large
download, so the first install takes a little longer. File dialogs are Qt's, so
`tkinter` is **not** needed.

---

## Run

```bash
# Just launch — opens to a welcome screen; pick a file or folder there:
python -m chronogate
# or:  python run.py

# A specific file:
python -m chronogate "3_FLIM_stack_ptu/stack.sptw/FLIM_stack_z30.ptu"

# A folder — loads the whole .ptu z-stack it finds (step planes in-app):
python -m chronogate "3_FLIM_stack_ptu"

# Pick channel / a single frame / preload settings:
python -m chronogate file.ptu --channel 0 --pick-frame 0 --settings my_gate.json

# Open straight into two-gate rapid-lifetime (RLD) mode:
python -m chronogate file.ptu --lifetime
```

ChronoGate is a native desktop app (PySide6/Qt) with the two plots embedded as
matplotlib canvases. The two plots sit across the **top** (a wide landscape decay
and a near-square image), with all the **controls in a rack along the bottom** —
every control visible at once, and a draggable divider to trade height between
the plots and the rack. A **menubar**, a **toolbar** (Open · Open folder · Load
IRF · Export · Intensity / Lifetime · Log Y), a **status bar**, and keyboard
shortcuts wrap the analysis.

**Opening data:** launching with no argument shows a **welcome screen** with
*Open .ptu file…* and *Open folder (stack)…* — no file is required up front. Open
a single `.ptu`, or open a **folder** and ChronoGate loads the numbered z-stack it
contains (`…_z1.ptu … _z65.ptu`); step through the planes with the **z-slice**
slider or `PgUp`/`PgDn`. File ▸ Open and the toolbar do the same from inside.

Drag inside the **decay** panel to set the gate; drag its **edges** to resize or
its **body** to move it. Or set the gate **exactly** with the **start / end** ns
spin boxes — the boxes and the draggable span stay in sync both ways. The image
panel and the readouts update live. Switch the toolbar to **Lifetime** for the
two-gate RLD map; the **Edit gate: A / B** radio (and the same ns boxes) then
targets whichever gate you want to move.

**Shortcuts:** `Ctrl+O` open · `Ctrl+E` export · `Ctrl+S/L` save/load settings ·
`I`/`T` intensity/lifetime · `L` log-Y · `F` subtract floor · `C` clear picks ·
`←`/`→` move the active gate, `Shift+←/→` resize it · `PgUp`/`PgDn` step z-slice.

### One-click launcher (macOS)

**`ChronoGate.app`** is the double-clickable app in the project root. It always
runs the code in the folder it lives in (it locates the project from its own
bundle path), so it can't launch a stale copy — keep it next to
`ChronoGate.command`. The very first launch opens a Terminal to show the one-time
environment setup; after that it launches silently. Under the hood it just calls
`ChronoGate.command`.

`ChronoGate.command` is the underlying script (also double-clickable) and needs
**no prior setup**. On **Apple Silicon it automatically uses a native arm64 Python**
(Homebrew's `/opt/homebrew/bin/python3` by default) instead of an x86-64
interpreter under Rosetta — the Qt GUI is far more stable that way. It uses a
**cached environment keyed to the architecture + `requirements.txt`** (a
`.run-venv-<arch>-<hash>` directory): the first launch installs the dependencies
(the Qt wheel is large, so allow a minute), and every launch afterwards starts
instantly; it rebuilds automatically if the architecture or requirements change,
pruning stale caches. Your data (`3_FLIM_stack_ptu/`, `Samples.sptw/`) and
exports (`chronogate_exports/`) are left untouched.

```bash
./ChronoGate.command                       # welcome screen (or a file dialog)
./ChronoGate.command path/to/file.ptu      # open a specific file
CHRONOGATE_PYTHON=/opt/homebrew/bin/python3 ./ChronoGate.command   # force an interpreter
```

> Running `python -m chronogate` directly also goes native automatically when the
> interpreter is *universal2* (it re-execs itself under `arch -arm64`). A
> single-architecture x86-64 build (e.g. a conda env) can't — it prints a warning
> suggesting a native Python or this launcher.

---

## Test

A correctness check builds the cube from a real example file and verifies that
prefix-sum gating equals a direct per-bin sum (plus parse-fidelity and
ns-calibration sanity checks), and that two-gate RLD recovers a known lifetime
from a synthetic mono-exponential (with the masking and Δt-guard edge cases):

```bash
python test_gating.py      # or: pytest test_gating.py
```

A second, GUI-side check (`test_ui_smoke.py`) builds the real Qt window on the
offscreen platform and drives both render modes, a gate edit, the lifetime
export, and a settings round-trip. It skips automatically if PySide6 is absent:

```bash
python test_ui_smoke.py    # or: pytest test_ui_smoke.py
```

---

## The example data (what these files actually are)

The `3_FLIM_stack_ptu/stack.sptw/` folder holds a 65-plane z-stack as **one
`.ptu` per plane** (`FLIM_stack_z1.ptu` … `z65.ptu`). Read from the headers:

| Property | Value |
|---|---|
| Record type | `PicoHarpT3` (read from file) |
| Image | 512 × 512 px, 1 frame per file |
| Channels | 1 active (hardware max 4) |
| Microtime resolution | ~96.97 ps/bin, 264 bins per laser period |
| Laser period / rep rate | 25.63 ns / 39.01 MHz |
| Photons | ~0.6 M (z1) to ~16.6 M (z30) per plane |

ChronoGate reads all of this from each file at load time — none of it is
hardcoded — so it should work on other PicoQuant imaging `.ptu` files too. If it
meets a record layout it cannot decode, it prints a clear error naming what it
found.

---

## Layout

```
chronogate/
  loader.py        # ptufile wrapper -> FlimCube (per-frame decode) + Irf + z-stack
  gating.py        # prefix sum, O(1) gating, ns/t0/baseline maths, two-gate RLD, IRF
  export.py        # 16-bit/float TIFF + colormapped PNG + decay CSV + provenance JSON
  __main__.py      # CLI entry (python -m chronogate)
  ui/              # the PySide6 desktop app (all Qt code lives here)
    app.py         #   QApplication bootstrap (theme + window + event loop)
    main_window.py #   QMainWindow: menubar, toolbar, splitter, docks, status bar
    controller.py  #   model + matplotlib artists + all gating/lifetime/refresh logic
    panels.py      #   the native-Qt control panels (Gate, Display, Lifetime, …)
    plot_canvas.py #   embedded matplotlib canvases (decay + image)
    theme.py       #   light clinical QSS + matching matplotlib rcParams
    icon.py        #   app/window logomark (assets/chronogate.svg)
run.py             # convenience launcher
test_gating.py     # prefix-sum / RLD correctness (GUI-free)
test_ui_smoke.py   # headless Qt-window smoke test
```

The analysis core (`loader`, `gating`, `export`) is GUI-free and reused as-is;
only the UI lives under `ui/`, so the tests and the maths never import Qt.

---

## Licensing

ChronoGate is released under the **MIT License** (see `LICENSE`). It is original
work. The FLIMfit project (GPL-2.0) was used **only as a conceptual reference**
for FLIM workflow conventions; **no FLIMfit source code was copied or ported**,
and ChronoGate depends only on permissively licensed packages.

The UI is **PySide6** (Qt for Python), under the **LGPL-3** — chosen over
PyQt/GPL specifically to keep ChronoGate's distribution permissive. The plots are
matplotlib canvases embedded in the Qt window, so on-screen and exported figures
stay identical.

> Performance note: gating is already O(pixels) via the prefix sum, so live
> dragging stays smooth. If very large images ever feel sluggish, the two plot
> canvases could move to `pyqtgraph` (MIT) without touching the analysis core.
