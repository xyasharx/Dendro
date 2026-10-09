FROM registry.fedoraproject.org/fedora:45

# Install GUI server, virtual display, GNOME schemas, and Dendro dependencies on Fedora 45
RUN dnf update -y && dnf install -y \
    git \
    python3 \
    python3-pip \
    python3-devel \
    python3-pyqt6 \
    python3-rpm \
    python3-libdnf5 \
    dnf5 \
    polkit \
    adwaita-icon-theme \
    breeze-icon-theme \
    gsettings-desktop-schemas \
    glib2 \
    dbus-x11 \
    xorg-x11-server-Xvfb \
    openbox \
    x11vnc \
    novnc \
    python3-websockify \
    which \
    procps-ng \
    sudo && \
    dnf clean all

# Copy and configure the virtual desktop launch script
COPY start-desktop.sh /usr/local/bin/start-desktop.sh
RUN chmod +x /usr/local/bin/start-desktop.sh

ENV DISPLAY=:1
