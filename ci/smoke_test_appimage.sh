#!/usr/bin/env bash
# Verify that the released AppImage maps a real X11 window in an isolated Xvfb
# server. APPIMAGE_EXTRACT_AND_RUN avoids a FUSE dependency in CI while still
# executing the generated AppImage launcher and its bundled runtime.
set -euo pipefail

if (( $# != 1 )); then
    printf 'Usage: %s PATH_TO_APPIMAGE\n' "$0" >&2
    exit 2
fi

readonly APPIMAGE=$1
readonly WINDOW_NAME='Dendro'

if [[ ! -f "$APPIMAGE" ]]; then
    printf 'AppImage does not exist: %s\n' "$APPIMAGE" >&2
    exit 1
fi

for command in Xvfb xdotool timeout; do
    if ! command -v "$command" >/dev/null 2>&1; then
        printf 'Required command is unavailable: %s\n' "$command" >&2
        exit 1
    fi
done

readonly TEMP_DIR="$(mktemp -d)"
app_pid=''
xvfb_pid=''

after_exit() {
    local status=$?

    trap - EXIT
    set +e

    if [[ -n "$app_pid" ]] && kill -0 "$app_pid" 2>/dev/null; then
        kill -TERM "$app_pid" 2>/dev/null
        wait "$app_pid" 2>/dev/null
    fi

    if [[ -n "$xvfb_pid" ]] && kill -0 "$xvfb_pid" 2>/dev/null; then
        kill -TERM "$xvfb_pid" 2>/dev/null
        wait "$xvfb_pid" 2>/dev/null
    fi

    if (( status != 0 )); then
        printf '%s\n' '--- AppImage stdout ---' >&2
        cat "${TEMP_DIR}/app.stdout" >&2 2>/dev/null || true
        printf '%s\n' '--- AppImage stderr ---' >&2
        cat "${TEMP_DIR}/app.stderr" >&2 2>/dev/null || true
        printf '%s\n' '--- Xvfb stderr ---' >&2
        cat "${TEMP_DIR}/xvfb.stderr" >&2 2>/dev/null || true
    fi

    rm -rf "$TEMP_DIR"
    exit "$status"
}

trap after_exit EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

mkfifo "${TEMP_DIR}/display-number"
# Opening the FIFO read/write prevents either side from blocking while the
# server reports the dynamically allocated display number through -displayfd.
exec {display_fd}<>"${TEMP_DIR}/display-number"

Xvfb -displayfd 3 -screen 0 800x600x24 \
    3>"${TEMP_DIR}/display-number" \
    2>"${TEMP_DIR}/xvfb.stderr" &
xvfb_pid=$!

if ! IFS= read -r -t 15 display_number <&"$display_fd"; then
    printf '%s\n' 'Xvfb did not report a ready display within 15 seconds.' >&2
    exit 1
fi
case "$display_number" in
    ''|*[!0-9]*)
        printf 'Xvfb reported an invalid display number: %s\n' "$display_number" >&2
        exit 1
        ;;
esac

APPIMAGE_EXTRACT_AND_RUN=1 DISPLAY=":${display_number}" "$APPIMAGE" \
    >"${TEMP_DIR}/app.stdout" \
    2>"${TEMP_DIR}/app.stderr" &
app_pid=$!

# xdotool --sync waits for the map event; this is not a startup sleep or a
# polling loop. The timeout is an explicit CI failure bound.
if ! DISPLAY=":${display_number}" timeout 25s \
    xdotool search --sync --onlyvisible --name "$WINDOW_NAME" \
    >"${TEMP_DIR}/window-ids"; then
    printf 'Dendro did not expose a visible X11 window within 25 seconds.\n' >&2
    exit 1
fi

if ! kill -0 "$app_pid" 2>/dev/null; then
    printf '%s\n' 'Dendro exposed a window but exited before the startup check completed.' >&2
    exit 1
fi

printf 'AppImage GUI startup passed on display :%s (window IDs: %s)\n' \
    "$display_number" "$(tr '\n' ' ' < "${TEMP_DIR}/window-ids")"