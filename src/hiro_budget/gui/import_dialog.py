"""Import dialog: pick statement files, preview them, then import."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMessageBox,
    QPushButton,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
)

from hiro_budget.app import App
from hiro_budget.gui.common import (
    amount_color,
    fit_to_screen,
    flat_table,
    fmt_date,
    fmt_number,
    highlight_color,
    money,
    warning_color,
)
from hiro_budget.gui.training import trainer
from hiro_budget.gui.widgets import banner
from hiro_budget.importers.base import ImportError_
from hiro_budget.services import import_batch, import_service
from hiro_budget.services.accounts import StatementGap, statement_gaps
from hiro_budget.services.import_service import ImportPreview, OverlapMode, statement_link


@dataclass
class Entry:
    path: Path
    preview: ImportPreview | None = None
    labels: list[str | None] | None = None
    error: str | None = None


class ImportDialog(QDialog):
    def __init__(self, app: App, parent=None, paths: list[Path] | None = None) -> None:
        super().__init__(parent)
        self.app = app
        self.entries: list[Entry] = []
        self.imported_any = False
        self.setWindowTitle(self.tr("Import statements"))
        fit_to_screen(self, 1000, 650)

        add_files = QPushButton(self.tr("Add files…"))
        add_folder = QPushButton(self.tr("Add folder…"))
        remove = QPushButton(self.tr("Remove"))
        add_files.clicked.connect(self._add_files)
        add_folder.clicked.connect(self._add_folder)
        remove.clicked.connect(self._remove)

        self.files = QTreeWidget()
        self.files.setRootIsDecorated(False)
        self.files.setHeaderLabels(
            [
                self.tr("File"),
                self.tr("Account"),
                self.tr("Period"),
                self.tr("Bookings"),
                self.tr("New"),
                self.tr("Duplicates"),
                self.tr("Balance"),
                self.tr("Status"),
            ]
        )
        self.files.header().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.files.header().setSectionResizeMode(7, QHeaderView.ResizeMode.Stretch)
        self.files.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.files.currentItemChanged.connect(self._show_preview)

        self.preview = QTableWidget(0, 5)
        flat_table(self.preview)
        self.booking_headers = [
            self.tr("Date"),
            self.tr("Payee"),
            self.tr("Amount"),
            self.tr("Category (preview)"),
            self.tr("Status"),
        ]
        self.holding_headers = [
            self.tr("Security"),
            self.tr("ISIN"),
            self.tr("Quantity"),
            self.tr("Price"),
            self.tr("Value"),
        ]
        self.preview.setHorizontalHeaderLabels(self.booking_headers)
        self.preview.verticalHeader().setVisible(False)
        self.preview.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.warnings = QLabel()
        self.warnings.setWordWrap(True)
        self.plugin_problems = QLabel()  # plugins left out: their files are not recognised
        self.plugin_problems.hide()
        self.progress = QLabel()  # what takes a moment after the files (the classifier)
        self.progress.hide()

        self.buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        self.import_button = self.buttons.addButton(
            self.tr("Import"), QDialogButtonBox.ButtonRole.AcceptRole
        )
        self.import_button.clicked.connect(self._import)
        self.buttons.rejected.connect(self.reject)

        top = QHBoxLayout()
        top.addWidget(add_files)
        top.addWidget(add_folder)
        top.addWidget(remove)
        top.addStretch(1)

        splitter = QSplitter(Qt.Orientation.Vertical)
        splitter.addWidget(self.files)
        splitter.addWidget(self.preview)
        splitter.setStretchFactor(1, 2)

        layout = QVBoxLayout(self)
        layout.addLayout(top)
        layout.addWidget(splitter, 1)
        layout.addWidget(self.warnings)
        layout.addWidget(self.plugin_problems)
        layout.addWidget(self.progress)
        layout.addWidget(self.buttons)

        self._update_buttons()
        if paths:
            self.add_paths(paths)
        else:
            self._show_plugin_problems()

    # -- file selection --------------------------------------------------------

    def _add_files(self) -> None:
        files, _ = QFileDialog.getOpenFileNames(
            self, self.tr("Select statements"), "", self.tr("Statements (*.pdf *.csv)")
        )
        self.add_paths([Path(f) for f in files])

    def _add_folder(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, self.tr("Select folder"))
        if folder:
            self.add_paths(
                sorted(
                    p
                    for p in Path(folder).iterdir()
                    if p.is_file() and import_service.is_statement_file(p)
                )
            )

    def _remove(self) -> None:
        for item in self.files.selectedItems():
            idx = self.files.indexOfTopLevelItem(item)
            self.files.takeTopLevelItem(idx)
            del self.entries[idx]
        self._refresh_items()
        self._update_buttons()

    def add_paths(self, paths: list[Path]) -> None:
        known = {e.path.resolve() for e in self.entries}
        QGuiApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            for path in paths:
                if path.resolve() in known:
                    continue
                entry = self._analyse(path)
                self.entries.append(entry)
                self.files.addTopLevelItem(QTreeWidgetItem())
        finally:
            QGuiApplication.restoreOverrideCursor()
        self._refresh_items()
        if self.files.currentItem() is None and self.files.topLevelItemCount():
            self.files.setCurrentItem(self.files.topLevelItem(0))
        self._update_buttons()
        self._show_plugin_problems()  # some show only when a plugin's code is loaded

    def _show_plugin_problems(self) -> None:
        from hiro_budget.importers import plugins

        s = self.app.settings
        reg = plugins.registry(s.plugins_dir, s.app.plugins.enabled)
        lines = [
            self.tr("Plugin %1 is left out: %2").replace("%1", name).replace("%2", problem)
            for name, problem in reg.problems()
        ]
        off = [p.name for p in reg.plugins if p.status == "disabled"]
        if off:
            lines.append(
                self.tr("Your plugins with program code are off: %1").replace("%1", ", ".join(off))
            )
        if lines:
            lines.append(self.tr("Settings → Plugins… shows them."))
            banner(self.plugin_problems, "warning").setText("\n".join(lines))
        self.plugin_problems.setVisible(bool(lines))

    def _preview(self, s, path: Path) -> ImportPreview:
        return import_batch.preview(s, self.app.settings, path)

    def _analyse(self, path: Path) -> Entry:
        entry = Entry(path)
        try:
            with self.app.session() as s:
                entry.preview = self._preview(s, path)
            with self.app.session_factory() as s:
                entry.labels = import_service.categorization_preview(
                    s, self.app.settings, entry.preview
                )
        except (ImportError_, OSError) as e:
            entry.error = str(e)
        return entry

    def _refresh_items(self) -> None:
        # Statuses depend on the other files (a file may fill another one's gap).
        for i, e in enumerate(self.entries):
            self._fill_item(self.files.topLevelItem(i), e)

    def _open_gaps(self, e: Entry) -> list[StatementGap]:
        """Gaps next to this file that the other files in the list do not fill.

        Of a gap that other files fill only partly, only the part next to this file is
        left; the rest is reported on the other files.
        """
        others = [
            link
            for o in self.entries
            if o is not e and o.preview is not None and not o.preview.errors
            if o.preview.account_name == e.preview.account_name
            if (link := statement_link(o.preview.statement, o.path.name)) is not None
        ]
        result = []
        for g in e.preview.gaps:
            between = [
                o
                for o in others
                if g.after.period_end < o.period_start and o.period_end < g.before.period_start
            ]
            this_is_after = g.after.source_file == e.path.name
            result += [
                x
                for x in statement_gaps([g.after, g.before, *between])
                if (x.after is g.after if this_is_after else x.before is g.before)
            ]
        return result

    def _gap_text(self, g: StatementGap) -> str:
        return (
            self.tr("Statements missing for %1 – %2: bookings of %3 are not imported.")
            .replace("%1", fmt_date(g.after.period_end + timedelta(days=1)))
            .replace("%2", fmt_date(g.before.period_start - timedelta(days=1)))
            .replace("%3", money(g.difference_cents))
        )

    def _fill_item(self, item: QTreeWidgetItem, e: Entry) -> None:
        item.setText(0, e.path.name)
        item.setToolTip(0, str(e.path))
        if e.error or e.preview is None:
            item.setText(7, e.error or "")
            item.setForeground(7, amount_color(-1))
            return
        p = e.preview
        st = p.statement
        if st.valuation:
            period = fmt_date(st.period_end)
        elif st.period_start:
            period = f"{fmt_date(st.period_start)} – {fmt_date(st.period_end)}"
        else:
            period = ""
        if not st.balance_checkable:
            balance = self.tr("not checked")
        elif st.balance_ok:
            balance = "✓ " + money(st.closing_balance_cents)
        else:
            balance = "✗ " + money(st.balance_difference_cents)
        account = p.account_name + ("" if p.account_exists else self.tr(" (new)"))
        if p.file_already_imported:
            status = self.tr("already imported")
        elif p.errors:
            status = "; ".join(p.errors)
        elif st.valuation and st.holdings:
            status = self.tr(
                "ready: securities account statement with %n position(s)", "", len(st.holdings)
            )
            if p.replaces_valuation:
                status += " – " + self.tr("replaces %1").replace("%1", p.replaces_valuation)
        elif st.valuation:
            status = self.tr(
                "ready: balance statement – sets the balance, bookings are not imported"
            )
            if p.replaces_valuation:
                status += " – " + self.tr("replaces %1").replace("%1", p.replaces_valuation)
        elif p.overlap is not None:
            status = self.tr("overlaps %1 stored bookings – you will be asked").replace(
                "%1", str(p.overlap.existing_count)
            )
        elif p.new_count == 0 and not p.links_new:
            status = self.tr("nothing new")
        else:
            status = self.tr("ready")
        if p.links_new and not p.file_already_imported:
            status += " – " + self.tr("%n payment(s) via bank", "", len(p.links_new))
        if p.skipped_fetched and not p.file_already_imported:
            first, last = p.fetched_days
            status += " – " + self.tr("%1–%2 per FinTS").replace("%1", fmt_date(first)).replace(
                "%2", fmt_date(last)
            )
        gaps = [] if p.errors or p.file_already_imported else self._open_gaps(e)
        if gaps:
            status += " – " + self.tr("statements missing, see below")
        counts = (
            ["", "", ""]
            if st.valuation
            else [str(len(st.transactions)), str(p.new_count), str(p.duplicate_count)]
        )
        for col, text in enumerate([e.path.name, account, period, *counts, balance, status]):
            item.setText(col, text)
        item.setToolTip(7, "\n".join([status, *(self._gap_text(g) for g in gaps)]))
        if not p.can_commit:
            item.setForeground(7, amount_color(-1))
        elif gaps:
            item.setForeground(7, warning_color())

    # -- preview ---------------------------------------------------------------

    def _show_preview(self) -> None:
        idx = self.files.indexOfTopLevelItem(self.files.currentItem())
        self.preview.setRowCount(0)
        self.warnings.clear()
        if idx < 0:
            return
        e = self.entries[idx]
        if e.preview is None:
            self.warnings.setText(e.error or "")
            return
        st = e.preview.statement
        header = self.preview.horizontalHeader()
        if st.holdings:
            self._show_holdings(e)
            return
        self.preview.setHorizontalHeaderLabels(self.booking_headers)
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Interactive)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.preview.setRowCount(len(st.transactions))
        labels = e.labels or [None] * len(st.transactions)
        for i, (t, dup, label) in enumerate(
            zip(st.transactions, e.preview.duplicate_flags, labels, strict=True)
        ):
            amount = QTableWidgetItem(money(t.amount_cents))
            amount.setForeground(amount_color(t.amount_cents))
            amount.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            category = QTableWidgetItem(label or "")
            if not dup and not label:
                category.setBackground(highlight_color())
            cells = [
                QTableWidgetItem(fmt_date(t.booking_date)),
                QTableWidgetItem(t.merchant or t.counterparty or t.booking_type),
                amount,
                category,
                QTableWidgetItem(
                    self.tr("balance check only")
                    if st.valuation
                    else self.tr("duplicate")
                    if dup
                    else self.tr("new, replaces manual booking")
                    if i in e.preview.matches
                    else self.tr("new")
                ),
            ]
            cells[1].setToolTip(t.raw_text)
            for col, cell in enumerate(cells):
                self.preview.setItem(i, col, cell)
        self.preview.resizeColumnToContents(0)
        self.preview.resizeColumnToContents(3)
        gaps = [self._gap_text(g) for g in self._open_gaps(e)]
        self.warnings.setText("\n".join(st.warnings + e.preview.errors + gaps))

    def _show_holdings(self, e: Entry) -> None:
        st = e.preview.statement
        header = self.preview.horizontalHeader()
        self.preview.setHorizontalHeaderLabels(self.holding_headers)
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Interactive)
        self.preview.setRowCount(len(st.holdings))
        right = Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        for i, h in enumerate(st.holdings):
            price = f"{fmt_number(h.price, 4, 2)} {h.price_currency or ''}" if h.price else ""
            cells = [
                QTableWidgetItem(h.name),
                QTableWidgetItem(h.isin or ""),
                QTableWidgetItem(f"{fmt_number(h.quantity)} {h.unit}"),
                QTableWidgetItem(price.strip()),
                QTableWidgetItem(money(h.value_cents)),
            ]
            for col in (2, 3, 4):
                cells[col].setTextAlignment(right)
            for col, cell in enumerate(cells):
                self.preview.setItem(i, col, cell)
        for col in (1, 2, 3, 4):
            self.preview.resizeColumnToContents(col)
        self.warnings.setText("\n".join(st.warnings + e.preview.errors))

    # -- import ----------------------------------------------------------------

    def _importable(self) -> list[Entry]:
        return [e for e in self.entries if e.preview and e.preview.can_commit]

    def _update_buttons(self) -> None:
        n = len(self._importable())
        self.import_button.setEnabled(n > 0)
        self.import_button.setText(self.tr("Import %n file(s)", "", n))

    def ask_overlap(self, p: ImportPreview) -> tuple[OverlapMode | None, bool]:
        """Ask how to handle an overlapping statement.

        Returns ``(mode, apply_to_all)``; mode None means cancel (skip this file).
        """
        o = p.overlap
        assert o is not None
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Question)
        box.setWindowTitle(self.tr("Overlapping statement"))
        box.setText(
            self.tr("'%1' covers %2 – %3. Account '%4' already has %5 bookings in this period.")
            .replace("%1", p.path.name)
            .replace("%2", fmt_date(o.date_from))
            .replace("%3", fmt_date(o.date_to))
            .replace("%4", p.account_name)
            .replace("%5", str(o.existing_count))
        )
        lines = [
            self.tr("Add missing: imports only the %1 bookings that are not stored yet.").replace(
                "%1", str(p.new_count)
            ),
            self.tr(
                "Overwrite: deletes the %1 stored bookings in this period and imports all %2 "
                "bookings of the file."
            )
            .replace("%1", str(o.existing_count))
            .replace("%2", str(len(p.statement.transactions))),
        ]
        if o.removed_on_overwrite:
            lines.append(
                self.tr(
                    "%1 stored bookings are not contained in the new file and would be removed."
                ).replace("%1", str(o.removed_on_overwrite))
            )
        if o.manual_kept:
            lines.append(
                self.tr(
                    "Manual categorizations and notes of %1 identical bookings are kept."
                ).replace("%1", str(o.manual_kept))
            )
        if o.manual_lost:
            lines.append(
                self.tr("Manual work on %1 removed bookings would be lost.").replace(
                    "%1", str(o.manual_lost)
                )
            )
        if o.files:
            lines.append(self.tr("Earlier imports: %1").replace("%1", ", ".join(o.files)))
        box.setInformativeText("\n\n".join(lines))
        add = box.addButton(self.tr("Add missing"), QMessageBox.ButtonRole.AcceptRole)
        overwrite = box.addButton(self.tr("Overwrite"), QMessageBox.ButtonRole.DestructiveRole)
        cancel = box.addButton(self.tr("Cancel"), QMessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(add)
        box.setEscapeButton(cancel)
        apply_all = QCheckBox(self.tr("Apply to all further overlapping files"))
        box.setCheckBox(apply_all)
        box.exec()
        clicked = box.clickedButton()
        mode = (
            OverlapMode.ADD
            if clicked is add
            else OverlapMode.OVERWRITE
            if clicked is overwrite
            else None
        )
        return mode, apply_all.isChecked()

    def _go_on_without_backup(self, error: Exception) -> bool:
        """The backup before the import failed: the user decides whether to go on."""
        QGuiApplication.restoreOverrideCursor()
        answer = QMessageBox.question(
            self,
            self.tr("Backup"),
            self.tr("The backup before the import failed:\n%1\n\nImport anyway?").replace(
                "%1", str(error)
            ),
        )
        QGuiApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        return answer == QMessageBox.StandardButton.Yes

    def _show_progress(self, text: str) -> None:
        self.progress.setText(text)
        self.progress.setVisible(bool(text))

    def _ask_overlap_with_normal_cursor(self, p: ImportPreview):
        QGuiApplication.restoreOverrideCursor()
        try:
            return self.ask_overlap(p)
        finally:
            QGuiApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)

    def _import(self) -> None:
        QGuiApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            batch = import_batch.import_files(
                self.app.session,
                self.app.settings,
                [e.path for e in self._importable()],
                self._ask_overlap_with_normal_cursor,
                backup_failed=self._go_on_without_backup,
            )
            if batch.stopped:
                return
            self.imported_any = self.imported_any or batch.imported_any
            if batch.imported_any and self.app.settings.app.ml.enabled:
                trainer(self.app).start()  # in the background (#55)
        finally:
            QGuiApplication.restoreOverrideCursor()
        added, duplicates, matched = batch.added, batch.duplicates, batch.matched
        replaced, valuations, linked = batch.replaced, batch.valuations, batch.linked
        linking, cancelled, errors = batch.linking, batch.count("cancelled"), batch.errors

        msg = self.tr("%1 bookings imported, %2 duplicates skipped.")
        msg = msg.replace("%1", str(added)).replace("%2", str(duplicates))
        if matched:
            msg += "\n" + self.tr("%1 manual bookings were replaced by imported ones.").replace(
                "%1", str(matched)
            )
        if replaced:
            msg += "\n" + self.tr("%1 stored bookings were replaced.").replace("%1", str(replaced))
        if valuations:
            msg += "\n" + self.tr(
                "%n securities account or balance statement(s) imported.", "", valuations
            )
        if linked:
            msg += "\n" + self.tr("%n payment(s) linked to their bank booking.", "", linked)
        if linking is not None and linking.waiting:
            msg += "\n" + self.tr(
                "%n payment(s) wait for their bank booking (linked after the next import or "
                "fetch).",
                "",
                linking.waiting,
            )
            if linking.overdue:
                msg += " " + self.tr("%n of them for more than 30 days.", "", linking.overdue)
        if cancelled:
            msg += "\n" + self.tr("%1 file(s) cancelled.").replace("%1", str(cancelled))
        if errors:
            QMessageBox.warning(self, self.tr("Import"), msg + "\n\n" + "\n".join(errors))
        else:
            QMessageBox.information(self, self.tr("Import"), msg)
        self.accept()
