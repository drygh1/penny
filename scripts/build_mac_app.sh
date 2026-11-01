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
  # What a friend sees next to the app before opening anything.
  cat > "$stage/How to install Penny.txt" <<NOTE
How to install Penny
====================

1. Drag Penny onto the Applications folder in this window.
2. Open Penny from Applications (or Launchpad, or Spotlight: Command-Space, type Penny).

If macOS says Penny can't be opened, or that Apple couldn't check it for malware
--------------------------------------------------------------------------------
That's because Penny isn't signed with a paid Apple developer account. It's a one-time step:

  a. Click Done (not Move to Trash).
  b. Open System Settings, then Privacy & Security.
  c. Scroll down to Security and click Open Anyway next to Penny, then confirm.

Penny opens normally from then on.

Is this the right version for your Mac?
---------------------------------------
This copy is for $([ "$arch" = arm64 ] && echo "Macs with Apple chips (M1, M2, M3 and later)" || echo "Macs with Intel processors").
To check yours: Apple menu, then About This Mac. "Chip: Apple M..." means Apple chip;
"Processor: ... Intel" means Intel. If it doesn't match, ask for the other version.

Once Penny is open, it walks you through everything else: trying it with demo data,
connecting your bank, and the rest. There's also a Help button at the top of the window.

Your data stays on your Mac. Penny $VERSION.
NOTE
  hdiutil create -quiet -volname Penny -srcfolder "$stage" -ov -format UDZO "dist/Penny-$VERSION-$label.dmg"
  echo "Built dist/Penny-$VERSION-$label.dmg"
}

for arch in $ARCHES; do
  build_one "$arch"
done
