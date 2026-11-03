"""GUI smoke tests for the budget tab (headless)."""

from datetime import date

import pytest
from PySide6.QtWidgets import QMessageBox
from sqlalchemy import select

from hiro_budget.config import TargetDef, load_settings
from hiro_budget.db.models import Category
from hiro_budget.gui.budget_view import (
    CAT_ROLE,
    COL_ACTIVITY,
    COL_ASSIGNED,
    COL_AVAILABLE,
    COL_CARRY,
    COL_NAME,
    COL_TARGET,
    PROGRESS_ROLE,
)
from hiro_budget.gui.common import money
from hiro_budget.gui.main_window import MainWindow
from hiro_budget.gui.move_money_dialog import MoveMoneyDialog
from hiro_budget.gui.proposal_dialog import ProposalDialog
from hiro_budget.gui.target_dialog import TargetDialog
from hiro_budget.services import budget, import_service
from hiro_budget.services import transactions as txs
from hiro_budget.services.budget import Envelope


@pytest.fixture(autouse=True)
def no_message_boxes(monkeypatch):
    for name in ("information", "warning", "critical"):
        monkeypatch.setattr(QMessageBox, name, lambda *a, **k: QMessageBox.StandardButton.Ok)


@pytest.fixture
def window(qtbot, app, statement_pdf):
    with app.session() as s:
        import_service.import_file(s, app.settings, statement_pdf)
    w = MainWindow(app)
    qtbot.addWidget(w)
    w.budget.go("2026-03")
    return w


def cat(app, key: str) -> int:
    with app.session() as s:
        return s.scalar(select(Category.id).where(Category.key == key))


def find_item(view, category_id: int):
    for i in range(view.tree.topLevelItemCount()):
        g = view.tree.topLevelItem(i)
        for j in range(g.childCount()):
            if g.child(j).data(COL_NAME, CAT_ROLE) == category_id:
                return g.child(j)
    return None


def test_budget_follows_the_overview_and_shows_month(window) -> None:
    assert window.tabs.widget(0) is window.overview
    assert window.tabs.widget(1) is window.budget
    view = window.budget
    assert view.data.month == "2026-03"
    assert "2026" in view.month_label.text()
    assert view.tree.topLevelItemCount() > 0
    assert view.warning.isVisible() or view.data.uncategorized_count  # 3 uncategorized
    # Giro 1000 + cash 100 + salary 4000 - uncategorized 250, 120 and 400 (principal)
    assert view.data.ready_to_assign == 4330_00
    assert "4.330,00" in view.rta_amount.text() or "4,330.00" in view.rta_amount.text()


def test_totals_row_and_breakdown(window) -> None:
    view = window.budget
    view.set_assigned(cat(window.app, "lebensmittel.supermarkt"), 300_00)
    t = view.totals_item
    assert t.text(COL_ASSIGNED).startswith("300,00") or t.text(COL_ASSIGNED).startswith("300.00")
    assert t.text(COL_ACTIVITY) == money(view.data.activity)
    assert "Ready to Assign" in t.toolTip(COL_AVAILABLE)
    assert t.toolTip(COL_ACTIVITY)
    # aligned with the tree's columns
    assert view.totals.columnWidth(COL_AVAILABLE) == view.tree.header().sectionSize(COL_AVAILABLE)
    # header: balance + inflow - assigned = Ready to Assign, one row each with a tooltip
    shown = [lbl for lbl in view.terms.values() if not lbl.isHidden()]
    assert all(lbl.toolTip() for lbl in shown)
    assert view.terms["carried"].isHidden() and view.terms["future"].isHidden()
    assert view.term_names["future"].isHidden()
    assert "Ready to Assign" in view.term_names["result"].text()
    assert "4.030,00" in view.terms["result"].text() or "4,030.00" in view.terms["result"].text()
    assert "Assigned" in view.rta_box.toolTip()
    # the card shows the calculation: balance + inflow - assigned
    assert view.rta_formula.text().count("+") == 1 and "−" in view.rta_formula.text()
    assert "€" not in view.rta_formula.text()


def test_collapse_and_expand_all(window) -> None:
    view = window.budget
    groups = [view.tree.topLevelItem(i) for i in range(view.tree.topLevelItemCount())]
    assert all(g.isExpanded() for g in groups)
    assert view.expand_button.text() == "Collapse all"
    view.expand_button.click()
    assert not any(g.isExpanded() for g in groups)
    assert view.expand_button.text() == "Expand all"
    view.refresh()  # stays collapsed
    assert not any(view.tree.topLevelItem(i).isExpanded() for i in range(len(groups)))
    view.tree.topLevelItem(0).setExpanded(True)  # one open: the button collapses again
    assert view.expand_button.text() == "Collapse all"
    view.expand_button.click()
    view.expand_button.click()
    assert all(view.tree.topLevelItem(i).isExpanded() for i in range(len(groups)))


def test_empty_categories_are_hidden(window) -> None:
    view = window.budget
    aerzte = cat(window.app, "gesundheit.aerzte")
    groceries = cat(window.app, "lebensmittel.supermarkt")
    assert view.hide_empty.isChecked()
    assert find_item(view, aerzte) is None  # nothing assigned, spent or carried
    assert find_item(view, groceries) is not None  # has bookings
    view.hide_empty.setChecked(False)
    assert find_item(view, aerzte) is not None
    view.hide_empty.setChecked(True)
    view.set_assigned(aerzte, 50_00)
    assert find_item(view, aerzte) is not None  # assigned: no longer empty


def test_from_last_month_column(window) -> None:
    view = window.budget
    groceries = cat(window.app, "lebensmittel.supermarkt")
    view.set_assigned(groceries, 300_00)
    view.go(budget.month_add("2026-03", 1))
    c = view.category(groceries)
    assert c.carry > 0
    assert find_item(view, groceries).text(COL_CARRY) == money(c.carry)
    assert view.totals_item.text(COL_CARRY) == money(view.data.carry)
    assert view.tree.headerItem().toolTip(COL_CARRY)


def test_assign_and_move_money(window) -> None:
    view = window.budget
    groceries, fun = (
        cat(window.app, "lebensmittel.supermarkt"),
        cat(window.app, "freizeit.restaurants"),
    )
    rta = view.data.ready_to_assign
    view.set_assigned(groceries, 300_00)
    assert view.category(groceries).assigned == 300_00
    assert view.data.ready_to_assign == rta - 300_00

    dlg = MoveMoneyDialog(view.data, view, from_id=groceries, to_id=fun, cents=50_00)
    dlg._accept()
    assert (dlg.from_id, dlg.to_id, dlg.cents) == (Envelope.of(groceries), Envelope.of(fun), 50_00)


def test_filter_overspent(window) -> None:
    view = window.budget
    view.filter.setCurrentIndex(view.filter.findData("overspent"))
    names = [
        view.tree.topLevelItem(i).child(j).text(COL_NAME)
        for i in range(view.tree.topLevelItemCount())
        for j in range(view.tree.topLevelItem(i).childCount())
    ]
    assert "Baufinanzierung" in names and "Supermarkt" in names  # spent without assignment
    assert all(view.category(c).overspent for c in [cat(window.app, "wohnen.baufinanzierung")])


def test_activity_double_click_opens_bookings(window) -> None:
    view = window.budget
    groceries = cat(window.app, "lebensmittel.supermarkt")
    view._double_clicked(find_item(view, groceries), COL_ACTIVITY)
    assert window.tabs.currentWidget() is window.transactions
    rows = window.transactions.model.rows()
    assert [r.payee for r in rows] == ["REWE MUSTERSTADT"]


def test_target_dialog_and_save(window, config_dir, monkeypatch) -> None:
    dlg = TargetDialog("Supermarkt", None)
    dlg.type.setCurrentIndex(dlg.type.findData("monthly"))
    dlg.amount.setValue(600)
    dlg._accept()
    assert dlg.result_target == TargetDef(type="monthly", amount=600)

    def fake_exec(d):
        d.result_target = TargetDef(type="monthly", amount=600)
        return 1

    monkeypatch.setattr(TargetDialog, "exec", fake_exec)
    view = window.budget
    groceries = cat(window.app, "lebensmittel.supermarkt")
    view.edit_target(groceries)
    assert view.category(groceries).needed == 600_00
    assert "600" in find_item(view, groceries).text(COL_TARGET)
    reloaded = load_settings(config_dir)
    assert any(
        c.target and c.target.amount == 600
        for g in reloaded.categories.groups
        for c in g.categories
    )
    view.assign_to_targets()
    assert view.category(groceries).assigned == 600_00


def test_cover_overspending_button(window) -> None:
    view = window.budget
    overspent = [e for e in view.data.envelopes if e.overspent]
    assert overspent and view.cover_button.isEnabled()  # bookings, nothing assigned
    deficit = -sum(e.available for e in overspent)
    rta = view.data.ready_to_assign
    view.cover_overspending()
    assert not any(e.overspent for e in view.data.envelopes)
    assert view.data.ready_to_assign == rta - deficit
    assert not view.cover_button.isEnabled()


def test_uncategorized_link_filters_transactions(window) -> None:
    window.budget.show_uncategorized.emit("2026-03")
    assert window.transactions.current_filter().category == txs.UNCATEGORIZED
    assert window.transactions.current_filter().date_from == date(2026, 3, 1)


def test_budget_refreshes_after_booking_change(window) -> None:
    from hiro_budget.services.transactions import ManualEntry

    view = window.budget
    before = view.data.ready_to_assign
    with window.app.session() as s:
        cash = s.scalar(select(budget.Account.id).where(budget.Account.name == "Bargeld"))
        txs.create_manual(
            s, window.app.settings, ManualEntry(cash, date(2026, 3, 9), 50_00, "Flohmarkt")
        )
    window.tabs.setCurrentWidget(window.transactions)
    window.transactions.data_changed.emit()
    window.tabs.setCurrentWidget(window.budget)  # refreshes when the tab is shown
    assert view.data.ready_to_assign == before + 50_00


def test_undo_and_redo_assignments(window, monkeypatch) -> None:
    view = window.budget
    groceries = cat(window.app, "lebensmittel.supermarkt")
    fun = cat(window.app, "freizeit.restaurants")
    rta = view.data.ready_to_assign
    assert not view.undo_button.isEnabled() and not view.redo_button.isEnabled()

    view.set_assigned(groceries, 300_00)
    view.set_assigned(fun, 50_00)
    assert "Assign" in view.undo_button.toolTip()
    view.undo()
    assert view.category(fun).assigned == 0 and view.category(groceries).assigned == 300_00
    view.undo()
    assert view.data.ready_to_assign == rta and not view.undo_button.isEnabled()
    view.redo()
    assert view.category(groceries).assigned == 300_00 and view.redo_button.isEnabled()

    # undo goes back to the month that changed
    view.go("2026-04")
    view.undo()
    assert view.month == "2026-03" and view.category(groceries).assigned == 0

    # a new change discards the redo steps
    view.set_assigned(fun, 10_00)
    assert not view.redo_button.isEnabled()


def test_clear_assigned_can_be_undone(window, monkeypatch) -> None:
    view = window.budget
    groceries = cat(window.app, "lebensmittel.supermarkt")
    view.set_assigned(groceries, 300_00)
    view.set_assigned(cat(window.app, "freizeit.restaurants"), 50_00)
    rta = view.data.ready_to_assign
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.No)
    view.clear_assigned()
    assert view.data.assigned_this_month == 350_00  # not confirmed
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.Yes)
    view.clear_assigned()
    assert view.data.assigned_this_month == 0
    assert view.data.ready_to_assign == rta + 350_00
    view.undo()
    assert view.data.assigned_this_month == 350_00
    assert view.category(groceries).assigned == 300_00


def test_applied_proposal_can_be_undone(window, monkeypatch) -> None:
    view = window.budget
    groceries = cat(window.app, "lebensmittel.supermarkt")

    def fake_exec(dlg):
        with window.app.session() as s:
            budget.set_assigned(s, view.month, groceries, 123_00)
        dlg.applied = 1
        return 1

    monkeypatch.setattr(ProposalDialog, "exec", fake_exec)
    view.propose_budget()
    assert view.category(groceries).assigned == 123_00
    view.undo()
    assert view.category(groceries).assigned == 0


def test_target_dialog_fits_its_rows(qtbot) -> None:
    """The rows shown depend on the type; the dialog must grow and shrink with them."""
    dlg = TargetDialog("Badezimmer", None)
    qtbot.addWidget(dlg)
    dlg.show()
    heights = {}
    for kind in ("monthly", "repeating", "by_date"):
        dlg.type.setCurrentIndex(dlg.type.findData(kind))
        assert dlg.width() >= 460
        assert dlg.height() >= dlg.sizeHint().height()  # nothing squeezed
        heights[kind] = dlg.height()
    assert heights["monthly"] < heights["by_date"] < heights["repeating"]


def test_group_rows_show_the_worst_category(window) -> None:
    from hiro_budget.gui.budget_view import GROUP_ROLE, _colors

    view = window.budget
    view.hide_empty.setChecked(True)

    def group_item(key: str):
        return next(
            view.tree.topLevelItem(i)
            for i in range(view.tree.topLevelItemCount())
            if view.tree.topLevelItem(i).data(COL_NAME, GROUP_ROLE) == key
        )

    colors = _colors()
    groceries = cat(window.app, "lebensmittel.supermarkt")
    assert view.category(groceries).overspent  # REWE booking, nothing assigned
    item = group_item("lebensmittel")
    assert item.background(COL_AVAILABLE).color() == colors["neg"]
    assert "Supermarkt" in item.toolTip(COL_AVAILABLE)

    # cover every overspent category of the group: it turns green
    for c in view.data.categories:
        if c.group_key == "lebensmittel" and c.overspent:
            view.set_assigned(c.category_id, -c.activity + 10_00)
    item = group_item("lebensmittel")
    assert item.background(COL_AVAILABLE).color() == colors["pos"]


def test_move_money_without_selection_targets_a_line_that_needs_money(window, monkeypatch):
    # #42: From and To both started as Ready to Assign
    view = window.budget
    shown = []
    monkeypatch.setattr(MoveMoneyDialog, "exec", lambda dlg: shown.append(dlg) or 0)
    view.move_money()
    dlg = shown[0]
    assert dlg.source.currentData() == "rta"
    first_overspent = next(e for e in view.data.envelopes if e.overspent and not e.hidden)
    assert dlg.target.currentData() == first_overspent.envelope
    assert round(dlg.amount.value() * 100) == -first_overspent.available
    dlg._accept()
    assert dlg.to_id == first_overspent.envelope and dlg.from_id is None


def test_move_money_needs_an_amount(window) -> None:
    view = window.budget
    groceries = cat(window.app, "lebensmittel.supermarkt")
    dlg = MoveMoneyDialog(view.data, view, to_id=groceries)
    assert dlg.amount.value() == 0
    dlg._accept()
    assert dlg.result() == 0 and dlg.error.text()


def test_target_progress_bar(window, monkeypatch) -> None:
    # #44: a progress bar under each target: share funded, amber while behind this month
    def fake_exec(d):
        d.result_target = TargetDef(type="monthly", amount=600)
        return 1

    monkeypatch.setattr(TargetDialog, "exec", fake_exec)
    view = window.budget
    groceries = cat(window.app, "lebensmittel.supermarkt")
    view.edit_target(groceries)
    assert find_item(view, groceries).data(COL_TARGET, PROGRESS_ROLE) == (0.0, True)
    assert find_item(view, groceries).toolTip(COL_TARGET).startswith("600")
    view.set_assigned(view.category(groceries).envelope, 150_00)
    assert find_item(view, groceries).data(COL_TARGET, PROGRESS_ROLE) == (0.25, True)
    view.set_assigned(view.category(groceries).envelope, 600_00)
    assert find_item(view, groceries).data(COL_TARGET, PROGRESS_ROLE) == (1.0, False)
    without = [  # shown categories without a target: no bar
        item
        for i in range(view.tree.topLevelItemCount())
        for j in range(view.tree.topLevelItem(i).childCount())
        if (item := view.tree.topLevelItem(i).child(j)).data(COL_NAME, CAT_ROLE) != groceries
    ]
    assert without
    assert all(item.data(COL_TARGET, PROGRESS_ROLE) is None for item in without)
    view.tree.grab()  # paints the bars without an error
