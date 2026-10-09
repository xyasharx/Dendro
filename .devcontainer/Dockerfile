FROM registry.fedoraproject.org/fedora:45

# Install full GNOME Shell environment, Mesa software rendering, and VNC bridge
RUN dnf update -y && dnf install -y \
    @gnome-desktop \
    gnome-shell \
    gnome-session \
    gnome-control-center \
    gnome-terminal \
    mutter \
    nautilus \
    mesa-dri-drivers \
    libglvnd-glx \
    xorg-x11-server-Xvfb \
    x11vnc \
    novnc \
    python3-websockify \
    dbus-x11 \
    dbus-daemon \
    python3-pyqt6 \
    python3-rpm \
    python3-libdnf5 \
    dnf5 \
    polkit \
    git \
    sudo && \
    dnf clean all

COPY start-desktop.sh /usr/local/bin/start-desktop.sh
RUN chmod +x /usr/local/bin/start-desktop.sh

ENV DISPLAY=:1
ENV LIBGL_ALWAYS_SOFTWARE=1
ENV GALLIUM_DRIVER=llvmpipe
