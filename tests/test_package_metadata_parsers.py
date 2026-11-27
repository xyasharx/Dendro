"""Focused fixtures for secure Comps and AppStream metadata parsing."""

from __future__ import annotations

import gzip
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


def test_appstream_gi_pool_integration_and_box_lifetime(
    tmp_path, monkeypatch
) -> None:
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
