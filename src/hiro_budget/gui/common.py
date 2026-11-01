"""Small GUI helpers shared by the views."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date

from PySide6.QtCore import QCoreApplication, QDate, QEventLoop, Qt
from PySide6.QtGui import (
    QColor,
    QFont,
    QFontMetrics,
    QGuiApplication,
    QKeySequence,
    QPalette,
)
from PySide6.QtWidgets import QApplication, QComboBox, QMessageBox, QTableView, QWidget
from sqlalchemy import select
from sqlalchemy.orm import Session

from hiro_budget.db.models import Account, Category, CategoryGroup
from hiro_budget.gui import theme
from hiro_budget.money import format_cents

_language = "de"


def set_language(language: str) -> None:
    global _language
    _language = language


def money(cents: int, currency: str | None = "€") -> str:
    return format_cents(cents, currency=currency, locale=_language)


def fmt_number(value: float, max_decimals: int = 5, min_decimals: int = 0) -> str:
    """Number with grouping and ``min_decimals`` to ``max_decimals`` decimals."""
    text = f"{value:,.{max_decimals}f}"
    if "." in text:
        whole, decimals = text.split(".")
        decimals = decimals.rstrip("0").ljust(min_decimals, "0")
        text = f"{whole}.{decimals}" if decimals else whole
    if _language == "de":
        text = text.translate(str.maketrans(",.", ".,"))
    return text


def fmt_date(d: date | None) -> str:
    if d is None:
        return ""
    return d.strftime("%d.%m.%Y") if _language == "de" else d.isoformat()


def to_qdate(d: date) -> QDate:
    return QDate(d.year, d.month, d.day)


def from_qdate(q: QDate) -> date:
    return date(q.year(), q.month(), q.day())


def fit_to_screen(widget: QWidget, width: int, height: int, share: float = 0.9) -> None:
    """Resize to ``width`` x ``height``, but at most ``share`` of the screen (laptops)."""
    screen = widget.screen() or QApplication.primaryScreen()
    if screen is None:
        widget.resize(width, height)
        return
    available = screen.availableGeometry()
    widget.resize(
        min(width, int(available.width() * share)), min(height, int(available.height() * share))
    )


def with_keys(text: str, keys: str | QKeySequence.StandardKey) -> str:
    """Tooltip text with its keyboard shortcut, e.g. "Undo (Strg+Z)"."""
    sequence = QKeySequence(keys).toString(QKeySequence.SequenceFormat.NativeText)
    return f"{text} ({sequence})" if sequence else text


def flat_table(view: QTableView, padding: int = 10) -> None:
    """Windows 11 look for tables: no grid lines, rows as high as the text needs."""
    view.setShowGrid(False)
    rows = view.verticalHeader()
    rows.setDefaultSectionSize(view.fontMetrics().height() + padding)
    rows.setMinimumSectionSize(view.fontMetrics().height() + 4)


def accent_color() -> QColor:
    """The Windows accent colour, in the shade the style uses for light or dark mode
    (the theme's accent for a custom theme)."""
    return QApplication.palette().color(QPalette.ColorRole.Accent)


def is_dark() -> bool:
    pal = QApplication.palette()
    return pal.color(QPalette.ColorRole.Window).lightness() < 128


def amount_color(cents: int) -> QColor:
    t = theme.tokens()
    if t is not None:
        return QColor(t["neg_text"] if cents < 0 else t["pos_text"])
    if cents < 0:
        return QColor("#ff8a80") if is_dark() else QColor("#b3261e")
    return QColor("#81c995") if is_dark() else QColor("#1e7b34")


def group_colors(session: Session) -> dict[int, str]:
    """Colour of each expense group (by id), as in the budget's group chips: the theme's
    page colours in the order of the groups. Empty for a theme without them."""
    t = theme.tokens()
    if t is None:
        return {}
    pages = t["pages"]
    ids = session.scalars(
        select(CategoryGroup.id)
        .where(CategoryGroup.is_income.is_(False))
        .order_by(CategoryGroup.sort, CategoryGroup.id)
    ).all()
    return {group_id: pages[i % len(pages)] for i, group_id in enumerate(ids)}


def highlight_color() -> QColor:
    t = theme.tokens()
    if t is not None:
        return QColor(t["notice"])
    return QColor("#5c4a00") if is_dark() else QColor("#fff4cc")


def warning_color() -> QColor:
    t = theme.tokens()
    if t is not None:
        return QColor(t["warn_text"])
    return QColor("#ffb74d") if is_dark() else QColor("#a15c00")


@dataclass
class Choice:
    """An entry for category/transfer pickers."""

    label: str
    category_id: int | None = None
    transfer_account_id: int | None = None
    is_group_header: bool = False
    group_id: int | None = None  # group headers of categories


def category_choices(session: Session, *, include_transfers: bool = True) -> list[Choice]:
    groups = session.scalars(
        select(CategoryGroup).where(CategoryGroup.hidden.is_(False)).order_by(CategoryGroup.sort)
    )
    out: list[Choice] = []
    for g in groups:
        out.append(Choice(g.name, is_group_header=True, group_id=g.id))
        for c in g.categories:
            if not c.hidden:
                out.append(Choice(f"    {c.name}", category_id=c.id))
    if include_transfers:
        accounts = session.scalars(select(Account).where(Account.is_own).order_by(Account.name))
        out.append(
            Choice(
                QCoreApplication.translate("common", "Transfer to own account"),
                is_group_header=True,
            )
        )
        for a in accounts:
            out.append(Choice(f"    ⇄ {a.name}", transfer_account_id=a.id))
    return out


ITEM_PADDING = 30  # around an entry in the open list (theme: 8 px each side, frame, margins)


def fill_choice_combo(combo: QComboBox, choices: list[Choice]) -> None:
    """Fill a combo with choices; group headers are shown but not selectable."""
    combo.clear()
    for ch in choices:
        combo.addItem(ch.label, ch)
        if ch.is_group_header:
            item = combo.model().item(combo.count() - 1)
            item.setEnabled(False)
            font = item.font()
            font.setBold(True)
            item.setFont(font)
    # The open list as wide as its longest entry, also below a narrow drop-down (the
    # category editor in a table cell would otherwise cut every name to "Ei…en").
    # Measured with the fonts, not sizeHintForColumn: through the stylesheet that takes
    # ~3 ms per entry (0.2 s for the categories of a large budget).
    view = combo.view()
    bold = QFont(view.font())
    bold.setBold(True)
    metrics = {False: QFontMetrics(view.font()), True: QFontMetrics(bold)}
    widest = max(
        (metrics[ch.is_group_header].horizontalAdvance(ch.label) for ch in choices), default=0
    )
    view.setMinimumWidth(widest + ITEM_PADDING + view.verticalScrollBar().sizeHint().width() + 24)


def select_choice(combo: QComboBox, category_id: int | None, transfer_id: int | None) -> None:
    for i in range(combo.count()):
        ch: Choice = combo.itemData(i)
        if (
            ch
            and not ch.is_group_header
            and (ch.category_id, ch.transfer_account_id)
            == (
                category_id,
                transfer_id,
            )
        ):
            combo.setCurrentIndex(i)
            return
    combo.setCurrentIndex(-1)


def category_lookup(session: Session) -> dict[int, Category]:
    return {c.id: c for c in session.scalars(select(Category))}


def learning_text() -> str:
    """What the classifier does while it trains (after an import, Retrain)."""
    return QCoreApplication.translate(
        "common", "Classifier is learning from the categorized bookings…"
    )


@contextmanager
def busy(show: Callable[[str], None], text: str) -> Iterator[None]:
    """Wait cursor and ``text`` (shown with ``show``, e.g. a label's ``setText``) while
    something takes a moment: painted before it starts, cleared afterwards."""
    QGuiApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
    show(text)
    QCoreApplication.processEvents(QEventLoop.ProcessEventsFlag.ExcludeUserInputEvents)
    try:
        yield
    finally:
        show("")
        QGuiApplication.restoreOverrideCursor()


def show_error(parent: QWidget | None, title: str, error: Exception | str) -> None:
    QMessageBox.critical(parent, title, str(error))
