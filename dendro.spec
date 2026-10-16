Name:           dendro
Version:        2.11.0
Release:        1%{?dist}
Summary:        Fedora DNF5 daemon package manager and dependency inspector

License:        GPL-3.0-or-later
URL:            https://github.com/xyasharx/Dendro
Source0:        %{url}/archive/refs/tags/v%{version}.tar.gz#/%{name}-%{version}.tar.gz

BuildArch:       noarch

BuildRequires:  appstream
BuildRequires:  desktop-file-utils
BuildRequires:  python3-devel
BuildRequires:  python3-pyqt6-base >= 6.6.0
BuildRequires:  python3-pytest
BuildRequires:  python3-pytest-qt
BuildRequires:  python3-rpm
BuildRequires:  pyproject-rpm-macros
BuildRequires:  python3-setuptools >= 69
BuildRequires:  python3-wheel

# The application is an unprivileged D-Bus client.  dnf5daemon-server owns
# package-manager authority and invokes Polkit for privileged operations.
Requires:       dnf5daemon-server >= 5.2.7
Requires:       dnf5daemon-server-polkit
Requires:       polkit
Requires:       python3-pyqt6-base >= 6.6.0
Requires:       python3-rpm

%description
Dendro is an unprivileged PyQt package-management frontend for Fedora.

It resolves, reviews, and executes package transactions through the system
DNF5 daemon on org.rpm.dnf.v0. The daemon owns RPM database access and uses
Polkit for authorization. Dendro does not run dnf5 through a generic privileged
shell command. Repository-file deletion uses the installed, argument-validated
Dendro helper because that operation is not exposed by the daemon API.

This RPM is supported on Fedora systems with dnf5daemon-server. It is not a
portable package manager for arbitrary Linux distributions.

%prep
%autosetup -n Dendro-%{version}

%build
%pyproject_wheel

%install
%pyproject_install

install -Dpm 0755 tools/dendro-repo-helper.py \
    %{buildroot}%{_libexecdir}/dendro-repo-helper
install -Dpm 0644 data/org.dendro.policy \
    %{buildroot}%{_datadir}/polkit-1/actions/org.dendro.policy
install -Dpm 0644 data/io.github.xyasharx.Dendro.desktop \
    %{buildroot}%{_datadir}/applications/io.github.xyasharx.Dendro.desktop
install -Dpm 0644 data/io.github.xyasharx.Dendro.metainfo.xml \
    %{buildroot}%{_metainfodir}/io.github.xyasharx.Dendro.metainfo.xml
install -Dpm 0644 data/icons/256x256/io.github.xyasharx.Dendro.png \
    %{buildroot}%{_datadir}/icons/hicolor/256x256/apps/io.github.xyasharx.Dendro.png
install -Dpm 0644 io.github.xyasharx.Dendro.svg \
    %{buildroot}%{_datadir}/icons/hicolor/scalable/apps/io.github.xyasharx.Dendro.svg

%check
# Keep Qt settings and caches inside the build tree; tests must not read or
# modify the builder user's desktop state.
export QT_QPA_PLATFORM=offscreen
export PYTHONPATH=.
export XDG_CONFIG_HOME=%{_builddir}/dendro-xdg-config
export XDG_CACHE_HOME=%{_builddir}/dendro-xdg-cache
%{__python3} -m pytest -q tests

# Validate the exact files that will be packaged. Do not suppress validation
# failures: launcher and AppStream metadata are part of the runtime contract.
desktop-file-validate %{buildroot}%{_datadir}/applications/io.github.xyasharx.Dendro.desktop
appstreamcli validate --no-net %{buildroot}%{_metainfodir}/io.github.xyasharx.Dendro.metainfo.xml

%files
%license LICENSE
%doc README.md
%{python3_sitelib}/core/
%{python3_sitelib}/ui/
%{python3_sitelib}/main.py*
%{python3_sitelib}/__pycache__/main.*.pyc
%{python3_sitelib}/dendro-%{version}.dist-info/
%{_bindir}/dendro
%{_libexecdir}/dendro-repo-helper
%{_datadir}/polkit-1/actions/org.dendro.policy
%{_datadir}/applications/io.github.xyasharx.Dendro.desktop
%{_metainfodir}/io.github.xyasharx.Dendro.metainfo.xml
%{_datadir}/icons/hicolor/256x256/apps/io.github.xyasharx.Dendro.png
%{_datadir}/icons/hicolor/scalable/apps/io.github.xyasharx.Dendro.svg

%changelog
* Wed Oct 14 2026 Yashar <yashar@duck.com> - 2.10.1-1
- Release 2.10.1: DNF5 Repo Overrides Ingestion, Live Network Diagnostics & UI Alignment
- Ingested DNF5 drop-in repository overrides (/etc/dnf/repos.override.d/99-config_manager.repo)
- Fixed repository disabling persistence so unchecked repositories stay disabled across restarts
- Added sequential command queueing in PolkitTransactionRunner for rapid multi-repo toggling
- Added real-time network error diagnosis and failure reporting to the Check Mirrors action
- Fixed status bar freeze on 0 updates by displaying explicit up-to-date confirmation
- Harmonized Enable COPR button height, padding, and corner radius with the adjacent text input
- Eliminated pipe contention between stdout and stderr readers during live mirror checks

* Wed Oct 14 2026 Yashar <yashar@duck.com> - 2.10.0-1
- Release 2.10.0: Preferences Dialog, Live Mirror Telemetry, Two-Phase Progress & Search Engine
- Special thanks to Ricky Tigg for feature proposals on RPM Epoch display, search filter syntax, and issue #40 feedback
- Added dedicated Preferences & About dialog with Ctrl+, shortcut integration
- Added on-demand RPM Epoch tag display (%{epoch}:%{evr}) with reactive tree view updates
- Implemented real-time search filter syntax validation with dynamic color-coding (green=valid, purple=invalid)
- Added native QCompleter filter discovery for fast keyboard autocompletion
- Enabled unrestricted global search across all categories when typing in the search bar
- Resolved DNF5 JSON evr schema parsing mismatch, restoring upgrade target versions in tables and buttons
- Implemented Two-Phase DNF Progress Engine (Download 0-30%, Transaction 30-100%) and merged process channels
- Added real-time mirror URL and repository streaming in footer status bar during check-mirrors
- Implemented immediate post-transaction cache eviction so upgraded packages disappear instantly from updates view
- Scoped toolbar button styles to fix squashed dialog buttons and unescaped ampersand mnemonics

* Tue Oct 13 2026 Yashar <yashar@duck.com> - 2.9.0-1
- Release 2.9.0: Multi-Tier Removal Safety Engine, Responsive Layouts & Search Polish
- Introduced deterministic multi-tier removal safety engine (Critical, High, Medium, Low, Safe)
- Resolved false-positive critical removal warnings on user software (kate) and subpackages (rpm-build, kernel-headers)
- Added F11 fullscreen shortcut with maximized window state retention
- Added table header context menu to toggle column visibility on demand
- Restored table horizontal scrollbar and responsive default column widths
- Unlocked sidebar resizing down to 180px and added QSettings layout persistence for splitters and headers
- Added status:available search filter and multi-word remote repository search query synthesis
- Unified all header buttons to a strict 28px height baseline and scoped dialog button styling
- Added real privileged repository file deletion (rm -f) via Polkit
- Fixed inspector queue action button state synchronization on spacebar and context-menu toggles

* Mon Oct 12 2026 Yashar <yashar@duck.com> - 2.8.3-1
- Release 2.8.3: Architectural Hardening, Singleton Runner Lifecycle & Zero Polling
- Completely eliminated residual 500ms sigint_timer idle wakeups in main.py
- Refactored PolkitTransactionRunner into a persistent singleton service on MainWindow, eliminating object leaks
- Replaced 300ms magic sleep timer with event-driven singleShot(0) queue post for CLI file inspection
- Ensured consistent ISO millisecond timestamp formatting across all startup telemetry and debug logs
- Resolved ruff F821 static analysis scope validation

* Sun Oct 11 2026 Yashar <yashar@duck.com> - 2.8.2-1
- Release 2.8.2: Idle CPU Quiescence, Vector Theme Toggle, UI Metric Alignment & DNF5 Hardening
- Special thanks to Ricky Tigg for detailed diagnostic traces, telemetry feedback, and testing on issue #38
- Replaced 1200ms background polling loop with true event-driven D-Bus portal SettingChanged signals (0.0% idle CPU)
- Appended explicit newlines to dnf5 repoquery format string (%{name}\n) to prevent package string concatenation
- Canonicalized UsrMerge symlinked file paths (/bin, /lib64) in find_package_owning_file
- Anchored log cursor to document end in transaction terminal to prevent log scrambling during user text selection
- Fixed premature iterator teardown UnboundLocalError during reverse dependency worker cancellation
- Precompiled regex patterns in critical sorting, filtering, and live progress stream evaluation paths

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
