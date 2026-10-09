<div align="center">
<p align="center">
  <img src="io.github.xyasharx.Dendro.svg" width="110" height="110" alt="Dendro Logo">
</p>

# Dendro

### Native Graphical Package Manager and Dependency Tree Explorer for Fedora Linux

[![License: GPL v3](https://img.shields.io/badge/License-GPLv3-blue.svg)](https://www.gnu.org/licenses/gpl-3.0)
[![Fedora](https://img.shields.io/badge/Platform-Fedora%2040%2B%20%7C%20Rawhide-3c6eb4?logo=fedora&logoColor=white)](https://getfedora.org)
[![Python](https://img.shields.io/badge/Python-3.12%2B-blue?logo=python&logoColor=white)](https://www.python.org/)
[![GUI](https://img.shields.io/badge/GUI-PyQt6-41cd52?logo=qt&logoColor=white)](https://riverbankcomputing.com/software/pyqt/)
[![Packaging](https://img.shields.io/badge/AppImage-Available-success?logo=linux&logoColor=white)](https://github.com/xyasharx/Dendro/releases)
[![Fedora COPR](https://copr.fedorainfracloud.org/coprs/xyasharx/dendro/package/dendro/status_image/last_build.png)](https://copr.fedorainfracloud.org/coprs/xyasharx/dendro/)

<p align="center">
  <a href="#key-features">Key Features</a> |
  <a href="#taxonomy-model">Taxonomy Model</a> |
  <a href="#architecture">Architecture</a> |
  <a href="#installation">Installation</a> |
  <a href="#search-syntax">Search Syntax</a> |
  <a href="#keyboard-shortcuts">Shortcuts</a> |
  <a href="#running-tests">Testing</a> |
  <a href="#license">License</a>
</p>

---

</div>

**Dendro** is a graphical package manager and dependency hierarchy inspector built for Fedora Linux. Powered by native **`librpm`** and **`libdnf5`** bindings, it provides in-process database queries, on-demand remote repository software search and installation, interactive dependency tree traversal, reverse dependency discovery across shared library sonames, file path ownership resolution (`rpm -qf`), file integrity audits (`rpm -V`), maintainer scriptlet auditing (`%pre`, `%post`), local RPM changelogs with CVE links, repository management, and privileged DNF transactions via Polkit.

---

## Key Features

- **Native librpm and libdnf5 Bindings:** Interrogates the local RPM database and DNF5 solver sacks directly in-process via C and C++ bindings under reentrant thread locks (`RPM_GLOBAL_LOCK`), eliminating the latency and fragility of parsing terminal output.
- **On-Demand Remote Package Search & Installation:** Search for uninstalled packages across enabled Fedora Core, RPM Fusion, and COPR repositories without preloading 75,000 packages into memory. Results can be inspected, queued, tested in dry-run simulation, and installed via Polkit with streaming terminal logs.
- **Native File Ownership Resolution (`rpm -qf`):** Enter any file path (e.g. `/usr/bin/git` or `/usr/lib64/libc.so.6`) or use the `file:` prefix directly in the search bar. Dendro executes a sub-millisecond B-Tree index query via `librpm` and selects the owning package in the tree.
- **Interactive "Jump to Package" Navigation:** Double-click any dependency in the main tree or any dependent package in the "Required By" list to navigate directly to that package's entry, automatically adjusting sidebar filters if the destination resides in another system pillar.
- **Two-Phase Deterministic Taxonomy:** Resolves Fedora's package metadata gap by decoupling physical delivery form factors (POSIX / FHS manifests) from functional domains (FreeDesktop XDG standards) across 6 core pillars without heuristic scoring or string guessing.
- **Decoupled System Protection & Removal Safety:** Separates root protection audits (`/etc/dnf/protected.d/`) from category classification. Components like PAM, Polkit, FirewallD, and kernel modules remain in their functional domains (Security, Hardware) while independently tracking protected status (`is_protected`) to warn before removal.
- **Constant-Time Upstream Overrides:** Resolves multi-role and atypical packaging layouts (`pipewire`, `wireplumber`, `mesa-dri-drivers`, `7zip`, `dendro`, desktop environment shells) in $O(1)$ time via an explicit taxonomy table, avoiding ad-hoc inline conditional logic.
- **Strict Launcher Validation via RPM DIRINDEXES:** Verifies desktop entry directory indices directly from RPM headers. Only files physically delivered to `/usr/share/applications/` qualify packages as interactive GUI applications, preventing autostart entries (`/etc/xdg/autostart/`) and internal background agents from causing misclassification.
- **Virtual Capability Ingestion:** Extracts standardized RPM capability contracts (`Provides:`) to accurately route Python distributions (`python3dist(...)`), Rust crates (`crate(...)`), GStreamer plugins (`gstreamer1(...)`), and Java artifacts (`mvn(...)`, `osgi(...)`).
- **Complete Reverse Dependency Resolution:** Discovers dependent packages by querying all capabilities and ELF `.so` sonames provided by a target package (e.g., `libssl.so.3()(64bit)`), rather than checking only literal package names.
- **Maintainer Scriptlet Audit Tab:** Audits pre-install, post-install, pre-uninstall, and post-uninstall shell scriptlets (`%pre`, `%post`, `%preun`, `%postun`) directly from RPM headers in fixed-width monospace font before installation.
- **Multiline Dry-Run Removal Safety Auditor:** Parses simulated transaction tables line-by-line across multi-package removal blocks, ensuring critical root pillars (`kernel`, `systemd`, `glibc`, `NetworkManager`) are flagged regardless of their line position.
- **Selective Upgrade Staging:** Allows individual package upgrades to be staged (`QUEUED_UPGRADE`), retained across category filters and search queries, and committed directly from the terminal console drawer.
- **File Integrity Verification (`rpm -V`):** Runs cryptographic digest, file size, mode, and timestamp audits directly from the package manifest inspector, identifying modified or missing files.
- **Local RPM Changelog with CVE Linking:** Reads maintainer release notes directly from local RPM headers, automatically hyperlinking Red Hat Security Database CVE numbers and Bugzilla issue references.
- **Two-Tier Capability Cache:** Uses an in-memory L1 cache backed by a persistent SQLite WAL database (`~/.cache/dendro/capabilities_v5.db`) to cache capability and provider lookups across sessions.
- **Sub-Second Startup Optimization:** Uses streaming XML root memory clearance during AppStream catalog ingestion, loading the entire package tree in under 500 milliseconds.
- **High-Contrast 1-Click Theme Engine:** Features two curated high-contrast palettes (Dark and Light) with a 1-click header toggle button, automatic desktop portal synchronization, and dynamic contrast adaptation for terminal consoles and tables.
- **Polkit Privilege Elevation:** Executes administrative tasks (`pkexec dnf5/dnf`) through an integrated drawer terminal with real-time ANSI log streaming and progress tracking.

---

## Taxonomy Model

Package managers on Fedora face a structural classification challenge:

- **Fedora Packaging Baseline:** Fedora deprecated the RPM `Group:` tag in 2012 (Fedora 17). Packages contain no canonical category tag in their RPM headers.
- **Comps Scope:** Distribution `comps.xml` metadata groups software primarily for operating system installation tasks (`@c-development`, `@gnome-desktop`), omitting standalone packages.
- **AppStream Scope:** FreeDesktop AppStream metadata targets end-user graphical applications, leaving libraries, system daemons, hardware drivers, and terminal utilities unrepresented.

### Two-Phase Classification Pipeline

Dendro addresses this by decoupling the **Physical Archetype** (what a package physically installs) from the **Functional Domain** (its operational role):

```text
[ RPM Package Manifest & Virtual Capabilities ]
                       │
                       ▼
┌─────────────────────────────────────────────────────────────┐
│ Phase 1: Physical Delivery Archetype (FHS Filesystem Audit) │
│ - O(1) Overrides    (PACKAGE_TAXONOMY_OVERRIDES map)        │
│ - Core System       (Base OS: glibc, systemd, dnf5, rpm)    │
│ - Hardware Driver   (/usr/lib/modules/, /usr/lib/firmware/) │
│ - Desktop App       (/usr/share/applications/ + DIRINDEXES) │
│ - System Daemon     (/usr/lib/systemd/ service units)       │
│ - Development SDK   (/usr/include/, *.pc files, -devel)     │
│ - GUI Toolkit       (/qt6/plugins, /gtk-3.0, toolkit libs)  │
│ - Media Codec       (/gstreamer-1.0, /vlc/plugins, codecs)  │
│ - Language Runtime  (Python site-packages, Rust, JVM, Node) │
│ - CLI Utility       (/usr/bin/ command in PATH)             │
│ - Shared Library    (/usr/lib64/ ELF .so fallback)          │
│ - Static Assets     (Fonts, themes, icon sets, locales)     │
└──────────────────────────────┬──────────────────────────────┘
                               │
                               ▼
┌─────────────────────────────────────────────────────────────┐
│ Phase 2: Functional Domain (FreeDesktop XDG / POSIX Roles)  │
│ - Desktop Apps  --> XDG Main Categories                     │
│                     (Internet, Multimedia, Graphics, etc.)  │
│ - CLI Tools     --> POSIX Operational Roles                 │
│                     (Editors, Shells, Archiving, Net, etc.) │
│ - Runtimes      --> Virtual Capabilities & Ecosystems       │
│                     (python3dist, crate, mvn, npm, so)      │
└─────────────────────────────────────────────────────────────┘
```

Interactive desktop applications are identified through physical `/usr/share/applications/` launcher manifests first, preventing media players and visual tools from being misclassified into low-level driver or service categories.

---

## Architecture

<div align="center">
  <img src="docs/architecture.svg" alt="Dendro Architecture and Subsystem Data Flow" width="100%">
</div>

Dendro executes native `librpm` and `libdnf5` operations across asynchronous worker threads (`QThreadPool`) to prevent blocking the Qt event loop:

```text
dendro/
├── core/
│   ├── backend.py            # Native librpm/libdnf5 bindings, taxonomy engine & worker threads
│   └── models.py             # DependencyTreeModel, PackageFilterProxyModel & TreeItem nodes
├── ui/
│   ├── delegates.py          # Vector branch rendering, status badges & upgrade paths
│   ├── dry_run_dialog.py     # Transaction simulation & multiline protected pillar warning
│   ├── header.py             # Search input, 1-click theme toggle, updates review & action bar
│   ├── history_dialog.py     # DNF transaction history & rollback viewer
│   ├── inspector_panel.py    # Metadata, rpm -V audit, scriptlets (%pre/%post), CVEs & reverse deps
│   ├── main_window.py        # Main window controller, thread pool management & tray integration
│   ├── repo_dialog.py        # Repository manager, COPR channel enabler & cache cleaner
│   ├── sidebar.py            # Two-tier navigation sidebar with live category counters
│   ├── styles.py             # High-contrast palettes, dynamic QSS builder & desktop portal sync
│   └── transaction_drawer.py # Terminal console drawer for real-time Polkit execution logs
├── data/
│   ├── icons/                # Vector and high-DPI raster application icons
│   ├── io.github.xyasharx.Dendro.desktop      # Desktop entry file
│   ├── io.github.xyasharx.Dendro.metainfo.xml # AppStream 1.0+ specification metadata
│   └── org.dendro.policy       # Polkit policy definition for privileged actions
├── dendro.spec                 # Fedora RPM packaging specification
└── main.py                     # Application entry point, icon search paths & exception handler
```

---

## Screenshots

<div align="center">
  <img src="data/screenshots/main_window.webp" alt="Dendro Dark Mode" width="900">

  <details>
    <summary><b>Click to view Light Theme Screenshot</b></summary>
    <br>
    <img src="data/screenshots/main_window_light.webp" alt="Dendro Light Mode" width="900">
  </details>
</div>

---

## Installation

### Option 1: Native Fedora RPM via COPR (Recommended)

Enable the repository and install Dendro via DNF:

```bash
# Enable the repository
sudo dnf copr enable xyasharx/dendro -y

# Install Dendro
sudo dnf install dendro -y

# Launch
dendro
```

---

### Option 2: Standalone AppImage

Download the latest AppImage from the [Releases](https://github.com/xyasharx/Dendro/releases) page:

```bash
chmod +x Dendro-x86_64.AppImage
./Dendro-x86_64.AppImage
```

---

### Option 3: Run from Source

```bash
# 1. Clone the repository
git clone https://github.com/xyasharx/Dendro.git
cd Dendro

# 2. Install dependencies on Fedora
sudo dnf install -y python3 python3-pyqt6 python3-rpm python3-libdnf5 polkit rpm dnf5 adwaita-icon-theme

# 3. Launch
python3 main.py
```

---

## Search Syntax

The search bar filters installed packages in real time using a 250ms debounced input, resolves file ownership directly, and queries remote DNF repositories on demand:

| Query Example | Description |
| :--- | :--- |
| `firefox` | Matches package names, summaries, and descriptions |
| `/usr/bin/git` or `file:/usr/lib64/libc.so.6` | Performs native `rpm -qf` file ownership lookup and selects the owning package |
| Pressing <kbd>Enter</kbd> (e.g. `blender`) | Queries remote DNF repositories for uninstalled packages to review and install |
| `type:gui` or `type:cli` | Filters by interface form factor (Desktop App vs CLI Tool vs Daemon) |
| `pillar:apps` or `pillar:hardware` | Filters by primary system pillar |
| `sub:desktop_internet` | Filters by granular subcategory key |
| `status:update` or `status:upgradable` | Shows installed packages with pending updates available |
| `status:user` or `status:manual` | Shows packages explicitly installed by the user |
| `status:orphan` | Displays unneeded leaf dependencies |
| `status:queued` | Shows packages staged for installation, removal, or upgrade |
| `arch:x86_64` or `arch:noarch` | Filters packages by target CPU architecture |
| `cat:graphics_drivers` or `cat:themes` | Filters packages by specific category identifier |
| `tag:python` or `tag:rust` | Filters packages by programming language ecosystem |
| `size:>100M` or `size:<50K` | Filters packages by installed disk size (`B`, `K`, `M`, `G`) |
| `repo:copr` | Filters packages installed from COPR repositories |
| `repo:fusion` | Filters packages from RPM Fusion channels |
| `license:gpl` or `license:mit` | Filters packages by software license identifier |

---

## Keyboard Shortcuts

| Shortcut | Action |
| :--- | :--- |
| <kbd>Ctrl</kbd> + <kbd>F</kbd> | Focus the search bar |
| <kbd>Ctrl</kbd> + <kbd>B</kbd> | Toggle / restore the category sidebar |
| <kbd>Ctrl</kbd> + <kbd>R</kbd> | Reload and re-index the system RPM database |
| <kbd>Ctrl</kbd> + <kbd>H</kbd> | Open DNF Transaction History & Rollback dialog |
| <kbd>Ctrl</kbd> + <kbd>I</kbd> | Toggle Package Inspector side panel |
| <kbd>Enter</kbd> | Trigger on-demand remote repository search for query text |
| <kbd>Space</kbd> | Toggle Install / Remove / Upgrade queue state for selected package |
| Double-Click | Jump directly to package definition from dependency tree or Required By list |

---

## Running Tests

Install test dependencies and run the test suite headlessly:

```bash
sudo dnf install -y python3-pytest python3-pytest-qt
QT_QPA_PLATFORM=offscreen pytest -v tests/
```

To run static analysis checking for undefined variables or missing imports:

```bash
pip install ruff
ruff check . --select F821,F822,F823
```

---

## Contributing

Bug reports, technical suggestions, and pull requests are welcome.

1. Fork the repository
2. Create your branch (`git checkout -b feature/your-feature`)
3. Commit your changes (`git commit -m 'Add feature'`)
4. Push to the branch (`git push origin feature/your-feature`)
5. Open a Pull Request

---

## License

This project is licensed under the **GNU General Public License v3.0 (GPL-3.0-or-later)**. See the [LICENSE](LICENSE) file for details.
