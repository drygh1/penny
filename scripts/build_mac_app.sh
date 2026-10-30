#!/bin/bash
# Build Penny.app and Penny.dmg in dist/, with Python and everything Penny needs inside.
# Friends drag Penny.app to Applications and open it; nothing else to install.
#
# The app is built for this Mac's chip (Apple Silicon or Intel). It isn't signed
# with an Apple Developer ID, so the first open needs System Settings → Privacy &
# Security → Open Anyway.
set -e
cd "$(dirname "$0")/.."
VERSION="${PENNY_VERSION:-1.0}"
BUILD=build/mac
APP=dist/Penny.app

if [ ! -x .venv-build/bin/pyinstaller ]; then
  uv venv .venv-build --python 3.13
fi
uv pip install --python .venv-build -q -r requirements-mac.txt

# Icon: assets/icon.png in the sizes macOS wants.
rm -rf "$BUILD" dist
ICONSET="$BUILD/Penny.iconset"
mkdir -p "$ICONSET"
for size in 16 32 128 256 512; do
  sips -z $size $size assets/icon.png --out "$ICONSET/icon_${size}x${size}.png" >/dev/null
  sips -z $((size * 2)) $((size * 2)) assets/icon.png --out "$ICONSET/icon_${size}x${size}@2x.png" >/dev/null
done
iconutil -c icns "$ICONSET" -o "$BUILD/Penny.icns"

# uvicorn finds its pieces by name at runtime, so PyInstaller needs to be told
# about them. Its optional speedups aren't used (see app/desktop.py), so leave them out.
.venv-build/bin/pyinstaller penny_app.py \
  --noconfirm --windowed --name Penny \
  --icon "$PWD/$BUILD/Penny.icns" \
  --osx-bundle-identifier local.penny.app \
  --add-data "$PWD/static:static" \
  --collect-submodules uvicorn \
  --hidden-import webview.platforms.cocoa \
  --exclude-module uvloop --exclude-module httptools --exclude-module watchfiles \
  --exclude-module websockets --exclude-module pytest \
  --distpath dist --workpath "$BUILD/work" --specpath "$BUILD"

PLIST="$APP/Contents/Info.plist"
plutil -replace CFBundleShortVersionString -string "$VERSION" "$PLIST"
plutil -replace CFBundleVersion -string "$VERSION" "$PLIST"
plutil -replace CFBundleDisplayName -string "Penny" "$PLIST"
plutil -replace NSHighResolutionCapable -bool true "$PLIST"
plutil -replace LSMinimumSystemVersion -string "12.3" "$PLIST"  # <dialog> needs Safari 15.4

# Changing Info.plist breaks PyInstaller's signature; sign again (ad hoc, no
# Developer ID), which Apple Silicon Macs need before they'll run the app at all.
codesign --force --deep --sign - "$APP"

# A disk image with an Applications shortcut to drag Penny onto.
STAGE="$BUILD/dmg"
mkdir -p "$STAGE"
cp -R "$APP" "$STAGE/"
ln -s /Applications "$STAGE/Applications"
hdiutil create -quiet -volname Penny -srcfolder "$STAGE" -ov -format UDZO "dist/Penny-$VERSION.dmg"

echo "Built $APP and dist/Penny-$VERSION.dmg ($(uname -m))"
