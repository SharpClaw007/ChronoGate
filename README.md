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
  selection** and **frame handling** (sum all frames, or pick one).
- **Two linked panels:**
  - *Left* — the decay curve vs microtime (ns), with a **draggable, resizable
    gate** (also settable by typing exact ns values), a **log/linear** y-toggle,
    a dashed **t0** (pulse) marker, and a dotted **noise-floor** line. The gate's
    **shaded highlight is the area between the curve and the noise floor** — i.e.
    exactly the quantity being integrated into the gated image.
  - *Right* — the **gated intensity image**, redrawn live as you drag, with a
    colorbar. The title shows the gate bounds (ns, absolute and relative to t0)
    and the total photons inside the gate.
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

### Deliberately out of scope (use FLIMfit)
Exponential/global lifetime fitting and IRF reconvolution, phasor analysis, FRET,
fluorescence anisotropy, reference-lifetime calibration, and OMERO/plate batch
processing. *Planned next:* rapid-lifetime (two-gate ratio) and an IRF overlay.

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

The file-open **dialog** uses `tkinter`, which ships with Python. On Homebrew
Python you may need `brew install python-tk`. It is **optional** — pass a file
path on the command line and tkinter is never touched.

All dependencies are permissively licensed (BSD / MIT / PSF): `ptufile`, `numpy`,
`matplotlib`, `tifffile`.

---

## Run

```bash
# File dialog (defaults to the 3_FLIM_stack_ptu folder):
python -m chronogate
# or:  python run.py

# A specific file:
python -m chronogate "3_FLIM_stack_ptu/stack.sptw/FLIM_stack_z30.ptu"

# A folder (opens the first .ptu found):
python -m chronogate "3_FLIM_stack_ptu"

# Pick channel / a single frame / preload settings:
python -m chronogate file.ptu --channel 0 --pick-frame 0 --settings my_gate.json
```

Drag inside the **left** panel to set the gate; drag its **edges** to resize or
its **body** to move it. Or set the gate **exactly** by typing nanosecond values
into the **start / end** boxes and pressing Enter — the boxes and the draggable
span stay in sync both ways. The right panel and the readouts update live.

### One-click launcher (macOS)

`ChronoGate.command` is a double-clickable launcher that needs **no prior
setup**. On open it builds a fresh, throwaway environment (`.run-venv`) and
installs the dependencies; when you close the window it tears that environment
back down (and clears Python caches). Your data (`3_FLIM_stack_ptu/`) and
exports (`chronogate_exports/`) are always left untouched, as is any persistent
`.venv` you created for development — the launcher uses its own directory.

```bash
./ChronoGate.command                       # file dialog
./ChronoGate.command path/to/file.ptu      # open a specific file
CHRONOGATE_PYTHON=/opt/homebrew/bin/python3 ./ChronoGate.command   # choose interpreter
```

Because it rebuilds every launch, the environment always reflects the current
code and `requirements.txt` (the trade-off is ~10–60 s of dependency install per
launch; pip's download cache keeps repeat builds quick).

---

## Test

A correctness check builds the cube from a real example file and verifies that
prefix-sum gating equals a direct per-bin sum (plus parse-fidelity and
ns-calibration sanity checks):

```bash
python test_gating.py      # or: pytest test_gating.py
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
  loader.py    # ptufile wrapper -> FlimCube (+ z-stack discovery)
  gating.py    # prefix sum, O(1) gating, ns/t0/baseline/threshold maths
  viewer.py    # the interactive matplotlib window
  export.py    # 16-bit TIFF + colormapped PNG + decay CSV + provenance JSON
  __main__.py  # CLI entry (python -m chronogate)
run.py         # convenience launcher
test_gating.py # prefix-sum-vs-direct-sum correctness test
```

---

## Licensing

ChronoGate is released under the **MIT License** (see `LICENSE`). It is original
work. The FLIMfit project (GPL-2.0) was used **only as a conceptual reference**
for FLIM workflow conventions; **no FLIMfit source code was copied or ported**,
and ChronoGate depends only on permissively licensed packages.

> Performance note: if live dragging ever feels sluggish on very large images,
> the viewer can be reimplemented on `pyqtgraph` (MIT) for smoother updates — at
> the cost of a Qt binding (use PySide6/LGPL, not PyQt/GPL, to keep the license
> permissive).
