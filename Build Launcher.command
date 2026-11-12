#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")"
project="$PWD"
staging="$(mktemp -d "$project/.launcher-build.XXXXXX")"
trap 'rm -rf -- "$staging"' EXIT
app="$staging/Couch Remote.app"
mkdir -p "$app/Contents/MacOS"
cat > "$app/Contents/Info.plist" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleExecutable</key><string>CouchRemote</string>
    <key>CFBundleIdentifier</key><string>local.couch-remote.launcher</string>
    <key>CFBundleName</key><string>Couch Remote</string>
    <key>CFBundlePackageType</key><string>APPL</string>
    <key>CFBundleVersion</key><string>1</string>
    <key>CFBundleShortVersionString</key><string>1.0</string>
    <key>LSMinimumSystemVersion</key><string>13.0</string>
    <key>NSLocalNetworkUsageDescription</key>
    <string>Connect your paired phone to Couch Remote on this Mac.</string>
</dict>
</plist>
PLIST
/usr/bin/swiftc -target "$(/usr/bin/uname -m)-apple-macos13.0" -swift-version 5 -parse-as-library \
    -O Launcher.swift -o "$app/Contents/MacOS/CouchRemote"
/usr/bin/codesign --force --sign - "$app"
if [[ -e 'Couch Remote.app' ]]; then
    mv 'Couch Remote.app' "$staging/previous.app"
fi
if ! mv "$app" 'Couch Remote.app'; then
    if [[ -d "$staging/previous.app" ]]; then
        mv "$staging/previous.app" 'Couch Remote.app'
    fi
    exit 1
fi
printf 'Built %s/Couch Remote.app\n' "$project"
