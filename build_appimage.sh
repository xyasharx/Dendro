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
readonly APPIMAGE_RUNTIME='runtime-x86_64'

readonly PYTHON_URL='https://github.com/astral-sh/python-build-standalone/releases/download/20261013/cpython-3.14.8%2B20261013-x86_64-unknown-linux-gnu-install_only.tar.gz'
readonly PYTHON_SHA256='322271a761140ffb3f522431048f701831e412c8f64770103d2b881ee2bbaed0'
# Pin an immutable appimagetool release rather than its moving `continuous`
# asset so a release build remains reproducible and the recorded hash remains
# meaningful.
readonly APPIMAGETOOL_URL='https://github.com/AppImage/appimagetool/releases/download/1.9.1/appimagetool-x86_64.AppImage'
readonly APPIMAGETOOL_SHA256='ed4ce84f0d9caff66f50bcca6ff6f35aae54ce8135408b3fa33abfc3cb384eb0'
# appimagetool otherwise downloads a mutable type2 runtime at packaging time.
# Keep the first executable code in the output AppImage versioned and verified.
readonly APPIMAGE_RUNTIME_URL='https://github.com/AppImage/type2-runtime/releases/download/20251108/runtime-x86_64'
readonly APPIMAGE_RUNTIME_SHA256='2fca8b443c92510f1483a883f60061ad09b46b978b2631c807cd873a47ec260d'

readonly PYQT6_URL='https://files.pythonhosted.org/packages/c3/a0/bd1399740dfa80c0a94d20b02d89962a31458233dcf70eaa09bfbccf3d0f/pyqt6-6.11.0-cp310-abi3-manylinux_2_34_x86_64.whl'
readonly PYQT6_SHA256='8555277989fa7d114cb3c3443fd261d566909f7268ceedd41d93a5f02d37ec05'
readonly PYQT6_QT6_URL='https://files.pythonhosted.org/packages/69/ef/e34054e814c874f5230326ffc791f602a13b2f5590ca9599ba45b5fc4bc0/pyqt6_qt6-6.11.2-py3-none-manylinux_2_34_x86_64.whl'
readonly PYQT6_QT6_SHA256='da7d747ad917f044d841ff834f17d2c50a0cc5d0e3b16479d8662544207d0567'
readonly PYQT6_SIP_URL='https://files.pythonhosted.org/packages/b0/46/c323b1627144379f4ca5ebd4a70c440b6c21a5936aa981450a54b7b2f770/pyqt6_sip-13.13.0-cp314-cp314-manylinux1_x86_64.manylinux_2_5_x86_64.whl'
readonly PYQT6_SIP_SHA256='6037f5f122b190ec1c3f53fdc796b68a9fbf83824b2d1c11f0ecbb787989a542'
readonly DEFUSEDXML_URL='https://files.pythonhosted.org/packages/07/6c/aa3f2f849e01cb6a001cd8554a88d4c77c5c1a31c95bdf1cf9301e6d9ef4/defusedxml-0.7.1-py2.py3-none-any.whl'
readonly DEFUSEDXML_SHA256='a352e7e428770286cc899e2542b6cdaedb2b4953ff269a210103ec58f6198a61'

# PyQt6 supplies Qt and libqxcb.so but not these XKB/XCB helper libraries.
# Bundle verified Ubuntu 22.04 packages: their ABI baseline matches the
# AppImage catalog's Ubuntu 22.04 test environment and avoids taking library
# binaries from a newer release-build host.
readonly QT_XCB_RUNTIME_BUNDLES=(
    'libxkbcommon.so.0|https://archive.ubuntu.com/ubuntu/pool/main/libx/libxkbcommon/libxkbcommon0_1.4.0-1_amd64.deb|a0470edb237d52b71ad41f755c043164abed01423585ed5ea19525b3baaf84f7'
    'libxkbcommon-x11.so.0|https://archive.ubuntu.com/ubuntu/pool/main/libx/libxkbcommon/libxkbcommon-x11-0_1.4.0-1_amd64.deb|e29c40fab6e0f055218aee4c7e60cd61ea82283229d7ac7ac02e226e92a5b0d5'
    'libxcb-cursor.so.0|https://archive.ubuntu.com/ubuntu/pool/universe/x/xcb-util-cursor/libxcb-cursor0_0.1.1-4ubuntu1_amd64.deb|c9b5d1ad4af57397b1bd77e0a92750e34419def134c0282a0836ae9efc07cf64'
    'libxcb-icccm.so.4|https://archive.ubuntu.com/ubuntu/pool/main/x/xcb-util-wm/libxcb-icccm4_0.4.1-1.1build2_amd64.deb|4dfe14a0ce681b823b740dabb3de84b3ce0958c9a404af61823b11f64047b0ef'
    'libxcb-util.so.1|https://archive.ubuntu.com/ubuntu/pool/main/x/xcb-util/libxcb-util1_0.4.0-1build2_amd64.deb|237d51a378b93c1669ff193084ebeb6422fe1e2ea96c2e9a0099eac40992a345'
    'libxcb-image.so.0|https://archive.ubuntu.com/ubuntu/pool/main/x/xcb-util-image/libxcb-image0_0.4.0-2_amd64.deb|b52babc3b4f2aa5241cac3a7888ce3b37d25a7f18867cae180e8cd11ec945cb9'
    'libxcb-keysyms.so.1|https://archive.ubuntu.com/ubuntu/pool/main/x/xcb-util-keysyms/libxcb-keysyms1_0.4.0-1build3_amd64.deb|1d62f96a793cc1aa0df860de3f7d83edea2fa461d97e2b3d4d7f7e7a10e55f42'
    'libxcb-render-util.so.0|https://archive.ubuntu.com/ubuntu/pool/main/x/xcb-util-renderutil/libxcb-render-util0_0.3.9-1build3_amd64.deb|f87659a2369430ed2acb1cdae54a06fa9d60727a6545594e213173ddb4377727'
    'libxcb-xkb.so.1|https://archive.ubuntu.com/ubuntu/pool/main/libx/libxcb/libxcb-xkb1_1.14-3ubuntu3_amd64.deb|4cf69af3ff6a60b63f9f6ea6e7d27471114c6bcbcf3f981f7b88f361029e5eb8'
)

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

bundle_qt_xcb_runtime() {
    local bundle_root="${APPDIR}/.qt-xcb-runtime"
    local bundle_spec soname url expected_sha256 archive extract_dir source_library license_file

    mkdir -p "$bundle_root"

    for bundle_spec in "${QT_XCB_RUNTIME_BUNDLES[@]}"; do
        IFS='|' read -r soname url expected_sha256 <<< "$bundle_spec"
        archive="${bundle_root}/${soname}.deb"
        extract_dir="${bundle_root}/${soname}"

        download_verified "$url" "$archive" "$expected_sha256"
        mkdir -p "$extract_dir"
        dpkg-deb --extract "$archive" "$extract_dir"
        source_library=$(find "$extract_dir" -type f -name "${soname}.*" -print -quit)
        if [[ -z "$source_library" ]]; then
            printf 'Verified archive for %s did not contain the expected library.\n' "$soname" >&2
            exit 1
        fi

        install -Dm0755 "$source_library" "${APPDIR}/usr/lib/${soname}"

        if [[ -d "${extract_dir}/usr/share/doc" ]]; then
            while IFS= read -r license_file; do
                install -Dm0644 "$license_file" \
                    "${APPDIR}/usr/share/doc/dendro-appimage-libraries/$(basename "$(dirname "$license_file")")/copyright"
            done < <(find "${extract_dir}/usr/share/doc" -type f -name copyright -print)
        fi
    done

    rm -rf "$bundle_root"
}

verify_qt_xcb_runtime() {
    local bundle_spec soname ignored_url ignored_sha256 pyqt6_dir plugin ldd_output missing

    pyqt6_dir=$(find "${APPDIR}/usr/lib" -type d -path '*/site-packages/PyQt6' -print -quit)
    if [[ -z "$pyqt6_dir" ]]; then
        printf 'Bundled PyQt6 directory could not be found for XCB validation.\n' >&2
        exit 1
    fi

    plugin="${pyqt6_dir}/Qt6/plugins/platforms/libqxcb.so"
    if [[ ! -f "$plugin" ]]; then
        printf 'Bundled Qt XCB platform plugin is missing: %s\n' "$plugin" >&2
        exit 1
    fi

    for bundle_spec in "${QT_XCB_RUNTIME_BUNDLES[@]}"; do
        IFS='|' read -r soname ignored_url ignored_sha256 <<< "$bundle_spec"
        if [[ ! -s "${APPDIR}/usr/lib/${soname}" ]]; then
            printf 'Bundled Qt XCB runtime library is missing or empty: %s\n' "$soname" >&2
            exit 1
        fi
    done

    if ! ldd_output=$(LD_LIBRARY_PATH="${pyqt6_dir}/Qt6/lib:${APPDIR}/usr/lib" ldd "$plugin" 2>&1); then
        printf 'Could not inspect the Qt XCB platform plugin dependency closure:\n%s\n' "$ldd_output" >&2
        exit 1
    fi

    missing=$(printf '%s\n' "$ldd_output" | awk '/not found/ { print $1 }')
    if [[ -n "$missing" ]]; then
        printf 'Qt XCB platform plugin has unresolved runtime libraries:\n%s\n' "$missing" >&2
        exit 1
    fi
}

if [[ "$(uname -m)" != "$ARCH" ]]; then
    printf 'This build script only supports an %s build host; found %s.\n' "$ARCH" "$(uname -m)" >&2
    exit 1
fi

for command in appstreamcli curl desktop-file-validate dpkg-deb find ldd sha256sum tar; do
    require_command "$command"
done

desktop-file-validate data/io.github.xyasharx.Dendro.desktop
appstreamcli validate --no-net data/io.github.xyasharx.Dendro.metainfo.xml

printf '%s\n' '==> Cleaning previous build artifacts'
rm -rf "$APPDIR" "$OUTPUT_APPIMAGE" "$APPIMAGETOOL" "$APPIMAGE_RUNTIME" squashfs-root "$PYTHON_ARCHIVE"

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

printf '%s\n' '==> Installing hash-verified runtime wheels'
cat > "$APPDIR/requirements-appimage.txt" <<EOF
PyQt6 @ ${PYQT6_URL} --hash=sha256:${PYQT6_SHA256}
PyQt6-Qt6 @ ${PYQT6_QT6_URL} --hash=sha256:${PYQT6_QT6_SHA256}
PyQt6-sip @ ${PYQT6_SIP_URL} --hash=sha256:${PYQT6_SIP_SHA256}
defusedxml @ ${DEFUSEDXML_URL} --hash=sha256:${DEFUSEDXML_SHA256}
EOF
"$BUNDLED_PYTHON" -m pip install \
    --disable-pip-version-check \
    --no-cache-dir \
    --no-deps \
    --require-hashes \
    --requirement "$APPDIR/requirements-appimage.txt"
rm "$APPDIR/requirements-appimage.txt"

printf '%s\n' '==> Bundling and validating Qt XCB runtime libraries'
bundle_qt_xcb_runtime
verify_qt_xcb_runtime

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

printf '%s\n' '==> Downloading verified appimagetool and AppImage runtime'
download_verified "$APPIMAGETOOL_URL" "$APPIMAGETOOL" "$APPIMAGETOOL_SHA256"
download_verified "$APPIMAGE_RUNTIME_URL" "$APPIMAGE_RUNTIME" "$APPIMAGE_RUNTIME_SHA256"
chmod 0755 "$APPIMAGETOOL" "$APPIMAGE_RUNTIME"

printf '%s\n' '==> Assembling AppImage'
APPIMAGE_EXTRACT_AND_RUN=1 "./$APPIMAGETOOL" --appimage-extract >/dev/null
# Use env rather than a shell assignment for ARCH: ARCH is deliberately readonly
# in this builder, while appimagetool's extracted launcher requires it in the
# child environment. Pass the runtime both explicitly and through the supported
# environment variable so appimagetool never falls back to its mutable download.
env APPIMAGE_EXTRACT_AND_RUN=1 \
    ARCH="$ARCH" \
    APPIMAGETOOL_RUNTIME_FILE="$(pwd)/$APPIMAGE_RUNTIME" \
    ./squashfs-root/AppRun --runtime-file "$APPIMAGE_RUNTIME" "$APPDIR" "$OUTPUT_APPIMAGE"

if [[ ! -s "$OUTPUT_APPIMAGE" ]]; then
    printf 'AppImage creation did not produce a non-empty %s.\n' "$OUTPUT_APPIMAGE" >&2
    exit 1
fi
printf 'Created %s\n' "$OUTPUT_APPIMAGE"
