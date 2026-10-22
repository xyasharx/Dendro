"""Focused fixtures for secure Comps and AppStream metadata parsing."""

from __future__ import annotations

import gzip
from io import BytesIO

import pytest
from defusedxml.common import EntitiesForbidden

from core.package_classifier import ProductionTaxonomyEngine
from core.package_metadata import (
    AppStreamCatalog,
    FedoraCompsCatalog,
    open_compressed_xml_metadata,
)
from core.taxonomy import ClassificationEvidence


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
