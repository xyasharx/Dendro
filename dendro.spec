Name:           dendro
Version:        1.0.0
Release:        1%{?dist}
Summary:        Modern graphical package manager with visual dependency trees

License:        GPL-3.0-or-later
URL:            https://github.com/xyasharx/dendro
Source0:        %{url}/archive/v%{version}/%{name}-%{version}.tar.gz

BuildArch:      noarch

# Build Dependencies
BuildRequires:  python3-devel
BuildRequires:  python3-pip
BuildRequires:  python3-setuptools
BuildRequires:  python3-wheel
BuildRequires:  desktop-file-utils
BuildRequires:  libappstream-glib

# Runtime Dependencies
Requires:       python3-pyqt6 >= 6.6.0
Requires:       python3-gobject
Requires:       polkit
Requires:       rpm
Requires:       dnf
Requires:       hicolor-icon-theme

%description
Fedora Package Tree is a fast graphical package manager for Fedora Linux.
It combines the clean workflow of Arch Linux's Pamac with an expandable
multi-level dependency tree viewer, orphan cleanup, and secure Polkit integration.

%prep
%autosetup -n dendro-%{version}

%generate_buildrequires
%pyproject_buildrequires

%build
%pyproject_wheel

%install
%pyproject_install

# Install Entrypoint Wrapper into /usr/bin
install -d %{buildroot}%{_bindir}
cat << 'EOF' > %{buildroot}%{_bindir}/dendro
#!/usr/bin/python3
from main import main
if __name__ == "__main__":
    main()
EOF
chmod 0755 %{buildroot}%{_bindir}/dendro

# Install Desktop Entry
install -D -m 0644 data/dendro.desktop %{buildroot}%{_datadir}/applications/dendro.desktop

# Install Polkit Security Policy
install -D -m 0644 data/org.dendro.policy %{buildroot}%{_datadir}/polkit-1/actions/org.dendro.policy

# Install AppStream Metainfo
install -D -m 0644 data/io.github.xyasharx.Dendro.metainfo.xml %{buildroot}%{_metainfodir}/io.github.xyasharx.Dendro.metainfo.xml

# Install Application Icon
install -D -m 0644 data/icons/128x128/io.github.xyasharx.Dendro.png %{buildroot}%{_datadir}/icons/hicolor/128x128/apps/io.github.xyasharx.Dendro.png

%check
# Validate Desktop Entry & AppStream Metadata
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
* Wed Aug 19 2026 yashar <yashar@duck.com> - 1.0.0-1
- Initial public release of Fedora Package Tree