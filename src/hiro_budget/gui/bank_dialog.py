"""Settings → Bank access: bank logins (FinTS), their accounts and where they go.

Per login: bank, login ID and the bank's accounts, each with the app account its bookings
go to (an existing one or a new one) and the first day fetched. "Test connection" asks for
the PIN, logs in and lists the accounts the bank offers. Saved to config.yaml; the PIN is
never stored.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date

from PySide6.QtCore import QCoreApplication, QDate, Qt
from PySide6.QtWidgets import (
    QComboBox,
    QDateEdit,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)
from sqlalchemy import select

from hiro_budget.app import App
from hiro_budget.config import BankAccountLink, BankLogin, FintsConfig
from hiro_budget.db.models import Account
from hiro_budget.gui.bank_fetch import BankWorker, ask_pin, ask_tan
from hiro_budget.gui.common import fit_to_screen, flat_table, show_error
from hiro_budget.services import bank, config_edit
from hiro_budget.services.bank import fetch

log = logging.getLogger(__name__)

# FinTS servers of banks the app knows (bank code -> URL); others are typed in
SERVERS = {"50010517": "https://fints.ing.de/fints/"}
NEW = "\x00new"  # combo data: create a new app account
NO_DATE = QDate(2000, 1, 1)  # the date edit's minimum: "as far back as possible"


def kind_labels() -> dict[str, str]:
    return {
        "giro": QCoreApplication.translate("bank_dialog", "Checking account"),
        "savings": QCoreApplication.translate("bank_dialog", "Savings account"),
        "credit_card": QCoreApplication.translate("bank_dialog", "Credit card"),
        "depot": QCoreApplication.translate("bank_dialog", "Securities account"),
        "loan": QCoreApplication.translate("bank_dialog", "Loan"),
    }


@dataclass
class _Row:
    link: BankAccountLink
    new_name: str  # name of the new app account, if one is created


def _qdate(d: date | None) -> QDate:
    return QDate(d.year, d.month, d.day) if d else NO_DATE


def _pydate(q: QDate) -> date | None:
    return None if q == NO_DATE else date(q.year(), q.month(), q.day())


class BankAccessDialog(QDialog):
    def __init__(self, app: App, parent=None) -> None:
        super().__init__(parent)
        self.app = app
        self.setWindowTitle(self.tr("Bank access"))
        fit_to_screen(self, 900, 560)
        self.logins: list[BankLogin] = [
            lg.model_copy(deep=True) for lg in app.settings.app.fints.logins
        ]
        self._current = -1
        with app.session() as s:
            self.accounts = {a.name: (a.id, a.kind.value) for a in s.scalars(select(Account))}

        self.login_list = QComboBox()
        self.login_list.setMinimumContentsLength(12)
        self.login_list.currentIndexChanged.connect(self._select)
        self.add_button = add = QPushButton(self.tr("Add"))
        add.clicked.connect(self._add_login)
        self.remove_button = QPushButton(self.tr("Remove"))
        self.remove_button.clicked.connect(self._remove_login)
        logins = QHBoxLayout()
        logins.addWidget(QLabel(self.tr("Bank login")))
        logins.addWidget(self.login_list)
        logins.addWidget(add)
        logins.addWidget(self.remove_button)
        logins.addStretch(1)

        self.name = QLineEdit()
        self.bank_code = QLineEdit()
        self.bank_code.textEdited.connect(self._bank_code_edited)
        self.server = QLineEdit()
        self.login_id = QLineEdit()
        self.login_id.setPlaceholderText(self.tr("e.g. the access number of your online banking"))
        form = QFormLayout()
        form.addRow(self.tr("Name"), self.name)
        form.addRow(self.tr("Bank code"), self.bank_code)
        form.addRow(self.tr("FinTS server"), self.server)
        form.addRow(self.tr("Login ID"), self.login_id)
        pin_note = QLabel(self.tr("The PIN is asked for at every fetch and is not saved."))
        pin_note.setWordWrap(True)
        form.addRow(self.tr("PIN"), pin_note)

        self.test_button = QPushButton(self.tr("Test connection…"))
        self.test_button.setToolTip(self.tr("Log in and list the accounts the bank offers"))
        self.test_button.clicked.connect(self._test)
        self.test_status = QLabel()
        self.test_status.setWordWrap(True)
        test = QHBoxLayout()
        test.addWidget(self.test_button)
        test.addWidget(self.test_status, 1)

        self.table = QTableWidget(0, 4)
        flat_table(self.table)
        self.table.setHorizontalHeaderLabels(
            [
                self.tr("Bank account"),
                self.tr("Type"),
                self.tr("App account"),
                self.tr("Fetch from"),
            ]
        )
        header = self.table.horizontalHeader()
        for col in (0, 1, 3):
            header.setSectionResizeMode(col, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self.table.verticalHeader().setVisible(False)
        accounts_note = QLabel(
            self.tr(
                "Bookings from the “Fetch from” day on come from the bank; statements (PDF) "
                "cover the days before. Securities accounts: holdings follow later."
            )
        )
        accounts_note.setWordWrap(True)

        self.detail = QWidget()
        right = QVBoxLayout(self.detail)
        right.setContentsMargins(0, 0, 0, 0)
        right.addLayout(form)
        right.addLayout(test)
        right.addWidget(QLabel(self.tr("Accounts")))
        right.addWidget(self.table, 1)
        right.addWidget(accounts_note)

        self.product_id = QLineEdit(app.settings.app.fints.product_id or "")
        self.product_id.setPlaceholderText(self.tr("not registered yet"))
        self.product_id.textChanged.connect(self._update_product_hint)
        self.product_hint = QLabel()
        self.product_hint.setWordWrap(True)
        product = QFormLayout()
        product.addRow(self.tr("Product ID"), self.product_id)
        product.addRow("", self.product_hint)

        box = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        box.accepted.connect(self._save)
        box.rejected.connect(self.reject)
        layout = QVBoxLayout(self)
        layout.addLayout(logins)
        layout.addWidget(self.detail, 1)
        layout.addLayout(product)
        layout.addWidget(box)

        for login in self.logins:
            self.login_list.addItem(login.name)
        self._update_product_hint()
        if self.logins:
            self._select(0)
        else:
            self._add_login()

    # -- logins --

    def _add_login(self) -> None:
        self._store()
        login = BankLogin(login_id="")
        self.logins.append(login)
        self.login_list.addItem(login.name)
        self.login_list.setCurrentIndex(len(self.logins) - 1)

    def _remove_login(self) -> None:
        row = self.login_list.currentIndex()
        if row < 0:
            return
        self._current = -1  # nothing to store from the removed one
        del self.logins[row]
        self.login_list.blockSignals(True)
        self.login_list.removeItem(row)
        self.login_list.blockSignals(False)
        if self.logins:
            self._select(self.login_list.currentIndex())
        else:
            self.table.setRowCount(0)
            self.detail.setEnabled(False)
            self.remove_button.setEnabled(False)

    def _select(self, row: int) -> None:
        if row == self._current:
            return
        self._store()
        self._current = row
        self.detail.setEnabled(row >= 0)
        self.remove_button.setEnabled(row >= 0)
        if row < 0:
            return
        login = self.logins[row]
        self.name.setText(login.name)
        self.bank_code.setText(login.bank_code)
        self.server.setText(login.server)
        self.login_id.setText(login.login_id)
        self.test_status.clear()
        self._fill_table(login.accounts)

    def _bank_code_edited(self, code: str) -> None:
        if code.strip() in SERVERS:
            self.server.setText(SERVERS[code.strip()])

    def _store(self) -> None:
        """The form back into the selected login."""
        if not 0 <= self._current < len(self.logins):
            return
        login = self.logins[self._current]
        login.name = self.name.text().strip() or "Bank"
        login.bank_code = self.bank_code.text().strip()
        login.server = self.server.text().strip()
        login.login_id = self.login_id.text().strip()
        login.accounts = self._links()
        self.login_list.setItemText(self._current, login.name)

    # -- accounts table --

    def _new_name(self, product: str, bank_id: str) -> str:
        login = self.name.text().strip() or "Bank"
        name = f"{login} {product}".strip()
        taken = set(self.accounts) | {
            self.table.cellWidget(r, 2).itemData(0, Qt.ItemDataRole.UserRole + 1)
            for r in range(self.table.rowCount())
        }
        return name if name not in taken else f"{name} {bank_id[-4:]}"

    def _fill_table(self, links: list[BankAccountLink]) -> None:
        self.table.setRowCount(0)
        for link in links:
            new_name = link.account if link.account not in self.accounts else ""
            self._add_row(link, new_name or self._new_name(link.product, link.bank_id))

    def _add_row(self, link: BankAccountLink, new_name: str) -> None:
        r = self.table.rowCount()
        self.table.insertRow(r)
        item = QTableWidgetItem(f"{link.product} ··{link.bank_id[-4:]}")
        item.setToolTip(link.bank_id)
        item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
        item.setData(Qt.ItemDataRole.UserRole, link)
        self.table.setItem(r, 0, item)
        kind = QTableWidgetItem(kind_labels().get(link.kind, link.kind))
        kind.setFlags(kind.flags() & ~Qt.ItemFlag.ItemIsEditable)
        self.table.setItem(r, 1, kind)

        combo = QComboBox()
        combo.addItem(self.tr("New: %1").replace("%1", new_name), NEW)
        combo.setItemData(0, new_name, Qt.ItemDataRole.UserRole + 1)
        for name in sorted(self.accounts, key=lambda n: (self.accounts[n][1] != link.kind, n)):
            combo.addItem(name, name)
        index = combo.findData(link.account)
        combo.setCurrentIndex(max(index, 0))
        self.table.setCellWidget(r, 2, combo)

        if not fetch.is_fetched(link):
            none = QTableWidgetItem("—")
            none.setFlags(none.flags() & ~Qt.ItemFlag.ItemIsEditable)
            none.setToolTip(self.tr("Securities accounts: holdings, not bookings (follows later)"))
            self.table.setItem(r, 3, none)
            return
        edit = QDateEdit()
        edit.setCalendarPopup(True)
        edit.setMinimumDate(NO_DATE)
        edit.setSpecialValueText(self.tr("as far back as possible"))
        edit.setDisplayFormat("dd.MM.yyyy")
        edit.setDate(_qdate(link.fetch_from))
        self.table.setCellWidget(r, 3, edit)
        combo.currentIndexChanged.connect(lambda _i, c=combo, e=edit: self._account_chosen(c, e))

    def _account_chosen(self, combo: QComboBox, edit: QDateEdit) -> None:
        """Another app account: its cut-over is the day after its last statement."""
        name = combo.currentData()
        account_id = None if name == NEW else self.accounts[name][0]
        with self.app.session() as s:
            edit.setDate(_qdate(fetch.suggested_fetch_from(s, account_id)))

    def _links(self) -> list[BankAccountLink]:
        links = []
        for r in range(self.table.rowCount()):
            link: BankAccountLink = self.table.item(r, 0).data(Qt.ItemDataRole.UserRole)
            combo: QComboBox = self.table.cellWidget(r, 2)
            name = combo.currentData()
            if name == NEW:
                name = combo.itemData(0, Qt.ItemDataRole.UserRole + 1)
            edit = self.table.cellWidget(r, 3)
            fetch_from = _pydate(edit.date()) if isinstance(edit, QDateEdit) else None
            links.append(link.model_copy(update={"account": name, "fetch_from": fetch_from}))
        return links

    # -- connection test --

    def _test(self) -> None:
        self._store()
        login = self.logins[self._current]
        if not login.login_id or not login.bank_code or not login.server:
            QMessageBox.information(
                self, self.windowTitle(), self.tr("Please fill in bank code, server and login ID.")
            )
            return
        pin = ask_pin(self, login)
        if pin is None:
            return
        product = self.product_id.text().strip() or None

        def job(confirm):
            with bank.connect(login, pin, product, confirm) as session:
                return session.accounts()

        self.test_button.setEnabled(False)
        self.test_status.setText(self.tr("Connecting…"))
        worker = BankWorker(job, self)
        worker.confirm_in_app.connect(
            lambda _c: self.test_status.setText(
                self.tr("Please confirm in the %1 app").replace("%1", login.name)
            )
        )
        worker.tan_needed.connect(lambda ch: ask_tan(self, worker, ch))
        worker.succeeded.connect(lambda accounts: self._offered(login, accounts))
        worker.failed.connect(self._test_failed)
        worker.finished.connect(lambda: self.test_button.setEnabled(True))
        self._worker = worker
        worker.start()

    def _test_failed(self, error: Exception) -> None:
        if isinstance(error, bank.PinError):
            text = self.tr("The bank rejected the login ID or PIN.")
        elif isinstance(error, bank.Offline):
            text = self.tr("The bank's server is not reachable.")
        elif isinstance(error, bank.Cancelled):
            text = self.tr("Cancelled.")
        else:
            text = self.tr("Failed: %1").replace("%1", str(error))
        self.test_status.setText("⚠ " + text)

    def _offered(self, login: BankLogin, offered: list[bank.BankAccount]) -> None:
        """Accounts the bank lists: known rows stay as they are, new ones are added with
        the app account of the same IBAN (or a new one) and their suggested cut-over."""
        if self.logins[self._current] is not login:
            return  # another login is shown by now
        known = {
            self.table.item(r, 0).data(Qt.ItemDataRole.UserRole).bank_id
            for r in range(self.table.rowCount())
        }
        added = 0
        with self.app.session() as s:
            for a in offered:
                if a.bank_id in known:
                    continue
                account = fetch.find_account(s, a)
                name = account.name if account is not None else ""
                log.info(
                    "bank account %s ...%s (%s): app account %s",
                    a.product,
                    a.bank_id[-4:],
                    a.kind,
                    name or "(none, new)",
                )
                link = BankAccountLink(
                    bank_id=a.bank_id,
                    product=a.product,
                    kind=a.kind,
                    account=name or "-",
                    fetch_from=fetch.suggested_fetch_from(s, account.id if account else None),
                )
                self._add_row(link, self._new_name(a.product, a.bank_id))
                added += 1
        self.test_status.setText(
            "✓ "
            + self.tr("Connected: %1 accounts, %2 new in the list.")
            .replace("%1", str(len(offered)))
            .replace("%2", str(added))
        )

    def done(self, result: int) -> None:
        worker = getattr(self, "_worker", None)
        if worker is not None and worker.isRunning():
            worker.cancel()
            worker.wait()
        super().done(result)

    # -- product ID, saving --

    def _update_product_hint(self) -> None:
        if self.product_id.text().strip():
            self.product_hint.setText(self.tr("Registered product ID of this app."))
        else:
            self.product_hint.setText(
                self.tr(
                    "Without a registered product ID the app sends a placeholder; the bank may "
                    "stop accepting it. Register the app with the Deutsche Kreditwirtschaft and "
                    "enter the ID here."
                )
            )

    def _save(self) -> None:
        self._store()
        logins = [lg for lg in self.logins if lg.login_id or lg.accounts]
        names = [link.account for lg in logins for link in lg.accounts]
        dupes = sorted({n for n in names if names.count(n) > 1})
        if dupes:
            QMessageBox.warning(
                self,
                self.windowTitle(),
                self.tr("Several bank accounts go to the same app account: %1").replace(
                    "%1", ", ".join(dupes)
                ),
            )
            return
        fints = FintsConfig(product_id=self.product_id.text().strip() or None, logins=logins)
        try:
            config_edit.save_fints(self.app.settings, fints)
        except Exception as e:
            show_error(self, self.windowTitle(), e)
            return
        self.accept()
