"""Bookings page: the category menu with groups (double-click / Enter) and the group filter."""

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QComboBox, QMenu, QMessageBox
from sqlalchemy import select

from hiro_budget.db.models import Category, CategoryGroup
from hiro_budget.gui.main_window import MainWindow
from hiro_budget.gui.models.transaction_table_model import ROW_ROLE, Col
from hiro_budget.services import import_service
from hiro_budget.services import transactions as txs

SHOWN: list[QMenu] = []  # menus opened by the view in a test


@pytest.fixture(autouse=True)
def no_dialogs(monkeypatch):
    for name in ("information", "warning", "critical"):
        monkeypatch.setattr(QMessageBox, name, lambda *a, **k: QMessageBox.StandardButton.Ok)
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.No)
    # menus would wait for a click: record them instead (patching QMenu.exec has no effect)
    from hiro_budget.gui import transactions_view

    class RecordingMenu(QMenu):
        def exec(self, *args):
            SHOWN.append(self)

    SHOWN.clear()
    monkeypatch.setattr(transactions_view, "QMenu", RecordingMenu)


@pytest.fixture
def view(qtbot, app, statement_pdf):
    with app.session() as s:
        import_service.import_file(s, app.settings, statement_pdf)
    w = MainWindow(app)
    qtbot.addWidget(w)
    w.tabs.setCurrentWidget(w.transactions)
    w.transactions.show_filter(txs.ALL)
    return w.transactions


def editable(view, categorized: bool):
    """A category cell that can be edited, of a booking with (or without) a category."""
    for r in range(view.proxy.rowCount()):
        index = view.proxy.index(r, Col.CATEGORY)
        row = index.data(ROW_ROLE)
        has = bool(row.splits and row.splits[0].category_id)
        if index.flags() & Qt.ItemFlag.ItemIsEditable and has == categorized:
            return index, row
    raise AssertionError("no such booking")


def group_menus(menu: QMenu) -> dict[str, QMenu]:
    return {a.text(): a.menu() for a in menu.actions() if a.menu() is not None}


def test_double_click_opens_the_group_menu(qtbot, view) -> None:
    opened = []
    view.open_category_menu = lambda index: opened.append(index.row())
    index, _row = editable(view, categorized=True)
    view.table.setCurrentIndex(index)
    view.table.edit(index)
    qtbot.waitUntil(lambda: opened == [index.row()])
    assert view.table.findChild(QComboBox) is None  # no drop-down editor any more


def test_menu_has_groups_and_ticks_the_current_category(view) -> None:
    index, row = editable(view, categorized=True)
    menu = view.open_category_menu(index)
    groups = group_menus(menu)
    expected = [ch.label.strip() for ch in view.choices if ch.is_group_header]
    assert list(groups) == expected  # categories and "Transfer to own account"
    current = row.splits[0].category_id
    ticked = [
        (title, a.text()) for title, sub in groups.items() for a in sub.actions() if a.isChecked()
    ]
    name = next(ch.label.strip() for ch in view.choices if ch.category_id == current)
    assert len(ticked) == 1 and ticked[0][1] == name
    assert groups[ticked[0][0]].menuAction().font().bold()  # its group stands out


def test_choosing_from_the_menu_assigns(view, app) -> None:
    index, row = editable(view, categorized=False)
    menu = view.open_category_menu(index)
    sub = next(iter(group_menus(menu).values()))
    action = sub.actions()[0]
    action.trigger()
    with app.session() as s:
        chosen = s.scalar(select(Category).where(Category.name == action.text()))
        assert chosen is not None
    refreshed = next(r for r in view.model.rows() if r.id == row.id)
    assert refreshed.splits[0].category_id == chosen.id


def test_context_menu_uses_the_same_groups(view) -> None:
    index, _row = editable(view, categorized=True)
    view.table.setCurrentIndex(index)
    view.table.selectRow(index.row())
    view._context_menu(view.table.visualRect(index).center())
    assign = next(a.menu() for a in SHOWN[-1].actions() if a.menu() is not None)
    expected = [ch.label.strip() for ch in view.choices if ch.is_group_header]
    assert list(group_menus(assign)) == expected


def test_filter_by_category_group(view, app) -> None:
    combo = view.category
    with app.session() as s:
        group = s.scalar(select(CategoryGroup).where(CategoryGroup.key == "einkommen"))
        in_group = set(s.scalars(select(Category.id).where(Category.group_id == group.id)))
    i = combo.findData(txs.group_filter(group.id))
    assert i >= 0 and combo.model().item(i).isEnabled() and combo.model().item(i).font().bold()
    combo.setCurrentIndex(i)
    assert view.current_filter().category == txs.group_filter(group.id)
    rows = view.model.rows()
    assert rows and all(any(sp.category_id in in_group for sp in r.splits) for r in rows)
    with app.session() as s:
        everything = txs.count_transactions(s, txs.TxFilter())
        assert (
            txs.count_transactions(s, txs.TxFilter(category=txs.group_filter(group.id)))
            == len(rows)
            < everything
        )
    # back and forward keep the group
    state = view.nav_state()
    combo.setCurrentIndex(combo.findData(txs.ALL))
    view.restore_nav_state(state)
    assert view.current_filter().category == txs.group_filter(group.id)
