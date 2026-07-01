"""The PySide6 (Qt) user interface for ChronoGate.

All Qt-dependent code lives in this subpackage. Importing it (or, more usefully,
calling :func:`launch`) is the only place PySide6 is required -- the analysis
modules (`loader`, `gating`, `export`) and the test suite stay Qt-free.
"""

from __future__ import annotations

from .app import launch

__all__ = ["launch"]
