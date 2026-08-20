Name:           dendro
Version:        1.0.0
Release:        1%{?dist}
Summary:        Visual package manager and dependency hierarchy explorer for Fedora Linux

License:        GPL-3.0-or-later
URL:            https://github.com/xyasharx/Dendro
Source0:        %{url}/archive/v%{version}/%{name}-%{version}.tar.gz

BuildArch:      noarch

# Build Dependencies
BuildRequires:  python3-devel
BuildRequires:  pyproject-rpm-macros
BuildRequires:  python3-setuptools
BuildRequires:  python3-wheel
BuildRequires:  desktop-file-utils
BuildRequires:  libappstream-glib

# Runtime Dependencies
Requires:       python3-pyqt6 >= 6.6.0
Requires:       polkit
Requires:       rpm
Requires:       dnf
Requires:       hicolor-icon-theme

%description
Dendro is a fast, graphical package manager and visual dependency explorer
designed specifically for Fedora Linux. It empowers users to inspect package
trees, remove orphaned libraries, and execute administrative actions safely
via native Polkit elevation.

%prep
%autosetup -n dendro-%{version}

%generate_buildrequires
%pyproject_buildrequires

%build
%pyproject_wheel

%install
%pyproject_install

# Install Executable Entrypoint Wrapper
install -d %{buildroot}%{_bindir}
cat << 'EOF' > %{buildroot}%{_bindir}/dendro
#!/usr/bin/python3
import sys
from main import main
if __name__ == "__main__":
    sys.exit(main())
EOF
chmod 0755 %{buildroot}%{_bindir}/dendro

# Install Desktop Integration File
install -D -m 0644 data/dendro.desktop %{buildroot}%{_datadir}/applications/dendro.desktop

# Install Polkit Security Action
install -D -m 0644 data/org.dendro.policy %{buildroot}%{_datadir}/polkit-1/actions/org.dendro.policy

# Install AppStream Metainfo
install -D -m 0644 data/io.github.xyasharx.Dendro.metainfo.xml %{buildroot}%{_metainfodir}/io.github.xyasharx.Dendro.metainfo.xml

# Install Application Icon
install -D -m 0644 data/icons/128x128/io.github.xyasharx.Dendro.png %{buildroot}%{_datadir}/icons/hicolor/128x128/apps/io.github.xyasharx.Dendro.png

%check
# Validate Desktop File and AppStream Metadata
desktop-file-validate %{buildroot}%{_datadir}/applications/dendro.desktop
appstream-util validate-relax --nonet %{buildroot}%{_metainfodir}/io.github.xyasharx.Dendro.metainfo.xml

%files
%license LICENSE
%doc README.md
%{_bindir}/dendro
%{python3_sitelib}/core/
%{python3_sitelib}/ui/
%{python3_sitelib}/main.py
%{python3_sitelib}/__pycache__/
%{python3_sitelib}/*.dist-info/
%{_datadir}/applications/dendro.desktop
%{_datadir}/polkit-1/actions/org.dendro.policy
%{_metainfodir}/io.github.xyasharx.Dendro.metainfo.xml
%{_datadir}/icons/hicolor/128x128/apps/io.github.xyasharx.Dendro.png

%changelog
* Thu Aug 20 2026 Yashar <ymz1376@gmail.com> - 1.0.0-1
- Initial production-ready release with DAG dependency resolution and Polkit integration
