"""Overview of the keyboard shortcuts (Help > Keyboard shortcuts, F1)."""

from __future__ import annotations

from PySide6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QDialogButtonBox,
    QHeaderView,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
)

from hiro_budget.gui import shortcuts
from hiro_budget.gui.common import fit_to_screen


class ShortcutsDialog(QDialog):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle(self.tr("Keyboard shortcuts"))
        fit_to_screen(self, 620, 640)
        sections = shortcuts.rows()  # one table for keys, tooltips and this overview
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
