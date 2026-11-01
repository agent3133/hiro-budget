"""Building blocks of the page layout, after the Obsidian design: a large page title,
small uppercase captions, detail panels as cards and a filled primary button.

Sizes are set here; colours come from the application stylesheet (``theme.py``) through
the object names, so they follow a theme switch without code in the views.
"""

from __future__ import annotations

from PySide6.QtCore import QLocale, QRectF, QSize, Qt, Signal
from PySide6.QtGui import QColor, QFont, QFontMetrics, QPainter, QPalette, QPen
from PySide6.QtWidgets import (
    QAbstractButton,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QStyle,
    QStyleOptionViewItem,
    QVBoxLayout,
    QWidget,
)

from hiro_budget.gui import theme


def page_title(text: str) -> QLabel:
    """The large title at the top of a page."""
    label = QLabel(text)
    label.setObjectName("pageTitle")
    font = label.font()
    font.setPointSize(font.pointSize() + 12)
    font.setBold(True)
    label.setFont(font)
    return label


def caption(text: str) -> QLabel:
    """Small uppercase label above a heading or a section ("KONTOEINSTELLUNGEN")."""
    label = QLabel(text.upper())
    label.setObjectName("caption")
    font = label.font()
    font.setPointSizeF(font.pointSizeF() - 0.5)
    font.setWeight(QFont.Weight.DemiBold)
    font.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 0.8)
    label.setFont(font)
    return label


def icon_button(glyph: str, fallback: str, tip: str, slot) -> QPushButton:
    """A small action as an icon (move up, remove, undo); its name as tooltip."""
    from hiro_budget.gui.navigation import set_glyph  # navigation uses this module

    button = QPushButton()
    set_glyph(button, glyph, fallback)
    button.setToolTip(tip)
    button.setAccessibleName(tip)
    button.setFixedWidth(40)
    button.clicked.connect(slot)
    return button


def action_bar(
    actions: list[QWidget], icons: list[QWidget] = (), end: QWidget | None = None
) -> QHBoxLayout:
    """The buttons of a list, below it (the same on every page): its actions on the
    left, small icon actions on the right, then ``end`` (e.g. a summary)."""
    from hiro_budget.gui.flow_layout import wrapping_row

    bar = QHBoxLayout()
    bar.setContentsMargins(0, 6, 0, 0)
    bar.addWidget(wrapping_row(actions), 1)
    for w in icons:
        bar.addWidget(w, 0, Qt.AlignmentFlag.AlignTop)
    if end is not None:
        bar.addSpacing(12)
        bar.addWidget(end, 0, Qt.AlignmentFlag.AlignVCenter)
    return bar


def banner(label: QLabel, kind: str = "info") -> QLabel:
    """Show a label as a coloured banner: "info", "success", "warning" or "error"."""
    label.setWordWrap(True)
    label.setProperty("banner", kind)
    label.style().unpolish(label)  # the stylesheet looks at the property when polishing
    label.style().polish(label)
    label.updateGeometry()  # the padding changes the size
    return label


def primary(button: QPushButton) -> QPushButton:
    """The main action of a page or panel: filled in the accent colour."""
    button.setDefault(True)  # drawn as the default button; Enter only acts in dialogs
    return button


def paint_chip(
    painter: QPainter,
    option: QStyleOptionViewItem,
    text: str,
    fill: str,
    ink: str,
    dashed_border: str | None = None,
) -> None:
    """A rounded label in a table cell, left-aligned (account type, "+ Category"); the
    selection is drawn behind it."""
    painter.save()
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    if option.state & QStyle.StateFlag.State_Selected:
        painter.fillRect(option.rect, option.palette.highlight())
    painter.setFont(option.font)
    width = option.fontMetrics.horizontalAdvance(text) + 18
    r = QRectF(option.rect).adjusted(6, 3, -6, -3)
    r.setWidth(min(width, r.width()))
    height = min(r.height(), option.fontMetrics.height() + 8)  # also in taller rows
    r = QRectF(r.left(), r.center().y() - height / 2, r.width(), height)
    if dashed_border:
        painter.setPen(QPen(QColor(dashed_border), 1, Qt.PenStyle.DashLine))
    else:
        painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QColor(fill))
    painter.drawRoundedRect(r, r.height() / 2, r.height() / 2)
    painter.setPen(QColor(ink))
    painter.drawText(r, Qt.AlignmentFlag.AlignCenter, text)
    painter.restore()


class StatCard(QFrame):
    """A key figure: small title, large value, a note below; coloured by its state
    ("pos", "neg", "warn" or "" for neutral). A click leads to the details."""

    clicked = Signal()

    def __init__(self, title: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("statCard")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.state = ""
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 12, 16, 12)
        layout.setSpacing(2)
        self.title = QLabel(title)
        self.value = QLabel()
        self.value.setObjectName("value")
        font = self.value.font()
        font.setPointSize(font.pointSize() + 8)
        font.setBold(True)
        self.value.setFont(font)
        self.note = QLabel()
        self.note.setWordWrap(True)
        for w in (self.title, self.value, self.note):
            layout.addWidget(w)
        layout.addStretch(1)

    def set_value(self, value: str, note: str = "", state: str = "") -> None:
        self.value.setText(value)
        self.note.setText(note)
        self.note.setVisible(bool(note))
        self.state = state
        self.restyle()

    def restyle(self) -> None:
        t = theme.tokens()
        if t is not None:
            background = t[self.state] if self.state else t["panel"]
            ink = t[self.state + "_text"] if self.state else t["text"]
            muted, radius = (ink if self.state else t["muted"]), 14
        else:
            background = {
                "pos": "rgba(46, 158, 91, 0.16)",
                "neg": "rgba(217, 83, 79, 0.18)",
                "warn": "rgba(232, 163, 61, 0.22)",
            }.get(self.state, "rgba(128, 128, 128, 0.10)")
            ink = muted = "palette(text)"
            radius = 12
        self.setStyleSheet(
            f"QFrame#statCard {{ background: {background}; border-radius: {radius}px; }}"
            f"QFrame#statCard QLabel {{ background: transparent; color: {muted}; }}"
            f"QFrame#statCard QLabel#value {{ color: {ink}; }}"
        )

    def mouseReleaseEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton and self.rect().contains(
            event.position().toPoint()
        ):
            self.clicked.emit()
        super().mouseReleaseEvent(event)


class QuickFilterButton(QAbstractButton):
    """A tab-like filter with its count as a chip ("Uncategorized  495"); the active one
    is bold and underlined in the accent colour. ``warn``: the chip turns amber while
    the count is not 0 (something to do)."""

    def __init__(self, text: str, warn: bool = False, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setText(text)
        self.setCheckable(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setAttribute(Qt.WidgetAttribute.WA_Hover)
        self.warn = warn
        self.count = 0

    def set_count(self, count: int) -> None:
        self.count = count
        self.updateGeometry()
        self.update()

    def _fonts(self) -> tuple[QFont, QFont]:
        bold = QFont(self.font())
        bold.setWeight(QFont.Weight.DemiBold)
        return bold, QFont(self.font())

    def sizeHint(self) -> QSize:
        bold, small = self._fonts()
        text = QFontMetrics(bold).horizontalAdvance(self.text())
        chip = QFontMetrics(small).horizontalAdvance(QLocale().toString(self.count)) + 14
        return QSize(10 + text + 8 + chip + 10, QFontMetrics(bold).height() + 18)

    def paintEvent(self, event) -> None:
        t = theme.tokens()
        pal = self.palette()
        bold, small = self._fonts()
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = QRectF(self.rect())
        if self.underMouse() and not self.isChecked():
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(t["line"]) if t else QColor(128, 128, 128, 30))
            painter.drawRoundedRect(r.adjusted(0, 2, 0, -4), 6, 6)
        if self.isChecked():
            ink = QColor(t["heading"]) if t else pal.text().color()
        else:
            ink = QColor(t["muted"]) if t else pal.placeholderText().color()
        painter.setFont(bold if self.isChecked() else self.font())
        painter.setPen(ink)
        fm = QFontMetrics(bold)
        text_rect = QRectF(10, 0, fm.horizontalAdvance(self.text()), r.height() - 3)
        painter.drawText(text_rect, Qt.AlignmentFlag.AlignVCenter, self.text())
        # the count as a chip
        number = QLocale().toString(self.count)
        painter.setFont(small)
        chip_w = QFontMetrics(small).horizontalAdvance(number) + 14
        chip_h = QFontMetrics(small).height() + 2
        chip = QRectF(text_rect.right() + 8, (r.height() - 3 - chip_h) / 2, chip_w, chip_h)
        attention = self.warn and self.count > 0
        if t is not None:
            fill = QColor(t["warn"] if attention else t["line"])
            chip_ink = QColor(t["warn_text"] if attention else t["muted"])
        else:
            fill = QColor(232, 163, 61, 70) if attention else QColor(128, 128, 128, 40)
            chip_ink = pal.text().color()
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(fill)
        painter.drawRoundedRect(chip, chip_h / 2, chip_h / 2)
        painter.setPen(chip_ink)
        painter.drawText(chip, Qt.AlignmentFlag.AlignCenter, number)
        if self.isChecked():  # the accent bar under the active filter
            accent = QColor(t["accent"]) if t else pal.color(QPalette.ColorRole.Accent)
            painter.fillRect(QRectF(4, r.height() - 3, r.width() - 8, 2.5), accent)
        painter.end()


class DetailCard(QFrame):
    """Panel next to a list (account, booking, rule): a card with a caption and the
    name of the selected item as heading; the content goes into ``body``."""

    def __init__(self, caption_text: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("detailCard")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 16, 18, 16)
        layout.setSpacing(2)
        layout.addWidget(caption(caption_text))
        self.heading = QLabel()
        self.heading.setObjectName("panelHeading")
        self.heading.setWordWrap(True)
        font = self.heading.font()
        font.setPointSize(font.pointSize() + 5)
        font.setBold(True)
        self.heading.setFont(font)
        layout.addWidget(self.heading)
        layout.addSpacing(10)
        self.body = QVBoxLayout()
        self.body.setContentsMargins(0, 0, 0, 0)
        layout.addLayout(self.body, 1)

    def set_heading(self, text: str) -> None:
        self.heading.setText(text)
