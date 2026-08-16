#!/usr/bin/env bash
#
# Build the Linux .deb.  Wraps the same PyInstaller onedir bundle the Windows
# build produces, so there is one build path for both platforms.
#
#   bash packaging/build_deb.sh          # version from git tag, else 0.0.0
#   VERSION=1.2.0 bash packaging/build_deb.sh
#
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

PKG=feelthebeat
ARCH="$(dpkg --print-architecture)"
VERSION="${VERSION:-$(git describe --tags --abbrev=0 2>/dev/null | sed 's/^v//' || true)}"
VERSION="${VERSION:-0.0.0}"
STAGE="build/deb/${PKG}_${VERSION}_${ARCH}"

echo "==> Building ${PKG} ${VERSION} (${ARCH})"

python3 packaging/make_icon.py
python3 -m PyInstaller --noconfirm --clean FeelTheBeat.spec

rm -rf "$STAGE"
mkdir -p "$STAGE/DEBIAN" \
         "$STAGE/opt/FeelTheBeat" \
         "$STAGE/usr/bin" \
         "$STAGE/usr/share/applications" \
         "$STAGE/usr/share/icons/hicolor/256x256/apps"

cp -a dist/FeelTheBeat/. "$STAGE/opt/FeelTheBeat/"
cp assets/icon-256.png "$STAGE/usr/share/icons/hicolor/256x256/apps/feelthebeat.png"

cat > "$STAGE/usr/bin/feelthebeat" <<'EOF'
#!/bin/sh
exec /opt/FeelTheBeat/FeelTheBeat "$@"
EOF
chmod 0755 "$STAGE/usr/bin/feelthebeat"

cat > "$STAGE/usr/share/applications/feelthebeat.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=FeelTheBeat
Comment=Beat-synced desktop overlay visualizer
Exec=feelthebeat
Icon=feelthebeat
Terminal=false
Categories=AudioVideo;Audio;
StartupNotify=false
EOF

INSTALLED_SIZE="$(du -sk "$STAGE" | cut -f1)"

cat > "$STAGE/DEBIAN/control" <<EOF
Package: ${PKG}
Version: ${VERSION}
Section: sound
Priority: optional
Architecture: ${ARCH}
Installed-Size: ${INSTALLED_SIZE}
Maintainer: ZeroTheScyther <zerothescyther@gmail.com>
Depends: libc6, libglib2.0-0, libxcb-xinerama0, libxcb-cursor0, libxkbcommon-x11-0, libfontconfig1, libfreetype6
Recommends: playerctl, pulseaudio-utils
Description: Beat-synced desktop overlay visualizer
 FeelTheBeat captures system audio, detects beats with per-band spectral flux
 onset detection, and plays a transparent click-through animation overlay in
 time with the music.
 .
 playerctl enables now-playing BPM lookup; pulseaudio-utils (pactl) enables
 automatic monitor-source selection and the --filter-apps option.
EOF

mkdir -p dist
OUT="dist/${PKG}_${VERSION}_${ARCH}.deb"
dpkg-deb --build --root-owner-group "$STAGE" "$OUT"

echo "==> Built $OUT ($(du -h "$OUT" | cut -f1))"
