#!/usr/bin/env python3
"""Convenience launcher equivalent to ``python -m chronogate``.

    python run.py                      # file dialog (defaults to 3_FLIM_stack_ptu)
    python run.py path/to/file.ptu     # open a specific file
    python run.py "3_FLIM_stack_ptu"   # open the first .ptu found in a folder
"""

from chronogate.__main__ import main

if __name__ == "__main__":
    raise SystemExit(main())
