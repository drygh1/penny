#!/bin/bash
# Build Penny.app in ~/Applications so Penny can be opened from Spotlight,
# Launchpad or the Dock. Re-run this if you move the penny folder.
set -e
PROJECT="$(cd "$(dirname "$0")/.." && pwd)"
APP="$HOME/Applications/Penny.app"

rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"

cat > "$APP/Contents/MacOS/Penny" <<SCRIPT
#!/bin/bash
exec "$PROJECT/scripts/launch.sh"
SCRIPT
chmod +x "$APP/Contents/MacOS/Penny"

cat > "$APP/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleName</key><string>Penny</string>
  <key>CFBundleDisplayName</key><string>Penny</string>
  <key>CFBundleIdentifier</key><string>local.penny.launcher</string>
  <key>CFBundleExecutable</key><string>Penny</string>
  <key>CFBundleIconFile</key><string>Penny</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleShortVersionString</key><string>1.0</string>
  <key>LSUIElement</key><true/>
</dict>
</plist>
PLIST

# Turn assets/icon.png into the .icns format macOS uses for app icons.
ICONSET="$(mktemp -d)/Penny.iconset"
mkdir -p "$ICONSET"
for size in 16 32 128 256 512; do
  sips -z $size $size "$PROJECT/assets/icon.png" --out "$ICONSET/icon_${size}x${size}.png" >/dev/null
  sips -z $((size * 2)) $((size * 2)) "$PROJECT/assets/icon.png" --out "$ICONSET/icon_${size}x${size}@2x.png" >/dev/null
done
iconutil -c icns "$ICONSET" -o "$APP/Contents/Resources/Penny.icns"
rm -rf "$(dirname "$ICONSET")"

touch "$APP"  # make Finder pick up the new icon
echo "Created $APP"
