#!/usr/bin/env bash
set -euo pipefail

APP_NAME="Dendro"
ARCH="x86_64"
OUTPUT_APPIMAGE="${APP_NAME}-${ARCH}.AppImage"
APPDIR="AppDir"

echo "==> 1. پاکسازی بیلد قبلی..."
rm -rf "${APPDIR}" "${OUTPUT_APPIMAGE}" appimagetool* squashfs-root python-standalone.tar.gz

echo "==> 2. ایجاد ساختار پوشه‌های AppDir..."
mkdir -p "${APPDIR}/usr/bin"
mkdir -p "${APPDIR}/usr/lib"
mkdir -p "${APPDIR}/usr/app"

echo "==> 3. دریافت CPython Standalone..."
PYTHON_URL="https://github.com/astral-sh/python-build-standalone/releases/download/20260814/cpython-3.14.7+20260814-x86_64-unknown-linux-gnu-install_only.tar.gz"
curl -fsSL -o python-standalone.tar.gz "${PYTHON_URL}"
tar -xzf python-standalone.tar.gz -C "${APPDIR}/usr" --strip-components=1

echo "==> 4. نصب PyQt6 درون محیط پایتون..."
"${APPDIR}/usr/bin/python3" -m pip install --upgrade pip
"${APPDIR}/usr/bin/python3" -m pip install \
    --no-warn-script-location \
    --no-cache-dir \
    PyQt6

echo "==> 5. پاکسازی امن فایل‌های اضافی (هدرها، تست‌ها و فایل‌های استاتیک)..."
# حذف هدرهای زمان کامپایل C و فایل‌های تست داخلی پایتون (کاملاً امن)
rm -rf "${APPDIR}/usr/include"
find "${APPDIR}/usr/lib" -name "*.a" -delete
find "${APPDIR}/usr/lib" -type d -name "test" -exec rm -rf {} + 2>/dev/null || true
find "${APPDIR}/usr/lib" -type d -name "idlelib" -exec rm -rf {} + 2>/dev/null || true
find "${APPDIR}/usr/lib" -type d -name "tkinter" -exec rm -rf {} + 2>/dev/null || true
find "${APPDIR}" -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true
find "${APPDIR}" -type f -name "*.pyc" -delete

echo "==> 6. کپی کدهای برنامه..."
cp -r core ui main.py "${APPDIR}/usr/app/"

echo "==> 7. کپی فایل‌های دسکتاپ و آیکون..."
cp data/AppRun "${APPDIR}/AppRun"
sed -i 's/\r$//' "${APPDIR}/AppRun"
chmod +x "${APPDIR}/AppRun"

# کپی فایل دسکتاپ استاندارد AppStream / Flatpak
cp data/io.github.xyasharx.Dendro.desktop "${APPDIR}/io.github.xyasharx.Dendro.desktop"

# کپی آیکون‌ها برای شناسایی توسط appimagetool
if [ -f "data/icons/128x128/io.github.xyasharx.Dendro.png" ]; then
    cp "data/icons/128x128/io.github.xyasharx.Dendro.png" "${APPDIR}/io.github.xyasharx.Dendro.png"
    cp "data/icons/128x128/io.github.xyasharx.Dendro.png" "${APPDIR}/.DirIcon"
fi

echo "==> 8. دریافت ابزار appimagetool..."
URL_APPIMAGE_OFFICIAL="https://github.com/AppImage/appimagetool/releases/download/continuous/appimagetool-x86_64.AppImage"
curl -fsSL -o appimagetool-x86_64.AppImage "${URL_APPIMAGE_OFFICIAL}"
chmod +x appimagetool-x86_64.AppImage

./appimagetool-x86_64.AppImage --appimage-extract > /dev/null

echo "==> 9. بیلد نهایی AppImage..."
export ARCH=x86_64
export APPIMAGE_EXTRACT_AND_RUN=1

./squashfs-root/AppRun "${APPDIR}" "${OUTPUT_APPIMAGE}"

echo "==> با موفقیت ساخته شد: ${OUTPUT_APPIMAGE}"
