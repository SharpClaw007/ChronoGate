#!/bin/bash
#
# ChronoGate launcher (double-clickable on macOS).
# -------------------------------------------------
# Opens a FRESH, self-contained environment, runs the viewer, then tears the
# environment back down when you close the window -- so nothing lingers between
# sessions and every launch reflects the current code/dependencies.
#
#   * On open : builds a throwaway virtual environment in ".run-venv" and
#               installs the dependencies from requirements.txt.
#   * On close: deletes ".run-venv" and Python bytecode caches.
#   * Always kept: your data (3_FLIM_stack_ptu) and your exports
#               (chronogate_exports). A persistent ".venv" (if you made one for
#               development) is left completely alone -- this uses its own dir.
#
# Usage:
#   Double-click in Finder, OR run:  ./ChronoGate.command [optional_file.ptu]
#   With no file argument, ChronoGate opens its file-picker dialog.
#
# Tip: set CHRONOGATE_PYTHON to choose the interpreter, e.g.
#   CHRONOGATE_PYTHON=/opt/homebrew/bin/python3.12 ./ChronoGate.command

set -euo pipefail

# Resolve this script's own folder so it works no matter where it's launched
# from (Finder starts double-clicked scripts with an arbitrary directory).
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$HERE"

VENV="$HERE/.run-venv"          # ephemeral; NOT the persistent dev .venv
PYTHON="${CHRONOGATE_PYTHON:-python3}"

cleanup() {
    echo
    echo "Cleaning up the ChronoGate environment..."
    rm -rf "$VENV"
    # Remove Python bytecode caches created while running.
    find "$HERE/chronogate" -type d -name '__pycache__' -prune -exec rm -rf {} + 2>/dev/null || true
    find "$HERE" -type f -name '*.pyc' -delete 2>/dev/null || true
    echo "Done. Left untouched: 3_FLIM_stack_ptu/ (data) and chronogate_exports/ (your exports)."
}
# Run cleanup on ANY exit: normal window close, Ctrl-C, or a build error.
trap cleanup EXIT

echo "================  ChronoGate  ================"
echo "Building a fresh environment (.run-venv) -- happens every launch..."

# Start from a guaranteed-clean venv (handles a leftover from an unclean exit).
rm -rf "$VENV"
"$PYTHON" -m venv "$VENV"
"$VENV/bin/pip" install --quiet --upgrade pip
"$VENV/bin/pip" install --quiet -r "$HERE/requirements.txt"

echo "Launching ChronoGate. Close the window to exit and clean up."
echo "=============================================="

# Pass through any arguments (e.g. a .ptu path). No args -> file dialog.
"$VENV/bin/python" -m chronogate "$@"
