#!/usr/bin/env bash
set -euo pipefail

APP_NAME="Dendro"
ARCH="x86_64"
OUTPUT_APPIMAGE="${APP_NAME}-${ARCH}.AppImage"
APPDIR="AppDir"

echo "==> 1. Cleaning previous build artifacts..."
rm -rf "${APPDIR}" "${OUTPUT_APPIMAGE}" appimagetool-x86_64.AppImage python-standalone.tar.gz

echo "==> 2. Creating AppDir structure..."
mkdir -p "${APPDIR}/usr/bin"
mkdir -p "${APPDIR}/usr/lib"
mkdir -p "${APPDIR}/usr/app"

echo "==> 3. Downloading Standalone Python..."
PYTHON_URL="https://github.com/astral-sh/python-build-standalone/releases/download/20240224/cpython-3.12.2%2B20240224-x86_64-unknown-linux-gnu-install_only.tar.gz"
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

echo "==> 7. Downloading appimagetool..."
curl -fsSL -o appimagetool-x86_64.AppImage "https://github.com/AppImage/AppImageKit/releases/download/continuous/appimagetool-x86_64.AppImage"
chmod +x appimagetool-x86_64.AppImage

echo "==> 8. Building AppImage..."
export ARCH=x86_64
./appimagetool-x86_64.AppImage --appimage-extract-and-run "${APPDIR}" "${OUTPUT_APPIMAGE}"

echo "==> Successfully created: ${OUTPUT_APPIMAGE}"
