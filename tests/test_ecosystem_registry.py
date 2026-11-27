"""Golden coverage for declarative RPM ecosystem classification."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from dendro.core.ecosystems import (
    ECOSYSTEM_SPECS,
    ecosystem_ids_for_capability,
    ecosystem_ids_for_directory,
    ecosystem_ids_for_package_name,
    ecosystem_registry_integrity_errors,
)
from dendro.core.package_classifier import ProductionTaxonomyEngine
from dendro.core.package_metadata import (
    AppStreamCatalog,
    FedoraCompsCatalog,
    PackagePhysicalAnatomy,
)
from dendro.core.taxonomy import (
    ClassificationEvidence,
    TaxonomyInputScope,
    category_matches,
    category_spec,
    matching_sidebar_categories,
)


def _empty_catalogs() -> tuple[AppStreamCatalog, FedoraCompsCatalog]:
    return AppStreamCatalog(load_catalog=False), FedoraCompsCatalog(load_comps=False)


@pytest.mark.parametrize(
    ("name", "capability", "expected_category", "expected_tag"),
    (
        ("python3-example", "python3dist(example)", "python_pkgs", "Python"),
        ("rust-example", "crate(example)", "rust_pkgs", "Rust"),
        ("java-example", "mvn(org.example:module)", "jvm_pkgs", "Java/JVM"),
        ("nodejs-example", "npm(example)", "nodejs_pkgs", "Node.js"),
        ("perl-Example-Module", "perl(Example::Module)", "perl_pkgs", "Perl"),
        ("texlive-example", "tex(example.sty)", "tex_pkgs", "TeX/LaTeX"),
        ("golang-example", "golang(example.org/module)", "go_pkgs", "Go"),
        ("ghc-Example", "ghc-prof(Example-1.0)", "haskell_pkgs", "Haskell"),
        ("rubygem-example", "rubygem(example)", "ruby_pkgs", "Ruby"),
        ("R-example", "R(example)", "r_pkgs", "R/CRAN"),
        ("php-example", "php-composer(example/library)", "php_pkgs", "PHP"),
        ("ocaml-example", "ocaml(Example)", "ocaml_pkgs", "OCaml"),
        ("mingw64-example", "mingw64(example.dll)", "mingw_pkgs", "MinGW"),
        ("mono-example", "mono(Example.Component)", "dotnet_pkgs", ".NET/Mono"),
        ("lua-example", "lua(example)", "lua_pkgs", "Lua"),
        ("tcl-example", "tcl(example)", "tcltk_pkgs", "Tcl/Tk"),
        ("erlang-example", "erlang(example)", "erlang_pkgs", "Erlang/Elixir"),
    ),
)
def test_rpm_capability_identifies_registered_ecosystem(
    name: str,
    capability: str,
    expected_category: str,
    expected_tag: str,
) -> None:
    appstream, comps = _empty_catalogs()
    anatomy = PackagePhysicalAnatomy.from_manifest_data([], [capability])

    decision = ProductionTaxonomyEngine.classify(
        name,
        anatomy=anatomy,
        provides=[capability],
        appstream=appstream,
        comps=comps,
        input_scope=TaxonomyInputScope.REMOTE_METADATA,
    )

    assert decision.primary_category == expected_category
    assert decision.evidence == ClassificationEvidence.RPM_MANIFEST
    assert decision.secondary_tags == (expected_tag,)
    assert category_spec(expected_category) is not None


@pytest.mark.parametrize(
    ("directory", "expected_id"),
    (
        ("/usr/share/perl5/vendor_perl/Example", "perl"),
        ("/usr/share/texlive/texmf-dist/tex", "tex"),
        ("/usr/x86_64-w64-mingw32/sys-root", "mingw"),
        ("/usr/lib64/ocaml/example", "ocaml"),
    ),
)
def test_runtime_paths_are_registered_as_manifest_evidence(
    directory: str, expected_id: str
) -> None:
    anatomy = PackagePhysicalAnatomy.from_manifest_data([directory], [])

    assert expected_id in anatomy.ecosystem_path_ids
    assert expected_id in anatomy.ecosystem_ids
    assert expected_id in ecosystem_ids_for_directory(directory)


def test_namespace_fallback_is_conservative_and_case_normalized() -> None:
    assert ecosystem_ids_for_package_name("perl-Example") == frozenset({"perl"})
    assert ecosystem_ids_for_package_name("R-Example") == frozenset({"r"})
    assert ecosystem_ids_for_package_name("perlite-tools") == frozenset()
    assert ecosystem_ids_for_capability("PerL(Example::Module)") == frozenset({"perl"})


def test_generic_cli_module_keeps_cli_primary_and_ecosystem_facet() -> None:
    appstream, comps = _empty_catalogs()
    anatomy = PackagePhysicalAnatomy.from_manifest_data(
        ["/usr/bin"], ["perl(Example::Module)"]
    )

    decision = ProductionTaxonomyEngine.classify(
        "example-tool",
        anatomy=anatomy,
        provides=["perl(Example::Module)"],
        appstream=appstream,
        comps=comps,
    )

    assert decision.primary_category == "cli_tools"
    assert decision.secondary_tags == ("Perl",)
    assert "Ecosystem facet Perl & CPAN: RPM virtual capability" in decision.rationale


def test_manifest_evidence_outranks_a_conflicting_conventional_name() -> None:
    appstream, comps = _empty_catalogs()
    provides = ["tex(example.sty)"]
    anatomy = PackagePhysicalAnatomy.from_manifest_data([], provides)

    decision = ProductionTaxonomyEngine.classify(
        "perl-looking-package",
        anatomy=anatomy,
        provides=provides,
        appstream=appstream,
        comps=comps,
    )

    assert decision.primary_category == "tex_pkgs"
    assert decision.secondary_tags == ("Perl", "TeX/LaTeX")


def test_conflicting_ecosystems_remain_visible_without_an_arbitrary_primary() -> None:
    appstream, comps = _empty_catalogs()
    provides = ["perl(Example::Module)", "tex(example.sty)"]
    anatomy = PackagePhysicalAnatomy.from_manifest_data([], provides)

    decision = ProductionTaxonomyEngine.classify(
        "ambiguous-runtime-bundle",
        anatomy=anatomy,
        provides=provides,
        appstream=appstream,
        comps=comps,
        input_scope=TaxonomyInputScope.REMOTE_METADATA,
    )

    assert decision.primary_category == "unclassified"
    assert decision.evidence == ClassificationEvidence.INSUFFICIENT_METADATA
    assert decision.secondary_tags == ("Perl", "TeX/LaTeX")
    assert "multiple ecosystems" in decision.rationale[0]


def test_supported_namespace_can_resolve_a_multi_ecosystem_manifest() -> None:
    appstream, comps = _empty_catalogs()
    provides = ["perl(Example::Module)", "tex(example.sty)"]
    anatomy = PackagePhysicalAnatomy.from_manifest_data([], provides)

    decision = ProductionTaxonomyEngine.classify(
        "perl-document-tool",
        anatomy=anatomy,
        provides=provides,
        appstream=appstream,
        comps=comps,
    )

    assert decision.primary_category == "perl_pkgs"
    assert decision.secondary_tags == ("Perl", "TeX/LaTeX")


def test_ecosystem_category_matches_a_secondary_tag_without_new_flags() -> None:
    package = SimpleNamespace(
        parent_pillar="pillar_cli",
        primary_category="cli_tools",
        sub_category="cli_general",
        secondary_tags=("Perl",),
        is_cli_tool=True,
    )

    assert category_matches(package, "perl_pkgs") is True
    assert "perl_pkgs" in matching_sidebar_categories(package)
    assert "cli_tools" in matching_sidebar_categories(package)


def test_ecosystem_registry_is_complete_and_categories_are_registered() -> None:
    assert ecosystem_registry_integrity_errors() == ()
    for spec in ECOSYSTEM_SPECS:
        category = category_spec(spec.category_id)
        assert category is not None
        assert category.facet_tag == spec.tag


def test_versioned_python_dist_and_site_packages_match_python_ecosystem() -> None:
    appstream, comps = _empty_catalogs()
    provides = ["python3.13dist(example)"]
    anatomy = PackagePhysicalAnatomy.from_manifest_data(
        ["/usr/lib/python3.13/site-packages/example"],
        provides,
    )

    assert "python" in anatomy.ecosystem_capability_ids
    assert "python" in anatomy.ecosystem_path_ids
    decision = ProductionTaxonomyEngine.classify(
        "python3-example-versioned",
        anatomy=anatomy,
        provides=provides,
        appstream=appstream,
        comps=comps,
        input_scope=TaxonomyInputScope.INSTALLED_MANIFEST,
    )
    assert decision.primary_category == "python_pkgs"
    assert decision.secondary_tags == ("Python",)


def test_rust_and_go_source_devel_packages_stay_in_their_ecosystems() -> None:
    appstream, comps = _empty_catalogs()

    rust_decision = ProductionTaxonomyEngine.classify(
        "rust-serde-devel",
        anatomy=PackagePhysicalAnatomy.from_manifest_data(
            ["/usr/share/cargo/registry/serde-1.0"], ["crate(serde)"]
        ),
        provides=["crate(serde)"],
        appstream=appstream,
        comps=comps,
        input_scope=TaxonomyInputScope.INSTALLED_MANIFEST,
    )
    assert rust_decision.primary_category == "rust_pkgs"
    assert rust_decision.evidence == ClassificationEvidence.RPM_MANIFEST

    go_decision = ProductionTaxonomyEngine.classify(
        "golang-github-pkg-errors-devel",
        anatomy=PackagePhysicalAnatomy.from_manifest_data(
            ["/usr/share/gocode/src/github.com/pkg/errors"],
            ["golang(github.com/pkg/errors)"],
        ),
        provides=["golang(github.com/pkg/errors)"],
        appstream=appstream,
        comps=comps,
        input_scope=TaxonomyInputScope.INSTALLED_MANIFEST,
    )
    assert go_decision.primary_category == "go_pkgs"
    assert go_decision.evidence == ClassificationEvidence.RPM_MANIFEST


def test_rpm_macros_and_bundled_ecosystems_behave_as_expected() -> None:
    appstream, comps = _empty_catalogs()

    macros_decision = ProductionTaxonomyEngine.classify(
        "fonts-rpm-macros",
        anatomy=PackagePhysicalAnatomy.from_manifest_data(
            ["/usr/lib/rpm/macros.d"], ["rpm_macro(fontpkg)"]
        ),
        provides=["rpm_macro(fontpkg)"],
        appstream=appstream,
        comps=comps,
        input_scope=TaxonomyInputScope.INSTALLED_MANIFEST,
    )
    assert macros_decision.primary_category == "devel"

    caddy_provides = [
        "user(caddy)",
        "group(caddy)",
        "bundled(golang(github.com/caddyserver/caddy/v2))",
    ]
    caddy_decision = ProductionTaxonomyEngine.classify(
        "caddy",
        anatomy=PackagePhysicalAnatomy.from_manifest_data([], caddy_provides),
        provides=caddy_provides,
        appstream=appstream,
        comps=comps,
        input_scope=TaxonomyInputScope.REMOTE_METADATA,
    )
    assert caddy_decision.primary_category == "systemd_services"
    assert "Go" in caddy_decision.secondary_tags

    ripgrep_provides = ["bundled(crate(ripgrep))"]
    ripgrep_decision = ProductionTaxonomyEngine.classify(
        "ripgrep",
        anatomy=PackagePhysicalAnatomy.from_manifest_data(
            ["/usr/bin"], ripgrep_provides
        ),
        provides=ripgrep_provides,
        appstream=appstream,
        comps=comps,
        input_scope=TaxonomyInputScope.INSTALLED_MANIFEST,
    )
    assert ripgrep_decision.primary_category == "cli_tools"
    assert "Rust" in ripgrep_decision.secondary_tags

    bundled_only_decision = ProductionTaxonomyEngine.classify(
        "bundled-rust-library",
        anatomy=PackagePhysicalAnatomy.from_manifest_data(
            [], ["bundled(crate(example))"]
        ),
        provides=["bundled(crate(example))"],
        appstream=appstream,
        comps=comps,
        input_scope=TaxonomyInputScope.REMOTE_METADATA,
    )
    assert bundled_only_decision.primary_category == "rust_pkgs"
    assert bundled_only_decision.evidence == ClassificationEvidence.RPM_MANIFEST


def test_compiled_ecosystem_indices_match_declarative_specs_for_all_inputs() -> None:
    sample_capabilities = [
        "",
        "   ",
        "libc.so.6()(64bit)",
        "pkgconfig(zlib)",
        "python3dist(pytest)",
        "python3.13dist(rich)",
        "PYTHON3DIST(Requests)",
        "crate(serde)",
        "mvn(org.example:core)",
        "osgi(org.example.bundle)",
        "npm(@scope/pkg)",
        "perl(Strict)",
        "PerL(Example::Module)",
        "tex(latex.fmt)",
        "golang(example.org/mod)",
        "ghc-prof(base-4.18)",
        "ghc-devel(base-4.18)",
        "rubygem(rake)",
        "R(ggplot2)",
        "php-composer(vendor/pkg)",
        "ocaml(Stdlib)",
        "mingw32(zlib1.dll)",
        "mingw64-pkgconfig(zlib)",
        "mono(System.Core)",
        "lua(lpeg)",
        "tcl(8.6)",
        "tk(8.6)",
        "elixir(plug)",
        "erlang(cowboy)",
    ]
    for cap in sample_capabilities:
        expected = frozenset(
            spec.identifier for spec in ECOSYSTEM_SPECS if spec.matches_capability(cap)
        )
        assert ecosystem_ids_for_capability(cap) == expected

    sample_dirs = [
        "",
        "/usr/bin",
        "/usr/lib64",
        "/usr/share/doc/pkg",
        "/usr/lib/python3.13/site-packages",
        "/usr/lib64/python3.12/site-packages/pkg",
        "/usr/share/cargo/registry/serde-1.0",
        "/usr/share/java/foo",
        "/usr/lib/jvm/java-21",
        "/usr/lib/node_modules/eslint",
        "/usr/share/perl5/vendor_perl",
        "/usr/lib64/perl5/auto",
        "/usr/share/texlive/texmf-dist",
        "/usr/share/gocode/src/example",
        "/usr/lib64/ghc-9.6",
        "/usr/share/gems/gems/rake",
        "/usr/lib64/R/library",
        "/usr/share/php/PSR",
        "/usr/lib64/ocaml/unix/",
        "/usr/x86_64-w64-mingw32/sys-root/mingw/bin",
        "/usr/lib/dotnet/shared",
        "/usr/lib64/mono/4.5",
        "/usr/share/lua/5.4",
        "/usr/lib64/tcl8.6",
        "/usr/lib64/erlang/lib",
    ]
    for directory in sample_dirs:
        expected = frozenset(
            spec.identifier
            for spec in ECOSYSTEM_SPECS
            if spec.matches_directory(directory)
        )
        assert ecosystem_ids_for_directory(directory) == expected

    sample_names = [
        "",
        "bash",
        "glibc",
        "python3-rich",
        "python3.13-pytest",
        "rust-serde-devel",
        "perl-Carp",
        "perlite-tools",
        "R-CRAN-ggplot2",
        "mingw64-zlib",
        "dotnet-runtime-8.0",
        "luajit-devel",
        "erlang-stdlib",
    ]
    for pkg_name in sample_names:
        expected = frozenset(
            spec.identifier
            for spec in ECOSYSTEM_SPECS
            if spec.matches_package_name(pkg_name)
        )
        assert ecosystem_ids_for_package_name(pkg_name) == expected


def test_classifier_computes_ecosystem_matches_once_per_classify(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    appstream, comps = _empty_catalogs()
    calls = 0
    original = ProductionTaxonomyEngine._compute_ecosystem_matches

    def spy_compute(context):
        nonlocal calls
        calls += 1
        return original(context)

    monkeypatch.setattr(
        ProductionTaxonomyEngine,
        "_compute_ecosystem_matches",
        staticmethod(spy_compute),
    )

    c_binary_anatomy = PackagePhysicalAnatomy.from_manifest_data(
        ["/usr/bin"], ["libc.so.6()(64bit)"]
    )
    decision_cli = ProductionTaxonomyEngine.classify(
        "grep",
        summary="Pattern matcher",
        anatomy=c_binary_anatomy,
        provides=["libc.so.6()(64bit)"],
        appstream=appstream,
        comps=comps,
        requires=["libc.so.6()(64bit)"],
    )
    assert decision_cli.primary_category == "cli_tools"
    assert calls == 1

    calls = 0
    rust_cli_anatomy = PackagePhysicalAnatomy.from_manifest_data(
        ["/usr/bin"], ["bundled(crate(ripgrep))", "crate(foo)"]
    )
    decision_rust = ProductionTaxonomyEngine.classify(
        "ripgrep",
        summary="Fast search",
        anatomy=rust_cli_anatomy,
        provides=["bundled(crate(ripgrep))", "crate(foo)"],
        appstream=appstream,
        comps=comps,
    )
    assert decision_rust.primary_category == "cli_tools"
    assert "Rust" in decision_rust.secondary_tags
    assert calls == 1
