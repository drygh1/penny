#!/bin/bash
# Build a universal Penny.app (Apple chips and Intel in one) and a drag-to-install
# .dmg, with Python and everything Penny needs inside. Friends drag Penny.app to
# Applications and open it; nothing else to install, and it doesn't matter which Mac.
#
#   scripts/build_mac_app.sh
#
# Makes dist/Penny-<version>.dmg (the version comes from app/__init__.py).
# Each half is built with the same Python release, so the two apps hold the
# same files; scripts/merge_universal.py fuses their compiled code with lipo.
# On an Apple Silicon Mac the Intel half builds through Rosetta
# (softwareupdate --install-rosetta) with an Intel Python that uv downloads.
#
# The app isn't signed with an Apple Developer ID, so the first open needs
# System Settings → Privacy & Security → Open Anyway.
set -e
cd "$(dirname "$0")/.."
VERSION="$(sed -n 's/^__version__ = "\(.*\)".*/\1/p' app/__init__.py)"
PYTHON=3.13.14  # the same release for both halves, so their files match
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

build_half() {
  local arch="$1" uvarch="$2"
  local venv=".venv-build-$arch"
  local python="cpython-$PYTHON-macos-$uvarch-none"
  # Each half gets its own Python and packages, so every compiled piece matches its chip.
  if [ ! -x "$venv/bin/pyinstaller" ] || ! "$venv/bin/python" -c "import platform,sys; sys.exit(platform.python_version() != '$PYTHON')"; then
    uv python install "$python"
    rm -rf "$venv"
    uv venv -q "$venv" --python "$(uv python find "$python")"
  fi
  uv pip install --python "$venv" -q -r requirements-mac.txt
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
    --distpath "$BUILD/$arch" --workpath "$BUILD/work-$arch" --specpath "$BUILD/spec-$arch" \
    --log-level WARN
}

build_half arm64 aarch64
build_half x86_64 x86_64

APP="$BUILD/universal/Penny.app"
mkdir -p "$BUILD/universal"
python3 scripts/merge_universal.py "$BUILD/arm64/Penny.app" "$BUILD/x86_64/Penny.app" "$APP"

PLIST="$APP/Contents/Info.plist"
plutil -replace CFBundleShortVersionString -string "$VERSION" "$PLIST"
plutil -replace CFBundleVersion -string "$VERSION" "$PLIST"
plutil -replace CFBundleDisplayName -string "Penny" "$PLIST"
plutil -replace NSHighResolutionCapable -bool true "$PLIST"
plutil -replace LSMinimumSystemVersion -string "12.3" "$PLIST"  # <dialog> needs Safari 15.4

# Merging and Info.plist changes break the signatures; sign again (ad hoc, no
# Developer ID), which Apple Silicon Macs need before they'll run the app at all.
codesign --force --deep --sign - "$APP"

# Stop if anything of yours ended up in the app: a database, a log, or your
# account details anywhere in any file (see app/share.py).
if ! .venv-build-arm64/bin/python -m app.share "$APP"; then
  echo "Not building the .dmg: the app has something of yours in it."
  exit 1
fi

# A disk image with an Applications shortcut to drag Penny onto, and the
# same install note the in-app "Share Penny with a friend" copy has.
STAGE="$BUILD/dmg"
mkdir -p "$STAGE"
cp -R "$APP" "$STAGE/"
ln -s /Applications "$STAGE/Applications"
.venv-build-arm64/bin/python -c "from app import share; print(share.install_note(universal=True), end='')" \
  > "$STAGE/How to install Penny.txt"
hdiutil create -quiet -volname Penny -srcfolder "$STAGE" -ov -format UDZO "dist/Penny-$VERSION.dmg"
echo "Built dist/Penny-$VERSION.dmg (works on Apple chip and Intel Macs)"
