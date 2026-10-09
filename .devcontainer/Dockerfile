FROM registry.fedoraproject.org/fedora:45

# Install GNOME desktop, Wayland compositors, Qt Wayland plugin, and noVNC
RUN dnf update -y && dnf install -y \
    @gnome-desktop \
    gnome-shell \
    gnome-session \
    gnome-control-center \
    mutter \
    xorg-x11-server-Xwayland \
    qt6-qtwayland \
    mesa-dri-drivers \
    libglvnd-glx \
    xorg-x11-server-Xvfb \
    x11vnc \
    novnc \
    python3-websockify \
    dbus-x11 \
    python3-pyqt6 \
    python3-rpm \
    python3-libdnf5 \
    dnf5 \
    polkit \
    git \
    which \
    procps-ng \
    sudo && \
    dnf clean all

COPY start-desktop.sh /usr/local/bin/start-desktop.sh
RUN chmod +x /usr/local/bin/start-desktop.sh

ENV LIBGL_ALWAYS_SOFTWARE=1
ENV GALLIUM_DRIVER=llvmpipe
