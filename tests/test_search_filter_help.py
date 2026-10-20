"""Regression coverage for the persistent search-filter reference."""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from ui.header import SEARCH_FILTER_TOOLTIP, HeaderBar


def test_filter_reference_stays_available_during_live_validation(qtbot) -> None:
    header = HeaderBar()
    qtbot.addWidget(header)

    header.search_input.setText("status:not-a-status")

    assert header.search_input.property("filterState") == "invalid"
    assert "Invalid filter syntax" in header.search_input.toolTip()
    assert header.search_help_btn.toolTip() == SEARCH_FILTER_TOOLTIP
    assert header.search_help_btn.accessibleName() == "Search filter reference"
    assert "filter:value" in SEARCH_FILTER_TOOLTIP
    assert "status:update" in SEARCH_FILTER_TOOLTIP
