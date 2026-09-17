<div align="center">
<p align="center">
  <img src="data/icons/256x256/io.github.xyasharx.Dendro.png" width="110" height="110" alt="Dendro Logo">
</p>

# Dendro

### Graphical Package Manager & Dependency Tree Explorer for Fedora Linux

[![License: GPL v3](https://img.shields.io/badge/License-GPLv3-blue.svg)](https://www.gnu.org/licenses/gpl-3.0)
[![Fedora](https://img.shields.io/badge/Platform-Fedora%2040%2B%20%7C%20Rawhide-3c6eb4?logo=fedora&logoColor=white)](https://getfedora.org)
[![Python](https://img.shields.io/badge/Python-3.10%2B-blue?logo=python&logoColor=white)](https://www.python.org/)
[![GUI](https://img.shields.io/badge/GUI-PyQt6-41cd52?logo=qt&logoColor=white)](https://riverbankcomputing.com/software/pyqt/)
[![Packaging](https://img.shields.io/badge/AppImage-Available-success?logo=linux&logoColor=white)](https://github.com/xyasharx/Dendro/releases)
[![Fedora COPR](https://copr.fedorainfracloud.org/coprs/xyasharx/dendro/package/dendro/status_image/last_build.png)](https://copr.fedorainfracloud.org/coprs/xyasharx/dendro/)

<p align="center">
  <a href="#key-features">Key Features</a> •
  <a href="#installation">Installation</a> •
  <a href="#search-syntax">Search Syntax</a> •
  <a href="#keyboard-shortcuts">Shortcuts</a> •
  <a href="#architecture">Architecture</a> •
  <a href="#license">License</a>
</p>

---

</div>

**Dendro** is a graphical package manager and dependency explorer built for Fedora Linux. It provides an intuitive interface to navigate RPM dependency trees, check reverse dependencies ("what depends on this package?"), inspect installed file paths, identify orphan packages, and manage DNF transactions safely with Polkit authentication.

---

## Key Features

- **Interactive Dependency Tree:** Expand any package to inspect its full dependency chain, direct requirements, and virtual RPM capabilities in a collapsible tree view.
- **Reverse Dependency Explorer:** Check which installed packages depend on a specific library before removing it to prevent breaking system components.
- **File & Metadata Inspector:** View descriptions, architectures, packager info, and browse installed package files (`/usr`, `/etc`, `/bin`) with path filtering.
- **DNF History & Rollback:** Browse past package installations, updates, and removals with support for undoing transactions (`dnf history undo`).
- **Dry-Run & Safety Checks:** Simulate transactions (`--assumeno`) before execution. Dendro warns you if critical core components (`kernel`, `systemd`, `glibc`, `gnome-shell`) are queued for removal.
- **Package Categorization:** Filters packages into Desktop Apps, CLI Tools, Runtimes (Python, Rust, Node.js), System Core, Libraries, and unneeded leaf orphans.
- **Non-Blocking UI:** RPM queries and transaction streams run in background threads (`QThreadPool`) to keep the Qt6 interface responsive.
- **Polkit Privilege Handling:** Executes root transactions securely via system authentication (`pkexec dnf5/dnf`) with live terminal output.

---

## Screenshots

<div align="center">
  <img src="data/screenshots/main_window.png" alt="Dendro Main Interface" width="900">
</div>

---

## Installation

### Option 1: Native Fedora RPM via COPR (Recommended)

Enable the COPR repository and install Dendro via DNF:

```bash
# Enable the repository
sudo dnf copr enable xyasharx/dendro -y

# Install Dendro
sudo dnf install dendro -y

# Launch
dendro
```

---

### Option 2: Standalone Portable AppImage

Download the latest AppImage from the [Releases](https://github.com/xyasharx/Dendro/releases) page:

```bash
# Make the AppImage executable
chmod +x Dendro-x86_64.AppImage

# Run
./Dendro-x86_64.AppImage
```

---

### Option 3: Run from Source

```bash
# 1. Clone the repository
git clone https://github.com/xyasharx/Dendro.git
cd Dendro

# 2. Install dependencies on Fedora
sudo dnf install -y python3 python3-devel python3-pyqt6 polkit rpm dnf

# 3. Set up a virtual environment with system site packages
python3 -m venv --system-site-packages venv
source venv/bin/activate

# 4. Install requirements and run
pip install -r requirements.txt
python3 main.py
```

---

### Option 4: Flatpak *(Planned)*

Flatpak packaging is planned for future releases once host integration and system package permissions are finalized.

---

## Search Syntax

The search bar updates results in real time and supports the following filter prefixes:

| Query Example | Description |
| :--- | :--- |
| `firefox` | Searches package names, summaries, and descriptions |
| `size:>100M` or `size:<50K` | Filters packages by installed disk size (`B`, `K`, `M`, `G`) |
| `repo:copr` | Filters packages installed from COPR repositories |
| `repo:fusion` | Filters packages from RPM Fusion repositories |
| `license:gpl` or `license:mit` | Filters packages by software license |
| `status:orphan` | Displays unneeded leaf dependencies (orphans) |
| `status:queued` | Shows packages staged for installation or removal |

---

## Keyboard Shortcuts

| Shortcut | Action |
| :--- | :--- |
| <kbd>Ctrl</kbd> + <kbd>F</kbd> | Focus the search bar |
| <kbd>Ctrl</kbd> + <kbd>R</kbd> | Reload and re-index system RPM database |
| <kbd>Ctrl</kbd> + <kbd>H</kbd> | Open DNF Transaction History & Rollback dialog |
| <kbd>Ctrl</kbd> + <kbd>I</kbd> | Toggle Package Inspector side panel |
| <kbd>Space</kbd> | Toggle Install / Remove queue state for selected package |

---

## Architecture

Dendro separates backend RPM and DNF database interactions from the PyQt6 presentation layer:

```text
dendro/
├── core/
│   ├── backend.py            # RPM/DNF queries, dependency graph, Polkit runner & cache
│   └── models.py             # TreeItem, DependencyTreeModel & filter proxy models
├── ui/
│   ├── delegates.py          # Custom branch rendering and badge styling
│   ├── dry_run_dialog.py     # Transaction simulation and critical package warnings
│   ├── header.py             # Search input bar and queue triggers
│   ├── history_dialog.py     # DNF transaction history and rollback viewer
│   ├── inspector_panel.py    # Package metadata, file list, and reverse dependencies
│   ├── main_window.py        # Main window and UI controller
│   ├── sidebar.py            # Categorized navigation with live item counts
│   ├── styles.py             # Dark theme styling (Catppuccin Mocha)
│   └── transaction_drawer.py # Terminal console output and progress drawer
├── data/
│   ├── icons/                # High-DPI application icons
│   ├── io.github.xyasharx.Dendro.desktop      # Desktop entry file
│   ├── io.github.xyasharx.Dendro.metainfo.xml # AppStream metadata
│   └── org.dendro.policy       # Polkit policy for privileged actions
├── dendro.spec                 # Fedora RPM packaging spec
└── main.py                     # Entry point and signal handling
```

---

## Running Tests

Run the test suite with pytest:

```bash
pytest -v tests/
```

---

## Contributing

Bug reports, suggestions, and pull requests are welcome.

1. Fork the repository
2. Create your branch (`git checkout -b feature/your-feature`)
3. Commit your changes (`git commit -m 'Add feature'`)
4. Push to the branch (`git push origin feature/your-feature`)
5. Open a Pull Request

---

## License

This project is licensed under the **GNU General Public License v3.0 (GPL-3.0-or-later)**. See the [LICENSE](LICENSE) file for details.

---

<div align="center">
  <sub>Built for the Fedora Linux community.</sub>
</div>
