"""Focused fixtures for secure Comps and AppStream metadata parsing."""

from __future__ import annotations

import gzip
import hashlib
import os
import sys
import types
from io import BytesIO

import pytest
from defusedxml.common import EntitiesForbidden

from dendro.core import package_metadata
from dendro.core.backend_parsers import classification_package_fields
from dendro.core.package_classifier import ProductionTaxonomyEngine
from dendro.core.package_metadata import (
    AppStreamCatalog,
    FedoraCompsCatalog,
    PackagePhysicalAnatomy,
    open_compressed_xml_metadata,
)
from dendro.core.package_types import PackageInfo
from dendro.core.taxonomy import ClassificationEvidence, TaxonomyInputScope


def test_appstream_indexes_every_pkgname_and_declared_component_signal(
    tmp_path,
) -> None:
    metadata = tmp_path / "catalog.xml"
    metadata.write_text(
        """<?xml version='1.0'?>
<components xmlns='https://www.freedesktop.org/software/appstream/catalog-1.0'>
  <component type='font'>
    <id>org.example.FontFamily</id>
    <pkgname>example-fonts</pkgname>
    <pkgname>example-fonts-common</pkgname>
    <categories><category>Graphics</category></categories>
  </component>
  <component type='inputmethod'>
    <id>org.example.InputMethod</id>
    <pkgname>example-ime</pkgname>
  </component>
  <component type='shell-extension'>
    <id>org.example.Extension</id>
    <bundle type='package'>example-shell-extension</bundle>
  </component>
  <component type='webapp'>
    <id>org.example.WebApp</id>
    <pkgname>example-webapp</pkgname>
  </component>
  <component type='runtime'>
    <id>org.example.Runtime</id>
    <pkgname>example-runtime</pkgname>
  </component>
</components>
""",
        encoding="utf-8",
    )
    catalog = AppStreamCatalog(load_catalog=False)

    catalog._parse_appstream_file(str(metadata))

    assert {"example-fonts", "example-fonts-common"}.issubset(catalog.font_packages)
    assert catalog.pkg_xdg_categories["example-fonts-common"] == {"graphics"}
    assert "example-ime" in catalog.inputmethod_packages
    assert "example-shell-extension" in catalog.addon_packages
    assert "example-webapp" in catalog.desktop_packages
    # Runtime is retained for metadata consumers but intentionally not a
    # positive desktop/library classifier signal.
    assert "example-runtime" in catalog.runtime_packages
    assert "example-runtime" not in catalog.desktop_packages
    assert "example-runtime" not in catalog.console_packages

    # The secondary pkgname is useful taxonomy input, not merely a catalog
    # record: it receives the same positive AppStream font classification.
    decision = ProductionTaxonomyEngine.classify(
        "example-fonts-common",
        appstream=catalog,
        comps=FedoraCompsCatalog(load_comps=False),
    )
    assert decision.primary_category == "fonts"
    assert decision.evidence == ClassificationEvidence.APPSTREAM

    input_method_decision = ProductionTaxonomyEngine.classify(
        "example-ime",
        appstream=catalog,
        comps=FedoraCompsCatalog(load_comps=False),
    )
    assert input_method_decision.primary_category == "desktop_addons"
    assert input_method_decision.evidence == ClassificationEvidence.APPSTREAM

    shell_extension_decision = ProductionTaxonomyEngine.classify(
        "example-shell-extension",
        appstream=catalog,
        comps=FedoraCompsCatalog(load_comps=False),
    )
    assert shell_extension_decision.primary_category == "desktop_addons"
    assert shell_extension_decision.evidence == ClassificationEvidence.APPSTREAM

    webapp_decision = ProductionTaxonomyEngine.classify(
        "example-webapp",
        appstream=catalog,
        comps=FedoraCompsCatalog(load_comps=False),
    )
    assert webapp_decision.primary_category == "user_apps"
    assert webapp_decision.evidence == ClassificationEvidence.APPSTREAM


def test_comps_indexes_compressed_fixture_and_prefers_system_tools_over_security(
    tmp_path,
) -> None:
    metadata = tmp_path / "comps.xml.gz"
    xml = b"""<?xml version='1.0'?>
<comps>
  <group><id>security-lab</id><packagelist><packagereq>htop</packagereq></packagelist></group>
  <group><id>system-tools</id><packagelist><packagereq>htop</packagereq></packagelist></group>
  <group><id>development-libs</id><name><id>security-lab</id></name><packagelist><packagereq>libexample-devel</packagereq></packagelist></group>
</comps>
"""
    with gzip.open(metadata, "wb") as stream:
        stream.write(xml)

    catalog = FedoraCompsCatalog(load_comps=False)
    with open_compressed_xml_metadata(str(metadata)) as stream:
        catalog._index_comps_stream(stream)

    # General command-line utility membership intentionally wins when a package
    # appears in both broad Security Lab and System Tools groups.
    assert catalog.get_domain_for_package("htop") == "cli_tools"
    # A nested id must not override the direct development-libs group id.
    assert catalog.get_domain_for_package("libexample-devel") == "c_libs"


def test_appstream_rejects_entity_expansion_without_indexing_packages(
    tmp_path, caplog
) -> None:
    metadata = tmp_path / "malicious.xml"
    metadata.write_text(
        """<?xml version='1.0'?>
<!DOCTYPE components [<!ENTITY dangerous 'do-not-expand'>]>
<components><component type='desktop'><pkgname>&dangerous;</pkgname></component></components>
""",
        encoding="utf-8",
    )
    catalog = AppStreamCatalog(load_catalog=False)

    catalog._parse_appstream_file(str(metadata))

    assert catalog.desktop_packages == set()
    assert "Could not parse AppStream metadata" in caplog.text


def test_comps_parser_uses_defused_xml_for_entity_expansion() -> None:
    catalog = FedoraCompsCatalog(load_comps=False)
    payload = BytesIO(
        b"<?xml version='1.0'?><!DOCTYPE comps [<!ENTITY x 'unsafe'>]>"
        b"<comps><group><id>system-tools</id><packagereq>&x;</packagereq></group></comps>"
    )

    with pytest.raises(EntitiesForbidden):
        catalog._index_comps_stream(payload)


def test_appstream_metainfo_join_and_all_appstream_1_0_kinds(tmp_path) -> None:
    metainfo_dir = tmp_path / "metainfo"
    metainfo_dir.mkdir()
    (metainfo_dir / "org.example.Editor.metainfo.xml").write_text(
        """<?xml version='1.0' encoding='UTF-8'?>
<component type="desktop-application">
  <id>org.example.Editor</id>
  <name>Example Editor</name>
  <categories>
    <category>Development</category>
    <category>IDE</category>
  </categories>
  <launchable type="desktop-id">org.example.Editor.desktop</launchable>
  <compulsory_for_desktop>GNOME</compulsory_for_desktop>
</component>
""",
        encoding="utf-8",
    )
    (metainfo_dir / "org.example.WebApp.metainfo.xml").write_text(
        """<?xml version='1.0' encoding='UTF-8'?>
<component type="web-application">
  <id>org.example.WebApp</id>
  <name>Example Cloud Mail</name>
  <launchable type="url">https://mail.example.org</launchable>
</component>
""",
        encoding="utf-8",
    )
    (metainfo_dir / "org.example.Repo.metainfo.xml").write_text(
        """<?xml version='1.0' encoding='UTF-8'?>
<component type="repository">
  <id>org.example.Repo</id>
  <name>Example Third-Party Repo</name>
</component>
""",
        encoding="utf-8",
    )
    (metainfo_dir / "org.example.OS.metainfo.xml").write_text(
        """<?xml version='1.0' encoding='UTF-8'?>
<component type="operating-system">
  <id>org.example.OS</id>
  <name>Example Linux</name>
</component>
""",
        encoding="utf-8",
    )
    (metainfo_dir / "org.example.GenericDaemon.metainfo.xml").write_text(
        """<?xml version='1.0' encoding='UTF-8'?>
<component type="generic">
  <id>org.example.GenericDaemon</id>
  <provides>
    <dbus type="system">org.example.Daemon</dbus>
    <dbus type="user">org.example.SessionHelper</dbus>
    <modalias>usb:v1D6Bp0002d*</modalias>
    <mediatype>application/x-example</mediatype>
    <binary>example-daemon</binary>
    <library>libexample.so.1</library>
    <python3>example_mod</python3>
    <id>org.example.LegacyId</id>
  </provides>
</component>
""",
        encoding="utf-8",
    )
    (metainfo_dir / "org.example.Firmware.metainfo.xml").write_text(
        """<?xml version='1.0' encoding='UTF-8'?>
<component type="generic">
  <id>org.example.Firmware</id>
  <provides>
    <firmware type="flashed">6de5d951-d755-576b-bd09-c5cf66b27234</firmware>
  </provides>
</component>
""",
        encoding="utf-8",
    )
    appdata_dir = tmp_path / "appdata"
    appdata_dir.mkdir()
    (appdata_dir / "legacy-tool.appdata.xml").write_text(
        """<?xml version='1.0' encoding='UTF-8'?>
<component type="console-application">
  <id>org.example.LegacyTool</id>
</component>
""",
        encoding="utf-8",
    )

    catalog = AppStreamCatalog(
        catalog_dirs=[],
        metainfo_dirs=[str(metainfo_dir), str(appdata_dir)],
        use_native_pool=False,
    )
    comps = FedoraCompsCatalog(load_comps=False)

    # 1. Upstream MetaInfo without <pkgname> joins via RPM metainfo(<file>)
    editor_anatomy = PackagePhysicalAnatomy.from_manifest_data(
        ["/usr/bin", "/usr/share/metainfo"],
        ["metainfo()", "metainfo(org.example.Editor.metainfo.xml)"],
        ["example-editor", "org.example.Editor.metainfo.xml"],
    )
    assert editor_anatomy.provided_metainfo_files == {"org.example.editor.metainfo.xml"}
    editor_decision = ProductionTaxonomyEngine.classify(
        "example-editor",
        anatomy=editor_anatomy,
        provides=["metainfo()", "metainfo(org.example.Editor.metainfo.xml)"],
        appstream=catalog,
        comps=comps,
    )
    assert editor_decision.primary_category == "user_apps"
    assert editor_decision.sub_category == "desktop_development"
    assert editor_decision.evidence == ClassificationEvidence.APPSTREAM
    assert any("compulsory for desktop: GNOME" in r for r in editor_decision.rationale)
    # Must unpack cleanly into PackageInfo without invalid flag kwargs.
    pkg = PackageInfo(
        name="example-editor",
        **classification_package_fields(editor_decision),
    )
    assert pkg.is_desktop_app is True

    # 2. Web application without categories routes cleanly to desktop_internet
    webapp_anatomy = PackagePhysicalAnatomy.from_manifest_data(
        [], ["metainfo(org.example.WebApp.metainfo.xml)"]
    )
    webapp_decision = ProductionTaxonomyEngine.classify(
        "example-webapp",
        anatomy=webapp_anatomy,
        appstream=catalog,
        comps=comps,
        input_scope=TaxonomyInputScope.REMOTE_METADATA,
    )
    assert webapp_decision.primary_category == "user_apps"
    assert webapp_decision.sub_category == "desktop_internet"
    assert webapp_decision.evidence == ClassificationEvidence.APPSTREAM

    # 3. Repository & Operating-System kinds classify accurately
    repo_decision = ProductionTaxonomyEngine.classify(
        "example-repo-config",
        anatomy=PackagePhysicalAnatomy.from_manifest_data(
            [], ["metainfo(org.example.Repo.metainfo.xml)"]
        ),
        appstream=catalog,
        comps=comps,
    )
    assert repo_decision.primary_category == "repository_sources"
    assert repo_decision.evidence == ClassificationEvidence.APPSTREAM

    os_decision = ProductionTaxonomyEngine.classify(
        "example-linux-release",
        anatomy=PackagePhysicalAnatomy.from_manifest_data(
            [], ["metainfo(org.example.OS.metainfo.xml)"]
        ),
        appstream=catalog,
        comps=comps,
    )
    assert os_decision.primary_category == "fedora_core"
    assert os_decision.evidence == ClassificationEvidence.APPSTREAM

    # 4. Generic component <provides> drives systemd_services & firmware without
    # blindly promoting generic components to desktop_app
    daemon_decision = ProductionTaxonomyEngine.classify(
        "example-daemon",
        anatomy=PackagePhysicalAnatomy.from_manifest_data(
            [], ["metainfo(org.example.GenericDaemon.metainfo.xml)"]
        ),
        appstream=catalog,
        comps=comps,
    )
    assert daemon_decision.primary_category == "systemd_services"
    assert daemon_decision.evidence == ClassificationEvidence.APPSTREAM

    fw_decision = ProductionTaxonomyEngine.classify(
        "example-fw",
        anatomy=PackagePhysicalAnatomy.from_manifest_data(
            [], ["metainfo(org.example.Firmware.metainfo.xml)"]
        ),
        appstream=catalog,
        comps=comps,
    )
    assert fw_decision.primary_category == "firmware"
    assert fw_decision.evidence == ClassificationEvidence.APPSTREAM

    # 5. Legacy /usr/share/appdata/*.appdata.xml joins cleanly even if <id> differs
    legacy_decision = ProductionTaxonomyEngine.classify(
        "legacy-tool",
        anatomy=PackagePhysicalAnatomy.from_manifest_data(
            ["/usr/share/appdata"],
            ["appdata()", "appdata(legacy-tool.appdata.xml)"],
            ["legacy-tool.appdata.xml"],
        ),
        appstream=catalog,
        comps=comps,
    )
    assert legacy_decision.primary_category == "cli_tools"
    assert legacy_decision.evidence == ClassificationEvidence.APPSTREAM

    # 6. Parsed <provides> richness is preserved on AppStreamComponent
    comp = catalog.component_for_metainfo("org.example.GenericDaemon.metainfo.xml")
    assert comp is not None
    assert comp.provided_dbus_system == ("org.example.Daemon",)
    assert comp.provided_dbus_user == ("org.example.SessionHelper",)
    assert comp.provided_modaliases == ("usb:v1D6Bp0002d*",)
    assert comp.provided_mediatypes == ("application/x-example",)
    assert comp.provided_binaries == ("example-daemon",)
    assert comp.provided_libraries == ("libexample.so.1",)
    assert comp.provided_python3 == ("example_mod",)
    assert comp.provided_ids == ("org.example.LegacyId",)


def test_appstream_desktop_id_join_guard_and_source_pkgname_isolation(tmp_path) -> None:
    catalog_dir = tmp_path / "swcatalog"
    catalog_dir.mkdir()
    (catalog_dir / "apps.xml").write_text(
        """<?xml version='1.0' encoding='UTF-8'?>
<components version="1.0">
  <component type="desktop-application">
    <id>org.freedesktop.IBus.Setup</id>
    <pkgname>ibus-setup</pkgname>
    <source_pkgname>ibus</source_pkgname>
    <launchable type="desktop-id">org.freedesktop.IBus.Setup.desktop</launchable>
  </component>
</components>
""",
        encoding="utf-8",
    )
    metainfo_dir = tmp_path / "metainfo"
    metainfo_dir.mkdir()
    (metainfo_dir / "org.example.Tool.metainfo.xml").write_text(
        """<?xml version='1.0' encoding='UTF-8'?>
<component type="desktop-application">
  <id>org.example.Tool</id>
  <launchable type="desktop-id">example-tool.desktop</launchable>
</component>
""",
        encoding="utf-8",
    )

    catalog = AppStreamCatalog(
        catalog_dirs=[str(catalog_dir)],
        metainfo_dirs=[str(metainfo_dir)],
        use_native_pool=False,
    )
    comps = FedoraCompsCatalog(load_comps=False)

    # source_pkgname is recorded without promoting sibling subpackage ibus-libs.
    assert catalog.pkg_source_packages["ibus-setup"] == "ibus"
    assert catalog.components_for_package("ibus-libs") == ()

    # Catalog component owned by ibus-setup must not attach to daemon package ibus
    # even if ibus lists the desktop ID.
    ibus_components = catalog.components_for_package(
        "ibus", desktop_ids=["org.freedesktop.IBus.Setup.desktop"]
    )
    assert ibus_components == ()

    # Upstream MetaInfo with no pkgname DOES join via launchable desktop-id.
    tool_decision = ProductionTaxonomyEngine.classify(
        "example-tool",
        anatomy=PackagePhysicalAnatomy.from_manifest_data(
            [], ["application(example-tool.desktop)"]
        ),
        appstream=catalog,
        comps=comps,
    )
    assert tool_decision.primary_category == "user_apps"
    assert tool_decision.evidence == ClassificationEvidence.APPSTREAM


def test_appstream_dep11_yaml_deduplication_and_remove_component(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr(package_metadata, "_pyyaml", None)

    yaml_dir = tmp_path / "yaml"
    yaml_dir.mkdir()
    dep11_payload = """---
File: DEP-11
Version: '0.16'
Origin: fedora-44
---
Type: desktop-application
ID: org.example.YamlApp
Package: yaml-app
SourcePackage: yaml-app-src
Name:
  C: YAML App
Description:
  C: |
    <p>Multi-line block scalar: safe colon handling.</p>
    <ul>
      <li>Item 1</li>
    </ul>
Categories:
- AudioVideo
- Player
Launchable:
  desktop-id:
    - org.example.YamlApp.desktop
CompulsoryForDesktop:
  - KDE
  - Plasma
Provides:
  binaries:
    - yaml-app
  mediatypes: [audio/ogg, audio/flac]
  fonts:
    - name: Example Sans
  dbus:
    - type: user
      service: org.example.YamlApp
    - type: system
      service: org.example.YamlHelper
  firmware:
    - type: runtime
      file: example/fw.bin
---
Type: addon
ID: org.example.StyleBAddon
Bundles:
- type: package
  id: yaml-addon
CompulsoryForDesktop: GNOME
---
Type: codec
ID: org.example.ToRemove
Package: obsolete-codec
"""
    # Compressed and uncompressed siblings: only one should be processed,
    # preferring compressed and discarding obsolete-codec via remove-component.
    (yaml_dir / "catalog.yml").write_text(
        """---
File: DEP-11
---
Type: desktop-application
ID: org.example.Stale
Package: stale-pkg
""",
        encoding="utf-8",
    )
    with gzip.open(yaml_dir / "catalog.yml.gz", "wb") as stream:
        stream.write(dep11_payload.encode("utf-8"))

    (yaml_dir / "ignored.yml").write_text(
        """---
File: NOT-DEP-11
---
Type: desktop-application
ID: org.example.Ignored
Package: ignored-pkg
""",
        encoding="utf-8",
    )

    xml_dir = tmp_path / "xml"
    xml_dir.mkdir()
    (xml_dir / "overrides.xml").write_text(
        """<?xml version='1.0' encoding='UTF-8'?>
<components>
  <component merge="remove-component">
    <id>org.example.ToRemove</id>
  </component>
</components>
""",
        encoding="utf-8",
    )

    catalog = AppStreamCatalog(
        catalog_dirs=[str(yaml_dir), str(xml_dir)],
        metainfo_dirs=[],
        use_native_pool=False,
    )

    assert "yaml-app" in catalog.desktop_packages
    assert "yaml-addon" in catalog.addon_packages
    assert catalog.pkg_xdg_categories["yaml-app"] == {"audiovideo", "player"}
    assert catalog.pkg_compulsory_desktops["yaml-app"] == {"KDE", "Plasma"}
    assert catalog.pkg_compulsory_desktops["yaml-addon"] == {"GNOME"}
    assert catalog.pkg_source_packages["yaml-app"] == "yaml-app-src"
    assert "stale-pkg" not in catalog.desktop_packages
    assert "ignored-pkg" not in catalog.desktop_packages
    assert "obsolete-codec" not in catalog.codec_packages

    comps = catalog.components_for_package("yaml-app")
    assert len(comps) == 1
    assert comps[0].launchable_desktop_ids == ("org.example.YamlApp.desktop",)
    assert comps[0].provided_binaries == ("yaml-app",)
    assert comps[0].provided_mediatypes == ("audio/ogg", "audio/flac")
    assert comps[0].provided_fonts == ("Example Sans",)
    assert comps[0].provided_dbus_system == ("org.example.YamlHelper",)
    assert comps[0].provided_dbus_user == ("org.example.YamlApp",)
    assert comps[0].provided_firmware_runtime == ("example/fw.bin",)


def test_appstream_xdg_data_home_and_invalidate_cache(tmp_path, monkeypatch) -> None:
    xdg_data = tmp_path / "xdg-data"
    user_swcatalog = xdg_data / "swcatalog" / "xml"
    user_metainfo = xdg_data / "metainfo"
    user_swcatalog.mkdir(parents=True)
    user_metainfo.mkdir(parents=True)
    (user_swcatalog / "user.xml").write_text(
        "<components><component type='font'><id>org.example.UserFont</id>"
        "<pkgname>user-fonts</pkgname></component></components>",
        encoding="utf-8",
    )
    (user_metainfo / "org.example.UserApp.metainfo.xml").write_text(
        "<component type='desktop-application'>"
        "<id>org.example.UserApp</id></component>",
        encoding="utf-8",
    )

    monkeypatch.setenv("XDG_DATA_HOME", str(xdg_data))
    AppStreamCatalog.invalidate_cache()
    try:
        catalog = AppStreamCatalog(
            catalog_dirs=None,
            metainfo_dirs=None,
            use_native_pool=False,
        )
        assert "user-fonts" in catalog.font_packages
        assert (
            catalog.component_for_metainfo("org.example.UserApp.metainfo.xml")
            is not None
        )
    finally:
        AppStreamCatalog.invalidate_cache()


def test_appstream_gi_pool_integration_and_box_lifetime(tmp_path, monkeypatch) -> None:
    xdg_data = tmp_path / "xdg-gi"
    (xdg_data / "swcatalog").mkdir(parents=True)
    monkeypatch.setenv("XDG_DATA_HOME", str(xdg_data))

    class _PoolFlags:
        LOAD_OS_CATALOG = 1 << 0
        LOAD_OS_METAINFO = 1 << 1
        LOAD_OS_DESKTOP_FILES = 1 << 2
        LOAD_FLATPAK = 1 << 3

    class _FormatStyle:
        CATALOG = 1

    class _BundleKind:
        PACKAGE = 1

    class _LaunchableKind:
        DESKTOP_ID = 1

    class _ProvidedKind:
        DBUS_SYSTEM = 1
        DBUS_USER = 2
        FIRMWARE_RUNTIME = 3
        FIRMWARE_FLASHED = 4
        MODALIAS = 5
        MEDIATYPE = 6
        FONT = 7
        BINARY = 8
        LIBRARY = 9
        PYTHON = 10
        ID = 11

    class _FakeLaunchable:
        def get_kind(self):
            return _LaunchableKind.DESKTOP_ID

        def get_entries(self):
            return ["org.example.GiApp.desktop"]

    class _FakeProvided:
        def __init__(self, kind, items):
            self._kind = kind
            self._items = items

        def get_kind(self):
            return self._kind

        def get_items(self):
            return self._items

    class _FakeComponent:
        def get_kind(self):
            return "web-application"

        def get_id(self):
            return "org.example.GiApp"

        def get_pkgnames(self):
            return ["gi-webapp"]

        def get_bundles(self):
            return []

        def get_source_pkgname(self):
            return "gi-webapp-src"

        def get_categories(self):
            return ["Network"]

        def get_compulsory_for_desktops(self):
            return ["GNOME"]

        def get_launchables(self):
            return [_FakeLaunchable()]

        def get_provided(self):
            return [
                _FakeProvided(_ProvidedKind.DBUS_USER, ["org.example.GiApp"]),
                _FakeProvided(_ProvidedKind.BINARY, ["gi-webapp"]),
            ]

    class _FakeBox:
        def __init__(self, items):
            self._items = items

        def get_size(self):
            return len(self._items)

        def index_safe(self, index):
            return self._items[index]

        def as_array(self):  # pragma: no cover - must never be used
            raise AssertionError("as_array() triggers PyGObject transfer-none hazard")

    recorded: dict[str, object] = {}

    class _FakePool:
        def set_flags(self, flags):
            recorded["flags"] = flags

        def add_extra_data_location(self, path, style):
            recorded.setdefault("extra_locations", []).append((path, style))

        def load(self, cancellable):
            recorded["loaded"] = True

        def get_components(self):
            return _FakeBox([_FakeComponent()])

    fake_appstream = types.ModuleType("gi.repository.AppStream")
    fake_appstream.PoolFlags = _PoolFlags
    fake_appstream.FormatStyle = _FormatStyle
    fake_appstream.BundleKind = _BundleKind
    fake_appstream.LaunchableKind = _LaunchableKind
    fake_appstream.ProvidedKind = _ProvidedKind
    fake_appstream.Pool = _FakePool
    fake_appstream.component_kind_to_string = lambda kind: kind

    fake_repo = types.ModuleType("gi.repository")
    fake_repo.AppStream = fake_appstream

    fake_gi = types.ModuleType("gi")
    fake_gi.require_version = lambda name, version: recorded.setdefault(
        "version", (name, version)
    )
    fake_gi.repository = fake_repo

    monkeypatch.setitem(sys.modules, "gi", fake_gi)
    monkeypatch.setitem(sys.modules, "gi.repository", fake_repo)
    monkeypatch.setitem(sys.modules, "gi.repository.AppStream", fake_appstream)

    catalog = AppStreamCatalog(use_native_pool=True)

    assert catalog.backend_used == "gi-appstream"
    assert recorded["version"] == ("AppStream", "1.0")
    assert recorded["flags"] == (
        _PoolFlags.LOAD_OS_CATALOG | _PoolFlags.LOAD_OS_METAINFO
    )
    assert recorded["extra_locations"] == [
        (str(xdg_data / "swcatalog"), _FormatStyle.CATALOG)
    ]
    assert "gi-webapp" in catalog.web_app_packages
    assert "gi-webapp" in catalog.desktop_packages
    assert catalog.pkg_compulsory_desktops["gi-webapp"] == {"GNOME"}
    assert catalog.pkg_source_packages["gi-webapp"] == "gi-webapp-src"
    comps = catalog.components_for_package("gi-webapp")
    assert len(comps) == 1
    assert comps[0].launchable_desktop_ids == ("org.example.GiApp.desktop",)
    assert comps[0].provided_binaries == ("gi-webapp",)
    assert comps[0].provided_dbus_user == ("org.example.GiApp",)


def test_package_physical_anatomy_from_rpm_header_verifies_metainfo_dir(
    monkeypatch,
) -> None:
    fake_rpm = types.ModuleType("rpm")
    fake_rpm.RPMTAG_DIRNAMES = 1
    fake_rpm.RPMTAG_PROVIDENAME = 2
    fake_rpm.RPMTAG_BASENAMES = 3
    fake_rpm.RPMTAG_DIRINDEXES = 4
    monkeypatch.setitem(sys.modules, "rpm", fake_rpm)

    header = {
        fake_rpm.RPMTAG_DIRNAMES: [
            b"/usr/share/metainfo/",
            b"/usr/share/doc/example/",
        ],
        fake_rpm.RPMTAG_PROVIDENAME: [
            b"metainfo()",
            b"metainfo(org.example.App.metainfo.xml)",
        ],
        fake_rpm.RPMTAG_BASENAMES: [
            b"org.example.App.metainfo.xml",
            b"sample.metainfo.xml",
        ],
        fake_rpm.RPMTAG_DIRINDEXES: [0, 1],
    }

    anatomy = PackagePhysicalAnatomy.from_rpm_header(header)
    assert anatomy.provides_appstream is True
    assert anatomy.has_metainfo_dir is True
    assert anatomy.provided_metainfo_files == {"org.example.app.metainfo.xml"}


def _encode_zck_compint(value: int) -> bytes:
    parts = bytearray()
    remaining = value
    while True:
        chunk = remaining % 128
        remaining = (remaining - chunk) // 128
        if remaining == 0:
            parts.append(chunk + 128)
            break
        parts.append(chunk)
    return bytes(parts)


def _build_synthetic_zck(
    chunks: list[bytes],
    *,
    comp_type: int = 0,
    dict_bytes: bytes = b"",
    compress_fn=None,
    flags: int = 0,
) -> bytes:
    """Build a valid zchunk v1 archive matching libzck's exact binary layout."""
    sha256_zero = b"\x00" * 32
    raw_chunks = [dict_bytes, *chunks]
    body = bytearray()
    index_entries = bytearray()

    for idx, raw in enumerate(raw_chunks):
        if idx == 0:
            comp_data = (
                compress_fn(raw, dict_bytes=None)
                if raw and compress_fn is not None
                else raw
            )
            digest = hashlib.sha256(comp_data).digest() if comp_data else sha256_zero
        else:
            comp_data = (
                compress_fn(raw, dict_bytes=dict_bytes or None)
                if compress_fn is not None
                else raw
            )
            digest = hashlib.sha256(comp_data).digest()
        body.extend(comp_data)
        index_entries.extend(digest)
        if flags & 0x04:
            index_entries.extend(hashlib.sha256(raw).digest() if raw else sha256_zero)
        index_entries.extend(_encode_zck_compint(len(comp_data)))
        index_entries.extend(_encode_zck_compint(len(raw)))

    index = (
        _encode_zck_compint(1)
        + _encode_zck_compint(len(raw_chunks))
        + bytes(index_entries)
    )
    data_digest = hashlib.sha256(body).digest()
    preface = (
        data_digest
        + _encode_zck_compint(flags)
        + _encode_zck_compint(comp_type)
        + _encode_zck_compint(len(index))
    )
    sigs = _encode_zck_compint(0)
    header_rest = preface + index + sigs
    lead_prefix = (
        b"\x00ZCK1" + _encode_zck_compint(1) + _encode_zck_compint(len(header_rest))
    )
    hdr_digest = hashlib.sha256(lead_prefix + header_rest).digest()
    return lead_prefix + hdr_digest + header_rest + bytes(body)


def test_open_compressed_xml_metadata_decodes_uncompressed_and_corrupt_zck(
    tmp_path,
) -> None:
    xml_part1 = b"<comps><group><id>fonts</id><packagelist>"
    xml_part2 = b"<packagereq>default-fonts-core-sans</packagereq></packagelist></group></comps>"
    zck_path = tmp_path / "comps.xml.zck"
    zck_path.write_bytes(_build_synthetic_zck([xml_part1, xml_part2], comp_type=0))

    with open_compressed_xml_metadata(str(zck_path)) as stream:
        catalog = FedoraCompsCatalog(load_comps=False)
        catalog._index_comps_stream(stream)

    assert catalog.get_domain_for_package("default-fonts-core-sans") == "fonts"

    tampered = bytearray(zck_path.read_bytes())
    tampered[-1] ^= 0xFF
    bad_path = tmp_path / "bad.xml.zck"
    bad_path.write_bytes(bytes(tampered))
    with (
        pytest.raises(ValueError, match="checksum"),
        open_compressed_xml_metadata(str(bad_path)),
    ):
        pass

    detached_path = tmp_path / "detached.zck"
    detached_path.write_bytes(b"\x00ZHR1" + b"\x00" * 32)
    with (
        pytest.raises(package_metadata.UnsupportedMetadataCompression),
        open_compressed_xml_metadata(str(detached_path)),
    ):
        pass


def test_open_compressed_xml_metadata_decodes_zstd_and_dict_zck(
    monkeypatch,
    tmp_path,
) -> None:
    xml = b"<comps><group><id>input-methods</id><packagelist><packagereq>ibus-anthy</packagereq></packagereq_end></packagelist></group></comps>".replace(
        b"</packagereq_end>", b""
    )

    # 1. Verify zstd CLI fallback works cleanly even when Python zstd modules are absent.
    fake_zstd = tmp_path / "fake-zstd"
    fake_zstd.write_text(
        f"#!/bin/sh\nprintf '%s' '{xml.decode('ascii')}'\n",
        encoding="utf-8",
    )
    fake_zstd.chmod(0o755)
    zst_path = tmp_path / "comps.xml.zst"
    zst_path.write_bytes(b"\x28\xb5\x2f\xfdplaceholder")

    monkeypatch.setattr(package_metadata, "_stdlib_zstd", None)
    monkeypatch.setattr(package_metadata, "_backports_zstd", None)
    monkeypatch.setattr(package_metadata, "_third_party_zstd", None)
    monkeypatch.setattr(
        package_metadata.shutil,
        "which",
        lambda name: str(fake_zstd) if name == "zstd" else None,
    )

    with open_compressed_xml_metadata(str(zst_path)) as stream:
        cli_catalog = FedoraCompsCatalog(load_comps=False)
        cli_catalog._index_comps_stream(stream)
    assert cli_catalog.get_domain_for_package("ibus-anthy") == "desktop_addons"


def test_open_compressed_xml_metadata_decodes_real_zstd_zck_with_dictionary(
    tmp_path,
) -> None:
    pep784_mod = (
        package_metadata._stdlib_zstd
        if package_metadata._stdlib_zstd is not None
        else package_metadata._backports_zstd
    )
    third_party = package_metadata._third_party_zstd
    if pep784_mod is None and third_party is None:
        pytest.skip("No Python zstd module installed in this interpreter")

    dict_bytes = (
        b"<comps><group><id>haskell</id><packagelist>"
        b"<packagereq>ghc-random</packagereq></packagelist></group></comps>"
    )
    chunk_a = b"<comps><group><id>haskell</id><packagelist>"
    chunk_b = b"<packagereq>ghc-random</packagereq></packagelist></group></comps>"

    def _compress(raw: bytes, *, dict_bytes: bytes | None) -> bytes:
        if pep784_mod is not None:
            if dict_bytes:
                zd = pep784_mod.ZstdDict(dict_bytes, is_raw=True)
                return pep784_mod.compress(raw, zstd_dict=zd)
            return pep784_mod.compress(raw)
        if dict_bytes:
            zd = third_party.ZstdCompressionDict(
                dict_bytes, dict_type=third_party.DICT_TYPE_RAWCONTENT
            )
            return third_party.ZstdCompressor(dict_data=zd).compress(raw)
        return third_party.ZstdCompressor().compress(raw)

    zck_path = tmp_path / "haskell-comps.xml.zck"
    zck_path.write_bytes(
        _build_synthetic_zck(
            [chunk_a, chunk_b],
            comp_type=2,
            dict_bytes=dict_bytes,
            compress_fn=_compress,
        )
    )

    with open_compressed_xml_metadata(str(zck_path)) as stream:
        catalog = FedoraCompsCatalog(load_comps=False)
        catalog._index_comps_stream(stream)
    assert catalog.get_domain_for_package("ghc-random") == "haskell_pkgs"


def test_comps_catalog_filters_disabled_repos_dedups_stale_caches_and_reads_sysimage(
    monkeypatch,
    tmp_path,
) -> None:
    # Configured repo files: fedora enabled, disabled-repo disabled via override.
    repos_d = tmp_path / "yum.repos.d"
    repos_d.mkdir()
    (repos_d / "fedora.repo").write_text(
        "[fedora]\nname=Fedora\nenabled=1\n\n[disabled-repo]\nenabled=1\n",
        encoding="utf-8",
    )
    override_d = tmp_path / "repos.override.d"
    override_d.mkdir()
    (override_d / "override.repo").write_text(
        "[disabled-repo]\nenabled=0\n",
        encoding="utf-8",
    )

    # Sysimage @System group
    sysimage_groups = tmp_path / "sysimage" / "groups"
    sysimage_groups.mkdir(parents=True)
    (sysimage_groups / "base-graphical.xml").write_text(
        "<comps><group><id>base-graphical</id><packagelist>"
        "<packagereq>glx-utils</packagereq></packagelist></group></comps>",
        encoding="utf-8",
    )

    # Disabled repo cache must be skipped.
    disabled_repodata = tmp_path / "cache" / "disabled-repo-12345678" / "repodata"
    disabled_repodata.mkdir(parents=True)
    (disabled_repodata / "repomd.xml").write_text(
        "<repomd><data type='group'><location href='repodata/comps.xml'/></data></repomd>",
        encoding="utf-8",
    )
    (disabled_repodata / "comps.xml").write_text(
        "<comps><group><id>core</id><packagelist><packagereq>should-be-skipped</packagereq></packagelist></group></comps>",
        encoding="utf-8",
    )

    # Two cache dirs for same enabled repo 'fedora': older must be ignored, newer repomd pointer chosen,
    # and an unreferenced sibling comps XML inside repodata must be ignored.
    old_repodata = tmp_path / "cache" / "fedora-00001111" / "repodata"
    old_repodata.mkdir(parents=True)
    old_repomd = old_repodata / "repomd.xml"
    old_repomd.write_text(
        "<repomd><data type='group'><location href='repodata/old-comps.xml'/></data></repomd>",
        encoding="utf-8",
    )
    (old_repodata / "old-comps.xml").write_text(
        "<comps><group><id>core</id><packagelist><packagereq>stale-pkg</packagereq></packagelist></group></comps>",
        encoding="utf-8",
    )
    os.utime(old_repomd, (1000, 1000))

    new_repodata = tmp_path / "cache" / "fedora-22223333" / "repodata"
    new_repodata.mkdir(parents=True)
    new_repomd = new_repodata / "repomd.xml"
    new_repomd.write_text(
        "<repomd><data type='group'><location href='repodata/active-comps.xml'/></data></repomd>",
        encoding="utf-8",
    )
    (new_repodata / "active-comps.xml").write_text(
        "<comps><group><id>fonts</id><packagelist><packagereq>fresh-font</packagereq></packagelist></group></comps>",
        encoding="utf-8",
    )
    (new_repodata / "orphan-comps.xml").write_text(
        "<comps><group><id>core</id><packagelist><packagereq>orphan-pkg</packagereq></packagelist></group></comps>",
        encoding="utf-8",
    )
    os.utime(new_repomd, (2000, 2000))

    monkeypatch.setattr(
        FedoraCompsCatalog,
        "_REPO_CONFIG_PATTERNS",
        (str(repos_d / "*.repo"), str(override_d / "*.repo")),
    )

    def fake_glob(pattern: str, recursive: bool = False) -> list[str]:
        if pattern == "/usr/lib/sysimage/libdnf5/comps_groups/groups/*.xml":
            return [str(sysimage_groups / "base-graphical.xml")]
        if pattern == "/var/cache/libdnf5/*/repodata/repomd.xml":
            return [
                str(disabled_repodata / "repomd.xml"),
                str(old_repomd),
                str(new_repomd),
            ]
        if pattern == "/var/cache/libdnf5/*/repodata/*comps*.xml*":
            return [
                str(disabled_repodata / "comps.xml"),
                str(old_repodata / "old-comps.xml"),
                str(new_repodata / "active-comps.xml"),
                str(new_repodata / "orphan-comps.xml"),
            ]
        if pattern in {str(repos_d / "*.repo"), str(override_d / "*.repo")}:
            import pathlib

            base = pathlib.Path(pattern).parent
            return sorted(str(p) for p in base.glob("*.repo"))
        return []

    monkeypatch.setattr(package_metadata.glob, "glob", fake_glob)

    catalog = FedoraCompsCatalog(use_daemon=False)

    assert catalog.get_domain_for_package("glx-utils") == "graphics_drivers"
    assert catalog.get_domain_for_package("fresh-font") == "fonts"
    assert catalog.get_domain_for_package("should-be-skipped") is None
    assert catalog.get_domain_for_package("stale-pkg") is None
    assert catalog.get_domain_for_package("orphan-pkg") is None


def test_production_taxonomy_uses_verified_comps_domains_without_false_core_or_security(
    tmp_path,
) -> None:
    comps_xml = tmp_path / "comps.xml"
    comps_xml.write_text(
        """<?xml version='1.0'?>
<comps>
  <group><id>core</id><packagelist><packagereq>dracut-config-rescue</packagereq></packagelist></group>
  <group><id>standard</id><packagelist><packagereq>tree</packagereq></packagelist></group>
  <group><id>security-lab</id><packagelist><packagereq>nmap</packagereq></packagelist></group>
  <group><id>fonts</id><packagelist><packagereq>default-fonts-core-sans</packagereq></packagelist></group>
  <group><id>input-methods</id><packagelist><packagereq>ibus-anthy</packagereq></packagelist></group>
  <group><id>mysql</id><packagelist><packagereq>mariadb-server</packagereq></packagelist></group>
  <group><id>base-graphical</id><packagelist><packagereq>glx-utils</packagereq></packagelist></group>
</comps>
""",
        encoding="utf-8",
    )
    comps = FedoraCompsCatalog(load_comps=False)
    with open_compressed_xml_metadata(str(comps_xml)) as stream:
        comps._index_comps_stream(stream)
    appstream = AppStreamCatalog(load_catalog=False)

    # 'standard' must NOT inflate 'tree' into critical fedora_core.
    assert comps.get_domain_for_package("tree") is None
    tree_remote = ProductionTaxonomyEngine.classify(
        "tree",
        appstream=appstream,
        comps=comps,
        input_scope=TaxonomyInputScope.REMOTE_METADATA,
    )
    assert tree_remote.primary_category == "unclassified"

    # 'security-lab' tools classify as cli_tools, not critical security_pkgs.
    nmap_remote = ProductionTaxonomyEngine.classify(
        "nmap",
        appstream=appstream,
        comps=comps,
        input_scope=TaxonomyInputScope.REMOTE_METADATA,
    )
    assert nmap_remote.primary_category == "cli_tools"
    assert nmap_remote.evidence == ClassificationEvidence.COMPS

    # Remote packages in verified groups resolve accurately via COMPS evidence.
    expectations = {
        "dracut-config-rescue": "fedora_core",
        "default-fonts-core-sans": "fonts",
        "ibus-anthy": "desktop_addons",
        "mariadb-server": "systemd_services",
        "glx-utils": "graphics_drivers",
    }
    for pkg_name, expected_cat in expectations.items():
        decision = ProductionTaxonomyEngine.classify(
            pkg_name,
            appstream=appstream,
            comps=comps,
            input_scope=TaxonomyInputScope.REMOTE_METADATA,
        )
        assert decision.primary_category == expected_cat
        assert decision.evidence == ClassificationEvidence.COMPS

    # Stronger physical RPM manifest still overrides Comps fallback.
    dracut_with_bin = ProductionTaxonomyEngine.classify(
        "dracut-config-rescue",
        anatomy=PackagePhysicalAnatomy.from_manifest_data(["/usr/bin"], []),
        appstream=appstream,
        comps=comps,
    )
    assert dracut_with_bin.primary_category == "cli_tools"
    assert dracut_with_bin.evidence == ClassificationEvidence.RPM_MANIFEST


def test_comps_catalog_loads_via_daemon_in_flatpak(monkeypatch) -> None:
    from dendro.core import dnf5daemon
    from dendro.core.dnf5daemon_types import DnfCompsGroup, DnfCompsPackage

    calls: list[dict[str, object]] = []

    class _FakeDaemonClient:
        def list_groups(self, **kwargs):
            calls.append(kwargs)
            return (
                DnfCompsGroup(
                    groupid="input-methods",
                    packages=(DnfCompsPackage(name="ibus-libpinyin"),),
                ),
            )

        def close(self) -> None:
            pass

    monkeypatch.setattr(package_metadata, "is_running_in_flatpak", lambda: True)
    monkeypatch.setattr(package_metadata.glob, "glob", lambda *_a, **_kw: [])
    monkeypatch.setattr(dnf5daemon, "Dnf5DaemonClient", _FakeDaemonClient)

    catalog = FedoraCompsCatalog()

    assert len(calls) == 1
    assert calls[0]["scope"] == "all"
    assert calls[0]["with_hidden"] is True
    assert catalog.daemon_groups_loaded is True
    assert catalog.get_domain_for_package("ibus-libpinyin") == "desktop_addons"
    assert catalog.get_groups_for_package("ibus-libpinyin") == ("input-methods",)


def test_package_physical_anatomy_from_rpm_header_verifies_desktop_ids_and_oldfilenames(
    monkeypatch,
) -> None:
    fake_rpm = types.ModuleType("rpm")
    fake_rpm.RPMTAG_DIRNAMES = 1118
    fake_rpm.RPMTAG_PROVIDENAME = 1047
    fake_rpm.RPMTAG_BASENAMES = 1117
    fake_rpm.RPMTAG_DIRINDEXES = 1116
    fake_rpm.RPMTAG_OLDFILENAMES = 1027
    monkeypatch.setitem(sys.modules, "rpm", fake_rpm)

    # 1. Compressed header: only files actually inside XDG applications/ yield IDs.
    sub_header = {
        fake_rpm.RPMTAG_DIRNAMES: [
            b"/usr/share/applications/kde4/",
            b"/etc/xdg/autostart/",
            b"/usr/share/kservices6/",
            b"/usr/share/doc/kwrite/",
        ],
        fake_rpm.RPMTAG_PROVIDENAME: [],
        fake_rpm.RPMTAG_BASENAMES: [
            b"kwrite.desktop",
            b"kwrite-autostart.desktop",
            b"kwrite-part.desktop",
            b"example.desktop",
        ],
        fake_rpm.RPMTAG_DIRINDEXES: [0, 1, 2, 3],
    }
    sub_anatomy = PackagePhysicalAnatomy.from_rpm_header(sub_header)
    assert sub_anatomy.has_desktop_file is True
    assert sub_anatomy.provided_desktop_ids == {"kde4-kwrite.desktop"}

    # 2. Package owning /usr/share/applications/ directory + unrelated .desktop clears flag.
    dir_only_header = {
        fake_rpm.RPMTAG_DIRNAMES: [
            b"/usr/share/applications/",
            b"/etc/xdg/autostart/",
        ],
        fake_rpm.RPMTAG_PROVIDENAME: [],
        fake_rpm.RPMTAG_BASENAMES: [b"daemon.desktop"],
        fake_rpm.RPMTAG_DIRINDEXES: [1],
    }
    dir_only_anatomy = PackagePhysicalAnatomy.from_rpm_header(dir_only_header)
    assert dir_only_anatomy.has_desktop_file is False
    assert dir_only_anatomy.provided_desktop_ids == set()

    # 3. Legacy RPM v3 header using RPMTAG_OLDFILENAMES.
    legacy_header = {
        fake_rpm.RPMTAG_OLDFILENAMES: [
            b"/usr/share/applications/legacy.desktop",
            b"/etc/xdg/autostart/legacy-tray.desktop",
            b"/usr/share/metainfo/org.example.Legacy.metainfo.xml",
            b"/usr/share/doc/legacy/sample.metainfo.xml",
        ],
        fake_rpm.RPMTAG_PROVIDENAME: [],
    }
    legacy_anatomy = PackagePhysicalAnatomy.from_rpm_header(legacy_header)
    assert legacy_anatomy.has_desktop_file is True
    assert legacy_anatomy.provided_desktop_ids == {"legacy.desktop"}
    assert legacy_anatomy.has_metainfo_dir is True
    assert legacy_anatomy.provided_metainfo_files == {"org.example.legacy.metainfo.xml"}


def test_desktop_entry_metadata_spec_keys_escaping_and_tryexec(
    tmp_path, monkeypatch
) -> None:
    exec_bin = tmp_path / "launch-me"
    exec_bin.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    exec_bin.chmod(0o755)

    non_exec = tmp_path / "not-executable"
    non_exec.write_text("data", encoding="utf-8")
    non_exec.chmod(0o644)

    # DBusActivatable=true without Exec, escaped semicolon in Categories, quoted binary, OnlyShowIn.
    dbus_desktop = tmp_path / "org.example.DbusApp.desktop"
    dbus_desktop.write_text(
        "[Desktop Entry]\n"
        "Type=Application\n"
        "Name=D-Bus\\sApp\\\\One\n"
        "DBusActivatable=true\n"
        "Categories=Utility;X-Custom\\;Tag;\n"
        f"TryExec={exec_bin}\n"
        "OnlyShowIn=GNOME;XFCE;\n"
        "\n"
        "[Desktop Action NewWindow]\n"
        "Name=New Window\n"
        "Exec=should-not-overwrite\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("XDG_CURRENT_DESKTOP", "ubuntu:GNOME")
    dbus_entry = package_metadata.DesktopEntryMetadata(str(dbus_desktop))
    assert dbus_entry.name == "D-Bus App\\One"
    assert dbus_entry.dbus_activatable is True
    assert dbus_entry.try_exec_available is True
    assert dbus_entry.is_nodisplay is False
    assert dbus_entry.categories == {"utility", "x-custom;tag"}
    assert dbus_entry.only_show_in == ("GNOME", "XFCE")
    assert dbus_entry.shown_in_current_desktop is True
    assert dbus_entry.is_launchable() is True
    assert dbus_entry.is_shown_in_desktop("KDE") is False
    assert dbus_entry.is_launchable("KDE") is False

    # OnlyShowIn / NotShowIn precedence over $XDG_CURRENT_DESKTOP colon order.
    not_show = tmp_path / "notshow.desktop"
    not_show.write_text(
        "[Desktop Entry]\n"
        "Type=Application\n"
        "Name=QuotedApp\n"
        'Exec="/usr/bin/my app" %U\n'
        "OnlyShowIn=GNOME;\n"
        "NotShowIn=KDE;\n",
        encoding="utf-8",
    )
    not_show_entry = package_metadata.DesktopEntryMetadata(str(not_show))
    assert not_show_entry.exec_cmd == "my app"
    assert not_show_entry.is_shown_in_desktop("KDE:GNOME") is False
    assert not_show_entry.is_shown_in_desktop("GNOME:KDE") is True
    assert not_show_entry.is_shown_in_desktop("XFCE") is False

    # NotShowIn alone blocks when any listed desktop matches before default.
    not_show_only = tmp_path / "notshow-only.desktop"
    not_show_only.write_text(
        "[Desktop Entry]\n"
        "Type=Application\n"
        "Name=NotShowOnly\n"
        "Exec=app\n"
        "NotShowIn=KDE;\n",
        encoding="utf-8",
    )
    not_show_only_entry = package_metadata.DesktopEntryMetadata(str(not_show_only))
    assert not_show_only_entry.is_shown_in_desktop("GNOME") is True
    assert not_show_only_entry.is_shown_in_desktop("GNOME:KDE") is False

    # Non-executable TryExec marks entry non-displayable/non-launchable.
    bad_tryexec = tmp_path / "badtry.desktop"
    bad_tryexec.write_text(
        "[Desktop Entry]\n"
        "Type=Application\n"
        "Name=BrokenTryExec\n"
        "Exec=broken\n"
        f"TryExec={non_exec}\n",
        encoding="utf-8",
    )
    bad_entry = package_metadata.DesktopEntryMetadata(str(bad_tryexec))
    assert bad_entry.try_exec_available is False
    assert bad_entry.is_nodisplay is True
    assert bad_entry.is_launchable("GNOME") is False

    # Removing legacy path substring heuristics means an arbitrary path segment
    # named "autostart" does not force is_auxiliary when Categories is GUI.
    gui_dir = tmp_path / "autostart"
    gui_dir.mkdir()
    gui_desktop = gui_dir / "gui.desktop"
    gui_desktop.write_text(
        "[Desktop Entry]\n"
        "Type=Application\n"
        "Name=GuiApp\n"
        "Exec=gui-app\n"
        "Categories=Network;WebBrowser;\n",
        encoding="utf-8",
    )
    gui_entry = package_metadata.DesktopEntryMetadata(str(gui_desktop))
    assert gui_entry.is_auxiliary is False


def test_system_desktop_index_xdg_precedence_subdirs_and_hidden_tombstone(
    tmp_path, monkeypatch
) -> None:
    override_share = tmp_path / "override_share"
    base_share = tmp_path / "base_share"
    (override_share / "applications").mkdir(parents=True)
    (base_share / "applications" / "kde4").mkdir(parents=True)

    # Higher precedence XDG dir overrides base entry with the same Desktop File ID.
    (override_share / "applications" / "editor.desktop").write_text(
        "[Desktop Entry]\n"
        "Type=Application\n"
        "Name=Override Editor\n"
        "Exec=override-editor\n"
        "Categories=Development;TextEditor;\n",
        encoding="utf-8",
    )
    (base_share / "applications" / "editor.desktop").write_text(
        "[Desktop Entry]\n"
        "Type=Application\n"
        "Name=Base Editor\n"
        "Exec=base-editor\n"
        "Categories=Utility;\n",
        encoding="utf-8",
    )
    # Hidden=true tombstone masks lower-precedence XDG entry.
    (override_share / "applications" / "masked.desktop").write_text(
        "[Desktop Entry]\nType=Application\nName=Masked\nExec=masked\nHidden=true\n",
        encoding="utf-8",
    )
    (base_share / "applications" / "masked.desktop").write_text(
        "[Desktop Entry]\n"
        "Type=Application\n"
        "Name=Should Remain Masked\n"
        "Exec=masked\n"
        "Categories=Graphics;\n",
        encoding="utf-8",
    )
    # Subdirectory entry uses FreeDesktop dash-joined Desktop File ID.
    (base_share / "applications" / "kde4" / "kwrite.desktop").write_text(
        "[Desktop Entry]\n"
        "Type=Application\n"
        "Name=KWrite\n"
        "Exec=kwrite\n"
        "Categories=Qt;KDE;Utility;TextEditor;\n",
        encoding="utf-8",
    )
    # Symlink cycle inside applications/ must terminate cleanly.
    cycle_link = base_share / "applications" / "loop"
    cycle_link.symlink_to(base_share / "applications")

    monkeypatch.setenv("XDG_DATA_DIRS", f"{override_share}:{base_share}")
    index = package_metadata.SystemDesktopIndex()

    assert "editor.desktop" in index.desktop_map
    assert index.desktop_map["editor.desktop"].name == "Override Editor"
    assert index.desktop_map["editor.desktop"].categories == {
        "development",
        "texteditor",
    }
    assert "masked.desktop" not in index.desktop_map
    assert "kde4-kwrite.desktop" in index.desktop_map
    assert index.desktop_map["kde4-kwrite.desktop"].desktop_id == "kde4-kwrite.desktop"
    assert "kwrite.desktop" not in index.desktop_map

    assert (
        package_metadata.desktop_file_id_from_path(
            "/usr/share/applications/kde4/kwrite.desktop"
        )
        == "kde4-kwrite.desktop"
    )
    assert (
        package_metadata.desktop_file_id_from_path(
            "/usr/share/applications-extra/foo.desktop"
        )
        is None
    )
    assert (
        package_metadata.desktop_file_id_from_path("/etc/xdg/autostart/foo.desktop")
        is None
    )


def test_classifier_respects_dbus_activatable_and_tryexec_desktop_entries(
    tmp_path, monkeypatch
) -> None:
    from dendro.core.package_classifier import ProductionTaxonomyEngine
    from dendro.core.taxonomy import TaxonomyInputScope

    xdg_share = tmp_path / "share"
    apps = xdg_share / "applications"
    apps.mkdir(parents=True)
    (apps / "org.example.DbusPaint.desktop").write_text(
        "[Desktop Entry]\n"
        "Type=Application\n"
        "Name=DbusPaint\n"
        "DBusActivatable=true\n"
        "Categories=Graphics;RasterGraphics;\n",
        encoding="utf-8",
    )
    (apps / "missing-exec.desktop").write_text(
        "[Desktop Entry]\n"
        "Type=Application\n"
        "Name=MissingExec\n"
        "Exec=missing-exec\n"
        "TryExec=/definitely/missing/dendro-nonexistent-binary\n"
        "Categories=Graphics;\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("XDG_DATA_DIRS", str(xdg_share))
    package_metadata.SystemDesktopIndex.reset_instance()
    appstream = package_metadata.AppStreamCatalog(load_catalog=False)
    comps = package_metadata.FedoraCompsCatalog(load_comps=False)

    dbus_decision = ProductionTaxonomyEngine.classify(
        name="dbuspaint",
        summary="Painting app",
        anatomy=PackagePhysicalAnatomy.from_file_paths(
            ["/usr/share/applications/org.example.DbusPaint.desktop"],
            [],
        ),
        desktop_entry_files=["org.example.DbusPaint.desktop"],
        appstream=appstream,
        comps=comps,
        input_scope=TaxonomyInputScope.INSTALLED_MANIFEST,
    )
    assert dbus_decision.primary_category == "user_apps"
    assert dbus_decision.sub_category == "desktop_graphics"
    assert dbus_decision.flags["is_desktop_app"] is True

    missing_tryexec_decision = ProductionTaxonomyEngine.classify(
        name="helper-cli",
        summary="CLI helper",
        anatomy=PackagePhysicalAnatomy.from_file_paths(
            ["/usr/bin/helper-cli", "/usr/share/applications/missing-exec.desktop"],
            [],
        ),
        desktop_entry_files=["missing-exec.desktop"],
        appstream=appstream,
        comps=comps,
        input_scope=TaxonomyInputScope.INSTALLED_MANIFEST,
    )
    assert missing_tryexec_decision.flags["is_desktop_app"] is False
    package_metadata.SystemDesktopIndex.reset_instance()


def test_section3_anatomy_sonames_icon_themes_and_subcategory_guards() -> None:
    from dendro.core.package_subcategories import cli_subcategory, desktop_subcategory

    hicolor = PackagePhysicalAnatomy.from_manifest_data(
        ["/usr/share/icons/hicolor/scalable/apps"], []
    )
    assert hicolor.has_themes_dir is False

    themed = PackagePhysicalAnatomy.from_file_paths(
        [
            "/usr/share/icons/CustomTheme/index.theme",
            "/usr/share/icons/CustomTheme/16x16/apps/app.png",
        ],
        [],
    )
    assert themed.has_themes_dir is True

    app_icons_only = PackagePhysicalAnatomy.from_file_paths(
        ["/usr/share/icons/CustomIcons/16x16/apps/app.png"],
        [],
    )
    assert app_icons_only.has_themes_dir is False

    kio = PackagePhysicalAnatomy.from_manifest_data(
        ["/usr/lib64/qt5/plugins/kf5/kio"],
        ["libKF5KIOCore.so.5()(64bit)"],
    )
    assert kio.has_kio_dir is True
    assert kio.has_gui_toolkit_dir is False

    rpm_macros = PackagePhysicalAnatomy.from_manifest_data(
        ["/usr/lib/rpm/macros.d", "/usr/lib/rpm/lua"],
        ["rpm_macro(test)", "rpm_lua(test.mod)"],
    )
    assert rpm_macros.has_development_data_dir is True
    assert rpm_macros.provides_rpm_macro is True

    elf = PackagePhysicalAnatomy.from_manifest_data(
        [],
        [
            "libGL.so.1",
            "perl(Foo.so)",
            "bundled(libfoo.so.2)",
            "libfoo.source(1)",
            "libbar.so.3()(64bit)",
        ],
    )
    assert elf.exported_sonames == ["libGL.so.1", "libbar.so.3()(64bit)"]

    assert desktop_subcategory({"science", "astronomy"}, False) == "desktop_science"
    assert desktop_subcategory({"education"}, False) == "desktop_science"
    assert desktop_subcategory({"healthfitness"}, False) == "desktop_science"
    assert desktop_subcategory({"science", "office"}, False) == "desktop_office"
    assert desktop_subcategory({"graphics", "science"}, False) == "desktop_graphics"
    assert (
        desktop_subcategory(set(), False, mimehandlers=("application/pdf",))
        == "desktop_office"
    )
    assert (
        desktop_subcategory(set(), False, summary="Scientific molecular viewer")
        == "desktop_science"
    )

    assert cli_subcategory("htop", ()) == "cli_monitoring"
    assert cli_subcategory("net-tools", ()) == "cli_networking"
    assert cli_subcategory("catimg", ()) == "cli_general"
    assert cli_subcategory("fd-find", ()) == "cli_search_files"
