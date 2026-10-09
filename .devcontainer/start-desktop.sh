#!/usr/bin/env bash
set -e

# 1. Clean previous locks
rm -f /tmp/.X1-lock /tmp/.X11-unix/X1

# 2. Start Xvfb virtual screen (1280x800 with 24-bit depth for GNOME compositing)
Xvfb :1 -screen 0 1280x800x24 +extension GLX +render -noreset &
sleep 2

export DISPLAY=:1
export LIBGL_ALWAYS_SOFTWARE=1
export GALLIUM_DRIVER=llvmpipe
export XDG_CURRENT_DESKTOP=GNOME
export XDG_SESSION_DESKTOP=gnome
export XDG_SESSION_TYPE=x11

# 3. Start D-Bus session bus if not present
if [ -z "$DBUS_SESSION_BUS_ADDRESS" ]; then
    eval $(dbus-launch --sh-syntax)
    export DBUS_SESSION_BUS_ADDRESS
fi

# 4. Start the authentic GNOME Shell in X11 fallback mode
gnome-shell --x11 --sm-disable &
sleep 3

# 5. Start x11vnc on port 5900
x11vnc -display :1 -nopw -listen localhost -forever -shared -bg

# 6. Ensure noVNC entrypoint exists
if [ -f "/usr/share/novnc/vnc.html" ] && [ ! -f "/usr/share/novnc/index.html" ]; then
    ln -s /usr/share/novnc/vnc.html /usr/share/novnc/index.html || true
fi

# 7. Start noVNC web proxy on port 6080
nohup websockify --web /usr/share/novnc 6080 localhost:5900 > /tmp/websockify.log 2>&1 &

echo "==> Fedora GNOME Shell ready at port 6080"
