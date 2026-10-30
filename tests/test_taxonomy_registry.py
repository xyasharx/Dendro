"""Golden/parity tests for the shared taxonomy registry and classifier boundary."""

from __future__ import annotations

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from core import backend, package_classifier
from core.backend import PackageInfo, PackageState
from core.models import DependencyTreeModel, PackageFilterProxyModel
from core.package_classifier import (
    ProductionTaxonomyEngine,
    package_role_policy_integrity_errors,
)
from core.package_metadata import (
    AppStreamCatalog,
    FedoraCompsCatalog,
    PackagePhysicalAnatomy,
)
from core.taxonomy import (
    ClassificationEvidence,
    RemovalRiskLevel,
    TaxonomyInputScope,
    category_matches,
    category_removal_risk,
    category_spec,
    matching_sidebar_categories,
    registry_integrity_errors,
    sidebar_category_ids,
)
from ui.inspector_panel import PackageInspectorPanel
from ui.sidebar import CategorySidebar, SidebarRoles


def _empty_catalogs() -> tuple[AppStreamCatalog, FedoraCompsCatalog]:
    return AppStreamCatalog(load_catalog=False), FedoraCompsCatalog(load_comps=False)


def test_backend_keeps_taxonomy_import_compatibility() -> None:
    assert backend.ProductionTaxonomyEngine is ProductionTaxonomyEngine
    assert backend.AppStreamCatalog is AppStreamCatalog
    assert backend.FedoraCompsCatalog is FedoraCompsCatalog
    assert backend.PackagePhysicalAnatomy is PackagePhysicalAnatomy


def test_every_explicit_package_policy_emits_a_coherent_registry_path() -> None:
    appstream, comps = _empty_catalogs()
    for package_name in package_classifier.PACKAGE_TAXONOMY_OVERRIDES:
        decision = ProductionTaxonomyEngine.classify(
            package_name,
            appstream=appstream,
            comps=comps,
        )
        assert category_spec(decision.primary_category) is not None
        assert category_spec(decision.sub_category) is not None
        assert decision.parent_pillar in {
            "pillar_apps",
            "pillar_cli",
            "pillar_hardware",
            "pillar_system",
            "pillar_libs",
            "pillar_maintenance",
        }


def test_remote_classification_skips_desktop_index_when_no_desktop_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    appstream, comps = _empty_catalogs()
    monkeypatch.setattr(
        package_classifier.SystemDesktopIndex,
        "get_instance",
        lambda: pytest.fail(
            "remote metadata classification must not walk desktop entries"
        ),
    )

    decision = ProductionTaxonomyEngine.classify(
        "remote-only-package",
        appstream=appstream,
        comps=comps,
        input_scope=TaxonomyInputScope.REMOTE_METADATA,
    )

    assert decision.primary_category == "unclassified"


def test_unknown_remote_metadata_is_neutral_not_a_guessed_c_library() -> None:
    appstream, comps = _empty_catalogs()
    decision = ProductionTaxonomyEngine.classify(
        "opaque-remote-package",
        appstream=appstream,
        comps=comps,
        input_scope=TaxonomyInputScope.REMOTE_METADATA,
    )

    assert decision.primary_category == "unclassified"
    assert decision.sub_category == "unclassified"
    assert decision.parent_pillar == "pillar_maintenance"
    assert decision.evidence == ClassificationEvidence.INSUFFICIENT_METADATA
    assert decision.confidence == 0.0
    assert decision.flags["is_c_lib"] is False
    assert "Remote metadata" in decision.rationale[0]

    unknown_package = PackageInfo(
        name="opaque-remote-package",
        parent_pillar=decision.parent_pillar,
        primary_category=decision.primary_category,
        sub_category=decision.sub_category,
        classification_evidence=decision.evidence.value,
        classification_input_scope=decision.input_scope.value,
    )
    safety = ProductionTaxonomyEngine.assess_removal_safety(unknown_package)
    assert safety.level == RemovalRiskLevel.REVIEW
    assert "Classification Incomplete" in safety.title

    # Historical/model data can contain a remembered pillar without the RPM
    # evidence that justified it. That alone must not imply library risk.
    stale_library_hint = PackageInfo(
        name="opaque-library-looking-package",
        parent_pillar="pillar_libs",
        primary_category="c_libs",
        sub_category="c_libs",
    )
    assert (
        ProductionTaxonomyEngine.assess_removal_safety(stale_library_hint).level
        == RemovalRiskLevel.REVIEW
    )


def test_representative_manifest_golden_categories_do_not_guess_unknown_data_as_libraries() -> (
    None
):
    appstream, comps = _empty_catalogs()
    cases = (
        (
            "0ad-data",
            PackagePhysicalAnatomy(),
            "unclassified",
            ClassificationEvidence.INSUFFICIENT_METADATA,
        ),
        (
            "appstream-data",
            PackagePhysicalAnatomy(),
            "system_data",
            ClassificationEvidence.MANUAL_OVERRIDE,
        ),
        (
            "audacity-manual",
            PackagePhysicalAnatomy(has_docs_dir=True),
            "unclassified",
            ClassificationEvidence.INSUFFICIENT_METADATA,
        ),
        (
            "glibc-langpack-fr",
            PackagePhysicalAnatomy(),
            "locales",
            ClassificationEvidence.NAME_HEURISTIC,
        ),
        (
            "example-meta-package",
            PackagePhysicalAnatomy(),
            "unclassified",
            ClassificationEvidence.INSUFFICIENT_METADATA,
        ),
        (
            "example-daemon",
            PackagePhysicalAnatomy(has_systemd_system=True),
            "systemd_services",
            ClassificationEvidence.RPM_MANIFEST,
        ),
        (
            "example-tool",
            PackagePhysicalAnatomy(has_binaries=True, has_user_bin=True),
            "cli_tools",
            ClassificationEvidence.RPM_MANIFEST,
        ),
        (
            "example-firmware",
            PackagePhysicalAnatomy(has_firmware_dir=True),
            "firmware",
            ClassificationEvidence.RPM_MANIFEST,
        ),
        (
            "libexample",
            PackagePhysicalAnatomy(exported_sonames=["libexample.so.1()(64bit)"]),
            "c_libs",
            ClassificationEvidence.RPM_MANIFEST,
        ),
    )

    for name, anatomy, expected_category, expected_evidence in cases:
        decision = ProductionTaxonomyEngine.classify(
            name,
            anatomy=anatomy,
            appstream=appstream,
            comps=comps,
        )
        assert decision.primary_category == expected_category, name
        assert decision.evidence == expected_evidence, name


@pytest.mark.parametrize(
    ("package_name", "expected_category"),
    (
        ("adobe-mappings-cmap", "reference_data"),
        ("adobe-mappings-cmap-deprecated", "reference_data"),
        ("adobe-mappings-pdf", "reference_data"),
        ("appstream-data", "system_data"),
        ("catatonit", "cli_tools"),
        ("chromium-common", "desktop_addons"),
        ("cmake-data", "devel"),
        ("cmake-filesystem", "devel"),
        ("color-filesystem", "system_data"),
        ("cups-filesystem", "systemd_services"),
        ("dbus", "systemd_services"),
        ("desktop-backgrounds-kde", "themes"),
        ("dnf5daemon-server-polkit", "systemd_services"),
        ("docbook-dtds", "reference_data"),
        ("docbook-style-xsl", "reference_data"),
        ("docker-compose", "cli_tools"),
        ("elfutils-default-yama-scope", "security_pkgs"),
        ("emacs-filesystem", "cli_tools"),
        ("f44-backgrounds-kde", "themes"),
        ("fedora-gpg-keys", "repository_sources"),
        ("fedora-logos-httpd", "themes"),
        ("fedora-repos", "repository_sources"),
        ("fonts-filesystem", "fonts"),
        ("fuse-common", "fedora_core"),
        ("git", "cli_tools"),
        ("glibc-locale-source", "locales"),
        ("glycin-loaders", "media_plugins"),
        ("google-noto-fonts-common", "fonts"),
        ("hunspell-en-US", "locales"),
        ("hunspell-filesystem", "locales"),
        ("hwdata", "system_data"),
        ("kbd-legacy", "system_data"),
        ("kbd-misc", "system_data"),
        ("kde-settings", "desktop_addons"),
        ("kde-settings-plasma", "desktop_addons"),
        ("kde-settings-pulseaudio", "desktop_addons"),
        ("kde-wallpapers", "themes"),
        ("kf5-frameworkintegration", "gui_toolkits"),
        ("kf5-sonnet", "gui_toolkits"),
        ("kf6-frameworkintegration", "gui_toolkits"),
        ("kvantum-data", "themes"),
        ("libdnf5-plugin-appstream", "fedora_core"),
        ("libksysguard-common", "desktop_addons"),
        ("libssh-config", "system_data"),
        ("libwacom-data", "system_data"),
        ("libX11-common", "gui_toolkits"),
        ("llvm-filesystem", "devel"),
        ("mesa-filesystem", "graphics_drivers"),
        ("moby-filesystem", "cli_tools"),
        ("ncurses-base", "fedora_core"),
        ("nginx-mimetypes", "systemd_services"),
        ("nginx-mod-fancyindex", "systemd_services"),
        ("pcre2-syntax", "reference_data"),
        ("pkgconf-m4", "devel"),
        ("plasma-breeze-common", "themes"),
        ("plasma-lookandfeel-fedora", "themes"),
        ("plasma-workspace-common", "desktop_addons"),
        ("poppler-data", "reference_data"),
        ("publicsuffix-list-dafsa", "system_data"),
        ("qt-settings", "gui_toolkits"),
        ("rdma-core-common", "system_data"),
        ("rootfiles", "fedora_core"),
        ("rpmfusion-free-release", "repository_sources"),
        ("rpmfusion-nonfree-release", "repository_sources"),
        ("samba-common", "systemd_services"),
        ("tesseract-common", "reference_data"),
        ("tzdata", "system_data"),
        ("vim-data", "cli_tools"),
        ("vim-filesystem", "cli_tools"),
        ("xml-common", "reference_data"),
        ("xorg-x11-drv-amdgpu", "graphics_drivers"),
        ("xorg-x11-drv-ati", "graphics_drivers"),
        ("xorg-x11-drv-nouveau", "graphics_drivers"),
        ("xorg-x11-drv-qxl", "graphics_drivers"),
        ("xorg-x11-fonts-100dpi", "fonts"),
        ("xorg-x11-fonts-75dpi", "fonts"),
        ("xorg-x11-fonts-misc", "fonts"),
        ("xorg-x11-server-common", "graphics_drivers"),
    ),
)
def test_installed_support_package_corpus_uses_explicit_role_policy(
    package_name: str,
    expected_category: str,
) -> None:
    """The reported Fedora KDE support-RPM corpus must not silently regress."""
    appstream, comps = _empty_catalogs()
    decision = ProductionTaxonomyEngine.classify(
        package_name,
        anatomy=PackagePhysicalAnatomy(),
        appstream=appstream,
        comps=comps,
    )

    assert decision.primary_category == expected_category
    assert decision.evidence == ClassificationEvidence.MANUAL_OVERRIDE


@pytest.mark.parametrize(
    ("directory", "expected_category"),
    (
        ("/etc/yum.repos.d", "repository_sources"),
        ("/usr/share/X11/fonts/100dpi", "fonts"),
        ("/usr/share/wallpapers/example", "themes"),
        ("/usr/share/docbook/xml", "reference_data"),
        ("/usr/share/zoneinfo/Europe", "system_data"),
        ("/usr/share/cmake/Modules", "devel"),
        ("/usr/lib64/xorg/modules/drivers", "graphics_drivers"),
    ),
)
def test_manifest_data_directories_classify_third_party_rpms(
    directory: str,
    expected_category: str,
) -> None:
    """FHS evidence must work without any Fedora-specific package name."""
    appstream, comps = _empty_catalogs()
    anatomy = PackagePhysicalAnatomy.from_manifest_data([directory], [])
    decision = ProductionTaxonomyEngine.classify(
        "third-party-example",
        anatomy=anatomy,
        appstream=appstream,
        comps=comps,
    )

    assert decision.primary_category == expected_category
    assert decision.evidence == ClassificationEvidence.RPM_MANIFEST


def test_package_role_registry_is_unambiguous_and_emits_valid_paths() -> None:
    assert package_role_policy_integrity_errors() == ()


def test_legacy_lib_firmware_manifest_classifies_microcode_ctl() -> None:
    """Fedora's RPM metadata records Intel microcode below legacy /lib."""
    appstream, comps = _empty_catalogs()
    anatomy = PackagePhysicalAnatomy.from_manifest_data(
        ["/lib/firmware/intel-ucode"], []
    )

    decision = ProductionTaxonomyEngine.classify(
        "microcode_ctl", anatomy=anatomy, appstream=appstream, comps=comps
    )

    assert anatomy.has_firmware_dir
    assert decision.parent_pillar == "pillar_hardware"
    assert decision.primary_category == "firmware"
    assert decision.sub_category == "firmware"
    assert decision.evidence == ClassificationEvidence.RPM_MANIFEST
    assert decision.flags["is_firmware"] is True


def test_explicit_package_policy_preserves_its_leaf_category() -> None:
    appstream, comps = _empty_catalogs()
    decision = ProductionTaxonomyEngine.classify(
        "desktop-file-utils",
        appstream=appstream,
        comps=comps,
    )

    assert decision.primary_category == "cli_tools"
    assert decision.sub_category == "cli_search_files"
    assert decision.evidence == ClassificationEvidence.MANUAL_OVERRIDE


def test_game_category_wins_over_steam_network_and_file_transfer_metadata() -> None:
    """Steam advertises Network and FileTransfer alongside its Game identity."""
    appstream, comps = _empty_catalogs()
    appstream.desktop_packages.add("steam")
    appstream.pkg_xdg_categories["steam"] = {"network", "filetransfer", "game"}

    decision = ProductionTaxonomyEngine.classify(
        "steam",
        summary="Application for managing and playing games on Steam",
        anatomy=PackagePhysicalAnatomy(has_desktop_file=True),
        appstream=appstream,
        comps=comps,
    )

    assert decision.parent_pillar == "pillar_apps"
    assert decision.primary_category == "user_apps"
    assert decision.sub_category == "desktop_games"
    assert decision.evidence == ClassificationEvidence.APPSTREAM
    assert decision.flags["is_desktop_app"] is True


def test_shared_library_requires_affirmative_rpm_manifest_evidence() -> None:
    appstream, comps = _empty_catalogs()
    anatomy = PackagePhysicalAnatomy.from_manifest_data([], ["libsignal.so.2()(64bit)"])
    decision = ProductionTaxonomyEngine.classify(
        "libsignal",
        anatomy=anatomy,
        appstream=appstream,
        comps=comps,
    )

    assert decision.primary_category == "c_libs"
    assert decision.evidence == ClassificationEvidence.RPM_MANIFEST
    assert decision.flags["is_c_lib"] is True


def test_general_cli_category_is_classified_visible_and_filterable(qtbot) -> None:
    appstream, comps = _empty_catalogs()
    decision = ProductionTaxonomyEngine.classify(
        "tally",
        anatomy=PackagePhysicalAnatomy(has_binaries=True, has_user_bin=True),
        appstream=appstream,
        comps=comps,
    )
    package = PackageInfo(
        name="tally",
        parent_pillar=decision.parent_pillar,
        primary_category=decision.primary_category,
        sub_category=decision.sub_category,
        classification_evidence=decision.evidence.value,
        is_cli_tool=decision.flags["is_cli_tool"],
    )

    assert decision.primary_category == "cli_tools"
    assert decision.sub_category == "cli_general"
    assert "cli_general" in sidebar_category_ids()
    assert category_matches(package, "cli_tools")
    assert category_matches(package, "cli_general")

    sidebar = CategorySidebar()
    qtbot.addWidget(sidebar)
    visible_tags = {
        sidebar.item(row).data(SidebarRoles.TagRole)
        for row in range(sidebar.count())
        if sidebar.item(row).data(SidebarRoles.TagRole)
    }
    assert "cli_general" in visible_tags
    assert "unclassified" in visible_tags


def test_inspector_shows_registry_label_and_classification_provenance(qtbot) -> None:
    panel = PackageInspectorPanel()
    qtbot.addWidget(panel)
    panel.set_package_info(
        PackageInfo(
            name="opaque-remote-package",
            summary="Metadata is incomplete",
            parent_pillar="pillar_maintenance",
            primary_category="unclassified",
            sub_category="unclassified",
            classification_evidence=ClassificationEvidence.INSUFFICIENT_METADATA.value,
            classification_input_scope=TaxonomyInputScope.REMOTE_METADATA.value,
        )
    )

    assert "Needs Classification" in panel.taxonomy_badge.text()
    assert "Insufficient metadata" in panel.provenance_label.text()
    assert "Remote repository metadata" in panel.provenance_label.text()
    assert "Classification Incomplete" in panel.safety_status_badge.text()


def test_registry_has_complete_sidebar_and_classifier_presentation_parity() -> None:
    assert registry_integrity_errors() == ()
    for identifier in sidebar_category_ids():
        spec = category_spec(identifier)
        assert spec is not None
        assert spec.label
        assert spec.tooltip
        assert spec.icon_names


def test_registry_owns_category_removal_risk_semantics() -> None:
    assert category_removal_risk("fedora_core") == RemovalRiskLevel.CRITICAL
    assert category_removal_risk("graphics_drivers") == RemovalRiskLevel.HIGH
    assert category_removal_risk("c_libs") == RemovalRiskLevel.MEDIUM
    assert category_removal_risk("fonts") == RemovalRiskLevel.LOW
    assert category_removal_risk("unclassified") == RemovalRiskLevel.REVIEW

    font_package = PackageInfo(
        name="example-fonts",
        parent_pillar="pillar_libs",
        primary_category="fonts",
        sub_category="fonts",
        classification_evidence=ClassificationEvidence.APPSTREAM.value,
        is_font=True,
    )
    assert (
        ProductionTaxonomyEngine.assess_removal_safety(font_package).level
        == RemovalRiskLevel.LOW
    )


def test_high_risk_category_is_not_marked_safe_just_because_it_is_a_leaf() -> None:
    # Also protects older stored PackageInfo records whose derived role flag was
    # not persisted even though their central taxonomy category was.
    driver_leaf = PackageInfo(
        name="example-driver-leaf",
        parent_pillar="pillar_hardware",
        primary_category="graphics_drivers",
        sub_category="graphics_drivers",
        is_orphan=True,
    )

    safety = ProductionTaxonomyEngine.assess_removal_safety(driver_leaf)
    assert safety.level == RemovalRiskLevel.HIGH
    assert "HIGH" in safety.title


def test_proxy_filter_uses_the_same_registry_predicate_as_sidebar_categories(
    qtbot,
) -> None:
    packages = [
        PackageInfo(
            name="tally",
            state=PackageState.INSTALLED,
            parent_pillar="pillar_cli",
            primary_category="cli_tools",
            sub_category="cli_general",
            is_cli_tool=True,
        ),
        PackageInfo(
            name="opaque-remote-package",
            state=PackageState.AVAILABLE,
            parent_pillar="pillar_maintenance",
            primary_category="unclassified",
            sub_category="unclassified",
        ),
    ]
    model = DependencyTreeModel()
    proxy = PackageFilterProxyModel()
    proxy.setSourceModel(model)
    model.set_packages(packages)

    proxy.set_category_filter("cli_general")
    assert proxy.rowCount() == 1
    assert proxy.index(0, 0).data() == "tally"

    proxy.set_category_filter("unclassified")
    assert proxy.rowCount() == 1
    assert proxy.index(0, 0).data() == "opaque-remote-package"

    proxy.set_category_filter("all")
    proxy.set_search_query("cat:cli_general")
    assert proxy.rowCount() == 1
    assert proxy.index(0, 0).data() == "tally"


def test_optimized_sidebar_memberships_match_the_registry_predicate() -> None:
    packages = (
        PackageInfo(
            name="font-package",
            parent_pillar="pillar_libs",
            primary_category="fonts",
            sub_category="fonts",
            is_font=True,
        ),
        PackageInfo(
            name="pending-package",
            parent_pillar="pillar_maintenance",
            primary_category="unclassified",
            sub_category="unclassified",
            state=PackageState.QUEUED_REMOVE,
        ),
    )
    for package in packages:
        expected = frozenset(
            identifier
            for identifier in sidebar_category_ids()
            if category_matches(package, identifier)
        )
        assert matching_sidebar_categories(package) == expected


def test_evidence_compatibility_is_not_presented_as_a_probability() -> None:
    assert ClassificationEvidence.MANUAL_OVERRIDE.legacy_confidence == 1.0
    assert ClassificationEvidence.RPM_MANIFEST.legacy_confidence < 1.0
    assert ClassificationEvidence.INSUFFICIENT_METADATA.legacy_confidence == 0.0
