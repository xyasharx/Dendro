%global srcname Dendro

Name:           dendro
Version:        2.8.1
Release:        1%{?dist}
Summary:        Visual package manager and dependency hierarchy explorer for Fedora Linux

License:        GPL-3.0-or-later
URL:            https://github.com/xyasharx/%{srcname}
Source0:        %{url}/archive/v%{version}/%{srcname}-%{version}.tar.gz

BuildArch:      noarch

BuildRequires:  python3-devel
BuildRequires:  pyproject-rpm-macros
BuildRequires:  python3-setuptools >= 61.0.0
BuildRequires:  python3-wheel
BuildRequires:  desktop-file-utils
BuildRequires:  librsvg2-tools

# Runtime dependencies
Requires:       python3-pyqt6 >= 6.6.0
Requires:       python3-rpm
Requires:       (python3-libdnf5 or dnf5)
Requires:       polkit
Requires:       rpm
Requires:       (dnf5 or dnf)
Requires:       hicolor-icon-theme
Requires:       (adwaita-icon-theme or breeze-icon-theme)
Recommends:     papirus-icon-theme

%description
Dendro is a fast, graphical package manager and visual dependency explorer
designed specifically for Fedora Linux. It empowers users to inspect package
trees, remove orphaned libraries, and execute administrative actions safely
via native Polkit elevation, powered by native librpm and libdnf5 bindings.

%prep
%autosetup -n %{srcname}-%{version}
find . -type f -exec sed -i 's/\r$//' {} +
%generate_buildrequires
%pyproject_buildrequires

%build
%pyproject_wheel

%install
%pyproject_install
%pyproject_save_files core ui main

# Install Desktop launcher
install -D -m 0644 data/io.github.xyasharx.Dendro.desktop %{buildroot}%{_datadir}/applications/io.github.xyasharx.Dendro.desktop

# Install Polkit Security Action
install -D -m 0644 data/org.dendro.policy %{buildroot}%{_datadir}/polkit-1/actions/org.dendro.policy

# Install AppStream Metadata
install -D -m 0644 data/io.github.xyasharx.Dendro.metainfo.xml %{buildroot}%{_metainfodir}/io.github.xyasharx.Dendro.metainfo.xml

# Install Scalable Vector Icon (Preferred by KDE Plasma and GNOME)
install -D -m 0644 io.github.xyasharx.Dendro.svg %{buildroot}%{_datadir}/icons/hicolor/scalable/apps/io.github.xyasharx.Dendro.svg

# Generate fresh, uncorrupted PNG icons directly from SVG source
for size in 128 256 512; do
    mkdir -p %{buildroot}%{_datadir}/icons/hicolor/${size}x${size}/apps
    rsvg-convert -w ${size} -h ${size} io.github.xyasharx.Dendro.svg -o %{buildroot}%{_datadir}/icons/hicolor/${size}x${size}/apps/io.github.xyasharx.Dendro.png
done

%check
desktop-file-validate %{buildroot}%{_datadir}/applications/io.github.xyasharx.Dendro.desktop

if command -v appstreamcli &> /dev/null; then
    appstreamcli validate --no-net %{buildroot}%{_metainfodir}/io.github.xyasharx.Dendro.metainfo.xml || true
elif command -v appstream-util &> /dev/null; then
    appstream-util validate-relax --nonet %{buildroot}%{_metainfodir}/io.github.xyasharx.Dendro.metainfo.xml || true
fi

%files -f %{pyproject_files}
%license LICENSE
%doc README.md
%{_bindir}/dendro
%{_datadir}/applications/io.github.xyasharx.Dendro.desktop
%{_datadir}/polkit-1/actions/org.dendro.policy
%{_metainfodir}/io.github.xyasharx.Dendro.metainfo.xml
%{_datadir}/icons/hicolor/*/apps/io.github.xyasharx.Dendro.*

%changelog
* Sat Oct 10 2026 Yashar <yashar@duck.com> - 2.8.1-1
- Release 2.8.1: Header Metric Alignment, Vector Theme Toggle & Toolbar Polish
- Resolved dark-mode theme toggle visibility bug caused by Adwaita hardcoded charcoal fills
- Introduced resolution-independent vector Sun and Moon rendering via QPainterPath
- Standardized all header buttons to a uniform 30px outer height, eliminating button height discrepancies
- Streamlined header updates view by removing redundant Show All Packages button
- Implemented defensive _find_theme_icon lookup verifying non-null QIcon state across candidate themes

* Thu Oct 08 2026 Yashar <yashar@duck.com> - 2.8.0-1
- Release 2.8.0: Remote Software Installation, File Ownership Lookup & Fast Startup
- Added on-demand remote package search and installation from enabled DNF repositories
- Integrated native rpm -qf file path lookup into search bar (/usr/bin/foo)
- Added double-click Jump to Package navigation in dependency tree and reverse-dep lists
- Added maintainer scriptlet audit tab (%pre, %post) in inspector panel
- Added Copy Dependency Chain clipboard action in right-click context menu
- Resolved 9-second startup delay via in-memory AppStream iterparse root cleanup
- Fixed GNOME default color-scheme mapping to light mode and removed titlebar flicker
- Streamlined to 2 high-contrast themes (dark/light) with 1-click header toggle button
- Synchronized console and table backgrounds dynamically across light and dark themes
- Fixed package removal UI synchronization and cached sidebar delegate palette for smooth 60fps scrolling

* Wed Oct 07 2026 Yashar <yashar@duck.com> - 2.7.1-1
- Release 2.7.1: GTK 3 Theme Startup Race Condition Fix & Monospace Console Polish
- Fixed GTK 3 dark theme startup race condition where Qt6 Wayland portal reported light mode before D-Bus handshake completion
- Fixed duplicate portal registration error (Connection already associated with an application ID) by configuring desktop file name prior to QApplication instantiation
- Applied native system monospace font (FixedFont) to the dry-run simulation console, repository table IDs, and transaction history commands
- Cleaned up duplicate timer initialization in main.py and fixed dry-run dialog indentation syntax

* Wed Oct 07 2026 Yashar <yashar@duck.com> - 2.7.0-1
- Release 2.7.0: System Typography Inheritance, Taxonomy Precision & Navigation Polish
- Inherited desktop environment point sizes from app.font() to respect accessibility scaling
- Stripped hardcoded pixel font sizes across stylesheets, dialogs, and item delegates
- Switched package versions, history commands, and dry-run console to system monospace font
- Fixed contextual action buttons failing to activate when opening updates from header
- Added direct return button in header to exit updates view and restore full package list
- Resolved plasma-desktop misclassification into Web Browsers via FHS shell containment checks
- Disambiguated generic Network category so network wizards route to settings/utilities
- Resolved kamera and dendro self-classification into Themes & Visual Assets
- Routed standalone Python CLI tools (ansible, meson, certbot) to cli_tools instead of libraries
- Routed standalone documentation (man-pages, git-doc) to devel instead of C shared libraries
- Decoupled TreeItem parent back-references on clear to prevent circular memory retention
- Added defensive busy cursor management to prevent wait cursor stacking and freezes
- Adopted adaptive window geometry to prevent window spillover on laptop displays

* Tue Oct 06 2026 Yashar <yashar@duck.com> - 2.6.0-1
- Release 2.6.0: GTK 3 Dark Mode, F45 DNF5 Relocation & Desktop Shell Taxonomy Fix
- Special thanks to Marcin Juszkiewicz for Fedora 45 testing and feedback, and Ricky-Tigg for reporting #38
- Resolved Issue #38: Added multi-tier GTK 3 dark theme detection across desktop settings
- Fixed CodeQL Alert #2: Safe repository host validation via urllib.parse.urlsplit
- Resolved desktop shell taxonomy collision routing plasma-desktop to desktop_addons
- Added Fedora 45 repository directory migration support (/usr/share/dnf5/repos.d/)
- Added third-party repository classification and repository configuration deletion
- Replaced generic satisfied dependency label with exact installed provider versions
- Disconnected right-panel Re-Scan from mutating the main window dependency tree
- Added sidebar toggle control (Ctrl+B), header filter reset, and error-safe busy cursor
- Added description paragraph reflowing and cleaned up taxonomy inspector card layout

* Sun Oct 04 2026 Yashar <yashar@duck.com> - 2.5.1-1
- Release 2.5.1: Capability-Driven Classification, Reverse Dep Sonames & Safety Parser
- Resolved silent reverse dependency failure by querying provided sonames and capabilities
- Fixed multiline dry-run removal parser to catch all protected packages in DNF removal table
- Isolated Qt5/Qt6 plugins from media codecs, routing them cleanly to gui_toolkits
- Expanded AppStream catalog indexing to active DNF/libdnf5 repository caches (COPR, RPM Fusion)
- Integrated RPM virtual capability extraction (python3dist, crate, gstreamer1, mvn, npm)
- Cleaned up test suite with regression tests for multiline removals and toolkit isolation

* Sun Oct 04 2026 Yashar <yashar@duck.com> - 2.5.0-1
- Release 2.5.0: The Deterministic Overhaul, System Protection Decoupling & Queue Polish
- Decoupled system protection checks from fedora_core to prevent category hijacking
- Correctly route PAM, shadow-utils, audit, polkit, and firewalld into security_pkgs
- Restored kernel-modules to hardware drivers while preserving is_protected status
- Implemented O(1) PACKAGE_TAXONOMY_OVERRIDES for constant-time edge-case resolution
- Added RPM DIRINDEXES validation to guarantee only /usr/share/applications launchers qualify
- Fixed sidebar counter pill double-counting across subcategories
- Completed QUEUED_UPGRADE workflow: proxy visibility, drawer execution, and context menu actions
- Added URL scheme sanitization and defensive RPM header timestamp parsing

* Sat Oct 03 2026 Yashar <yashar@duck.com> - 2.4.0-1
- Release 2.4.0: Live Mirror Refresh, DNF Cache Cleaner & Streamlined Navigation
- Added live repository synchronization with --refresh flag in update checker
- Integrated DNF package cache cleaner into Repositories dialog (dnf clean all)
- Streamlined sidebar categories and removed redundant storage audit channel
- Fixed indentation syntax issue in sidebar count aggregator

* Wed Sep 30 2026 Yashar <yashar@duck.com> - 2.3.0-1
- Release 2.3.0: Selective Upgrades, Queue Discard & UI Contrast Fix
- Added selective per-package upgrade staging and execution (QUEUED_UPGRADE)
- Added one-click queue discard action to revert staged changes
- Fixed theme background inversion bug in tree view via DendroTreeView
- Removed empty expansion chevrons from packages with zero dependencies
- Fixed taskbar and tray icon resolution to display application logo

* Tue Sep 29 2026 Yashar <yashar@duck.com> - 2.2.0-1
- Release 2.2.0: In-App Upgrades, Local RPM Handler & UI Overhaul
- Added in-app system upgrade execution (dnf upgrade) with live terminal logs
- Added batch leaf orphan cleanup (dnf autoremove) via Polkit elevation
- Registered MIME association for local .rpm files with in-process header inspector
- Integrated system tray icon with background update checks and desktop notifications
- Overhauled sidebar with right-aligned counter pills and typographic section headers
- Modernized tree view with micro-bordered status badges and split-color upgrade arrows

* Mon Sep 28 2026 Yashar <yashar@duck.com> - 2.1.0-1
- Release 2.1.0: The Deterministic Taxonomy & Core Pipeline Overhaul
- Replaced heuristic scoring engine with deterministic two-phase classification pipeline
- Decoupled physical delivery form factors (FHS manifests) from functional domains (XDG specs)
- Resolved application launcher promotion for autostart, Akonadi, and background desktop files
- Fixed CLI tool categorization for packages with /usr/libexec helpers (7zip)
- Corrected categorization for headless Java runtimes and auxiliary help packages
- Purged dead heuristic code and synchronized test suite for 100% deterministic coverage

* Mon Sep 28 2026 Yashar <yashar@duck.com> - 2.0.3-1
- Release 2.0.3: Tarball Source and Extraction Directory Alignment
- Fixed uppercase tarball source filename and extraction directory mismatch (Dendro vs dendro)
- Added srcname macro to ensure clean local rpmbuild and mock builds from GitHub release tarballs

* Sun Sep 27 2026 Yashar <yashar@duck.com> - 2.0.1-1
- Release 2.0.1: The Two-Tier System Taxonomy & Visual Contrast Release
- Introduced full two-tier hierarchical taxonomy across 6 core system pillars
- Added granular FreeDesktop XDG subcategories for desktop applications
- Added POSIX operational subcategories for command-line utilities
- Integrated automated ingestion of Fedora comps.xml distribution compose metadata
- Resolved light and dark theme icon contrast issues with dynamic QPalette synchronization
- Assigned distinct vector icons for Rust, Java, and Node.js runtimes
- Integrated native in-process system removal safety auditor
- Purged all raw unicode emoji fonts and glyphs to guarantee Fontconfig crash immunity

* Sun Sep 27 2026 Yashar <yashar@duck.com> - 1.9.4-2
- Release 1.9.4: Native FreeDesktop Icon Migration & Fontconfig Crash Fix
- Fixed C-level segmentation fault (SIGSEGV) in libfontconfig (FcCharSetFindLeafForward) on startup (#32)
- Replaced all raw unicode font emojis across buttons, dialogs, and sidebar with native FreeDesktop QIcon theme icons
- Switched close buttons to native window-close vector icons
- Prevented Fontconfig glyph shaping crashes during QPushButton layout size calculations
