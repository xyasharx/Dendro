#!/usr/bin/env bash
set -e

# 1. Clean previous display locks and runtime sockets
rm -f /tmp/.X1-lock /tmp/.X11-unix/X1
mkdir -p /tmp/runtime-root
chmod 700 /tmp/runtime-root
export XDG_RUNTIME_DIR=/tmp/runtime-root

# 2. Start virtual video canvas (1366x768 for standard workstation aspect ratio)
Xvfb :1 -screen 0 1366x768x24 +extension GLX +render -noreset &
sleep 2

export DISPLAY=:1
export LIBGL_ALWAYS_SOFTWARE=1
export GALLIUM_DRIVER=llvmpipe

# 3. Start D-Bus session bus
if [ -z "$DBUS_SESSION_BUS_ADDRESS" ]; then
    eval $(dbus-launch --sh-syntax)
    export DBUS_SESSION_BUS_ADDRESS
fi

# 4. Launch GNOME Shell as a nested Wayland Compositor
# This creates the real Wayland display socket: wayland-0
export WAYLAND_DISPLAY=wayland-0
export XDG_CURRENT_DESKTOP=GNOME
export XDG_SESSION_DESKTOP=gnome
export XDG_SESSION_TYPE=wayland

gnome-shell --nested --wayland &
sleep 3

# 5. Start x11vnc to stream the display to the browser
x11vnc -display :1 -nopw -listen localhost -forever -shared -bg

# 6. Ensure noVNC entrypoint exists
if [ -f "/usr/share/novnc/vnc.html" ] && [ ! -f "/usr/share/novnc/index.html" ]; then
    ln -s /usr/share/novnc/vnc.html /usr/share/novnc/index.html || true
fi

# 7. Start web bridge on port 6080
nohup websockify --web /usr/share/novnc 6080 localhost:5900 > /tmp/websockify.log 2>&1 &

echo "=========================================================="
echo " Fedora 45 GNOME Wayland Session is Ready!"
echo " WAYLAND_DISPLAY: $WAYLAND_DISPLAY"
echo " Access in browser via port 6080"
echo "=========================================================="
