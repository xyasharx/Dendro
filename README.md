# Fedora Package Tree 🌳

[![License: GPL v3](https://img.shields.io/badge/License-GPLv3-blue.svg)](https://www.gnu.org/licenses/gpl-3.0)
[![Fedora](https://img.shields.io/badge/Platform-Fedora%20Linux-blue?logo=fedora)](https://getfedora.org)
[![PyQt6](https://img.shields.io/badge/GUI-PyQt6-green?logo=qt)](https://www.riverbankcomputing.com/software/pyqt/)

**Fedora Package Tree** is a fast, modern graphical package manager and dependency hierarchy visualizer for Fedora Linux, heavily inspired by Arch Linux's Pamac.

---

## ✨ Features

- ⚡ **Asynchronous Non-Blocking Core**: All RPM database queries and Polkit transactions execute in dedicated background worker threads without freezing the user interface.
- 🌳 **Multi-Level Dependency Graph**: Expand any package node to visualize its dependencies, sub-dependencies, and virtual provides in a collapsible hierarchy.
- 🔄 **Cycle & Orphan Detection**: Automatically identifies circular dependencies and isolates unneeded leaf packages (orphans).
- 🎨 **Modern Dark Theme**: Flat UI styling with native-painted status pills, SVG branch expanders, and visual state indicators.
- 🔒 **Native Polkit Privileges**: Installs and removes packages using `pkexec dnf` with standard system authentication modals.

---

## 🚀 Quick Start

### Option 1: Standalone AppImage (Recommended)
Download the latest `FedoraPackageTree-x86_64.AppImage` from the [Releases](https://github.com/YOUR_USERNAME/fedora-pamac/releases) tab:

```bash
chmod +x FedoraPackageTree-x86_64.AppImage
./FedoraPackageTree-x86_64.AppImage
```

### Option 2: Running from Source

```bash
# 1. Clone repository
git clone https://github.com/YOUR_USERNAME/fedora-pamac.git
cd fedora-pamac

# 2. Install dependencies on Fedora
sudo dnf install -y python3 python3-pip qt6-qtbase polkit

# 3. Create virtual environment with system access
python3 -m venv --system-site-packages venv
source venv/bin/activate
pip install -r requirements.txt

# 4. Launch
python3 main.py
```

---

## 🛠️ Project Architecture

```text
├── core/
│   ├── backend.py       # Thread-safe RPM queries, DAG resolution, pkexec runner
│   └── models.py        # QAbstractItemModel tree model & QSortFilterProxyModel
├── ui/
│   ├── delegates.py     # Custom QStyledItemDelegate for status badges
│   ├── header.py        # Search bar with debouncing & Apply queue trigger
│   ├── main_window.py   # Primary MVC controller
│   ├── sidebar.py       # Category navigation list
│   ├── styles.py        # Modern dark QSS stylesheet
│   └── transaction_drawer.py # Real-time log drawer & progress monitor
└── main.py              # Application entrypoint & High-DPI configuration
```

---

## 📄 License
Distributed under the **GNU General Public License v3.0 (GPL-3.0-or-later)**. See `LICENSE` for details.