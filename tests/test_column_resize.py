"""Dragging a column border resizes the column on its left (budget and bookings)."""

import pytest
from PySide6.QtCore import QPoint, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QMessageBox

from hiro_budget.gui.budget_view import COL_ASSIGNED, COL_NAME, BudgetView
from hiro_budget.services import import_service


@pytest.fixture(autouse=True)
def no_message_boxes(monkeypatch):
    for name in ("information", "warning", "critical"):
        monkeypatch.setattr(QMessageBox, name, lambda *a, **k: QMessageBox.StandardButton.Ok)


def drag_border(qtbot, header, section: int, dx: int) -> None:
    """Drag the right border of ``section`` by ``dx`` pixels with the mouse."""
    x = header.sectionViewportPosition(section) + header.sectionSize(section) - 1
    y = header.height() // 2
    viewport = header.viewport()
    QTest.mousePress(
        viewport, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, QPoint(x, y)
    )
    for step in range(1, 6):
        QTest.mouseMove(viewport, QPoint(x + dx * step // 5, y))
    QTest.mouseRelease(
        viewport, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, QPoint(x + dx, y)
    )


@pytest.fixture
def budget(qtbot, app, statement_pdf):
    with app.session() as s:
        import_service.import_file(s, app.settings, statement_pdf)
    view = BudgetView(app)
    qtbot.addWidget(view)
    view.resize(1400, 700)
    view.show()
    qtbot.waitExposed(view)
    view.go("2026-03")
    return view


def test_budget_columns_resize_by_dragging(qtbot, budget) -> None:
    h = budget.tree.header()
    widths = [h.sectionSize(c) for c in range(h.count())]
    drag_border(qtbot, h, COL_ASSIGNED, 40)
    assert h.sectionSize(COL_ASSIGNED) == widths[COL_ASSIGNED] + 40
    assert h.sectionSize(COL_NAME) == widths[COL_NAME]  # the column on the left stays
    drag_border(qtbot, h, COL_NAME, -50)
    assert h.sectionSize(COL_NAME) == widths[COL_NAME] - 50
    # the totals row follows the columns
    assert budget.totals.columnWidth(COL_ASSIGNED) == h.sectionSize(COL_ASSIGNED)


def test_budget_column_widths_are_remembered(qtbot, app, budget) -> None:
    budget.tree.setColumnWidth(COL_NAME, 333)
    budget.save_state()
    again = BudgetView(app)
    qtbot.addWidget(again)
    assert again.tree.header().sectionSize(COL_NAME) == 333


def test_bookings_columns_resize_by_dragging(qtbot, app) -> None:
    from hiro_budget.gui.models.transaction_table_model import Col
    from hiro_budget.gui.transactions_view import TransactionsView

    view = TransactionsView(app)
    qtbot.addWidget(view)
    view.set_two_lines(False)  # separate purpose column
    view.resize(1800, 700)  # the list next to the booking card
    view.show()
    qtbot.waitExposed(view)
    h = view.table.horizontalHeader()
    # first start: the purpose column gets the free width
    qtbot.waitUntil(lambda: h.sectionSize(Col.PURPOSE) > 300)
    purpose = h.sectionSize(Col.PURPOSE)
    drag_border(qtbot, h, Col.PURPOSE, -60)
    assert h.sectionSize(Col.PURPOSE) == purpose - 60
    payee = h.sectionSize(Col.PAYEE)
    drag_border(qtbot, h, Col.AMOUNT, 30)
    assert h.sectionSize(Col.PAYEE) == payee  # only the dragged column changes


def test_bookings_widths_saved_by_older_versions_stay_resizable(qtbot, app) -> None:
    from PySide6.QtWidgets import QHeaderView

    from hiro_budget.gui.models.transaction_table_model import Col
    from hiro_budget.gui.transactions_view import TransactionsView

    old = TransactionsView(app)
    qtbot.addWidget(old)
    # earlier versions gave the purpose column an automatic (not draggable) width
    old.table.horizontalHeader().setSectionResizeMode(Col.PURPOSE, QHeaderView.ResizeMode.Stretch)
    old.save_state()
    view = TransactionsView(app)
    qtbot.addWidget(view)
    mode = view.table.horizontalHeader().sectionResizeMode(Col.PURPOSE)
    assert mode == QHeaderView.ResizeMode.Interactive


def test_bookings_columns_fit_when_the_list_narrows(qtbot, app) -> None:
    from hiro_budget.gui.models.transaction_table_model import Col
    from hiro_budget.gui.transactions_view import PURPOSE_MIN_WIDTH, TransactionsView

    view = TransactionsView(app)
    qtbot.addWidget(view)
    view.set_two_lines(False)  # separate purpose column
    view.resize(1800, 700)
    view.show()
    qtbot.waitExposed(view)
    h = view.table.horizontalHeader()
    qtbot.waitUntil(lambda: h.sectionSize(Col.PURPOSE) > 300)
    view.splitter.setSizes([900, 900])  # a wider booking card: the purpose column gives way
    qtbot.waitUntil(
        lambda: (
            sum(h.sectionSize(c) for c in range(h.count() - 1)) <= view.table.viewport().width()
            or h.sectionSize(Col.PURPOSE) == PURPOSE_MIN_WIDTH
        )
    )


def test_two_lines_per_booking(qtbot, app, statement_pdf) -> None:
    from hiro_budget.gui.models.transaction_table_model import Col
    from hiro_budget.gui.transactions_view import TransactionsView
    from hiro_budget.services import import_service

    with app.session() as s:
        import_service.import_file(s, app.settings, statement_pdf)
    view = TransactionsView(app)
    qtbot.addWidget(view)
    view.resize(1800, 700)
    view.show()
    qtbot.waitExposed(view)
    table, h = view.table, view.table.horizontalHeader()
    assert view.two_lines  # the default
    assert table.isColumnHidden(Col.TYPE) and table.isColumnHidden(Col.PURPOSE)
    one_line = view._one_line_height
    assert table.rowHeight(0) > one_line
    assert "·" in view.model.headerData(Col.PAYEE, h.orientation())
    qtbot.waitUntil(lambda: h.sectionSize(Col.PAYEE) > 300)  # payee takes the free width
    table.viewport().grab()  # the two-line cells draw
    view.set_two_lines(False)
    assert not table.isColumnHidden(Col.PURPOSE) and table.rowHeight(0) == one_line
    assert app.ui_state.get("transactions.two_lines") is False
    again = TransactionsView(app)
    qtbot.addWidget(again)
    assert not again.two_lines  # remembered
