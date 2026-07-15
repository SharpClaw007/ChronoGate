"""ChronoGate -- an interactive time-gating viewer for FLIM photon data.

ChronoGate loads a PicoQuant ``.ptu`` TTTR file (the raw, photon-by-photon
stream written by SymPhoTime), reconstructs a per-pixel histogram of photon
*arrival delays*, and lets you drag a time "gate" across the fluorescence
decay while watching the gated intensity image update live.

A 60-second FLIM primer (everything this tool relies on):

* A pulsed laser fires repeatedly. For every detected photon the hardware
  records *when* it arrived relative to the most recent laser pulse -- this
  delay is called the **microtime**. Photons from short-lived states arrive
  soon after the pulse; long-lived states arrive later. The histogram of
  microtimes, per pixel, is the **fluorescence decay**.
* "Time gating" simply means: keep only the photons whose microtime falls in
  a chosen window, and sum them per pixel to form an image. Sliding that
  window changes which lifetime population you emphasise -- a fast, fit-free
  way to get lifetime *contrast*.
* The microtime axis is calibrated in real time (nanoseconds) using the TCSPC
  bin width stored in the file header, so gate edges are physically meaningful.

This package is original work, released under the MIT licence. It uses the
FLIMfit project only as a *conceptual* reference for workflow conventions; no
FLIMfit (GPL) source code was copied or ported.
"""

__version__ = "0.12.0"
# Note: the Qt UI lives in the `ui` subpackage and is imported lazily (only when
# the app launches), so importing this package stays dependency-light.
__all__ = ["loader", "gating", "export", "ui"]
