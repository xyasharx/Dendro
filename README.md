<div align="center">
<p align="center">
  <img src="data/icons/256x256/io.github.xyasharx.Dendro.png" width="110" height="110" alt="Dendro Logo">
</p>

# 🌳 Dendro

### Modern Graphical Package Manager & Interactive Dependency Explorer for Fedora Linux

[![License: GPL v3](https://img.shields.io/badge/License-GPLv3-blue.svg)](https://www.gnu.org/licenses/gpl-3.0)
[![Fedora](https://img.shields.io/badge/Platform-Fedora%2040%2B%20%7C%20Rawhide-3c6eb4?logo=fedora&logoColor=white)](https://getfedora.org)
[![Python](https://img.shields.io/badge/Python-3.10%2B-blue?logo=python&logoColor=white)](https://www.python.org/)
[![GUI](https://img.shields.io/badge/GUI-PyQt6-41cd52?logo=qt&logoColor=white)](https://riverbankcomputing.com/software/pyqt/)
[![Packaging](https://img.shields.io/badge/AppImage-Available-success?logo=linux&logoColor=white)](https://github.com/xyasharx/Dendro/releases)

<p align="center">
  <a href="#-key-features">Key Features</a> •
  <a href="#-installation">Installation</a> •
  <a href="#-power-search-syntax">Search Syntax</a> •
  <a href="#-keyboard-shortcuts">Shortcuts</a> •
  <a href="#-architecture">Architecture</a> •
  <a href="#-license">License</a>
</p>

---

</div>

**Dendro** is a fast, modern graphical package manager and visual dependency hierarchy explorer built natively for Fedora Linux. Designed for both everyday users and system developers, Dendro empowers you to visually explore package relationships, query reverse dependencies, inspect installed package files, identify orphans, simulate changes safely, and manage transactions via native Polkit privileges (`dnf` / `dnf5`).

---

## ✨ Key Features

- 🌳 **Multi-Level Interactive Dependency Tree**: Expand any package to inspect its full dependency graph—direct requirements, sub-dependencies, and virtual RPM capabilities—rendered with native anti-aliased branch vectors and DAG cycle detection.
- 🔍 **Reverse Dependency Explorer ("What Requires This?")**: Determine which packages depend on a specific library before removing it, preventing accidental system breakage.
- 📋 **Integrated File & Metadata Inspector**: View full package descriptions, architectures, packager data, and browse installed file trees (`/bin`, `/etc`, `/usr`) with instant path filtering.
- 🕒 **DNF Transaction History & One-Click Rollback**: Browse past system package installations, updates, and removals with the ability to safely undo transactions (`dnf history undo`).
- 🛡️ **Pre-Flight Dry-Run & Fedora Core Safety Guard**: Simulate queued transactions (`--assumeno`) before execution. Dendro warns you immediately if a critical system pillar (`kernel`, `systemd`, `gnome-shell`, `glibc`) is queued for accidental removal.
- 🏷️ **Smart 18+ Categorization Engine**: Automatically classifies packages into Desktop Apps, CLI Tools, Language Runtimes (Python, Rust, JVM, Node.js), Fedora Core Pillars, Kernel/DKMS Modules, C/C++ Libraries, Firmware, Themes, and Leaf Orphans.
- ⚡ **Asynchronous Non-Blocking Engine**: RPM database queries and transaction streams run in background threads (`QThreadPool`), keeping the Qt6 interface smooth and 100% responsive.
- 🔒 **Native Polkit Elevation**: Executes transactions securely via standard system authentication (`pkexec dnf5/dnf`) with real-time ANSI-cleansed terminal streaming.

---

## 📸 Screenshots

<div align="center">
  <img src="data/screenshots/main_window.png" alt="Dendro Main Interface" width="900">
</div>

---

## 🚀 Installation

### Option 1: Standalone Portable AppImage (Recommended)

Download the zero-dependency, self-contained binary from the [Releases](https://github.com/xyasharx/Dendro/releases) page:

```bash
# 1. Make the AppImage executable
chmod +x Dendro-x86_64.AppImage

# 2. Run directly
./Dendro-x86_64.AppImage
```

---

### Option 2: Run from Source

```bash
# 1. Clone the repository
git clone https://github.com/xyasharx/Dendro.git
cd Dendro

# 2. Install system dependencies on Fedora
sudo dnf install -y python3 python3-devel python3-pyqt6 polkit rpm dnf

# 3. Create and activate a virtual environment with system packages access
python3 -m venv --system-site-packages venv
source venv/bin/activate

# 4. Install requirements and run
pip install -r requirements.txt
python3 main.py
```

---

### Option 3: Native Fedora RPM via COPR *(⏳ Coming Soon)*

Official repository integration for automatic updates via `dnf` is currently being set up:

```bash
# [Coming Soon] Enable the Dendro repository
sudo dnf copr enable xyasharx/dendro -y

# [Coming Soon] Install Dendro
sudo dnf install dendro -y
```

---

### Option 4: Flathub Flatpak *(⏳ Coming Soon / Planned)*

Flatpak package distribution via Flathub is planned for upcoming releases:

```bash
# [Coming Soon] Install from Flathub
flatpak install flathub io.github.xyasharx.Dendro
flatpak run io.github.xyasharx.Dendro
```

---

## 🔍 Power Search Syntax

Dendro features real-time 250ms debounced search supporting specialized filter tokens:

| Search Query Example | Description |
| :--- | :--- |
| `firefox` | Searches package names, summaries, and descriptions. |
| `size:>100M` or `size:<50K` | Filters packages by installed disk size (`B`, `K`, `M`, `G`). |
| `repo:copr` | Filters packages installed from COPR repositories. |
| `repo:fusion` | Filters packages from RPM Fusion repositories. |
| `license:gpl` or `license:mit` | Filters packages by declared software license. |
| `status:orphan` | Isolates unneeded leaf dependencies (orphans). |
| `status:queued` | Displays all packages staged for installation or removal. |

---

## ⌨️ Keyboard Shortcuts

| Shortcut | Action |
| :--- | :--- |
| <kbd>Ctrl</kbd> + <kbd>F</kbd> | Focus the top search bar |
| <kbd>Ctrl</kbd> + <kbd>R</kbd> | Reload and re-index system RPM database |
| <kbd>Ctrl</kbd> + <kbd>H</kbd> | Open DNF Transaction History & Rollback dialog |
| <kbd>Ctrl</kbd> + <kbd>I</kbd> | Toggle Package Inspector side panel |
| <kbd>Space</kbd> | Toggle Install / Remove queue state for selected package |

---

## 🛠️ Architecture

Dendro follows a strict Model-View-Controller (MVC) architecture separating system RPM/DNF database operations from Qt presentation:

```text
dendro/
├── core/
│   ├── backend.py            # RPM/DNF queries, DAG resolver, Polkit runner & SQLite cache
│   └── models.py             # TreeItem, DependencyTreeModel & PackageFilterProxyModel
├── ui/
│   ├── delegates.py          # Vector indicator branch style & custom badge painters
│   ├── dry_run_dialog.py     # Pre-flight transaction simulation & system danger alert
│   ├── header.py             # Debounced power-search bar & queue trigger
│   ├── history_dialog.py     # DNF transaction history & rollback manager
│   ├── inspector_panel.py    # Tabbed inspector (Metadata, File tree, Reverse deps)
│   ├── main_window.py        # Central MVC application controller
│   ├── sidebar.py            # Categorized navigation with live counters
│   ├── styles.py             # Modern Catppuccin-inspired dark theme
│   └── transaction_drawer.py # Terminal console & progress bar drawer
├── data/
│   ├── icons/                # High-DPI application icons (128px, 256px, 512px)
│   ├── io.github.xyasharx.Dendro.desktop    # XDG desktop integration
│   ├── io.github.xyasharx.Dendro.metainfo.xml # AppStream metadata
│   └── org.dendro.policy     # Native Polkit action security policy
├── dendro.spec               # Production Fedora RPM packaging spec
└── main.py                   # High-DPI bootstrap & POSIX signal handlers
```

---

## 🧪 Running Automated Tests

Run the headless unit and model test suite:

```bash
pytest -v tests/
```

---

## 🤝 Contributing

Contributions, bug reports, and feature requests are welcome!

1. Fork the repository
2. Create your feature branch (`git checkout -b feature/AmazingFeature`)
3. Commit your changes (`git commit -m 'feat: add some AmazingFeature'`)
4. Push to the branch (`git push origin feature/AmazingFeature`)
5. Open a Pull Request

---

## 📄 License

This project is licensed under the **GNU General Public License v3.0 (GPL-3.0-or-later)**. See the [LICENSE](LICENSE) file for details.

---

<div align="center">
  <sub>Built with ❤️ for the Fedora Linux community.</sub>
</div>
