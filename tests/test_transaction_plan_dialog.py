"""Widget-level checks for structured transaction review presentation."""
from __future__ import annotations

from core.dnf5daemon import TransactionItem, TransactionPlan, TransactionRequest
from ui.transaction_plan_dialog import TransactionPlanReviewDialog


def _plan(*, protected: bool) -> TransactionPlan:
    request = TransactionRequest.from_values(removals=["systemd"] if protected else ["htop"])
    package_name = "systemd" if protected else "htop"
    return TransactionPlan(
        token="dialog-plan",
        session_path="/org/rpm/dnf/v0/session/dialog",
        request=request,
        items=(
            TransactionItem(
                object_type="Package",
                action="Remove",
                reason="User",
                attributes={},
                package={"name": package_name, "version": "1", "release": "1.fc44", "arch": "x86_64"},
            ),
        ),
        resolver_result=0,
        warnings=(),
        critical_removals=("systemd",) if protected else (),
    )


def test_review_dialog_renders_structured_plan_and_protected_removal(qtbot) -> None:
    dialog = TransactionPlanReviewDialog(_plan(protected=True))
    qtbot.addWidget(dialog)

    assert dialog.windowTitle() == "Review DNF5 Transaction Plan"
    assert "Remove: systemd-1-1.fc44.x86_64 (User)" in dialog.plan_text.toPlainText()
    assert dialog.execute_button.text() == "Execute Protected Removal"


def test_review_dialog_uses_normal_execution_label_for_nonprotected_plan(qtbot) -> None:
    dialog = TransactionPlanReviewDialog(_plan(protected=False))
    qtbot.addWidget(dialog)

    assert "Remove: htop-1-1.fc44.x86_64 (User)" in dialog.plan_text.toPlainText()
    assert dialog.execute_button.text() == "Execute and Authenticate"
