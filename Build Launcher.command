#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")"
project="$PWD"
staging="$(mktemp -d "$project/.launcher-build.XXXXXX")"
trap 'rm -rf -- "$staging"' EXIT
app="$staging/Lazy Remote.app"
mkdir -p "$app/Contents/MacOS" "$app/Contents/Resources"
iconset="$staging/LazyRemote.iconset"
mkdir "$iconset"
for size in 16 32 128 256 512; do
    retina=$((size * 2))
    /usr/bin/sips -z "$size" "$size" assets/logo.png --out "$iconset/icon_${size}x${size}.png" >/dev/null
    /usr/bin/sips -z "$retina" "$retina" assets/logo.png --out "$iconset/icon_${size}x${size}@2x.png" >/dev/null
done
/usr/bin/iconutil -c icns "$iconset" -o "$app/Contents/Resources/LazyRemote.icns"
cat > "$app/Contents/Info.plist" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleExecutable</key><string>LazyRemote</string>
    <key>CFBundleIdentifier</key><string>local.couch-remote.launcher</string>
    <key>CFBundleName</key><string>Lazy Remote</string>
    <key>CFBundleIconFile</key><string>LazyRemote.icns</string>
    <key>CFBundlePackageType</key><string>APPL</string>
    <key>CFBundleVersion</key><string>1</string>
    <key>CFBundleShortVersionString</key><string>1.0</string>
    <key>LSMinimumSystemVersion</key><string>13.0</string>
    <key>NSLocalNetworkUsageDescription</key>
    <string>Connect your paired phone to Lazy Remote on this Mac.</string>
</dict>
</plist>
PLIST

# Build one binary that runs on both Apple Silicon and Intel Macs.
universal() {
    local output="$1"
    shift
    for arch in arm64 x86_64; do
        # SWIFTC_FLAGS lets a sandboxed shell pass -disable-sandbox; normal builds leave it empty.
        /usr/bin/swiftc ${SWIFTC_FLAGS:-} -target "$arch-apple-macos13.0" -swift-version 5 -O "$@" -o "$staging/$arch.bin"
    done
    /usr/bin/lipo -create "$staging/arm64.bin" "$staging/x86_64.bin" -output "$output"
    rm -f "$staging/arm64.bin" "$staging/x86_64.bin"
}

universal "$app/Contents/MacOS/LazyRemote" -parse-as-library Launcher.swift

# Everything the server needs ships inside the app, so users only download the app.
server="$app/Contents/Resources/server"
mkdir -p "$server/assets"
cp auth.py platform_support.py server.py tls_setup.py index.html remote.js remote.css "$server/"
cp assets/logo.svg assets/touch-icon.png "$server/assets/"
universal "$server/input_helper" input_helper.swift
/usr/bin/codesign --force --sign - --identifier local.couch-remote.input-helper "$server/input_helper"
/usr/bin/codesign --force --sign - "$app"

if [[ -e 'Lazy Remote.app' ]]; then
    mv 'Lazy Remote.app' "$staging/previous.app"
fi
if ! mv "$app" 'Lazy Remote.app'; then
    if [[ -d "$staging/previous.app" ]]; then
        mv "$staging/previous.app" 'Lazy Remote.app'
    fi
    exit 1
fi
printf 'Built %s/Lazy Remote.app\n' "$project"
