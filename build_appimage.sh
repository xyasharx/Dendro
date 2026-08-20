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

echo "==> 5. بهینه‌سازی و رژیم حجم (حذف ماژول‌های بلااستفاده پایتون و Qt6)..."
# ۱. حذف هدرهای کامپایل C و فایل‌های استاتیک پایتون (صرفه‌جویی ~25MB)
rm -rf "${APPDIR}/usr/include"
find "${APPDIR}/usr/lib" -name "*.a" -delete
find "${APPDIR}/usr/lib" -type d -name "test" -exec rm -rf {} + 2>/dev/null || true
find "${APPDIR}/usr/lib" -type d -name "idlelib" -exec rm -rf {} + 2>/dev/null || true
find "${APPDIR}/usr/lib" -type d -name "tkinter" -exec rm -rf {} + 2>/dev/null || true

# ۲. پیدا کردن مسیر PyQt6_Qt6
QT6_DIR=$(find "${APPDIR}/usr/lib" -type d -name "Qt6" | head -n 1)

if [ -d "${QT6_DIR}" ]; then
    echo "بهینه‌سازی کتابخانه‌های Qt6 در: ${QT6_DIR}"
    
    # حذف کتابخانه‌های فوق‌العاده سنگین بی‌استفاده (QML, Quick, 3D, Designer, Sql, Sensors و...)
    # Dendro فقط به Core, Gui, Widgets, DBus, Xcb, Wayland نیاز دارد
    find "${QT6_DIR}/lib" -maxdepth 1 -type f \( \
        -name "libQt6Qml*" -o \
        -name "libQt6Quick*" -o \
        -name "libQt63D*" -o \
        -name "libQt6Designer*" -o \
        -name "libQt6Sql*" -o \
        -name "libQt6Multimedia*" -o \
        -name "libQt6Positioning*" -o \
        -name "libQt6Sensors*" -o \
        -name "libQt6SerialPort*" -o \
        -name "libQt6Test*" -o \
        -name "libQt6VirtualKeyboard*" -o \
        -name "libQt6WebChannel*" \
    \) -delete 2>/dev/null || true

    # حذف پلاگین‌های بی‌استفاده
    rm -rf "${QT6_DIR}/plugins/designer"
    rm -rf "${QT6_DIR}/plugins/qmltooling"
    rm -rf "${QT6_DIR}/plugins/sqldrivers"
    rm -rf "${QT6_DIR}/plugins/multimedia"
    rm -rf "${QT6_DIR}/plugins/position"
    rm -rf "${QT6_DIR}/plugins/sensors"
    rm -rf "${QT6_DIR}/qml"
fi

# ۳. فشرده‌سازی و Strip کردن نمادهای دیباگ باینری‌ها
if command -v strip >/dev/null 2>&1; then
    echo "حذف نمادهای دیباگ باینری‌ها (Strip)..."
    find "${APPDIR}/usr" -type f -name "*.so*" -exec strip --strip-unneeded {} + 2>/dev/null || true
    strip --strip-unneeded "${APPDIR}/usr/bin/python3"* 2>/dev/null || true
fi

# ۴. پاکسازی کش‌های موقت پایتون
find "${APPDIR}" -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true
find "${APPDIR}" -type f -name "*.pyc" -delete

echo "==> 6. کپی کدهای برنامه..."
cp -r core ui main.py "${APPDIR}/usr/app/"

echo "==> 7. کپی فایل‌های دسکتاپ و آیکون..."
cp data/AppRun "${APPDIR}/AppRun"
sed -i 's/\r$//' "${APPDIR}/AppRun"
chmod +x "${APPDIR}/AppRun"

cp data/dendro.desktop "${APPDIR}/dendro.desktop"

if [ -f "data/icons/128x128/io.github.xyasharx.Dendro.png" ]; then
    cp "data/icons/128x128/io.github.xyasharx.Dendro.png" "${APPDIR}/io.github.xyasharx.Dendro.png"
    cp "data/icons/128x128/io.github.xyasharx.Dendro.png" "${APPDIR}/dendro.png"
    cp "data/icons/128x128/io.github.xyasharx.Dendro.png" "${APPDIR}/.DirIcon"
fi

echo "==> 8. دریافت ابزار appimagetool..."
URL_APPIMAGE_OFFICIAL="https://github.com/AppImage/appimagetool/releases/download/continuous/appimagetool-x86_64.AppImage"
curl -fsSL -o appimagetool-x86_64.AppImage "${URL_APPIMAGE_OFFICIAL}"
chmod +x appimagetool-x86_64.AppImage

./appimagetool-x86_64.AppImage --appimage-extract > /dev/null

echo "==> 9. بیلد نهایی AppImage با بالاترین سطح فشرده‌سازی (XZ)..."
export ARCH=x86_64
export APPIMAGE_EXTRACT_AND_RUN=1

# استفاده از الگوریتم فشرده‌سازی XZ به جای ZSTD پیش‌فرض برای کمترین حجم ممکن
./squashfs-root/AppRun -comp xz "${APPDIR}" "${OUTPUT_APPIMAGE}"

echo "==> با موفقیت ساخته شد: ${OUTPUT_APPIMAGE}"
