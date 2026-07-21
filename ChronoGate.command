#!/bin/bash
#
# ChronoGate launcher (double-clickable on macOS).
# -------------------------------------------------
# On Apple Silicon it runs the app with a NATIVE arm64 Python (Homebrew's by
# default) rather than an x86-64 interpreter under Rosetta -- the Qt GUI is much
# more stable that way. It uses a cached environment keyed to pyproject.toml
# AND the architecture, so the first launch installs the dependencies (the Qt
# wheel is large) and every launch afterwards is instant.
#
# pyproject.toml is the SINGLE source of dependencies (there is no
# requirements.txt). The venv is an editable install (`pip install -e .`), and
# its cache key is a hash of pyproject.toml -- so changing *any* dependency (or
# the pinned python) rebuilds the environment on the next launch automatically.
# This is what prevents a stale cached venv from crashing after a dep is added.
#
#   * Interpreter: a native arm64 python3 on Apple Silicon (auto-detected:
#     /opt/homebrew/bin/python3, then Homebrew versioned, then a universal
#     /usr/bin/python3). Override with CHRONOGATE_PYTHON.
#   * Environment dir: ".run-venv-<arch>-<hash>"; rebuilt if the architecture or
#     any dependency changes, stale ones pruned.
#   * Always kept: your data (3_FLIM_stack_ptu, Samples.sptw) and exports.
#
# Usage:  double-click in Finder, OR:  ./ChronoGate.command [optional_file.ptu]

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$HERE"

# --- On Apple Silicon, make sure THIS SCRIPT runs natively (not under Rosetta). ---
if [ "$(sysctl -n hw.optional.arm64 2>/dev/null || echo 0)" = "1" ]; then
    APPLE_SILICON=1
    if [ "$(sysctl -in sysctl.proc_translated 2>/dev/null || echo 0)" = "1" ]; then
        exec arch -arm64 /bin/bash "$0" "$@"
    fi
else
    APPLE_SILICON=0
fi

echo "================  ChronoGate  ================"

# --- Choose the interpreter (prefer a native arm64 one on Apple Silicon). ---
is_arm64_py() { [ "$("$1" -c 'import platform;print(platform.machine())' 2>/dev/null)" = "arm64" ]; }

PYTHON=""
if [ -n "${CHRONOGATE_PYTHON:-}" ]; then
    PYTHON="$CHRONOGATE_PYTHON"
elif [ "$APPLE_SILICON" = "1" ]; then
    for cand in \
        /opt/homebrew/bin/python3 \
        /opt/homebrew/opt/python@3.13/bin/python3 \
        /opt/homebrew/opt/python@3.12/bin/python3 \
        /opt/homebrew/opt/python@3.11/bin/python3 \
        /usr/local/bin/python3 \
        /usr/bin/python3 ; do
        if [ -x "$cand" ] && is_arm64_py "$cand"; then PYTHON="$cand"; break; fi
    done
    if [ -z "$PYTHON" ]; then
        echo "ERROR: no native arm64 Python 3 found." >&2
        echo "  Install one:   brew install python" >&2
        echo "  or set CHRONOGATE_PYTHON to an arm64 python3." >&2
        exit 1
    fi
else
    PYTHON="python3"
fi
echo "Interpreter: $PYTHON  ($("$PYTHON" -c 'import platform,sys;print(platform.machine(),"· Python",sys.version.split()[0])'))"

# --- Cached environment, keyed to (architecture, dependencies). ---
# Hash pyproject.toml -- the single dependency source -- so any dep change yields
# a new venv name and an automatic rebuild (no stale-environment crashes).
DEP_HASH="$(shasum -a 256 "$HERE/pyproject.toml" | cut -c1-12)"
ARCH_TAG="$("$PYTHON" -c 'import platform;print(platform.machine())')"
VENV="$HERE/.run-venv-$ARCH_TAG-$DEP_HASH"

for d in "$HERE"/.run-venv*; do
    if [ -e "$d" ] && [ "$d" != "$VENV" ]; then
        echo "Removing stale environment: $(basename "$d")"
        rm -rf "$d"
    fi
done

if [ ! -f "$VENV/.installed" ]; then
    echo "First launch for this setup -- building $(basename "$VENV")."
    echo "(PySide6/Qt is a large one-time download; this can take a minute.)"
    rm -rf "$VENV"
    "$PYTHON" -m venv "$VENV"
    "$VENV/bin/pip" install --quiet --upgrade pip
    "$VENV/bin/pip" install -e "$HERE"          # deps come from pyproject.toml
    touch "$VENV/.installed"
else
    echo "Using cached environment: $(basename "$VENV")"
fi

echo "Launching ChronoGate (native $ARCH_TAG). Close the window to exit."
echo "=============================================="

# The venv's python matches $PYTHON's architecture, so this runs natively.
"$VENV/bin/python" -m chronogate "$@"
