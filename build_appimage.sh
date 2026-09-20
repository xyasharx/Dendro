#!/usr/bin/env bash
set -euo pipefail

APP_NAME="Dendro"
ARCH="x86_64"
OUTPUT_APPIMAGE="${APP_NAME}-${ARCH}.AppImage"
APPDIR="AppDir"

echo "==> 1. Cleaning previous build artifacts..."
rm -rf "${APPDIR}" "${OUTPUT_APPIMAGE}" appimagetool* squashfs-root python-standalone.tar.gz

echo "==> 2. Creating standard AppDir hierarchy..."
mkdir -p "${APPDIR}/usr/bin"
mkdir -p "${APPDIR}/usr/lib"
mkdir -p "${APPDIR}/usr/app"
mkdir -p "${APPDIR}/usr/share/applications"
mkdir -p "${APPDIR}/usr/share/metainfo"
mkdir -p "${APPDIR}/usr/share/appdata"
mkdir -p "${APPDIR}/usr/share/icons/hicolor/128x128/apps"
mkdir -p "${APPDIR}/usr/share/icons/hicolor/256x256/apps"
mkdir -p "${APPDIR}/usr/share/icons/hicolor/512x512/apps"

echo "==> 3. Downloading standalone Python runtime..."
PYTHON_URL="https://github.com/astral-sh/python-build-standalone/releases/download/20260814/cpython-3.14.7+20260814-x86_64-unknown-linux-gnu-install_only.tar.gz"
curl -fsSL -o python-standalone.tar.gz "${PYTHON_URL}"
tar -xzf python-standalone.tar.gz -C "${APPDIR}/usr" --strip-components=1

echo "==> 4. Installing PyQt6 into bundled runtime..."
"${APPDIR}/usr/bin/python3" -m pip install --upgrade pip
"${APPDIR}/usr/bin/python3" -m pip install \
    --no-warn-script-location \
    --no-cache-dir \
    PyQt6

echo "==> 5. Stripping unnecessary library files to reduce bundle size..."
rm -rf "${APPDIR}/usr/include"
find "${APPDIR}/usr/lib" -name "*.a" -delete
find "${APPDIR}/usr/lib" -type d -name "test" -exec rm -rf {} + 2>/dev/null || true
find "${APPDIR}/usr/lib" -type d -name "idlelib" -exec rm -rf {} + 2>/dev/null || true
find "${APPDIR}/usr/lib" -type d -name "tkinter" -exec rm -rf {} + 2>/dev/null || true
find "${APPDIR}" -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true
find "${APPDIR}" -type f -name "*.pyc" -delete

echo "==> 6. Copying updated Dendro application code (v1.3.0)..."
cp -r core ui main.py "${APPDIR}/usr/app/"

echo "==> 7. Installing desktop entries, AppStream metadata, and icons..."
cp data/AppRun "${APPDIR}/AppRun"
chmod +x "${APPDIR}/AppRun"

# Create symlink for Exec=dendro
ln -sf ../../AppRun "${APPDIR}/usr/bin/dendro"

# Copy desktop entries
cp data/io.github.xyasharx.Dendro.desktop "${APPDIR}/io.github.xyasharx.Dendro.desktop"
cp data/io.github.xyasharx.Dendro.desktop "${APPDIR}/usr/share/applications/io.github.xyasharx.Dendro.desktop"

# Copy AppStream metadata with both modern and legacy extensions for appimagetool compatibility
if [ -f "data/io.github.xyasharx.Dendro.metainfo.xml" ]; then
    cp data/io.github.xyasharx.Dendro.metainfo.xml "${APPDIR}/usr/share/metainfo/io.github.xyasharx.Dendro.metainfo.xml"
    cp data/io.github.xyasharx.Dendro.metainfo.xml "${APPDIR}/usr/share/metainfo/io.github.xyasharx.Dendro.appdata.xml"
    cp data/io.github.xyasharx.Dendro.metainfo.xml "${APPDIR}/usr/share/appdata/io.github.xyasharx.Dendro.appdata.xml"
fi

# Sanitize any carriage return characters
find "${APPDIR}" -type f \( -name "*.desktop" -o -name "*.xml" -o -name "AppRun" \) -exec sed -i 's/\r$//' {} +

# Copy icons
for size in 128x128 256x256 512x512; do
    if [ -f "data/icons/${size}/io.github.xyasharx.Dendro.png" ]; then
        cp "data/icons/${size}/io.github.xyasharx.Dendro.png" "${APPDIR}/usr/share/icons/hicolor/${size}/apps/io.github.xyasharx.Dendro.png"
    fi
done

cp "data/icons/128x128/io.github.xyasharx.Dendro.png" "${APPDIR}/io.github.xyasharx.Dendro.png"
cp "data/icons/128x128/io.github.xyasharx.Dendro.png" "${APPDIR}/.DirIcon"

echo "==> 8. Fetching appimagetool..."
URL_APPIMAGE_OFFICIAL="https://github.com/AppImage/appimagetool/releases/download/continuous/appimagetool-x86_64.AppImage"
curl -fsSL -o appimagetool-x86_64.AppImage "${URL_APPIMAGE_OFFICIAL}"
chmod +x appimagetool-x86_64.AppImage

./appimagetool-x86_64.AppImage --appimage-extract > /dev/null

echo "==> 9. Assembling final Dendro v1.3.0 AppImage..."
export ARCH=x86_64
export APPIMAGE_EXTRACT_AND_RUN=1

./squashfs-root/AppRun "${APPDIR}" "${OUTPUT_APPIMAGE}"

echo "==> Successfully created: ${OUTPUT_APPIMAGE}"
