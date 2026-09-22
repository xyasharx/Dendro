Name:           dendro
Version:        1.4.1
Release:        1%{?dist}
Summary:        Visual package manager and dependency hierarchy explorer for Fedora Linux

License:        GPL-3.0-or-later
URL:            https://github.com/xyasharx/Dendro
Source0:        %{name}-%{version}.tar.gz

BuildArch:      noarch

BuildRequires:  python3-devel
BuildRequires:  pyproject-rpm-macros
BuildRequires:  python3-setuptools
BuildRequires:  python3-wheel
BuildRequires:  desktop-file-utils

# Runtime dependencies
Requires:       python3-pyqt6 >= 6.6.0
Requires:       python3-rpm
Requires:       (python3-libdnf5 or dnf5)
Requires:       polkit
Requires:       rpm
Requires:       (dnf5 or dnf)
Requires:       hicolor-icon-theme

%description
Dendro is a fast, graphical package manager and visual dependency explorer
designed specifically for Fedora Linux. It empowers users to inspect package
trees, remove orphaned libraries, and execute administrative actions safely
via native Polkit elevation, powered by native librpm and libdnf5 bindings.

%prep
%autosetup -n %{name}-%{version}
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

# Install Icons
install -D -m 0644 data/icons/128x128/io.github.xyasharx.Dendro.png %{buildroot}%{_datadir}/icons/hicolor/128x128/apps/io.github.xyasharx.Dendro.png
install -D -m 0644 data/icons/256x256/io.github.xyasharx.Dendro.png %{buildroot}%{_datadir}/icons/hicolor/256x256/apps/io.github.xyasharx.Dendro.png
install -D -m 0644 data/icons/512x512/io.github.xyasharx.Dendro.png %{buildroot}%{_datadir}/icons/hicolor/512x512/apps/io.github.xyasharx.Dendro.png

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
%{_datadir}/icons/hicolor/*/apps/io.github.xyasharx.Dendro.png

%changelog
* Tue Sep 22 2026 Yashar <yashar@duck.com> - 1.4.1-1
- Release 1.4.1
- Adapted execution footprint to Fedora 42+ unified /usr/bin and /usr/sbin layout
- Added /usr/libexec detection to isolate internal background helpers from user CLI tools
- Integrated FHS manual page section analysis (man8 daemons vs man1 user commands)
- Added automated test cases for unified binary execution and internal helpers

* Tue Sep 22 2026 Yashar <yashar@duck.com> - 1.4.0-1
- Release 1.4.0
- Introduced intelligent multi-factor semantic decision engine
- Added natural language domain profiling and physical anatomy extraction
- Added explainable classification confidence and rationale card to inspector panel
- Added direct root execution bypass for containerized environments
- Solved false-positive categorization for language CLI tools and firmware GUIs

* Sun Sep 20 2026 Yashar <yashar@duck.com> - 1.3.0-1
- Release 1.3.0
- Upgraded to native librpm and libdnf5 backend bindings
- Added L1 RAM + L2 SQLite WAL capability resolution caching
- Added dry-run simulation and system root component removal protection
- Fixed headless build validation in Mock/COPR

* Fri Aug 21 2026 Yashar <yashar@duck.com> - 1.2.0-1
- Fix line endings sanitation and universal AppStream validator
