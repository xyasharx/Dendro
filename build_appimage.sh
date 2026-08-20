#!/usr/bin/env bash
set -euo pipefail

APP_NAME="Dendro"
ARCH="x86_64"
OUTPUT_APPIMAGE="${APP_NAME}-${ARCH}.AppImage"
APPDIR="AppDir"

echo "==> 1. Cleaning previous build artifacts..."
rm -rf "${APPDIR}" "${OUTPUT_APPIMAGE}" appimagetool* squashfs-root python-standalone.tar.gz

echo "==> 2. Creating AppDir structure..."
mkdir -p "${APPDIR}/usr/bin"
mkdir -p "${APPDIR}/usr/lib"
mkdir -p "${APPDIR}/usr/app"

echo "==> 3. Downloading Standalone Python..."
PYTHON_URL="https://github.com/astral-sh/python-build-standalone/releases/download/20260814/cpython-3.14.7+20260814-x86_64-unknown-linux-gnu-install_only.tar.gz"
curl -fsSL -o python-standalone.tar.gz "${PYTHON_URL}"
tar -xzf python-standalone.tar.gz -C "${APPDIR}/usr" --strip-components=1

echo "==> 4. Installing PyQt6 & dependencies into AppDir..."
"${APPDIR}/usr/bin/python3" -m pip install --upgrade pip
"${APPDIR}/usr/bin/python3" -m pip install \
    --no-warn-script-location \
    --prefix="${APPDIR}/usr" \
    PyQt6>=6.6.0

echo "==> 5. Copying Application Code..."
cp -r core ui main.py "${APPDIR}/usr/app/"

echo "==> 6. Copying Desktop Integration Files..."
cp data/AppRun "${APPDIR}/AppRun"
sed -i 's/\r$//' "${APPDIR}/AppRun"
chmod +x "${APPDIR}/AppRun"

cp data/dendro.desktop "${APPDIR}/dendro.desktop"

# Copy Icon
if [ -f "data/icons/128x128/io.github.xyasharx.Dendro.png" ]; then
    cp "data/icons/128x128/io.github.xyasharx.Dendro.png" "${APPDIR}/io.github.xyasharx.Dendro.png"
    cp "data/icons/128x128/io.github.xyasharx.Dendro.png" "${APPDIR}/dendro.png"
    cp "data/icons/128x128/io.github.xyasharx.Dendro.png" "${APPDIR}/.DirIcon"
else
    convert -size 128x128 xc:#1e1e2e -fill "#89b4fa" -draw "circle 64,64 64,120" "${APPDIR}/Dendro.png" 2>/dev/null || touch "${APPDIR}/Dendro.png"
    cp "${APPDIR}/Dendro.png" "${APPDIR}/io.github.xyasharx.Dendro.png"
    cp "${APPDIR}/Dendro.png" "${APPDIR}/.DirIcon"
fi

echo "==> 7. Downloading and preparing appimagetool..."
URL_APPIMAGE_OFFICIAL="https://github.com/AppImage/appimagetool/releases/download/continuous/appimagetool-x86_64.AppImage"
URL_GO_APPIMAGE="https://github.com/probonopd/go-appimage/releases/download/continuous/appimagetool-continuous-x86_64.AppImage"

DOWNLOAD_SUCCESS=false

if curl -fsSL --retry 3 --connect-timeout 10 -o appimagetool-x86_64.AppImage "${URL_APPIMAGE_OFFICIAL}"; then
    echo "Successfully downloaded official appimagetool."
    DOWNLOAD_SUCCESS=true
elif curl -fsSL --retry 3 --connect-timeout 10 -o appimagetool-x86_64.AppImage "${URL_GO_APPIMAGE}"; then
    echo "Fallback: Successfully downloaded go-appimage tool."
    DOWNLOAD_SUCCESS=true
fi

if [ "${DOWNLOAD_SUCCESS}" = false ]; then
    echo "ERROR: Failed to download appimagetool from all sources." >&2
    exit 1
fi

chmod +x appimagetool-x86_64.AppImage

# Extract tool to guarantee execution on headless CI runners without FUSE
echo "==> Extracting appimagetool for FUSE-less execution..."
./appimagetool-x86_64.AppImage --appimage-extract > /dev/null

echo "==> 8. Building AppImage..."
export ARCH=x86_64
export APPIMAGE_EXTRACT_AND_RUN=1

# Use extracted AppRun from appimagetool to bypass any container/FUSE limitations
./squashfs-root/AppRun "${APPDIR}" "${OUTPUT_APPIMAGE}"

echo "==> Successfully created: ${OUTPUT_APPIMAGE}"
