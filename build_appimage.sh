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

echo "==> 3. Downloading Standalone Relocatable Python 3.12..."
PYTHON_URL="https://github.com/astral-sh/python-build-standalone/releases/download/20240224/cpython-3.12.2+20240224-x86_64-unknown-linux-gnu-install_only.tar.gz"
curl -Lo python-standalone.tar.gz "${PYTHON_URL}"
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
if [ -f "data/AppRun" ]; then
    cp data/AppRun "${APPDIR}/AppRun"
else
    cat << 'EOF' > "${APPDIR}/AppRun"
#!/bin/bash
HERE="$(dirname "$(readlink -f "${0}")")"
export APPDIR="${HERE}"
export PATH="${APPDIR}/usr/bin:${PATH}"
export LD_LIBRARY_PATH="${APPDIR}/usr/lib:${APPDIR}/usr/lib64:${LD_LIBRARY_PATH}"
export PYTHONHOME="${APPDIR}/usr"
export PYTHONPATH="${APPDIR}/usr/lib/python3.12/site-packages:${APPDIR}/usr/app:${PYTHONPATH}"
exec "${APPDIR}/usr/bin/python3" "${APPDIR}/usr/app/main.py" "$@"
EOF
fi
chmod +x "${APPDIR}/AppRun"

# کپی فایل دسکتاپ با نام جدید dendro.desktop
if [ -f "data/dendro.desktop" ]; then
    cp data/dendro.desktop "${APPDIR}/dendro.desktop"
elif [ -f "data/fedora-pamac-tree.desktop" ]; then
    cp data/fedora-pamac-tree.desktop "${APPDIR}/dendro.desktop"
fi

# کپی آیکون
if [ -f "data/icons/128x128/io.github.xyasharx.Dendro.png" ]; then
    cp "data/icons/128x128/io.github.xyasharx.Dendro.png" "${APPDIR}/dendro.png"
else
    touch "${APPDIR}/dendro.png"
fi

echo "==> 7. Downloading appimagetool..."
curl -Lo appimagetool-x86_64.AppImage "https://github.com/AppImage/AppImageKit/releases/download/continuous/appimagetool-x86_64.AppImage"
chmod +x appimagetool-x86_64.AppImage

echo "==> 8. Building AppImage..."
export ARCH=x86_64
./appimagetool-x86_64.AppImage --appimage-extract-and-run "${APPDIR}" "${OUTPUT_APPIMAGE}"

echo "==> Successfully created: ${OUTPUT_APPIMAGE}"
