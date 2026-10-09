#!/usr/bin/env bash
set -e

# 1. Start clean D-Bus session
if [ -z "$DBUS_SESSION_BUS_ADDRESS" ]; then
    eval $(dbus-launch --sh-syntax)
    export DBUS_SESSION_BUS_ADDRESS
fi

# 2. Kill any stale display locks on :1
rm -f /tmp/.X1-lock /tmp/.X11-unix/X1

# 3. Start Xvfb (Virtual Screen 1280x800)
Xvfb :1 -screen 0 1280x800x24 &
sleep 2

export DISPLAY=:1

# 4. Start Openbox window manager
openbox &
sleep 1

# 5. Start x11vnc on port 5900
x11vnc -display :1 -nopw -listen localhost -forever -shared -bg

# 6. Ensure index.html exists in noVNC web root
if [ -f "/usr/share/novnc/vnc.html" ] && [ ! -f "/usr/share/novnc/index.html" ]; then
    ln -s /usr/share/novnc/vnc.html /usr/share/novnc/index.html || true
fi

# 7. Start websockify / noVNC web server on 6080 in the background
nohup websockify --web /usr/share/novnc 6080 localhost:5900 > /tmp/websockify.log 2>&1 &

echo "==> Virtual desktop started successfully on port 6080"
