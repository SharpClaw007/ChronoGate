#!/bin/bash
# Build a drag-to-Applications .dmg from the PyInstaller macOS bundle.
# Dependency-free (hdiutil ships with macOS). Run after:
#   pyinstaller packaging/chronogate.spec --noconfirm
#
#   packaging/make_dmg.sh [VERSION]
#
# Writes dist/ChronoGate-<version>.dmg
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$HERE"

VERSION="${1:-0.0.0}"
APP="dist/ChronoGate.app"
DMG="dist/ChronoGate-${VERSION}.dmg"

[ -d "$APP" ] || { echo "error: $APP not found (run PyInstaller first)" >&2; exit 1; }

# Stage the .app beside an /Applications symlink so the DMG window offers the
# familiar drag-to-install gesture.
STAGE="$(mktemp -d)"
cp -R "$APP" "$STAGE/"
ln -s /Applications "$STAGE/Applications"

rm -f "$DMG"
hdiutil create -volname "ChronoGate" -srcfolder "$STAGE" -ov -format UDZO "$DMG"
rm -rf "$STAGE"
echo "Built $DMG"
