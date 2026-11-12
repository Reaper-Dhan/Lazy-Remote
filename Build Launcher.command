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

# A private Python ships inside the app, so a Mac needs no developer tools.
# python-build-standalone, pinned by release and checked against its SHA-256.
python_release=20261110
python_version=3.12.16
python_arm64=2f11b8aa3726bdbd87dd9eb76568412faddf4f47e26913fa45d6a2e424972acd
python_x86_64=b78a4c479f1964ec484880bee567568266f68da102269fb93a05fe8716b45f91
cache="$project/.cache/python"
mkdir -p "$cache"
for arch in arm64 x86_64; do
    triple=$([[ $arch == arm64 ]] && echo aarch64 || echo x86_64)
    archive="cpython-$python_version+$python_release-$triple-apple-darwin-install_only_stripped.tar.gz"
    expected_var="python_$arch"
    if [[ ! -f "$cache/$archive" ]]; then
        /usr/bin/curl -fsSL --retry 3 -o "$cache/$archive.part" \
            "https://github.com/astral-sh/python-build-standalone/releases/download/$python_release/${archive//+/%2B}"
        mv "$cache/$archive.part" "$cache/$archive"
    fi
    if [[ "$(/usr/bin/shasum -a 256 "$cache/$archive" | cut -d' ' -f1)" != "${!expected_var}" ]]; then
        rm -f "$cache/$archive"
        echo "Checksum mismatch for $archive" >&2
        exit 1
    fi
    mkdir -p "$staging/python-$arch"
    /usr/bin/tar -xzf "$cache/$archive" -C "$staging/python-$arch"
done
python="$app/Contents/Resources/python"
mv "$staging/python-arm64/python" "$python"
# Drop what the server never imports: tests, Tk, IDLE, headers and docs.
( cd "$python" && rm -rf include share lib/tcl* lib/tk* lib/itcl* lib/thread* lib/pkgconfig \
    lib/libtcl* lib/libtk* bin/idle* bin/pydoc* bin/2to3* \
    lib/python3.12/{test,idlelib,tkinter,turtledemo,ensurepip,lib2to3,pydoc_data,unittest,config-3.12-darwin} \
    lib/python3.12/lib-dynload/_tkinter* lib/python3.12/site-packages )
# Merge each Intel binary into its Apple Silicon twin, then re-sign it (lipo drops signatures).
while IFS= read -r -d '' file; do
    relative="${file#"$python"/}"
    twin="$staging/python-x86_64/python/$relative"
    if /usr/bin/lipo -info "$file" >/dev/null 2>&1; then
        if [[ -f "$twin" ]] && [[ "$(/usr/bin/lipo -archs "$file")" == arm64 ]]; then
            /usr/bin/lipo -create "$file" "$twin" -output "$file.universal"
            mv "$file.universal" "$file"
        fi
        /usr/bin/codesign --force --sign - "$file" 2>/dev/null
    fi
done < <(find "$python" -type f \( -perm -u+x -o -name '*.so' -o -name '*.dylib' \) -print0)
rm -rf "$staging/python-arm64" "$staging/python-x86_64"
# Precompile now: the app is signed, so nothing may be written into it at run time.
"$python/bin/python3" -m compileall -q -j 0 "$python/lib/python3.12" >/dev/null
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

mkdir -p dist
zip="dist/Lazy-Remote-macOS.zip"
rm -f "$zip"
/usr/bin/ditto -c -k --keepParent 'Lazy Remote.app' "$zip"
printf 'Packed %s/%s to share or attach to a GitHub release\n' "$project" "$zip"
