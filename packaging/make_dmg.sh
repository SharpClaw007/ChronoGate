#!/bin/bash
# Build a drag-to-Applications .dmg from the PyInstaller macOS bundle.
# Dependency-free (hdiutil ships with macOS). Run after:
#   pyinstaller packaging/chronogate.spec --noconfirm
#
#   packaging/make_dmg.sh [VERSION] [ARCH]
#
# Writes dist/ChronoGate-<version>.dmg, or dist/ChronoGate-<version>-<arch>.dmg
# when ARCH is given (e.g. arm64, x86_64) so Apple-Silicon and Intel builds do
# not collide as CI artifacts.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$HERE"

VERSION="${1:-0.0.0}"
ARCH="${2:-}"
APP="dist/ChronoGate.app"
if [ -n "$ARCH" ]; then
  DMG="dist/ChronoGate-${VERSION}-${ARCH}.dmg"
else
  DMG="dist/ChronoGate-${VERSION}.dmg"
fi

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
