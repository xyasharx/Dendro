"""Keep AGENTS.md and SECURITY.md from drifting away from the code.

These two documents are read instead of the code more often than the code is read
-- AGENTS.md is the first thing a coding agent reads, and SECURITY.md is the
first thing a reporter reads. Both are prose, so nothing noticed when they went
stale: AGENTS.md described a module list that had lost eight modules and gained
a note telling the reader to go looking for Flatpak support that had already
shipped two releases earlier, and it reproduced a CI file list that had fallen
behind the real one.

Each test here pins one way a document can lie about the repository:

- a path it names that does not exist,
- a version it names that is not the shipped one,
- a supported Fedora release the CI matrix does not exercise,
- a repository-map statement contradicted by the tree.

They are deliberately about *facts the code owns*, not about wording: a
document is allowed to be reorganised freely, and none of this should make
editing prose a fight with a test suite.
"""

from __future__ import annotations

import re
import tomllib
import xml.etree.ElementTree as ElementTree
from datetime import date
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).parents[1]
AGENTS = PROJECT_ROOT / "AGENTS.md"
SECURITY = PROJECT_ROOT / "SECURITY.md"
CI_WORKFLOW = PROJECT_ROOT / ".github" / "workflows" / "ci.yml"
SITE = PROJECT_ROOT / "docs"
SITEMAP = SITE / "sitemap.xml"
PAGE = SITE / "index.html"

# Paths written in the docs as shorthand for something under a known directory.
# Each entry maps a bare name to the prefixes it may legitimately stand for.
_SHORTHAND_PREFIXES = ("", "core/", "ui/", "tools/", "tests/", "data/")

_DOC_PATH_PATTERN = re.compile(
    r"`([A-Za-z0-9_./-]+\.(?:py|md|xml|yml|yaml|spec|sh|policy|desktop|toml|json))`"
)


def _project_version() -> str:
    with (PROJECT_ROOT / "pyproject.toml").open("rb") as stream:
        return str(tomllib.load(stream)["project"]["version"])


def _documented_paths(document: Path) -> list[str]:
    return sorted(set(_DOC_PATH_PATTERN.findall(document.read_text(encoding="utf-8"))))


def _resolves(reference: str) -> bool:
    """True when ``reference`` names something in the tree.

    A doc may write a path relative to a directory (`main_window.py` for
    `ui/main_window.py`) or in full; both resolve. Anything resolving to neither
    is a claim the repository does not support.
    """
    if any(character in reference for character in "*?["):
        return True  # a glob, not a single path
    return any(
        (PROJECT_ROOT / f"{prefix}{reference}").exists()
        for prefix in _SHORTHAND_PREFIXES
    )


@pytest.mark.parametrize(
    "document", [AGENTS, SECURITY], ids=["AGENTS.md", "SECURITY.md"]
)
def test_every_path_a_document_names_exists(document: Path) -> None:
    """A doc that names a module that no longer exists costs an agent a detour.

    This is the failure the repository map actually had: it listed 15 of 28
    `core/` modules and 12 of 22 `ui/` ones, so the modules added for Flatpak,
    key prompts, theming and the window split were invisible to anyone reading
    it.
    """
    missing = [path for path in _documented_paths(document) if not _resolves(path)]
    assert not missing, (
        f"{document.name} names paths that do not exist: {missing}. "
        "Update the document or restore the file."
    )


@pytest.mark.parametrize(
    "document", [AGENTS, SECURITY], ids=["AGENTS.md", "SECURITY.md"]
)
def test_a_document_names_no_version_but_the_shipped_one(document: Path) -> None:
    """A version in either document must be the one the repository ships.

    Both documents are read by people deciding whether their install is
    supported, so a stale version there is a support answer that is simply
    wrong.

    The rule is deliberately absolute rather than heuristic. An earlier version
    of this test allowed an old number anywhere in a sentence containing a word
    like "release", and that let every stale claim through: the line
    "Latest 2.x release (2.19.0)" contains "release" and was therefore accepted
    while being exactly the defect the test exists to catch. Historical versions
    are described in prose instead ("every tag before this one"), so no version
    number other than the shipped one is needed here.
    """
    version = _project_version()
    text = document.read_text(encoding="utf-8")
    claimed = sorted(set(re.findall(r"\b2\.\d+\.\d+\b", text)))
    stale = [found for found in claimed if found != version]
    assert not stale, (
        f"{document.name} names {stale} while the project ships {version}. If the "
        "sentence is about history, describe it in words rather than pinning an old "
        "release that will drift again next version."
    )


def test_the_supported_fedora_floor_matches_the_ci_matrix() -> None:
    """The floor the docs promise must be one CI actually exercises.

    A support claim the matrix does not cover is a release nobody tested, and an
    expired image tag fails before any test runs. This is the invariant AGENTS.md
    already states for humans; here it is for the file itself.
    """
    matrix = re.search(
        r"fedora:\s*\[([^\]]+)\]", CI_WORKFLOW.read_text(encoding="utf-8")
    )
    assert matrix is not None, "ci.yml no longer declares a fedora matrix"
    releases = sorted(
        int(value.strip().strip("'\"")) for value in matrix.group(1).split(",")
    )

    for document in (AGENTS, SECURITY):
        text = document.read_text(encoding="utf-8")
        floor = re.search(r"Fedora\s+(\d+)\s*(?:\+|or newer)", text)
        if floor is None:
            continue
        claimed = int(floor.group(1))
        assert claimed in releases, (
            f"{document.name} promises Fedora {claimed}+ but ci.yml exercises {releases}"
        )
        assert releases[0] == claimed, (
            f"{document.name} sets the floor at Fedora {claimed} but ci.yml starts at "
            f"{releases[0]}"
        )


def _names_module(text: str, module: str) -> bool:
    """True when the document names ``module``, in either of the two forms used.

    The repository map lists modules as tree entries (`│ ├── backend.py`), while
    prose refers to them as `core/backend.py`. Both count; requiring backticks
    would fail against the map itself.
    """
    return (
        re.search(rf"(?<![A-Za-z0-9_]){re.escape(module)}(?![A-Za-z0-9_])", text)
        is not None
    )


def test_the_agents_map_names_every_backend_module() -> None:
    """Every `core/` module belongs in the prompt's map, or it is invisible.

    The map is the one part of AGENTS.md an agent uses to decide where a change
    goes. A module missing from it is a module an agent will not know exists.
    """
    text = AGENTS.read_text(encoding="utf-8")
    modules = sorted(
        path.name
        for path in (PROJECT_ROOT / "core").glob("*.py")
        if not path.name.startswith("_")
    )
    absent = [name for name in modules if not _names_module(text, name)]
    assert not absent, (
        f"AGENTS.md's repository map omits core/{absent}. Add each with a one-line "
        "purpose, or the next agent will not know it is there."
    )


def test_the_agents_map_names_every_ui_module() -> None:
    """Same rule for `ui/`: the map is how an agent finds the right widget."""
    text = AGENTS.read_text(encoding="utf-8")
    modules = sorted(
        path.name
        for path in (PROJECT_ROOT / "ui").glob("*.py")
        if path.name != "__init__.py"
    )
    absent = [name for name in modules if not _names_module(text, name)]
    assert not absent, (
        f"AGENTS.md's repository map omits ui/{absent}. Add each with a one-line purpose."
    )


@pytest.mark.parametrize(
    "document", [AGENTS, SECURITY], ids=["AGENTS.md", "SECURITY.md"]
)
def test_a_document_is_well_formed_markdown(document: Path) -> None:
    """Balanced fences and one title: an unbalanced doc renders the rest as code.

    Appending a section inside an unclosed fence silently swallows everything
    after it, which is how a document can keep growing while the part people
    need stops rendering.
    """
    text = document.read_text(encoding="utf-8")
    assert text.count("```") % 2 == 0, f"{document.name} has an unclosed code fence"
    # A '#' line inside a fenced block is a shell or Python comment, not a
    # heading; only lines outside fences count.
    in_fence = False
    titles = []
    for line in text.splitlines():
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
        elif not in_fence and line.startswith("# "):
            titles.append(line)
    assert len(titles) == 1, f"{document.name} has {len(titles)} top-level titles"
    assert titles[0].strip("# ").upper().startswith(document.stem.upper())


def test_security_policy_covers_the_privilege_paths_that_exist() -> None:
    """Every module that can authorise a privileged action is in the trust model.

    The policy went stale in the direction that matters most: Flatpak's
    system-wide path and the repository signing-key prompt both ask for
    privilege and both postdated the section that claims to describe every
    boundary, so a report against either had no stated home.
    """
    text = SECURITY.read_text(encoding="utf-8")
    for module in (
        "flatpak_system_uninstall.py",
        "flatpak_system_update.py",
        "flatpak_runtime_removal.py",
        "key_import_dialog.py",
        "operation_gate.py",
        "repository_addition.py",
        "repository_deletion.py",
    ):
        assert module in text, (
            f"SECURITY.md's trust model does not mention {module}, which can take part "
            "in a privileged or trust-bearing action"
        )


def test_security_policy_points_at_a_real_reporting_channel() -> None:
    """A policy whose reporting URL is a placeholder sends reporters nowhere."""
    text = SECURITY.read_text(encoding="utf-8")
    assert "https://github.com/xyasharx/Dendro/security/advisories/new" in text
    assert "TODO" not in text
    assert "<insert" not in text.lower()


# ---------------------------------------------------------------------------
# README.md, which is the project's landing page in search results. These check
# the facts a stale README gets wrong, not the prose: the support floor it
# advertises, the badge that contradicts it, and the structure that decides
# whether the page can be indexed as a coherent document at all.
# ---------------------------------------------------------------------------

README = PROJECT_ROOT / "README.md"


def _outside_fences(text: str) -> list[str]:
    """Lines that are real markdown rather than a code sample."""
    in_fence = False
    kept = []
    for line in text.splitlines():
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
            continue
        if not in_fence:
            kept.append(line)
    return kept


def _readme() -> str:
    return README.read_text(encoding="utf-8")


def test_the_readme_badge_states_the_supported_fedora_floor() -> None:
    """The Platform badge must not advertise a release the project dropped.

    It read `Fedora 41+` for two releases while the body, SECURITY.md and the CI
    matrix all said 43+, so the most visible claim on the page contradicted the
    one below it. A badge is the first thing a visitor reads and the easiest
    thing to forget.
    """
    text = _readme()
    badge = re.search(r"badge/Platform-Fedora%20(\d+)%2B", text)
    assert badge is not None, "the Platform badge is missing or changed shape"
    badge_floor = int(badge.group(1))

    body = re.search(r"Fedora\s+(\d+)\s+or newer", text)
    assert body is not None, "the README no longer states a Fedora floor"
    assert badge_floor == int(body.group(1)), (
        f"the badge advertises Fedora {badge_floor}+ but the body states "
        f"Fedora {body.group(1)}+"
    )


def test_the_readme_states_the_same_fedora_floor_as_the_ci_matrix() -> None:
    """A support claim CI does not exercise is a promise nobody tested."""
    matrix = re.search(
        r"fedora:\s*\[([^\]]+)\]", CI_WORKFLOW.read_text(encoding="utf-8")
    )
    assert matrix is not None
    releases = sorted(int(v.strip().strip("'\"")) for v in matrix.group(1).split(","))
    body = re.search(r"Fedora\s+(\d+)\s+or newer", _readme())
    assert body is not None
    assert int(body.group(1)) == releases[0], (
        f"the README promises Fedora {body.group(1)}+ but ci.yml starts at {releases[0]}"
    )


def test_the_readme_keeps_one_h1_and_balanced_fences() -> None:
    """One H1 is what tells a crawler what the page is about.

    Extra top-level titles turn the document into several competing headings,
    and an unclosed fence swallows the rest of the page as one code block — both
    silently, because the page still renders.
    """
    text = _readme()
    assert text.count("```") % 2 == 0, "README.md has an unclosed code fence"
    titles = [line for line in _outside_fences(text) if line.startswith("# ")]
    assert len(titles) == 1, f"README.md has {len(titles)} top-level headings: {titles}"


def test_every_readme_anchor_resolves_to_a_heading() -> None:
    """A broken table-of-contents link is a dead end in the first screenful."""
    text = _readme()
    slugs = set()
    for line in _outside_fences(text):
        if line.startswith("#"):
            title = line.lstrip("#").strip()
            slugs.add(re.sub(r"[^\w\s-]", "", title.lower()).replace(" ", "-"))
    # Anchors appear in two forms: markdown links `](#slug)` and the header
    # nav's raw HTML `href="#slug"`. An earlier version of this test only
    # matched the first form and therefore never checked the navigation bar,
    # which is where the anchors a visitor actually clicks live.
    anchors = set(re.findall(r"\]\(#([a-z0-9-]+)\)", text))
    anchors |= set(re.findall(r'href="#([a-z0-9-]+)"', text))
    broken = sorted(anchors - slugs)
    assert not broken, f"README.md links to anchors that do not exist: {broken}"


def test_every_readme_image_has_descriptive_alt_text() -> None:
    """Alt text is indexed, and an empty one is an accessibility defect.

    Google reads it as page content and a screen reader reads it aloud, so
    `alt=""` or `alt="screenshot"` throws away both.
    """
    text = _readme()
    alts = re.findall(r'<img[^>]*alt="([^"]*)"', text)
    assert alts, "the README has no images"
    weak = [alt for alt in alts if len(alt.split()) < 3]
    assert not weak, f"README.md images have uninformative alt text: {weak}"


def test_the_readme_answers_the_questions_people_search_for() -> None:
    """The FAQ is the part of the page search queries actually match.

    These are the phrasings a new Fedora user types, and each one has a factual
    answer in this project (it is not a dnf replacement; it never runs as root;
    Atomic is supported read-only; the daemon is required).
    """
    text = _readme().lower()
    for question in (
        "replacement for `dnf`",
        "run as root",
        "silverblue",
        "dnf5daemon-server",
        "flatpak",
        "languages",
    ):
        assert question in text, (
            f"README.md no longer answers a question about {question!r}"
        )


def test_the_readme_does_not_advertise_a_stale_release_number() -> None:
    """Pinning a version in the README guarantees a wrong number next release.

    The badge above already reports the latest release dynamically, so a literal
    version is redundant as well as stale.
    """
    version = _project_version()
    found = set(re.findall(r"\b2\.\d+\.\d+\b", _readme()))
    stale = sorted(found - {version})
    assert not stale, (
        f"README.md names {stale} while the project ships {version}; the release "
        "badge reports this dynamically, so no literal version is needed"
    )


def test_the_readme_source_install_includes_adwaita_wayland_decoration() -> None:
    """Source installs in containers must not fall back to Qt's bradient CSD."""
    text = _readme()
    section = re.search(
        r"### From source\s+```bash\s+(.*?)```",
        text,
        re.DOTALL,
    )
    assert section is not None, "README.md is missing the '### From source' block"
    assert "qt6-qtwayland-adwaita-decoration" in section.group(1)


# --- the published site -------------------------------------------------------
#
# The site is crawled by Google, which means two of its files are contracts with
# a crawler rather than for a reader. Both were wrong once: the sitemap carried
# <changefreq> and <priority>, which Google documents as ignored, and omitted
# <lastmod>, which it honours; and a robots.txt sat in docs/, where it is served
# at /Dendro/robots.txt -- a subdirectory of the xyasharx.github.io host, which
# Google documents as a location no crawler reads. A file that looks like it
# works is worse than no file.


def test_the_sitemap_is_valid_and_lists_only_the_page_that_exists() -> None:
    urlset = ElementTree.parse(SITEMAP).getroot()
    assert urlset.tag.endswith("urlset")

    locations = [
        element.text for element in urlset.iter() if element.tag.endswith("loc")
    ]
    assert locations, "the sitemap lists no URLs"

    for location in locations:
        slug = location.split("/Dendro/", 1)[-1].strip("/")
        assert (SITE / (slug or "index.html")).exists(), (
            f"sitemap lists {location}, which the site does not serve"
        )


def test_the_sitemap_omits_what_google_ignores_and_states_what_it_reads() -> None:
    sitemap = SITEMAP.read_text(encoding="utf-8")

    # Google documents both of these as ignored, so they are noise that implies
    # a signal they do not carry.
    for ignored in ("<changefreq>", "<priority>"):
        assert ignored not in sitemap, (
            f"sitemap carries {ignored}, which Google ignores"
        )

    # <lastmod> is honoured, but only while it stays truthful.
    lastmod = re.search(r"<lastmod>(\d{4}-\d{2}-\d{2})</lastmod>", sitemap)
    assert lastmod, "the sitemap states no lastmod"
    assert date.fromisoformat(lastmod.group(1)) <= date.today()


def test_no_robots_file_sits_where_no_crawler_would_read_it() -> None:
    """A project site is a subdirectory, so its robots.txt is inert.

    Google: "The robots.txt file must be located at the root of the site host
    to which it applies ... It cannot be placed in a subdirectory." Until there
    is a user-site repository serving the host root, any robots.txt committed
    here would be served and ignored.
    """
    assert not (SITE / "robots.txt").exists(), (
        "docs/robots.txt is served at /Dendro/robots.txt, which no crawler reads; "
        "the host-root file belongs in a <user>.github.io repository"
    )


def test_the_page_is_indexable_and_offers_a_canonical_url() -> None:
    page = PAGE.read_text(encoding="utf-8")

    assert 'rel="canonical"' in page
    # A noindex here would silently undo the canonical and the sitemap together.
    assert "noindex" not in page

    canonical = re.search(r'<link rel="canonical" href="([^"]+)"', page)
    assert canonical, "the page declares no canonical URL"
    assert canonical.group(1).startswith("https://")


def test_the_search_console_verification_token_stays_in_the_head() -> None:
    """Search Console re-checks this tag forever, and un-verifies without it.

    Google: verification "lasts as long as Search Console can confirm the
    presence and validity of your verification token", which it checks
    periodically; when the check fails the property's permissions expire. So
    the tag is not setup scaffolding to be tidied away afterwards -- deleting
    it as "leftover" silently costs access to the property.
    """
    page = PAGE.read_text(encoding="utf-8")

    head = page.split("<head>", 1)[1].split("</head>", 1)[0]
    tokens = re.findall(
        r'<meta\s+name="google-site-verification"\s+content="([^"]+)"', head
    )
    assert tokens, "the google-site-verification meta tag is gone from <head>"

    for token in tokens:
        # The token is a fixed-length opaque string; a truncated paste is the
        # likeliest way to break verification without noticing.
        assert len(token) == 43, f"verification token is {len(token)} characters"
        assert re.fullmatch(r"[A-Za-z0-9_-]+", token), "token is not URL-safe base64"
