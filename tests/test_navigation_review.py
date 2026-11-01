"""Bookings tab: jump between transfer and counter-booking; review classifier suggestions."""

from datetime import date

import pytest
from PySide6.QtWidgets import QMessageBox
from sqlalchemy import select

from hiro_budget.db.models import Account, Category, Split, SplitSource
from hiro_budget.gui.main_window import MainWindow
from hiro_budget.services import import_service
from hiro_budget.services import transactions as txs


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
    return w


def account_id(app, name: str) -> int:
    with app.session() as s:
        return s.scalar(select(Account.id).where(Account.name == name))


def selected_ids(view) -> list[int]:
    return [r.id for r in view.selected_rows()]


# --- original booking <-> counter-booking ------------------------------------------


def test_go_to_counter_booking_and_back(window) -> None:
    app, view = window.app, window.transactions
    with app.session() as s:
        hw = txs.list_transactions(s, txs.TxFilter(text="Handwerker"))[0]
        txs.mark_transfer(s, hw.id, account_id(app, "Bargeld"))
    with app.session() as s:
        original = txs.get_row(s, hw.id)
    assert len(original.counterparts) == 1
    cp_id, cp_account = original.counterparts[0]
    assert cp_account == "Bargeld"
    with app.session() as s:
        counter = txs.get_row(s, cp_id)
    assert counter.counterpart_of_id == hw.id

    # the counter-booking is hidden by the account filter: it is widened
    view.account.setCurrentIndex(view.account.findData(account_id(app, "ING Girokonto")))
    view.show_booking(cp_id)
    assert selected_ids(view) == [cp_id]
    assert view.account.currentData() is None
    view.show_booking(counter.counterpart_of_id)
    assert selected_ids(view) == [hw.id]


def test_go_to_booking_outside_the_date_range(window) -> None:
    app, view = window.app, window.transactions
    with app.session() as s:
        hw = txs.list_transactions(s, txs.TxFilter(text="Handwerker"))[0]
    view.date_from.setDate(date(2026, 3, 20))  # after the booking (10.03.2026)
    view.search.setText("nothing matches this")
    view.refresh()
    view.show_booking(hw.id)
    assert selected_ids(view) == [hw.id]
    assert view.search.text() == ""


# --- review of classifier suggestions -------------------------------------------------


def suggest(app, texts: list[str]) -> list[int]:
    """Turn the bookings found by ``texts`` into classifier suggestions."""
    ids = []
    with app.session() as s:
        cat = s.scalar(select(Category).where(Category.key == "sonstiges.online"))
        for text in texts:
            row = txs.list_transactions(s, txs.TxFilter(text=text))[0]
            for split in s.scalars(select(Split).where(Split.transaction_id == row.id)):
                split.source, split.category, split.confidence = SplitSource.ML, cat, 0.8
            ids.append(row.id)
    return ids


def test_review_tab_and_bar(window) -> None:
    view = window.transactions
    view.refresh()
    assert view.quick[txs.REVIEW].count == 0  # nothing to review
    ids = suggest(window.app, ["REWE", "Handwerker"])
    view.refresh()
    assert view.quick[txs.REVIEW].count == 2
    assert view.review_bar.isHidden()

    view.quick[txs.REVIEW].click()  # the tab starts the review
    assert view.quick[txs.REVIEW].isChecked() and not view.quick[txs.ALL].isChecked()
    assert view.current_filter().category == txs.REVIEW
    assert not view.review_bar.isHidden()
    assert sorted(r.id for r in view.model.rows()) == sorted(ids)
    assert "(2)" in view.accept_all_button.text()

    first = selected_ids(view)
    view.accept_selected()  # the next suggestion is selected afterwards
    assert [r.id for r in view.model.rows()] == [i for i in ids if i not in first]
    assert selected_ids(view) == [r.id for r in view.model.rows()]
    with window.app.session() as s:
        assert txs.get_row(s, first[0]).splits[0].source == SplitSource.MANUAL
        assert txs.review_count(s) == 1


def test_accept_all_shown(window, monkeypatch) -> None:
    view = window.transactions
    suggest(window.app, ["REWE", "Handwerker"])
    view.start_review()
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.No)
    view.accept_all_shown()
    assert view.model.rowCount() == 2
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.Yes)
    view.accept_all_shown()
    assert view.model.rowCount() == 0
    assert view.quick[txs.REVIEW].count == 0


# --- quick filters and assigning by payee ------------------------------------------


def test_quick_filters_count_and_switch(window) -> None:
    view = window.transactions
    view.refresh()
    with window.app.session() as s:
        everything = len(txs.list_transactions(s, view.current_filter()))
        uncategorized = txs.count_transactions(s, txs.TxFilter(category=txs.UNCATEGORIZED))
    assert view.quick[txs.ALL].count == everything and view.quick[txs.ALL].isChecked()
    assert view.quick[txs.UNCATEGORIZED].count == uncategorized == 3
    assert not view.payee_button.isHidden()
    view.quick[txs.UNCATEGORIZED].click()
    assert view.current_filter().category == txs.UNCATEGORIZED
    assert view.model.rowCount() == 3 and view.quick[txs.UNCATEGORIZED].isChecked()
    view.quick[txs.UNCATEGORIZED].click()  # clicking the active one keeps it active
    assert view.quick[txs.UNCATEGORIZED].isChecked()
    view.quick[txs.ALL].click()
    assert view.model.rowCount() == everything


def test_assign_by_payee_dialog(window) -> None:
    from hiro_budget.gui.payee_review_dialog import PayeeReviewDialog

    with window.app.session() as s:
        repair = s.scalar(select(Category.id).where(Category.key == "wohnen.instandhaltung"))
    dialog = PayeeReviewDialog(window.app)
    assert dialog.groups.topLevelItemCount() == 3
    assert not dialog.assign_button.isEnabled()  # no category chosen yet
    # the group of "Handwerker GmbH": its category and a rule
    for i in range(dialog.groups.topLevelItemCount()):
        if dialog.groups.topLevelItem(i).text(0).startswith("Handwerker"):
            dialog.groups.setCurrentItem(dialog.groups.topLevelItem(i))
    assert dialog.card.heading.text().startswith("Handwerker")
    index = next(
        i
        for i in range(dialog.category.count())
        if getattr(dialog.category.itemData(i), "category_id", None) == repair
    )
    dialog.category.setCurrentIndex(index)
    assert dialog.assign_button.isEnabled() and dialog.rule.isEnabled()
    rules_before = len(window.app.settings.rules.rules)
    dialog.assign()
    assert dialog.groups.topLevelItemCount() == 2 and dialog.assigned == 1
    assert dialog.rules_added == 1
    assert len(window.app.settings.rules.rules) == rules_before + 1
    assert window.app.settings.rules.rules[0].category == "wohnen.instandhaltung"
    # an unticked booking stays for another category
    item = dialog.bookings.topLevelItem(0)
    item.setCheckState(0, item.checkState(0).Unchecked)
    assert not dialog.assign_button.isEnabled()
    # the page counts what is left
    view = window.transactions
    view.refresh()
    assert view.quick[txs.UNCATEGORIZED].count == 2
