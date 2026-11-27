"""Declarative RPM ecosystem evidence shared by metadata and classification.

An ecosystem is not inferred from a package's implementation language alone.
Each entry below names concrete RPM capabilities, conventional Fedora package
names, or runtime payload locations that can affirmatively identify a language
or platform ecosystem.  Classifiers retain the underlying signal so a package
with several valid roles can expose ecosystem membership as a facet.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Final

PYTHON_DIST_CAPABILITY_RE: Final[re.Pattern[str]] = re.compile(
    r"^python(?:\d+(?:\.\d+)*)?dist\(", re.IGNORECASE
)
_PYTHON_PACKAGE_NAME_RE: Final[re.Pattern[str]] = re.compile(
    r"^python(?:3(?:\.\d+)?)?-", re.IGNORECASE
)
_PYTHON_SITE_PACKAGES_DIR_RE: Final[re.Pattern[str]] = re.compile(
    r"^/(?:usr/)?(?:lib|lib64)/python\d+(?:\.\d+)*/(?:site|dist)-packages(?:/|$)",
    re.IGNORECASE,
)
BUNDLED_ECOSYSTEM_CAPABILITY_PATTERNS: Final[
    tuple[tuple[str, re.Pattern[str]], ...]
] = (
    ("rust", re.compile(r"^bundled\(\s*crate\(", re.IGNORECASE)),
    ("go", re.compile(r"^bundled\(\s*golang\(", re.IGNORECASE)),
)


_EMPTY_IDS: Final[frozenset[str]] = frozenset()


def is_python_dist_capability(capability: str) -> bool:
    """Return True when ``capability`` is a Fedora Python dist() provide."""
    cleaned = capability.strip()
    if len(cleaned) < 11 or cleaned[:6].lower() != "python":
        return False
    return bool(PYTHON_DIST_CAPABILITY_RE.match(cleaned))


def _bundled_ecosystem_ids_for_clean_capability(
    cleaned: str, normalized: str
) -> frozenset[str]:
    if not normalized.startswith("bundled("):
        return _EMPTY_IDS
    matched: list[str] = []
    for ecosystem_id, pattern in BUNDLED_ECOSYSTEM_CAPABILITY_PATTERNS:
        if pattern.match(cleaned):
            matched.append(ecosystem_id)
    return frozenset(matched) if matched else _EMPTY_IDS


def bundled_ecosystem_ids_for_capability(capability: str) -> frozenset[str]:
    """Return ecosystem IDs signaled by a ``bundled(crate/golang(...))`` provide."""
    cleaned = capability.strip()
    return _bundled_ecosystem_ids_for_clean_capability(cleaned, cleaned.casefold())


@dataclass(frozen=True, slots=True)
class EcosystemSpec:
    """One evidence-backed language or platform ecosystem."""

    identifier: str
    category_id: str
    tag: str
    label: str
    capability_prefixes: tuple[str, ...] = ()
    capability_patterns: tuple[re.Pattern[str], ...] = ()
    directory_prefixes: tuple[str, ...] = ()
    directory_patterns: tuple[re.Pattern[str], ...] = ()
    exact_package_names: tuple[str, ...] = ()
    package_name_prefixes: tuple[str, ...] = ()
    package_name_patterns: tuple[re.Pattern[str], ...] = ()

    def matches_capability(self, capability: str) -> bool:
        cleaned = capability.strip()
        normalized = cleaned.casefold()
        if normalized.startswith(self.capability_prefixes):
            return True
        return any(pattern.match(cleaned) for pattern in self.capability_patterns)

    def matches_directory(self, directory: str) -> bool:
        cleaned = directory.strip().rstrip("/")
        normalized = cleaned.casefold()
        if any(
            normalized == prefix or normalized.startswith(f"{prefix}/")
            for prefix in self.directory_prefixes
        ):
            return True
        return any(pattern.match(cleaned) for pattern in self.directory_patterns)

    def matches_package_name(self, package_name: str) -> bool:
        cleaned = package_name.strip()
        normalized = cleaned.casefold()
        if normalized in self.exact_package_names or normalized.startswith(
            self.package_name_prefixes
        ):
            return True
        return any(pattern.match(cleaned) for pattern in self.package_name_patterns)


# The sequence is stable for user-visible facet ordering.  It is not a hidden
# precedence list: the classifier resolves multi-ecosystem conflicts explicitly.
ECOSYSTEM_SPECS: Final[tuple[EcosystemSpec, ...]] = (
    EcosystemSpec(
        "python",
        "python_pkgs",
        "Python",
        "Python",
        capability_prefixes=("python3dist(",),
        capability_patterns=(PYTHON_DIST_CAPABILITY_RE,),
        directory_patterns=(_PYTHON_SITE_PACKAGES_DIR_RE,),
        exact_package_names=("python", "python3"),
        package_name_prefixes=("python-", "python3-"),
        package_name_patterns=(_PYTHON_PACKAGE_NAME_RE,),
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
        directory_prefixes=(
            "/usr/lib/dotnet",
            "/usr/lib64/dotnet",
            "/usr/lib/mono",
            "/usr/lib64/mono",
        ),
        exact_package_names=("mono", "dotnet"),
        package_name_prefixes=("aspnetcore-", "dotnet-", "mono-"),
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


def _build_ecosystem_indices() -> tuple[
    dict[str, frozenset[str]],
    tuple[tuple[str, str], ...],
    tuple[EcosystemSpec, ...],
    dict[str, frozenset[str]],
    frozenset[int],
    int,
    tuple[EcosystemSpec, ...],
    dict[str, frozenset[str]],
    dict[str, tuple[tuple[str, str], ...]],
    tuple[EcosystemSpec, ...],
]:
    cap_parens_tmp: dict[str, set[str]] = {}
    cap_raw_prefixes: list[tuple[str, str]] = []
    cap_pattern_specs: list[EcosystemSpec] = []

    dir_prefix_tmp: dict[str, set[str]] = {}
    dir_depths: set[int] = set()
    dir_pattern_specs: list[EcosystemSpec] = []

    exact_pkg_tmp: dict[str, set[str]] = {}
    pkg_prefix_tmp: dict[str, list[tuple[str, str]]] = {}
    pkg_pattern_specs: list[EcosystemSpec] = []

    for spec in ECOSYSTEM_SPECS:
        for prefix in spec.capability_prefixes:
            norm_prefix = prefix.casefold()
            if norm_prefix.endswith("(") and norm_prefix.count("(") == 1:
                ns = norm_prefix[:-1]
                cap_parens_tmp.setdefault(ns, set()).add(spec.identifier)
            else:
                cap_raw_prefixes.append((norm_prefix, spec.identifier))
        if spec.capability_patterns:
            cap_pattern_specs.append(spec)

        for dprefix in spec.directory_prefixes:
            clean_dir = dprefix.rstrip("/").casefold()
            dir_prefix_tmp.setdefault(clean_dir, set()).add(spec.identifier)
            dir_depths.add(clean_dir.count("/"))
        if spec.directory_patterns:
            dir_pattern_specs.append(spec)

        for exact_name in spec.exact_package_names:
            exact_pkg_tmp.setdefault(exact_name.casefold(), set()).add(spec.identifier)
        for nprefix in spec.package_name_prefixes:
            norm_nprefix = nprefix.casefold()
            if norm_nprefix:
                pkg_prefix_tmp.setdefault(norm_nprefix[0], []).append(
                    (norm_nprefix, spec.identifier)
                )
        if spec.package_name_patterns:
            pkg_pattern_specs.append(spec)

    return (
        {k: frozenset(v) for k, v in cap_parens_tmp.items()},
        tuple(cap_raw_prefixes),
        tuple(cap_pattern_specs),
        {k: frozenset(v) for k, v in dir_prefix_tmp.items()},
        frozenset(dir_depths),
        max(dir_depths, default=0),
        tuple(dir_pattern_specs),
        {k: frozenset(v) for k, v in exact_pkg_tmp.items()},
        {k: tuple(v) for k, v in pkg_prefix_tmp.items()},
        tuple(pkg_pattern_specs),
    )


(
    _CAPABILITY_PARENS_INDEX,
    _CAPABILITY_RAW_PREFIXES,
    _CAPABILITY_PATTERN_SPECS,
    _DIR_PREFIX_INDEX,
    _DIR_PREFIX_DEPTHS,
    _DIR_MAX_DEPTH,
    _DIR_PATTERN_SPECS,
    _EXACT_PACKAGE_NAME_INDEX,
    _PACKAGE_PREFIX_BY_FIRST_CHAR,
    _PACKAGE_PATTERN_SPECS,
) = _build_ecosystem_indices()


def _ecosystem_ids_for_clean_capability(
    cleaned: str, normalized: str
) -> frozenset[str]:
    if not normalized:
        return _EMPTY_IDS
    result: frozenset[str] | None = None
    head, sep, _ = normalized.partition("(")
    if sep:
        result = _CAPABILITY_PARENS_INDEX.get(head)
    if _CAPABILITY_RAW_PREFIXES:
        extra = [
            eid
            for prefix, eid in _CAPABILITY_RAW_PREFIXES
            if normalized.startswith(prefix)
        ]
        if extra:
            result = frozenset(extra) if result is None else result | frozenset(extra)
    if normalized.startswith("python"):
        if (
            result is None or "python" not in result
        ) and PYTHON_DIST_CAPABILITY_RE.match(cleaned):
            result = frozenset(("python",)) if result is None else result | {"python"}
    elif _CAPABILITY_PATTERN_SPECS:
        for spec in _CAPABILITY_PATTERN_SPECS:
            if spec.identifier == "python":
                continue
            if result is not None and spec.identifier in result:
                continue
            if any(pattern.match(cleaned) for pattern in spec.capability_patterns):
                result = (
                    frozenset((spec.identifier,))
                    if result is None
                    else result | {spec.identifier}
                )
    return result if result is not None else _EMPTY_IDS


def _ecosystem_ids_for_clean_directory(cleaned: str, normalized: str) -> frozenset[str]:
    if not normalized:
        return _EMPTY_IDS
    result: frozenset[str] | None = None
    if normalized.startswith("/") and _DIR_MAX_DEPTH >= 1:
        parts = normalized.split("/", _DIR_MAX_DEPTH + 1)
        limit = min(len(parts), _DIR_MAX_DEPTH + 1)
        if limit >= 2:
            candidate = ""
            for depth in range(1, limit):
                candidate = f"{candidate}/{parts[depth]}"
                if depth in _DIR_PREFIX_DEPTHS:
                    hit = _DIR_PREFIX_INDEX.get(candidate)
                    if hit is not None:
                        result = hit if result is None else result | hit
    if "/python" in normalized:
        if (
            result is None or "python" not in result
        ) and _PYTHON_SITE_PACKAGES_DIR_RE.match(cleaned):
            result = frozenset(("python",)) if result is None else result | {"python"}
    elif _DIR_PATTERN_SPECS:
        for spec in _DIR_PATTERN_SPECS:
            if spec.identifier == "python":
                continue
            if result is not None and spec.identifier in result:
                continue
            if any(pattern.match(cleaned) for pattern in spec.directory_patterns):
                result = (
                    frozenset((spec.identifier,))
                    if result is None
                    else result | {spec.identifier}
                )
    return result if result is not None else _EMPTY_IDS


def ecosystem_ids_for_capability(capability: str) -> frozenset[str]:
    """Return every ecosystem positively identified by one RPM capability."""
    cleaned = capability.strip()
    return _ecosystem_ids_for_clean_capability(cleaned, cleaned.casefold())


def ecosystem_ids_for_directory(directory: str) -> frozenset[str]:
    """Return every ecosystem positively identified by one RPM directory."""
    cleaned = directory.strip().rstrip("/")
    return _ecosystem_ids_for_clean_directory(cleaned, cleaned.casefold())


def ecosystem_ids_for_package_name(package_name: str) -> frozenset[str]:
    """Return conservative Fedora-name fallback matches for one package name."""
    cleaned = package_name.strip()
    if not cleaned:
        return _EMPTY_IDS
    normalized = cleaned.casefold()
    result: frozenset[str] | None = _EXACT_PACKAGE_NAME_INDEX.get(normalized)
    bucket = _PACKAGE_PREFIX_BY_FIRST_CHAR.get(normalized[0])
    if bucket is not None:
        prefix_hits = [
            eid
            for prefix, eid in bucket
            if normalized.startswith(prefix) and (result is None or eid not in result)
        ]
        if prefix_hits:
            result = (
                frozenset(prefix_hits)
                if result is None
                else result | frozenset(prefix_hits)
            )
    if normalized.startswith("python"):
        if (result is None or "python" not in result) and _PYTHON_PACKAGE_NAME_RE.match(
            cleaned
        ):
            result = frozenset(("python",)) if result is None else result | {"python"}
    elif _PACKAGE_PATTERN_SPECS:
        for spec in _PACKAGE_PATTERN_SPECS:
            if spec.identifier == "python":
                continue
            if result is not None and spec.identifier in result:
                continue
            if any(pattern.match(cleaned) for pattern in spec.package_name_patterns):
                result = (
                    frozenset((spec.identifier,))
                    if result is None
                    else result | {spec.identifier}
                )
    return result if result is not None else _EMPTY_IDS


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
