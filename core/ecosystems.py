"""Declarative RPM ecosystem evidence shared by metadata and classification.

An ecosystem is not inferred from a package's implementation language alone.
Each entry below names concrete RPM capabilities, conventional Fedora package
names, or runtime payload locations that can affirmatively identify a language
or platform ecosystem.  Classifiers retain the underlying signal so a package
with several valid roles can expose ecosystem membership as a facet.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final


@dataclass(frozen=True, slots=True)
class EcosystemSpec:
    """One evidence-backed language or platform ecosystem."""

    identifier: str
    category_id: str
    tag: str
    label: str
    capability_prefixes: tuple[str, ...] = ()
    directory_prefixes: tuple[str, ...] = ()
    exact_package_names: tuple[str, ...] = ()
    package_name_prefixes: tuple[str, ...] = ()

    def matches_capability(self, capability: str) -> bool:
        normalized = capability.strip().casefold()
        return normalized.startswith(self.capability_prefixes)

    def matches_directory(self, directory: str) -> bool:
        normalized = directory.strip().rstrip("/").casefold()
        return any(
            normalized == prefix or normalized.startswith(f"{prefix}/")
            for prefix in self.directory_prefixes
        )

    def matches_package_name(self, package_name: str) -> bool:
        normalized = package_name.strip().casefold()
        return normalized in self.exact_package_names or normalized.startswith(
            self.package_name_prefixes
        )


# The sequence is stable for user-visible facet ordering.  It is not a hidden
# precedence list: the classifier resolves multi-ecosystem conflicts explicitly.
ECOSYSTEM_SPECS: Final[tuple[EcosystemSpec, ...]] = (
    EcosystemSpec(
        "python",
        "python_pkgs",
        "Python",
        "Python",
        capability_prefixes=("python3dist(",),
        exact_package_names=("python", "python3"),
        package_name_prefixes=("python-", "python3-"),
    ),
    EcosystemSpec(
        "rust",
        "rust_pkgs",
        "Rust",
        "Rust & Cargo",
        capability_prefixes=("crate(",),
        directory_prefixes=("/usr/share/cargo/registry",),
        exact_package_names=("cargo", "rust", "rustc"),
        package_name_prefixes=("cargo-", "rust-"),
    ),
    EcosystemSpec(
        "jvm",
        "jvm_pkgs",
        "Java/JVM",
        "Java & JVM",
        capability_prefixes=("mvn(", "osgi("),
        directory_prefixes=("/usr/share/java", "/usr/lib/jvm", "/usr/lib64/jvm"),
        exact_package_names=("java", "maven"),
        package_name_prefixes=("java-", "maven-", "openjdk-"),
    ),
    EcosystemSpec(
        "nodejs",
        "nodejs_pkgs",
        "Node.js",
        "Node.js & npm",
        capability_prefixes=("npm(",),
        directory_prefixes=("/usr/lib/node_modules", "/usr/share/nodejs"),
        exact_package_names=("nodejs", "npm"),
        package_name_prefixes=("nodejs-", "npm-"),
    ),
    EcosystemSpec(
        "perl",
        "perl_pkgs",
        "Perl",
        "Perl & CPAN",
        capability_prefixes=("perl(",),
        directory_prefixes=(
            "/usr/share/perl5",
            "/usr/lib/perl5",
            "/usr/lib64/perl5",
        ),
        exact_package_names=("perl",),
        package_name_prefixes=("perl-",),
    ),
    EcosystemSpec(
        "tex",
        "tex_pkgs",
        "TeX/LaTeX",
        "TeX & LaTeX",
        capability_prefixes=("tex(",),
        directory_prefixes=(
            "/usr/share/texlive",
            "/usr/share/texmf",
            "/usr/lib/texlive",
            "/usr/lib64/texlive",
        ),
        exact_package_names=("tex", "texlive"),
        package_name_prefixes=("tex-", "texlive-"),
    ),
    EcosystemSpec(
        "go",
        "go_pkgs",
        "Go",
        "Go Modules & Toolchain",
        capability_prefixes=("golang(",),
        directory_prefixes=(
            "/usr/share/gocode",
            "/usr/share/golang",
            "/usr/lib/golang",
            "/usr/lib64/golang",
        ),
        exact_package_names=("go", "golang"),
        package_name_prefixes=("compat-golang-", "golang-"),
    ),
    EcosystemSpec(
        "haskell",
        "haskell_pkgs",
        "Haskell",
        "Haskell & GHC",
        capability_prefixes=("ghc(", "ghc-prof("),
        directory_prefixes=("/usr/lib/ghc", "/usr/lib64/ghc"),
        exact_package_names=("ghc",),
        package_name_prefixes=("ghc-", "ghc8.", "ghc9."),
    ),
    EcosystemSpec(
        "ruby",
        "ruby_pkgs",
        "Ruby",
        "Ruby & RubyGems",
        capability_prefixes=("ruby(", "rubygem("),
        directory_prefixes=(
            "/usr/share/gems",
            "/usr/share/ruby",
            "/usr/lib/ruby",
            "/usr/lib64/ruby",
        ),
        exact_package_names=("ruby", "jruby"),
        package_name_prefixes=("ruby-", "rubygem-"),
    ),
    EcosystemSpec(
        "r",
        "r_pkgs",
        "R/CRAN",
        "R & CRAN",
        capability_prefixes=("r(",),
        directory_prefixes=("/usr/lib/r", "/usr/lib64/r", "/usr/share/r"),
        exact_package_names=("r",),
        package_name_prefixes=("r-",),
    ),
    EcosystemSpec(
        "php",
        "php_pkgs",
        "PHP",
        "PHP & Composer",
        capability_prefixes=("php-composer(", "php-pear(", "php-pecl("),
        directory_prefixes=("/usr/share/php", "/usr/lib/php", "/usr/lib64/php"),
        exact_package_names=("php",),
        package_name_prefixes=("php-",),
    ),
    EcosystemSpec(
        "ocaml",
        "ocaml_pkgs",
        "OCaml",
        "OCaml",
        capability_prefixes=("ocaml(",),
        directory_prefixes=("/usr/lib/ocaml", "/usr/lib64/ocaml"),
        exact_package_names=("ocaml",),
        package_name_prefixes=("ocaml-",),
    ),
    EcosystemSpec(
        "mingw",
        "mingw_pkgs",
        "MinGW",
        "MinGW Cross-Compilation",
        capability_prefixes=(
            "mingw32(",
            "mingw64(",
            "mingw32-pkgconfig(",
            "mingw64-pkgconfig(",
        ),
        directory_prefixes=(
            "/usr/i686-w64-mingw32",
            "/usr/x86_64-w64-mingw32",
        ),
        exact_package_names=("mingw32-filesystem", "mingw64-filesystem"),
        package_name_prefixes=("mingw32-", "mingw64-"),
    ),
    EcosystemSpec(
        "dotnet",
        "dotnet_pkgs",
        ".NET/Mono",
        ".NET & Mono",
        capability_prefixes=("mono(",),
        directory_prefixes=("/usr/lib/mono", "/usr/lib64/mono"),
        exact_package_names=("mono", "dotnet"),
        package_name_prefixes=("dotnet-", "mono-"),
    ),
    EcosystemSpec(
        "lua",
        "lua_pkgs",
        "Lua",
        "Lua & LuaRocks",
        capability_prefixes=("lua(",),
        directory_prefixes=("/usr/share/lua", "/usr/lib/lua", "/usr/lib64/lua"),
        exact_package_names=("lua", "luajit", "luarocks"),
        package_name_prefixes=("compat-lua-", "lua-", "luajit-", "luarocks-"),
    ),
    EcosystemSpec(
        "tcltk",
        "tcltk_pkgs",
        "Tcl/Tk",
        "Tcl/Tk",
        capability_prefixes=("tcl(", "tk("),
        directory_prefixes=(
            "/usr/lib/tcl",
            "/usr/lib/tk",
            "/usr/lib64/tcl",
            "/usr/lib64/tk",
            "/usr/share/tcl",
            "/usr/share/tk",
        ),
        exact_package_names=("tcl", "tcl8", "tk"),
        package_name_prefixes=("tcl-", "tk-"),
    ),
    EcosystemSpec(
        "erlang",
        "erlang_pkgs",
        "Erlang/Elixir",
        "Erlang & Elixir",
        capability_prefixes=("elixir(", "erlang("),
        directory_prefixes=("/usr/lib/erlang", "/usr/lib64/erlang"),
        exact_package_names=("elixir", "erlang"),
        package_name_prefixes=("elixir-", "erlang-"),
    ),
)

_ECOSYSTEM_SPECS_BY_ID: Final[dict[str, EcosystemSpec]] = {
    spec.identifier: spec for spec in ECOSYSTEM_SPECS
}


def ecosystem_spec(identifier: str) -> EcosystemSpec | None:
    """Return one registered ecosystem by its stable internal identifier."""
    return _ECOSYSTEM_SPECS_BY_ID.get(identifier)


def ecosystem_ids_for_capability(capability: str) -> frozenset[str]:
    """Return every ecosystem positively identified by one RPM capability."""
    return frozenset(
        spec.identifier
        for spec in ECOSYSTEM_SPECS
        if spec.matches_capability(capability)
    )


def ecosystem_ids_for_directory(directory: str) -> frozenset[str]:
    """Return every ecosystem positively identified by one RPM directory."""
    return frozenset(
        spec.identifier for spec in ECOSYSTEM_SPECS if spec.matches_directory(directory)
    )


def ecosystem_ids_for_package_name(package_name: str) -> frozenset[str]:
    """Return conservative Fedora-name fallback matches for one package name."""
    return frozenset(
        spec.identifier
        for spec in ECOSYSTEM_SPECS
        if spec.matches_package_name(package_name)
    )


def ecosystem_registry_integrity_errors() -> tuple[str, ...]:
    """Return declarative-registry defects for tests and startup diagnostics."""
    errors: list[str] = []
    seen_identifiers: set[str] = set()
    seen_categories: set[str] = set()
    seen_tags: set[str] = set()
    for spec in ECOSYSTEM_SPECS:
        if not spec.identifier or spec.identifier in seen_identifiers:
            errors.append(
                f"duplicate or empty ecosystem identifier: {spec.identifier!r}"
            )
        seen_identifiers.add(spec.identifier)
        if not spec.category_id or spec.category_id in seen_categories:
            errors.append(
                f"duplicate or empty ecosystem category: {spec.category_id!r}"
            )
        seen_categories.add(spec.category_id)
        if not spec.tag or spec.tag in seen_tags:
            errors.append(f"duplicate or empty ecosystem tag: {spec.tag!r}")
        seen_tags.add(spec.tag)
        if not spec.capability_prefixes and not spec.directory_prefixes:
            errors.append(f"ecosystem {spec.identifier!r} has no manifest evidence")
        if any("(" not in prefix for prefix in spec.capability_prefixes):
            errors.append(
                f"ecosystem {spec.identifier!r} has an invalid capability prefix"
            )
    return tuple(errors)
