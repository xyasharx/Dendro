"""Regression coverage for the persistent search-filter reference."""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import Qt
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication

from dendro.core.host_environment import HostEnvironment
from dendro.core.i18n import set_active_locale
from dendro.core.models import PackageInfo, PackageState
from dendro.core.taxonomy import category_label
from dendro.ui.header import (
    HeaderBar,
    is_file_lookup_query,
    is_structured_filter_query,
    search_filter_tooltip,
    should_trigger_remote_search_on_enter,
)
from dendro.ui.main_window import MainWindow

# The reference is rendered on read rather than stored as an English constant,
# so that a language switch reaches it. Every assertion below is unchanged.
SEARCH_FILTER_TOOLTIP = search_filter_tooltip()


def test_filter_reference_stays_available_during_live_validation(qtbot) -> None:
    set_active_locale("en")
    header = HeaderBar()
    qtbot.addWidget(header)

    header.search_input.setText("status:not-a-status")

    assert header.search_input.property("filterState") == "invalid"
    assert "Invalid filter syntax" in header.search_input.toolTip()
    # Rendered again rather than compared to a module-level constant: the
    # reference is now built per call, so identity cannot be relied on.
    assert header.search_help_btn.toolTip() == search_filter_tooltip()
    assert header.search_help_btn.accessibleName() == "Search filter reference"
    assert "filter:value" in SEARCH_FILTER_TOOLTIP
    assert "status:update" in SEARCH_FILTER_TOOLTIP


def test_size_filter_requires_a_real_comparison(qtbot) -> None:
    set_active_locale("en")
    header = HeaderBar()
    qtbot.addWidget(header)

    header.search_input.setText("size:not-a-size")

    assert header.search_input.property("filterState") == "invalid"
    assert "Invalid size comparison" in header.search_input.toolTip()


def test_arch_filter_accepts_non_example_rpm_architectures(qtbot) -> None:
    set_active_locale("en")
    header = HeaderBar()
    qtbot.addWidget(header)

    header.search_input.setText("arch:")
    assert header.search_input.property("filterState") == "neutral"
    assert "exact RPM architecture" in header.search_input.toolTip()
    assert "not a complete list" in header.search_input.toolTip()

    header.search_input.setText("arch:riscv64")
    assert header.search_input.property("filterState") == "valid"
    assert "Invalid filter syntax" not in header.search_input.toolTip()
    assert "other valid RPM architectures are accepted too" in search_filter_tooltip()


def test_search_enter_query_classification() -> None:
    assert is_file_lookup_query("/usr/bin/git") is True
    assert is_file_lookup_query("file:/usr/bin/git") is True
    assert is_file_lookup_query("neovim") is False

    assert is_structured_filter_query("status:update") is True
    assert is_structured_filter_query("cat:cli_general") is True
    assert is_structured_filter_query("cat:cli_general htop") is True
    assert is_structured_filter_query("perl-Net::HTTP") is False

    assert should_trigger_remote_search_on_enter("") is False
    assert should_trigger_remote_search_on_enter("   ") is False
    assert should_trigger_remote_search_on_enter("status:update") is False
    assert should_trigger_remote_search_on_enter("cat:cli_general") is False
    assert should_trigger_remote_search_on_enter("/usr/bin/git") is False
    assert should_trigger_remote_search_on_enter("file:/usr/bin/git") is False
    assert should_trigger_remote_search_on_enter("neovim") is True
    assert should_trigger_remote_search_on_enter("gcc-c++") is True
    assert should_trigger_remote_search_on_enter("perl-Net::HTTP") is True


def test_return_pressed_guards_local_queries_file_paths_and_completer_popup(
    qtbot,
) -> None:
    set_active_locale("en")
    header = HeaderBar()
    qtbot.addWidget(header)
    header.resize(960, header.heightForWidth(960))
    header.show()
    QApplication.processEvents()

    remote_queries: list[str] = []
    local_queries: list[str] = []
    header.remote_search_requested.connect(remote_queries.append)
    header.search_changed.connect(local_queries.append)

    # 1. Confirming a visible QCompleter popup with Return must not dispatch a
    # remote repository query even though "stat" contains no colon yet.
    header.search_input.setFocus()
    header.search_input.setText("stat")
    header.filter_completer.setCompletionPrefix("stat")
    header.filter_completer.complete()
    QApplication.processEvents()
    popup = header.filter_completer.popup()
    assert popup is not None
    assert popup.model().rowCount() > 0
    popup.setCurrentIndex(popup.model().index(0, 0))
    assert popup.isVisible()

    QTest.keyClick(header.search_input, Qt.Key.Key_Return)
    QApplication.processEvents()
    assert remote_queries == []

    # 2. Structured filter-only queries flush local search immediately instead of
    # firing a remote search.
    popup.hide()
    header.search_input.setText("status:update")
    header.debounce_timer.start()
    QTest.keyClick(header.search_input, Qt.Key.Key_Return)
    assert remote_queries == []
    assert local_queries[-1] == "status:update"
    assert not header.debounce_timer.isActive()

    # 3. File path / file: queries flush local search and never fire remote DNF.
    for path_query in ("/usr/bin/git", "file:/usr/bin/git"):
        header.search_input.setText(path_query)
        header.debounce_timer.start()
        QTest.keyClick(header.search_input, Qt.Key.Key_Return)
        assert remote_queries == []
        assert local_queries[-1] == path_query

    # 4. Plain package queries dispatch remote search on Return once popup is closed.
    header.search_input.setText("neovim")
    assert not popup.isVisible()
    QTest.keyClick(header.search_input, Qt.Key.Key_Return)
    assert remote_queries == ["neovim"]

    # 5. The explicit trailing Search Remote action remains usable for any text.
    header.search_input.setText("cat:cli_general")
    header.remote_search_action.trigger()
    assert remote_queries == ["neovim", "cat:cli_general"]


def test_header_search_scope_chip_toggles_and_retranslates(qtbot) -> None:
    set_active_locale("en")
    header = HeaderBar()
    qtbot.addWidget(header)
    header.show()
    QApplication.processEvents()
    emitted: list[bool] = []
    header.search_scope_toggled.connect(emitted.append)

    assert header.search_scope_btn.isVisible() is False

    user_apps_label = category_label("user_apps")
    header.set_search_scope_state(
        visible=True, search_all_packages=True, category_id="user_apps"
    )
    assert header.search_scope_btn.isVisible() is True
    assert header.search_scope_btn.isChecked() is True
    assert header.search_scope_btn.text() == "All packages"
    assert user_apps_label in header.search_scope_btn.toolTip()
    # Programmatic sync does not re-emit user toggle signal.
    assert emitted == []

    header.search_scope_btn.click()
    assert emitted == [False]
    assert header.search_scope_btn.isChecked() is False
    assert header.search_scope_btn.text() == "In category"
    assert user_apps_label in header.search_scope_btn.toolTip()

    set_active_locale("de")
    try:
        header.retranslate()
        assert header.search_scope_btn.text() == "In Kategorie"
    finally:
        set_active_locale("en")


def test_main_window_surfaces_search_scope_and_allows_category_narrowing(
    qtbot, monkeypatch, tmp_path
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setattr(MainWindow, "_load_packages", lambda self: None)
    set_active_locale("en")
    window = MainWindow(host_environment=HostEnvironment.traditional())
    qtbot.addWidget(window)
    monkeypatch.setattr(window.thread_pool, "start", lambda *_args, **_kwargs: None)

    packages = [
        PackageInfo(
            name="firefox",
            version="154.0",
            release="1.fc44",
            arch="x86_64",
            summary="Web browser",
            state=PackageState.INSTALLED,
            parent_pillar="pillar_apps",
            sub_category="desktop_internet",
            primary_category="user_apps",
            classification_confidence=1.0,
            is_desktop_app=True,
        ),
        PackageInfo(
            name="htop",
            version="3.3.0",
            release="1.fc44",
            arch="x86_64",
            summary="Terminal process viewer",
            state=PackageState.INSTALLED,
            parent_pillar="pillar_cli",
            sub_category="cli_monitoring",
            primary_category="cli_tools",
            classification_confidence=1.0,
            is_cli_tool=True,
        ),
    ]
    window.tree_model.set_packages(packages)
    window._catalog_loaded = True
    window.sidebar.select_category("user_apps", emit=False)
    window._on_sidebar_category_selected("user_apps")

    user_apps_label = category_label("user_apps")
    assert window.header.search_scope_btn.isHidden() is True

    # Typing a query under user_apps reveals the scope chip and notifies the user.
    window._on_search_query_changed("htop")
    assert window.header.search_scope_btn.isHidden() is False
    assert window.header.search_scope_btn.isChecked() is True
    assert window.proxy_model.rowCount() == 1
    assert "Searching all packages" in window.status_bar.currentMessage()

    # Toggling scope to "In category" restricts results back to user_apps.
    window.header.search_scope_btn.click()
    assert window.header.search_scope_btn.isChecked() is False
    assert window.proxy_model.rowCount() == 0
    assert user_apps_label in window.status_bar.currentMessage()
    assert user_apps_label in window.tree_view.empty_state()[1]

    # Clearing the search hides the scope chip again.
    window._on_search_query_changed("")
    assert window.header.search_scope_btn.isHidden() is True
    window.close()
