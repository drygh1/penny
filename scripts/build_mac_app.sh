#!/bin/bash
# Build Penny.app and a drag-to-install .dmg, with Python and everything Penny needs inside.
# Friends drag Penny.app to Applications and open it; nothing else to install.
#
#   scripts/build_mac_app.sh              both: Apple Silicon and Intel
#   scripts/build_mac_app.sh arm64        Apple Silicon (M1 and later) only
#   scripts/build_mac_app.sh x86_64       Intel only
#
# Makes dist/Penny-<version>-apple-silicon.dmg and dist/Penny-<version>-intel.dmg.
# The version comes from app/__init__.py. On an Apple Silicon Mac the Intel build
# runs through Rosetta (softwareupdate --install-rosetta) with an Intel Python
# that uv downloads.
#
# The app isn't signed with an Apple Developer ID, so the first open needs
# System Settings → Privacy & Security → Open Anyway.
set -e
cd "$(dirname "$0")/.."
VERSION="$(sed -n 's/^__version__ = "\(.*\)".*/\1/p' app/__init__.py)"
ARCHES="${1:-arm64 x86_64}"
BUILD=build/mac

rm -rf "$BUILD" dist
mkdir -p dist

# Icon: assets/icon.png in the sizes macOS wants.
ICONSET="$BUILD/Penny.iconset"
mkdir -p "$ICONSET"
for size in 16 32 128 256 512; do
  sips -z $size $size assets/icon.png --out "$ICONSET/icon_${size}x${size}.png" >/dev/null
  sips -z $((size * 2)) $((size * 2)) assets/icon.png --out "$ICONSET/icon_${size}x${size}@2x.png" >/dev/null
done
iconutil -c icns "$ICONSET" -o "$BUILD/Penny.icns"

build_one() {
  local arch="$1" label venv out app
  case "$arch" in
    arm64) label=apple-silicon ;;
    x86_64) label=intel ;;
    *) echo "Unknown architecture: $arch (use arm64 or x86_64)"; exit 1 ;;
  esac
  # Each chip gets its own Python and packages, so every compiled piece matches it.
  venv=".venv-build-$arch"
  if [ ! -x "$venv/bin/pyinstaller" ]; then
    uv python install "cpython-3.13-macos-$arch-none"
    uv venv "$venv" --python "$(uv python find "cpython-3.13-macos-$arch-none")"
  fi
  uv pip install --python "$venv" -q -r requirements-mac.txt

  out="dist/$label"
  app="$out/Penny.app"
  # uvicorn finds its pieces by name at runtime, so PyInstaller needs to be told
  # about them. Its optional speedups aren't used (see app/desktop.py), so leave them out.
  arch "-$arch" "$venv/bin/pyinstaller" penny_app.py \
    --noconfirm --windowed --name Penny \
    --icon "$PWD/$BUILD/Penny.icns" \
    --osx-bundle-identifier local.penny.app \
    --target-architecture "$arch" \
    --add-data "$PWD/static:static" \
    --collect-submodules uvicorn \
    --hidden-import webview.platforms.cocoa \
    --exclude-module uvloop --exclude-module httptools --exclude-module watchfiles \
    --exclude-module websockets --exclude-module pytest \
    --distpath "$out" --workpath "$BUILD/work-$arch" --specpath "$BUILD/spec-$arch" \
    --log-level WARN

  local plist="$app/Contents/Info.plist"
  plutil -replace CFBundleShortVersionString -string "$VERSION" "$plist"
  plutil -replace CFBundleVersion -string "$VERSION" "$plist"
  plutil -replace CFBundleDisplayName -string "Penny" "$plist"
  plutil -replace NSHighResolutionCapable -bool true "$plist"
  plutil -replace LSMinimumSystemVersion -string "12.3" "$plist"  # <dialog> needs Safari 15.4

  # Changing Info.plist breaks PyInstaller's signature; sign again (ad hoc, no
  # Developer ID), which Apple Silicon Macs need before they'll run the app at all.
  codesign --force --deep --sign - "$app"

  # A disk image with an Applications shortcut to drag Penny onto.
  local stage="$BUILD/dmg-$arch"
  mkdir -p "$stage"
  cp -R "$app" "$stage/"
  ln -s /Applications "$stage/Applications"
  hdiutil create -quiet -volname Penny -srcfolder "$stage" -ov -format UDZO "dist/Penny-$VERSION-$label.dmg"
  echo "Built dist/Penny-$VERSION-$label.dmg"
}

for arch in $ARCHES; do
  build_one "$arch"
done
