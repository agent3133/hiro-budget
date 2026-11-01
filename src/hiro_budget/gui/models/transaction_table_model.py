"""Table model over ``TransactionRow`` objects."""

from __future__ import annotations

from enum import IntEnum

from PySide6.QtCore import (
    QAbstractTableModel,
    QModelIndex,
    QPersistentModelIndex,
    QSortFilterProxyModel,
    Qt,
)
from PySide6.QtGui import QFont

from hiro_budget.db.models import SplitSource
from hiro_budget.gui.common import amount_color, fmt_date, highlight_color, money
from hiro_budget.services.transactions import TransactionRow

ROW_ROLE = Qt.ItemDataRole.UserRole + 2


class Col(IntEnum):
    DATE = 0
    ACCOUNT = 1
    TYPE = 2
    PAYEE = 3
    PURPOSE = 4
    AMOUNT = 5
    CATEGORY = 6
    SOURCE = 7


class TransactionTableModel(QAbstractTableModel):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._rows: list[TransactionRow] = []
        self._sort: tuple[int, Qt.SortOrder] | None = None  # column, order
        self._headers = {
            Col.DATE: self.tr("Date"),
            Col.ACCOUNT: self.tr("Account"),
            Col.TYPE: self.tr("Type"),
            Col.PAYEE: self.tr("Payee"),
            Col.PURPOSE: self.tr("Purpose"),
            Col.AMOUNT: self.tr("Amount"),
            Col.CATEGORY: self.tr("Category"),
            Col.SOURCE: self.tr("Source"),
        }
        # two lines per booking: payee over purpose, account over booking type
        self.two_lines = False
        self._two_line_headers = {
            Col.PAYEE: self.tr("Payee · purpose"),
            Col.ACCOUNT: self.tr("Account · type"),
        }

    def set_two_lines(self, on: bool) -> None:
        self.two_lines = on
        self.headerDataChanged.emit(Qt.Orientation.Horizontal, 0, len(Col) - 1)

    # -- data access ---------------------------------------------------------

    def set_rows(self, rows: list[TransactionRow]) -> None:
        self.beginResetModel()
        self._rows = rows
        if self._sort is not None:
            self._sort_rows()
        self.endResetModel()

    # -- sorting (here, not in the proxy: Qt would ask Python for a key at every one of
    # the n log n comparisons, a second for ten years of bookings) ---------------------

    def sort(self, column: int, order: Qt.SortOrder = Qt.SortOrder.AscendingOrder) -> None:
        self._sort = (column, order)
        self.layoutAboutToBeChanged.emit()
        old = self.persistentIndexList()  # selection and current row stay on their rows
        ids = [self._rows[i.row()].id for i in old]
        self._sort_rows()
        position = {r.id: i for i, r in enumerate(self._rows)}
        self.changePersistentIndexList(
            old, [self.index(position[id_], i.column()) for id_, i in zip(ids, old, strict=True)]
        )
        self.layoutChanged.emit()

    def _sort_rows(self) -> None:
        column, order = self._sort
        col = Col(column)
        if col == Col.DATE:
            key = lambda r: (r.booking_date.toordinal(), r.id)  # noqa: E731
        elif col == Col.AMOUNT:
            key = lambda r: r.amount_cents  # noqa: E731
        else:
            key = lambda r: self.display(r, col).lower()  # noqa: E731
        self._rows.sort(key=key, reverse=order == Qt.SortOrder.DescendingOrder)

    def rows(self) -> list[TransactionRow]:
        return self._rows

    def row_at(self, row: int) -> TransactionRow:
        return self._rows[row]

    def replace_row(self, new: TransactionRow) -> None:
        for i, r in enumerate(self._rows):
            if r.id == new.id:
                self._rows[i] = new
                self.dataChanged.emit(self.index(i, 0), self.index(i, len(Col) - 1))
                return

    # -- labels --------------------------------------------------------------

    def category_label(self, r: TransactionRow) -> str:
        if r.is_split:
            return self.tr("Split (%n parts)", "", len(r.splits))
        s = r.splits[0] if r.splits else None
        if s is None:
            return ""
        if s.transfer_account_name and s.category_name:
            return f"{s.category_name} (⇄ {s.transfer_account_name})"
        if s.transfer_account_name:
            return f"⇄ {s.transfer_account_name}"
        return s.category_name or ""

    def source_label(self, r: TransactionRow) -> str:
        if r.is_counterpart:
            return self.tr("counter-booking")
        sources = {s.source for s in r.splits}
        if SplitSource.MANUAL in sources:
            return self.tr("manual")
        if SplitSource.ML in sources:
            conf = max((s.confidence or 0) for s in r.splits)
            return self.tr("suggestion %1%").replace("%1", str(round(conf * 100)))
        if SplitSource.MORTGAGE in sources:
            return self.tr("mortgage")
        if SplitSource.TRANSFER in sources:
            return self.tr("transfer")
        if SplitSource.RULE in sources:
            rule = next((s.rule_id for s in r.splits if s.rule_id), "")
            return self.tr("rule: %1").replace("%1", rule)
        return ""

    def split_tooltip(self, r: TransactionRow) -> str:
        lines = []
        for s in r.splits:
            parts = []
            if s.category_name:
                parts.append(f"{s.group_name}: {s.category_name}")
            if s.transfer_account_name:
                parts.append(f"⇄ {s.transfer_account_name}")
            if s.needs_category:
                parts.append(self.tr("uncategorized"))
            target = " ".join(parts)
            memo = f" ({s.memo})" if s.memo else ""
            lines.append(f"{money(s.amount_cents)}  {target}{memo}")
        return "\n".join(lines)

    def display(self, r: TransactionRow, col: Col) -> str:
        if col == Col.DATE:
            return fmt_date(r.booking_date)
        if col == Col.ACCOUNT:
            return r.account_name
        if col == Col.TYPE:
            return r.booking_type
        if col == Col.PAYEE:
            return r.payee
        if col == Col.PURPOSE:
            return f"✎ {r.notes} — {r.purpose}" if r.notes else r.purpose
        if col == Col.AMOUNT:
            return money(r.amount_cents)
        if col == Col.CATEGORY:
            return self.category_label(r)
        return self.source_label(r)

    # -- Qt model API --------------------------------------------------------

    def rowCount(self, parent: QModelIndex | QPersistentModelIndex = QModelIndex()) -> int:  # noqa: B008
        return 0 if parent.isValid() else len(self._rows)

    def columnCount(self, parent: QModelIndex | QPersistentModelIndex = QModelIndex()) -> int:  # noqa: B008
        return 0 if parent.isValid() else len(Col)

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):
        if orientation == Qt.Orientation.Horizontal and role == Qt.ItemDataRole.DisplayRole:
            if self.two_lines and section in self._two_line_headers:
                return self._two_line_headers[Col(section)]
            return self._headers[Col(section)]
        return None

    def flags(self, index):
        f = super().flags(index)
        r = self._rows[index.row()]
        if index.column() == Col.CATEGORY and not r.is_split and not r.is_counterpart:
            f |= Qt.ItemFlag.ItemIsEditable
        return f

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        r = self._rows[index.row()]
        col = Col(index.column())

        if role == ROW_ROLE:
            return r
        if role == Qt.ItemDataRole.DisplayRole:
            return self.display(r, col)
        if role == Qt.ItemDataRole.TextAlignmentRole and col == Col.AMOUNT:
            return int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        if role == Qt.ItemDataRole.ForegroundRole and col == Col.AMOUNT:
            return amount_color(r.amount_cents)
        if role == Qt.ItemDataRole.BackgroundRole and col == Col.CATEGORY:
            if r.is_uncategorized or r.needs_review:
                return highlight_color()
        if role == Qt.ItemDataRole.FontRole and col == Col.SOURCE and r.needs_review:
            f = QFont()
            f.setItalic(True)
            return f
        if role == Qt.ItemDataRole.ToolTipRole:
            if col == Col.CATEGORY:
                return self.split_tooltip(r)
            if col in (Col.PURPOSE, Col.PAYEE):
                if r.notes:
                    return self.tr("Note: %1").replace("%1", r.notes) + "\n\n" + r.raw_text
                return r.raw_text
        return None


class ModelSortProxy(QSortFilterProxyModel):
    """Passes the rows through in the order of the model, which sorts itself (see
    ``TransactionTableModel.sort``); the view still talks to a proxy."""

    def sort(self, column: int, order: Qt.SortOrder = Qt.SortOrder.AscendingOrder) -> None:
        self.sourceModel().sort(column, order)
