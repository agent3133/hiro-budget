"""Regex cheat sheet (#30): a "?" in every regex field opens it next to the field (also F1
there), Help > Regex cheat sheet… opens it as a window that can stay open."""

from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtCore import QCoreApplication, QPoint, QRectF, Qt
from PySide6.QtGui import QFont, QFontDatabase, QGuiApplication, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QDialogButtonBox,
    QFrame,
    QGridLayout,
    QLabel,
    QLineEdit,
    QScrollArea,
    QToolTip,
    QVBoxLayout,
    QWidget,
)

from hiro_budget.gui.common import fit_to_screen
from hiro_budget.gui.navigation import ICON_HELP, glyph_icon
from hiro_budget.gui.widgets import caption, icon_button

ICON_COPY = ""  # Copy
REGEX_HELP = "regexHelp"  # property of a line edit that takes a regex (F1 opens the sheet)


@dataclass(frozen=True)
class Example:
    pattern: str
    meaning: str
    matches: str  # a sample text the pattern finds (checked by a test)
    misses: str  # a sample text it does not find


def syntax() -> list[tuple[str, str]]:
    return [
        (
            "(?i)",
            QCoreApplication.translate(
                "RegexHelp", "At the start: ignore upper and lower case (almost always wanted)"
            ),
        ),
        ("rewe|aldi", QCoreApplication.translate("RegexHelp", "One or the other")),
        ("^…", QCoreApplication.translate("RegexHelp", "The text starts here")),
        ("…$", QCoreApplication.translate("RegexHelp", "The text ends here")),
        (".", QCoreApplication.translate("RegexHelp", "Any one character")),
        (".*", QCoreApplication.translate("RegexHelp", "Any text, also none")),
        ("\\d   \\d+", QCoreApplication.translate("RegexHelp", "A digit / one or more digits")),
        ("\\s", QCoreApplication.translate("RegexHelp", "A space (or tab)")),
        (
            "[abc]   [0-9]",
            QCoreApplication.translate("RegexHelp", "One of these characters / a range"),
        ),
        ("\\b", QCoreApplication.translate("RegexHelp", "Word boundary: start or end of a word")),
        ("x?", QCoreApplication.translate("RegexHelp", "The character before is optional")),
        (
            "\\.  \\+  \\*  \\?  \\(  \\)  \\|",
            QCoreApplication.translate(
                "RegexHelp", "The character itself (these have a meaning otherwise)"
            ),
        ),
    ]


def examples() -> list[Example]:
    return [
        Example(
            "(?i)rewe|edeka|aldi",
            QCoreApplication.translate("RegexHelp", "Several shops in one rule"),
            "EDEKA Center",
            "Lidl",
        ),
        Example(
            "(?i)\\bdm\\b",
            QCoreApplication.translate("RegexHelp", "A short name only as a whole word"),
            "DM Drogerie Markt",
            "Admiral Kino",
        ),
        Example(
            "(?i)extra-?konto",
            QCoreApplication.translate("RegexHelp", "With or without a hyphen"),
            "Einzahlung Extrakonto",
            "Extra Konto",
        ),
        Example(
            "^Lastschrift",
            QCoreApplication.translate("RegexHelp", "The text starts with a word (case counts)"),
            "Lastschrift Stadtwerke",
            "Rücklastschrift",
        ),
        Example(
            "^Gehalt/Rente$",
            QCoreApplication.translate("RegexHelp", "Exactly this text, nothing before or after"),
            "Gehalt/Rente",
            "Gehalt/Rente Nachzahlung",
        ),
        Example(
            "(?i)^rewe\\s*\\d+",
            QCoreApplication.translate("RegexHelp", "A store with its branch number"),
            "REWE 4711",
            "REWE Online",
        ),
        Example(
            "(?i)kundennr\\.?\\s*12345",
            QCoreApplication.translate("RegexHelp", "A customer number in the purpose"),
            "Kundennr. 12345 Strom",
            "Kundennr. 54321 Strom",
        ),
        Example(
            "(?i)netflix|disney\\+",
            QCoreApplication.translate("RegexHelp", "A special character as itself (here the +)"),
            "Disney+ Abo",
            "Disney Store",
        ),
    ]


def _mono() -> QFont:
    font = QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont)
    font.setFamilies(["Cascadia Mono", "Consolas", *font.families()])  # clearer than Courier
    font.setPointSizeF(QApplication.font().pointSizeF())
    return font


def _pattern_label(text: str) -> QLabel:
    label = QLabel(text)
    label.setFont(_mono())
    label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
    return label


class RegexHelp(QScrollArea):
    """The sheet itself: a note, the syntax and examples with copy buttons."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWidgetResizable(True)
        self.setFrameShape(QFrame.Shape.NoFrame)
        body = QWidget()
        layout = QVBoxLayout(body)
        layout.setContentsMargins(16, 12, 16, 12)
        note = QLabel(
            self.tr(
                "Rules use regular expressions. A pattern is found anywhere in the text "
                "unless it uses ^ or $. Upper and lower case count unless the pattern "
                "starts with (?i). The rule editor shows right away how many bookings match."
            )
        )
        note.setWordWrap(True)
        layout.addWidget(note)

        layout.addSpacing(8)
        layout.addWidget(caption(self.tr("Syntax")))
        grid = QGridLayout()
        grid.setHorizontalSpacing(16)
        grid.setColumnStretch(1, 1)
        for row, (pattern, meaning) in enumerate(syntax()):
            grid.addWidget(_pattern_label(pattern), row, 0)
            text = QLabel(meaning)
            text.setWordWrap(True)
            grid.addWidget(text, row, 1)
        layout.addLayout(grid)

        layout.addSpacing(8)
        layout.addWidget(caption(self.tr("Examples")))
        grid = QGridLayout()
        grid.setHorizontalSpacing(16)
        grid.setVerticalSpacing(8)
        grid.setColumnStretch(1, 1)
        self.copy_buttons: dict[str, QWidget] = {}
        for row, ex in enumerate(examples()):
            grid.addWidget(_pattern_label(ex.pattern), row, 0, Qt.AlignmentFlag.AlignTop)
            text = QLabel(
                f"{ex.meaning}<br><span style='color: {self._muted()}'>"
                + self.tr("finds “%1”, not “%2”").replace("%1", ex.matches).replace("%2", ex.misses)
                + "</span>"
            )
            text.setWordWrap(True)
            grid.addWidget(text, row, 1)
            button = icon_button(
                ICON_COPY, "⧉", self.tr("Copy"), lambda _=False, p=ex.pattern: self.copy(p)
            )
            button.setAutoDefault(False)  # in a dialog: not the default button
            self.copy_buttons[ex.pattern] = button
            grid.addWidget(button, row, 2, Qt.AlignmentFlag.AlignTop)
        layout.addLayout(grid)
        layout.addStretch(1)
        self.setWidget(body)

    def _muted(self) -> str:
        return self.palette().placeholderText().color().name()

    def copy(self, pattern: str) -> None:
        QGuiApplication.clipboard().setText(pattern)
        button = self.copy_buttons.get(pattern)
        if button is not None and button.isVisible():
            QToolTip.showText(
                button.mapToGlobal(QPoint(0, button.height())), self.tr("Copied"), button
            )


class RegexHelpPopup(QFrame):
    """The sheet next to a field; closes with Esc or a click outside."""

    def __init__(self, anchor: QWidget) -> None:
        super().__init__(anchor, Qt.WindowType.Popup)
        self.setFrameShape(QFrame.Shape.StyledPanel)
        self.setObjectName("regexHelpPopup")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.sheet = RegexHelp(self)
        layout.addWidget(self.sheet)
        fit_to_screen(self, 620, 560, share=0.8)
        self._place(anchor)

    def keyPressEvent(self, event) -> None:
        if event.key() == Qt.Key.Key_Escape:
            self.close()
        else:
            super().keyPressEvent(event)

    def _place(self, anchor: QWidget) -> None:
        """Below the field, right edges aligned; inside the screen."""
        pos = anchor.mapToGlobal(QPoint(anchor.width() - self.width(), anchor.height() + 2))
        screen = anchor.screen() or QGuiApplication.primaryScreen()
        if screen is not None:
            area = screen.availableGeometry()
            if pos.y() + self.height() > area.bottom():  # no room below: above the field
                pos.setY(anchor.mapToGlobal(QPoint(0, 0)).y() - self.height() - 2)
            pos.setX(max(area.left(), min(pos.x(), area.right() - self.width())))
            pos.setY(max(area.top(), min(pos.y(), area.bottom() - self.height())))
        self.move(pos)


class RegexHelpWindow(QDialog):
    """Help > Regex cheat sheet…: a window beside the app, not blocking it."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle(self.tr("Regex cheat sheet"))
        self.setModal(False)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 12)
        self.sheet = RegexHelp(self)
        layout.addWidget(self.sheet)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.close)
        buttons.setContentsMargins(0, 0, 12, 0)
        layout.addWidget(buttons)
        fit_to_screen(self, 660, 720)


def show_popup(edit: QLineEdit) -> RegexHelpPopup:
    popup = RegexHelpPopup(edit)
    popup.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
    popup.show()
    return popup


def _text_icon(text: str, color, size: int = 16) -> QIcon:
    """Without the icon font: the text drawn as an icon."""
    pixmap = QPixmap(size * 2, size * 2)
    pixmap.setDevicePixelRatio(2)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setPen(color)
    font = QFont()
    font.setPixelSize(round(size * 0.85))
    font.setBold(True)
    painter.setFont(font)
    painter.drawText(QRectF(0, 0, size, size), Qt.AlignmentFlag.AlignCenter, text)
    painter.end()
    return QIcon(pixmap)


def attach(edit: QLineEdit) -> None:
    """A "?" inside the field that opens the cheat sheet next to it."""
    color = edit.palette().placeholderText().color()
    icon = glyph_icon(ICON_HELP, color, 16)
    action = edit.addAction(
        icon if not icon.isNull() else _text_icon("?", color),
        QLineEdit.ActionPosition.TrailingPosition,
    )
    if not icon.isNull():
        action.setProperty("glyph", ICON_HELP)  # redrawn after a theme switch
    action.setToolTip(QCoreApplication.translate("RegexHelp", "Regex cheat sheet (F1)"))
    action.setObjectName("regexHelpAction")
    action.triggered.connect(lambda: show_popup(edit))
    edit.setProperty(REGEX_HELP, True)
