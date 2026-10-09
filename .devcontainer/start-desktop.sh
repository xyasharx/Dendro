#!/usr/bin/env bash
set -e

# 1. Start D-Bus session if not running
if [ -z "$DBUS_SESSION_BUS_ADDRESS" ]; then
    eval $(dbus-launch --sh-syntax)
    export DBUS_SESSION_BUS_ADDRESS
fi

# 2. Start Xvfb (Virtual Screen 1280x800)
Xvfb :1 -screen 0 1280x800x24 &
sleep 1

# 3. Start lightweight window manager
openbox &

# 4. Start VNC bridge
x11vnc -display :1 -nopw -listen localhost -xkb -ncache 10 -forever &
sleep 1

# 5. Start noVNC web server on port 6080
websockify --web /usr/share/novnc 6080 localhost:5900 &
echo "==> Desktop ready at http://localhost:6080/vnc.html"
