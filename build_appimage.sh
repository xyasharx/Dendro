#!/usr/bin/env bash
# Build the supported x86_64 AppImage. This bundle is an unprivileged DNF5
# daemon client; it does not contain a privileged helper or a host-command
# bridge. Runtime package transactions require the host's org.rpm.dnf.v0
# service and its Polkit policy.
set -euo pipefail

readonly APP_NAME='Dendro'
readonly ARCH='x86_64'
readonly APPDIR='AppDir'
readonly OUTPUT_APPIMAGE="${APP_NAME}-${ARCH}.AppImage"
readonly PYTHON_ARCHIVE='python-standalone.tar.gz'
readonly APPIMAGETOOL='appimagetool-x86_64.AppImage'

readonly PYTHON_URL='https://github.com/astral-sh/python-build-standalone/releases/download/20261013/cpython-3.14.8%2B20261013-x86_64-unknown-linux-gnu-install_only.tar.gz'
readonly PYTHON_SHA256='322271a761140ffb3f522431048f701831e412c8f64770103d2b881ee2bbaed0'
# Pin an immutable appimagetool release rather than its moving `continuous`
# asset so a release build remains reproducible and the recorded hash remains
# meaningful.
readonly APPIMAGETOOL_URL='https://github.com/AppImage/appimagetool/releases/download/1.9.1/appimagetool-x86_64.AppImage'
readonly APPIMAGETOOL_SHA256='ed4ce84f0d9caff66f50bcca6ff6f35aae54ce8135408b3fa33abfc3cb384eb0'

readonly PYQT6_URL='https://files.pythonhosted.org/packages/c3/a0/bd1399740dfa80c0a94d20b02d89962a31458233dcf70eaa09bfbccf3d0f/pyqt6-6.11.0-cp310-abi3-manylinux_2_34_x86_64.whl'
readonly PYQT6_SHA256='8555277989fa7d114cb3c3443fd261d566909f7268ceedd41d93a5f02d37ec05'
readonly PYQT6_QT6_URL='https://files.pythonhosted.org/packages/69/ef/e34054e814c874f5230326ffc791f602a13b2f5590ca9599ba45b5fc4bc0/pyqt6_qt6-6.11.2-py3-none-manylinux_2_34_x86_64.whl'
readonly PYQT6_QT6_SHA256='da7d747ad917f044d841ff834f17d2c50a0cc5d0e3b16479d8662544207d0567'
readonly PYQT6_SIP_URL='https://files.pythonhosted.org/packages/b0/46/c323b1627144379f4ca5ebd4a70c440b6c21a5936aa981450a54b7b2f770/pyqt6_sip-13.13.0-cp314-cp314-manylinux1_x86_64.manylinux_2_5_x86_64.whl'
readonly PYQT6_SIP_SHA256='6037f5f122b190ec1c3f53fdc796b68a9fbf83824b2d1c11f0ecbb787989a542'

require_command() {
    if ! command -v "$1" >/dev/null 2>&1; then
        printf 'Required command is unavailable: %s\n' "$1" >&2
        exit 1
    fi
}

download_verified() {
    local url=$1
    local destination=$2
    local expected_sha256=$3

    curl --fail --location --proto '=https' --tlsv1.2 --output "$destination" "$url"
    printf '%s  %s\n' "$expected_sha256" "$destination" | sha256sum --check --status
}

if [[ "$(uname -m)" != "$ARCH" ]]; then
    printf 'This build script only supports an %s build host; found %s.\n' "$ARCH" "$(uname -m)" >&2
    exit 1
fi

for command in appstreamcli curl desktop-file-validate sha256sum tar; do
    require_command "$command"
done

desktop-file-validate data/io.github.xyasharx.Dendro.desktop
appstreamcli validate --no-net data/io.github.xyasharx.Dendro.metainfo.xml

printf '%s\n' '==> Cleaning previous build artifacts'
rm -rf "$APPDIR" "$OUTPUT_APPIMAGE" "$APPIMAGETOOL" squashfs-root "$PYTHON_ARCHIVE"

printf '%s\n' '==> Creating AppDir hierarchy'
mkdir -p \
    "$APPDIR/usr/app" \
    "$APPDIR/usr/bin" \
    "$APPDIR/usr/share/applications" \
    "$APPDIR/usr/share/icons/hicolor/128x128/apps" \
    "$APPDIR/usr/share/icons/hicolor/256x256/apps" \
    "$APPDIR/usr/share/icons/hicolor/512x512/apps" \
    "$APPDIR/usr/share/icons/hicolor/scalable/apps" \
    "$APPDIR/usr/share/metainfo"

printf '%s\n' '==> Downloading verified standalone Python runtime'
download_verified "$PYTHON_URL" "$PYTHON_ARCHIVE" "$PYTHON_SHA256"
tar --extract --gzip --file "$PYTHON_ARCHIVE" --directory "$APPDIR/usr" --strip-components=1

readonly BUNDLED_PYTHON="$APPDIR/usr/bin/python3"
if [[ ! -x "$BUNDLED_PYTHON" ]]; then
    printf 'The standalone Python archive did not provide %s.\n' "$BUNDLED_PYTHON" >&2
    exit 1
fi

printf '%s\n' '==> Installing hash-verified PyQt6 wheels'
cat > "$APPDIR/requirements-appimage.txt" <<EOF
PyQt6 @ ${PYQT6_URL} --hash=sha256:${PYQT6_SHA256}
PyQt6-Qt6 @ ${PYQT6_QT6_URL} --hash=sha256:${PYQT6_QT6_SHA256}
PyQt6-sip @ ${PYQT6_SIP_URL} --hash=sha256:${PYQT6_SIP_SHA256}
EOF
"$BUNDLED_PYTHON" -m pip install \
    --disable-pip-version-check \
    --no-cache-dir \
    --no-deps \
    --require-hashes \
    --requirement "$APPDIR/requirements-appimage.txt"
rm "$APPDIR/requirements-appimage.txt"

printf '%s\n' '==> Copying Dendro application and desktop metadata'
# Keep the canonical project metadata beside the source launcher so its
# dependency-free --version path reports the same release version as the wheel.
cp -a core ui main.py pyproject.toml "$APPDIR/usr/app/"
find "$APPDIR/usr/app" -type d -name '__pycache__' -prune -exec rm -rf {} +
find "$APPDIR/usr/app" -type f -name '*.py[co]' -delete

install -Dm0755 data/AppRun "$APPDIR/AppRun"
ln -s ../../AppRun "$APPDIR/usr/bin/dendro"
install -Dm0644 data/io.github.xyasharx.Dendro.desktop "$APPDIR/io.github.xyasharx.Dendro.desktop"
install -Dm0644 data/io.github.xyasharx.Dendro.desktop "$APPDIR/usr/share/applications/io.github.xyasharx.Dendro.desktop"
# appimagetool 1.x discovers the established .appdata.xml name. The source
# metadata remains in the modern .metainfo.xml form; avoid installing both
# names in one AppImage because they describe the same component twice.
install -Dm0644 data/io.github.xyasharx.Dendro.metainfo.xml "$APPDIR/usr/share/metainfo/io.github.xyasharx.Dendro.appdata.xml"

for size in 128x128 256x256 512x512; do
    install -Dm0644 \
        "data/icons/${size}/io.github.xyasharx.Dendro.png" \
        "$APPDIR/usr/share/icons/hicolor/${size}/apps/io.github.xyasharx.Dendro.png"
done
install -Dm0644 \
    io.github.xyasharx.Dendro.svg \
    "$APPDIR/usr/share/icons/hicolor/scalable/apps/io.github.xyasharx.Dendro.svg"
install -Dm0644 data/icons/128x128/io.github.xyasharx.Dendro.png "$APPDIR/io.github.xyasharx.Dendro.png"
install -Dm0644 data/icons/128x128/io.github.xyasharx.Dendro.png "$APPDIR/.DirIcon"

printf '%s\n' '==> Downloading verified appimagetool'
download_verified "$APPIMAGETOOL_URL" "$APPIMAGETOOL" "$APPIMAGETOOL_SHA256"
chmod 0755 "$APPIMAGETOOL"

printf '%s\n' '==> Assembling AppImage'
APPIMAGE_EXTRACT_AND_RUN=1 "./$APPIMAGETOOL" --appimage-extract >/dev/null
# Use env rather than a shell assignment for ARCH: ARCH is deliberately readonly
# in this builder, while appimagetool's extracted launcher requires it in the
# child environment.
env APPIMAGE_EXTRACT_AND_RUN=1 ARCH="$ARCH" ./squashfs-root/AppRun "$APPDIR" "$OUTPUT_APPIMAGE"

if [[ ! -s "$OUTPUT_APPIMAGE" ]]; then
    printf 'AppImage creation did not produce a non-empty %s.\n' "$OUTPUT_APPIMAGE" >&2
    exit 1
fi
printf 'Created %s\n' "$OUTPUT_APPIMAGE"
