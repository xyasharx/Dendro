"""Presentational decisions that do not need a window to answer.

These were module-level functions in ``dendro/ui/main_window.py``: pure lookups, string
formatting and a large empty-state decision table.  None of them touch widget
state, and all of them were unreachable without constructing a ``MainWindow``,
which is why they had no tests -- a 396-case input matrix ran at zero coverage
because the only entry point was a window that needs a display server, a daemon
and a package catalog.

They live here so the decisions can be tested on their own and so the window
module is about a window.  ``HostEnvironment`` is the only dependency heavier
than the standard library, and it is a value object.
"""

from __future__ import annotations

import os

from PyQt6.QtGui import QIcon

from dendro.core.host_environment import HostEnvironment
from dendro.core.i18n import format_byte_size as format_byte_size
from dendro.core.i18n import tr
from dendro.core.taxonomy import category_label


def window_host_environment(window: object) -> HostEnvironment:
    """Return ``window.host_environment`` or a traditional default for test stubs."""
    environment = getattr(window, "host_environment", None)
    if isinstance(environment, HostEnvironment):
        return environment
    return HostEnvironment.traditional()


def icon_theme_installed(theme_name: str) -> bool:
    """Return True when ``theme_name`` has an ``index.theme`` on Qt's search path."""
    if not theme_name:
        return False
    for search_root in QIcon.themeSearchPaths():
        if not search_root:
            continue
        candidate = os.path.join(search_root, theme_name, "index.theme")
        if os.path.isfile(candidate):
            return True
    return False


PAIRED_ICON_THEMES: dict[str, tuple[str, str]] = {
    "breeze": ("breeze", "breeze-dark"),
    "breeze-dark": ("breeze", "breeze-dark"),
    "papirus": ("Papirus", "Papirus-Dark"),
    "papirus-dark": ("Papirus", "Papirus-Dark"),
    "papirus-light": ("Papirus-Light", "Papirus-Dark"),
    "yaru": ("Yaru", "Yaru-dark"),
    "yaru-dark": ("Yaru", "Yaru-dark"),
}


def select_matching_icon_theme(current_theme: str, *, is_dark: bool) -> str | None:
    """Return a paired icon theme only when it actually exists on disk.

    Never overwrite an active desktop theme such as GNOME's ``Adwaita`` with an
    uninstalled theme like ``breeze-dark``.
    """
    normalized = (current_theme or "").strip()
    if normalized:
        pair = PAIRED_ICON_THEMES.get(normalized.casefold())
        if pair is None:
            return None
        target = pair[1] if is_dark else pair[0]
        if target != normalized and icon_theme_installed(target):
            return target
        return None

    fallback_order = (
        ("breeze-dark", "Papirus-Dark", "Adwaita", "breeze", "hicolor")
        if is_dark
        else ("breeze", "Papirus", "Adwaita", "hicolor")
    )
    for candidate in fallback_order:
        if icon_theme_installed(candidate):
            return candidate
    return None


def describe_empty_category_view(
    category: str,
    *,
    search_query: str = "",
    catalog_loaded: bool = False,
    catalog_error: str = "",
    environment: HostEnvironment | None = None,
    search_all_packages: bool | None = None,
) -> tuple[str, str]:
    """Return a truthful ``(title, detail)`` empty-state message for the tree view."""
    env = environment if environment is not None else HostEnvironment.traditional()
    normalized_category = category.strip().lower()

    if search_query.strip():
        if search_all_packages is True and normalized_category not in ("", "all"):
            return (
                tr("No Matching Packages"),
                tr(
                    "Searching all packages — no installed packages match your current "
                    "search filter. Clear the search bar or search remote repositories."
                ),
            )
        if search_all_packages is False and normalized_category not in ("", "all"):
            label = category_label(normalized_category, fallback="")
            if label:
                if normalized_category in ("queued", "updates_available", "orphans"):
                    return (
                        tr("No Matching Packages"),
                        tr(
                            "No packages in '{category}' match your current search filter. "
                            "Clear the search bar or select All Installed Packages in the sidebar."
                        ).format(category=label),
                    )
                return (
                    tr("No Matching Packages"),
                    tr(
                        "No packages in '{category}' match your current search filter. "
                        "Switch search scope to All packages or adjust the search bar."
                    ).format(category=label),
                )
        return (
            tr("No Matching Packages"),
            tr(
                "No packages match the active search and category filter. "
                "Clear or adjust the search bar to see more packages."
            ),
        )

    clean_error = " ".join(catalog_error.split())
    if clean_error and not catalog_loaded and normalized_category != "queued":
        if len(clean_error) > 220:
            clean_error = f"{clean_error[:217].rstrip()}…"
        return (
            tr("Could Not Load Installed Packages"),
            tr("{error} Use Reload DB to try again.").format(error=clean_error),
        )

    if normalized_category == "queued":
        if not env.supports_dnf5_daemon_mutations:
            _title, reason = env.dnf5_daemon_unavailable(tr("queue package changes"))
            return (tr("Transaction Queue Unavailable"), reason)
        return (
            tr("Transaction Queue Is Empty"),
            tr(
                "Packages you queue to install, upgrade, or remove in this session "
                "appear here before you click Apply Changes. Service and system "
                "restarts are tracked separately via 'dnf5 needs-restarting'."
            ),
        )

    if normalized_category == "updates_available":
        if not env.supports_dnf5_daemon_queries:
            _title, reason = env.dnf5_daemon_unavailable(
                tr("check DNF5 package updates")
            )
            return (tr("Update Checks Unavailable"), reason)
        if not catalog_loaded:
            return ("", "")
        return (
            tr("No Available Updates"),
            tr(
                "All installed RPM packages match the versions currently reported by "
                "enabled repositories. Use Check Mirrors to refresh repository metadata."
            ),
        )

    if normalized_category == "orphans":
        if not env.supports_orphan_detection:
            return (
                tr("Orphan Detection Unavailable"),
                env.feature_unavailable("Orphan package detection"),
            )
        if not catalog_loaded:
            return ("", "")
        return (
            tr("No Orphan Packages"),
            tr("No unneeded leaf dependencies were reported on this system."),
        )

    if not catalog_loaded:
        return ("", "")

    if env.is_flatpak:
        return (
            tr("Host RPM Database Not Exposed to Sandbox"),
            tr(
                "This Flatpak build connects only to the host DNF5 daemon. "
                "Use Available Updates to review upgrades or manage DNF5 repositories."
            ),
        )

    if normalized_category == "repository_sources":
        return (
            tr("No Repository Source Packages Found"),
            tr(
                "Installed RPM packages that ship DNF repository definitions or "
                "distribution GPG signing keys appear here."
            ),
        )
    if normalized_category == "copr_repos":
        return (
            tr("No COPR Packages Installed"),
            tr(
                "Installed packages originating from Fedora Community COPR "
                "repositories appear here."
            ),
        )
    if normalized_category == "rpmfusion_repos":
        return (
            tr("No RPM Fusion Packages Installed"),
            tr(
                "Installed packages originating from RPM Fusion Free or Nonfree "
                "repositories appear here."
            ),
        )
    if normalized_category == "unclassified":
        return (
            tr("No Unclassified Packages"),
            tr(
                "Every loaded package had enough verified metadata to receive a category."
            ),
        )
    if normalized_category == "user_installed":
        if env.is_atomic_ostree:
            return (
                tr("No Layered or Overridden RPMs"),
                tr(
                    "This deployment matches its base OSTree image with no layered, "
                    "local, or overridden RPM packages."
                ),
            )
        return (
            tr("No User-Installed Packages Reported"),
            tr(
                "Explicitly requested RPM packages appear here once install-reason "
                "metadata is loaded."
            ),
        )

    label = category_label(normalized_category, fallback="")
    if label and normalized_category != "all":
        return (
            tr("No Packages in {category}").format(category=label),
            tr("No installed packages on this system match this category."),
        )
    return ("", "")


def refresh_window_empty_state(window: object) -> None:
    """Update ``window.tree_view`` empty-state copy when wired (safe on stubs)."""
    tree_view = getattr(window, "tree_view", None)
    proxy_model = getattr(window, "proxy_model", None)
    if tree_view is None or proxy_model is None:
        return
    setter = getattr(tree_view, "set_empty_state", None)
    if not callable(setter):
        return
    effective_scope_fn = getattr(proxy_model, "effective_search_scope_all", None)
    search_all_packages = (
        bool(effective_scope_fn()) if callable(effective_scope_fn) else None
    )
    title, detail = describe_empty_category_view(
        getattr(proxy_model, "category_filter", "all"),
        search_query=getattr(proxy_model, "search_query", ""),
        catalog_loaded=bool(getattr(window, "_catalog_loaded", False)),
        catalog_error=str(getattr(window, "_catalog_error", "") or ""),
        environment=window_host_environment(window),
        search_all_packages=search_all_packages,
    )
    setter(title, detail)
