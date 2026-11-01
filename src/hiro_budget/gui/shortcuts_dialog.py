"""Overview of the keyboard shortcuts (Help > Keyboard shortcuts, F1)."""

from __future__ import annotations

from PySide6.QtGui import QKeySequence
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QDialogButtonBox,
    QHeaderView,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
)

from hiro_budget.gui.common import fit_to_screen


def keys_text(*keys: str) -> str:
    """Key combinations as the user's language writes them (e.g. Strg+Z)."""
    return " / ".join(
        QKeySequence(k).toString(QKeySequence.SequenceFormat.NativeText) for k in keys
    )


class ShortcutsDialog(QDialog):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle(self.tr("Keyboard shortcuts"))
        fit_to_screen(self, 620, 640)
        sections = [
            (
                self.tr("Everywhere"),
                [
                    (keys_text("Ctrl+Tab", "Ctrl+PgDown"), self.tr("Next page")),
                    (keys_text("Ctrl+Shift+Tab", "Ctrl+PgUp"), self.tr("Previous page")),
                    (keys_text("Ctrl+1") + " … " + keys_text("Ctrl+6"), self.tr("Go to a page")),
                    (
                        keys_text("Alt+Left", "Alt+Right"),
                        self.tr("Back / forward (also the mouse buttons)"),
                    ),
                    (keys_text("Ctrl+B"), self.tr("Collapse or expand the sidebar")),
                    (keys_text("Ctrl+I"), self.tr("Import statements")),
                    (keys_text("Ctrl+N"), self.tr("New booking")),
                    (keys_text("Ctrl+F"), self.tr("Search bookings")),
                    (keys_text("Ctrl+S"), self.tr("Save the changes of the current page")),
                    (keys_text("F5"), self.tr("Refresh")),
                    (keys_text("Shift+F10"), self.tr("Context menu of the selected row")),
                    (
                        self.tr("Alt + first letter"),
                        self.tr("Open a menu of the sidebar (File, View, …)"),
                    ),
                    (keys_text("F1"), self.tr("This overview")),
                ],
            ),
            (
                self.tr("Budget"),
                [
                    (
                        keys_text("Alt+Left", "Alt+Right"),
                        self.tr("Previous / next month (instead of back / forward)"),
                    ),
                    (keys_text("Alt+Home"), self.tr("Current month")),
                    (keys_text("Return", "F2"), self.tr("Assign an amount to the selected row")),
                    (keys_text("Ctrl+M"), self.tr("Move money from or to the selected row")),
                    (keys_text("Ctrl+Z", "Ctrl+Y"), self.tr("Undo / redo")),
                ],
            ),
            (
                self.tr("Bookings"),
                [
                    (keys_text("Return", "F2"), self.tr("Change the category")),
                    (keys_text("Return"), self.tr("Accept a suggestion (when reviewing)")),
                    (keys_text("Del"), self.tr("Delete manual bookings")),
                    (keys_text("Esc"), self.tr("From the search box back to the list")),
                ],
            ),
            (
                self.tr("Categories & rules"),
                [
                    (
                        self.tr("Double-click") + ", " + keys_text("F2"),
                        self.tr("Rename a category or group, or change its key"),
                    ),
                    (
                        self.tr("Drag & drop"),
                        self.tr(
                            "Reorder categories or move them to another group;\n"
                            "bookings, budget amounts and rules move with them"
                        ),
                    ),
                    (
                        self.tr("Key"),
                        self.tr(
                            "Identifies a category in rules and the configuration;\n"
                            "a changed key is updated there"
                        ),
                    ),
                    (
                        self.tr("Remove"),
                        self.tr("Hides a category; its bookings and history are kept"),
                    ),
                ],
            ),
            (
                self.tr("Lists and dialogs"),
                [
                    (keys_text("Tab", "Shift+Tab"), self.tr("Next / previous field")),
                    (keys_text("Up", "Down"), self.tr("Move in a list")),
                    (keys_text("Space"), self.tr("Tick a checkbox")),
                    (keys_text("Return", "Esc"), self.tr("OK / cancel")),
                ],
            ),
        ]
        tree = QTreeWidget()
        tree.setHeaderLabels([self.tr("Keys"), self.tr("Action")])
        tree.setRootIsDecorated(False)
        tree.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        for title, rows in sections:
            header = QTreeWidgetItem([title, ""])
            font = header.font(0)
            font.setBold(True)
            header.setFont(0, font)
            tree.addTopLevelItem(header)
            for keys, action in rows:
                header.addChild(QTreeWidgetItem([keys, action]))
        tree.expandAll()
        tree.header().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        layout = QVBoxLayout(self)
        layout.addWidget(tree, 1)
        layout.addWidget(buttons)
