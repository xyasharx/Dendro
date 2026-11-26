"""The empty-state and formatting decisions, tested without a window.

These functions were unreachable except through a ``MainWindow``, which needs a
display, a DNF5 daemon and a package catalog.  The empty-state table is the
largest decision block in the window and had no direct coverage; it is a pure
function of its arguments, so it can be pinned exhaustively here instead.
"""

from __future__ import annotations

import pytest

from dendro.core.host_environment import HostEnvironment, HostEnvironmentKind
from dendro.ui.window_presentation import (
    describe_empty_category_view,
    format_byte_size,
    icon_theme_installed,
    select_matching_icon_theme,
)

TRADITIONAL = HostEnvironment.traditional()
ATOMIC = HostEnvironment(kind=HostEnvironmentKind.ATOMIC_OSTREE)
FLATPAK = HostEnvironment(kind=HostEnvironmentKind.FLATPAK_SANDBOX)


@pytest.mark.parametrize(
    ("size", "expected"),
    [
        (0, "0 B"),
        (1, "1 B"),
        (1023, "1023 B"),
        (1024, "1.0 KB"),
        (1536, "1.5 KB"),
        (123456, "120.6 KB"),
        (1024**2, "1.00 MB"),
        (1024**3, "1.00 GB"),
        (5 * 1024**3, "5.00 GB"),
        # A negative size is a malformed header, not a negative byte count.
        (-5, "0 B"),
    ],
)
def test_byte_sizes_are_never_truncated_to_zero(size: int, expected: str) -> None:
    assert format_byte_size(size) == expected


def test_a_small_rpm_is_not_reported_as_zero() -> None:
    """The bug this guards: a sub-kilobyte package reading as "0.0 KB"."""

    assert format_byte_size(512) == "512 B"
    assert format_byte_size(512) != "0.0 KB"


class TestEmptyState:
    def test_a_search_always_explains_the_search(self) -> None:
        for environment in (TRADITIONAL, ATOMIC, FLATPAK):
            for loaded in (False, True):
                title, detail = describe_empty_category_view(
                    "installed",
                    search_query="nginx",
                    catalog_loaded=loaded,
                    environment=environment,
                )
                assert title == "No Matching Packages"
                assert "search" in detail.casefold()

    def test_a_search_wins_over_a_catalog_error(self) -> None:
        """A stale error must not be reported while a live filter is active."""

        title, _detail = describe_empty_category_view(
            "all",
            search_query="nginx",
            catalog_loaded=False,
            catalog_error="metadata load failed",
            environment=TRADITIONAL,
        )
        assert title == "No Matching Packages"

    def test_a_catalog_error_is_reported_and_trimmed(self) -> None:
        title, detail = describe_empty_category_view(
            "installed",
            catalog_loaded=False,
            catalog_error="  metadata\n\tload  failed  ",
            environment=TRADITIONAL,
        )
        assert title == "Could Not Load Installed Packages"
        # Whitespace is collapsed so a multi-line daemon message stays one line.
        assert "metadata load failed" in detail
        assert "\n" not in detail

    def test_a_long_error_is_truncated_with_an_ellipsis(self) -> None:
        _title, detail = describe_empty_category_view(
            "installed",
            catalog_loaded=False,
            catalog_error="x" * 400,
            environment=TRADITIONAL,
        )
        assert "…" in detail
        assert len(detail) < 400

    def test_a_loaded_catalog_does_not_report_an_error(self) -> None:
        """The error is only meaningful while nothing has loaded."""

        title, detail = describe_empty_category_view(
            "user_installed",
            catalog_loaded=True,
            catalog_error="stale failure",
            environment=TRADITIONAL,
        )
        assert title == "No User-Installed Packages Reported"
        assert "stale failure" not in detail

    def test_the_queue_is_described_in_every_environment(self) -> None:
        """Queueing is a daemon action, so a sandboxed build can still queue.

        An environment that cannot queue must explain itself; one that can must
        not claim the feature is missing.
        """

        for environment in (TRADITIONAL, ATOMIC, FLATPAK):
            title, detail = describe_empty_category_view(
                "queued", catalog_loaded=True, environment=environment
            )
            if environment.supports_dnf5_daemon_mutations:
                assert title == "Transaction Queue Is Empty", environment.kind
                assert detail, environment.kind
            else:
                assert title == "Transaction Queue Unavailable", environment.kind
                assert detail, environment.kind

    def test_the_queue_is_described_when_it_is_capable(self) -> None:
        title, detail = describe_empty_category_view(
            "queued", catalog_loaded=True, environment=TRADITIONAL
        )
        assert title == "Transaction Queue Is Empty"
        assert "Apply Changes" in detail

    def test_nothing_is_said_before_the_catalog_loads(self) -> None:
        """An empty view during loading must not claim the system is empty."""

        for category in (
            "updates_available",
            "orphans",
            "installed",
            "all",
            "repository_sources",
            "copr_repos",
            "rpmfusion_repos",
            "unclassified",
            "user_installed",
        ):
            assert describe_empty_category_view(
                category, catalog_loaded=False, environment=TRADITIONAL
            ) == ("", ""), category

    def test_a_sandboxed_build_explains_why_it_sees_no_rpms(self) -> None:
        title, detail = describe_empty_category_view(
            "installed", catalog_loaded=True, environment=FLATPAK
        )
        assert title == "Host RPM Database Not Exposed to Sandbox"
        assert "daemon" in detail

    def test_atomic_hosts_use_layering_language(self) -> None:
        title, detail = describe_empty_category_view(
            "user_installed", catalog_loaded=True, environment=ATOMIC
        )
        assert title == "No Layered or Overridden RPMs"
        assert "OSTree" in detail

    def test_a_traditional_host_does_not_use_layering_language(self) -> None:
        title, _detail = describe_empty_category_view(
            "user_installed", catalog_loaded=True, environment=TRADITIONAL
        )
        assert title == "No User-Installed Packages Reported"

    def test_every_category_with_a_label_names_itself(self) -> None:
        """A category the sidebar offers must not fall through to a blank state."""

        for category in (
            "repository_sources",
            "copr_repos",
            "rpmfusion_repos",
            "unclassified",
        ):
            title, detail = describe_empty_category_view(
                category, catalog_loaded=True, environment=TRADITIONAL
            )
            assert title, category
            assert detail, category

    def test_the_all_category_stays_silent(self) -> None:
        """ "No packages in All" would be wrong: the window is simply empty."""

        assert describe_empty_category_view(
            "all", catalog_loaded=True, environment=TRADITIONAL
        ) == ("", "")

    def test_an_unknown_category_does_not_invent_a_label(self) -> None:
        assert describe_empty_category_view(
            "not_a_category", catalog_loaded=True, environment=TRADITIONAL
        ) == ("", "")

    def test_the_decision_is_case_and_whitespace_insensitive(self) -> None:
        assert describe_empty_category_view(
            "  Queued  ", catalog_loaded=True, environment=TRADITIONAL
        ) == describe_empty_category_view(
            "queued", catalog_loaded=True, environment=TRADITIONAL
        )

    def test_no_argument_combination_raises(self) -> None:
        """Exhaustive: every category against both load states and both errors."""

        categories = (
            "all",
            "queued",
            "updates_available",
            "orphans",
            "repository_sources",
            "copr_repos",
            "rpmfusion_repos",
            "unclassified",
            "user_installed",
            "installed",
            "",
        )
        for category in categories:
            for loaded in (False, True):
                for search in ("", "nginx"):
                    for error in ("", "x" * 400):
                        for environment in (TRADITIONAL, ATOMIC, FLATPAK):
                            title, detail = describe_empty_category_view(
                                category,
                                search_query=search,
                                catalog_loaded=loaded,
                                catalog_error=error,
                                environment=environment,
                            )
                            assert isinstance(title, str)
                            assert isinstance(detail, str)
                            # Never half-set: a title with no detail (or the
                            # reverse) renders as a blank panel with a heading.
                            assert bool(title) == bool(detail), (
                                category,
                                loaded,
                                search,
                                len(error),
                            )


class TestIconTheme:
    def test_an_empty_name_is_not_installed(self) -> None:
        assert icon_theme_installed("") is False

    def test_a_name_that_is_not_on_the_search_path_is_not_installed(self) -> None:
        assert icon_theme_installed("dendro-no-such-theme-xyz") is False

    def test_an_unpaired_theme_is_left_alone(self, monkeypatch) -> None:
        """Never overwrite an active desktop theme such as GNOME's Adwaita."""

        monkeypatch.setattr(
            "dendro.ui.window_presentation.icon_theme_installed", lambda _name: True
        )
        assert select_matching_icon_theme("Adwaita", is_dark=True) is None

    def test_a_paired_theme_is_switched_only_when_both_exist(self, monkeypatch) -> None:
        monkeypatch.setattr(
            "dendro.ui.window_presentation.icon_theme_installed", lambda _name: True
        )
        assert select_matching_icon_theme("papirus", is_dark=True) == "Papirus-Dark"
        assert select_matching_icon_theme("papirus-dark", is_dark=False) == "Papirus"

        # The paired target is missing: switching would blank every icon.
        monkeypatch.setattr(
            "dendro.ui.window_presentation.icon_theme_installed", lambda _name: False
        )
        assert select_matching_icon_theme("papirus", is_dark=True) is None

    def test_the_target_that_is_already_active_is_not_returned(
        self, monkeypatch
    ) -> None:
        """Re-applying the active theme reloads every icon for no change.

        ``papirus-dark`` pairs to ("Papirus", "Papirus-Dark"), so in dark mode
        the pair's target is the theme already in use; in light mode the same
        theme has somewhere to go.
        """

        monkeypatch.setattr(
            "dendro.ui.window_presentation.icon_theme_installed", lambda _name: True
        )
        assert select_matching_icon_theme("papirus-dark", is_dark=False) == "Papirus"

    def test_the_fallback_order_prefers_a_dark_theme_for_a_dark_ui(
        self, monkeypatch
    ) -> None:
        installed = {"Adwaita"}
        monkeypatch.setattr(
            "dendro.ui.window_presentation.icon_theme_installed",
            lambda name: name in installed,
        )
        assert select_matching_icon_theme("", is_dark=True) == "Adwaita"
        assert select_matching_icon_theme("", is_dark=False) == "Adwaita"

    def test_no_fallback_is_invented_when_nothing_is_installed(
        self, monkeypatch
    ) -> None:
        monkeypatch.setattr(
            "dendro.ui.window_presentation.icon_theme_installed", lambda _name: False
        )
        assert select_matching_icon_theme("", is_dark=True) is None
