"""Settings → Bank access: bank logins, their accounts and where they go.

Works with any connector plugin (#82): the form of a login is built from the connector's
settings model (``login_model``: e.g. bank code, server, login ID for FinTS), the settings
shared by its logins from ``options_model`` (the FinTS product ID). Per login: name,
settings and the bank's accounts, each with the app account its bookings go to (an existing
one or a new one) and the first day fetched. "Test connection" asks for the secret (PIN),
logs in and lists the accounts the bank offers. Saved to config.yaml; the secret is never
stored.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date

from pydantic import BaseModel, ValidationError
from PySide6.QtCore import QCoreApplication, QDate, QObject, Qt, Signal
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

from hiro_budget.app import App
from hiro_budget.config import BankAccessConfig, BankAccountLink, BankLogin
from hiro_budget.gui.bank_fetch import BankWorker, ask_secret, ask_tan, secret_label
from hiro_budget.gui.common import fit_to_screen, flat_table, format_iban, show_error
from hiro_budget.importers.connectors import (
    Cancelled,
    Connector,
    LoginError,
    Offline,
    RemoteAccount,
    error_message,
    suggestions,
    text,
)
from hiro_budget.services import config_edit, connections, lookups

log = logging.getLogger(__name__)

NEW = "\x00new"  # combo data: create a new app account
DEFAULT_NAME = "Bank"  # a new login's name until the user (or the connector) names it
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


def _default(model: type[BaseModel], name: str) -> str:
    info = model.model_fields[name]
    if info.is_required() or info.default is None:
        return ""
    return str(info.default)


class SettingsForm(QObject):
    """Text fields for the fields of a connector's settings model (titles and
    descriptions translated by the connector), as rows of ``form`` from row ``row`` on (so
    they line up with the rows around them). ``hints``: descriptions as labels below the
    fields instead of placeholders. ``name_suggested``: the connector's name for the login
    (a known bank code)."""

    name_suggested = Signal(str)

    def __init__(
        self,
        connector: Connector,
        model: type[BaseModel],
        language: str,
        form: QFormLayout,
        row: int,
        hints: bool = False,
    ) -> None:
        super().__init__(form)
        self.connector, self.model, self.form = connector, model, form
        self.edits: dict[str, QLineEdit] = {}
        self.hints: dict[str, QLabel] = {}
        for name, info in model.model_fields.items():
            edit = QLineEdit()
            description = text(connector, info.description, language) if info.description else ""
            if description and not hints:
                edit.setPlaceholderText(description)
            edit.textEdited.connect(self._edited)
            form.insertRow(row, text(connector, info.title or name, language), edit)
            row += 1
            self.edits[name] = edit
            if description and hints:
                hint = QLabel(description)
                hint.setWordWrap(True)
                form.insertRow(row, "", hint)
                row += 1
                self.hints[name] = hint

    def remove(self) -> None:
        """Take the rows out of the form (their widgets are deleted)."""
        for widget in [*self.edits.values(), *self.hints.values()]:
            self.form.removeRow(widget)
        self.edits, self.hints = {}, {}
        self.deleteLater()

    def set_values(self, values: dict) -> None:
        for name, edit in self.edits.items():
            value = values.get(name)
            edit.setText(_default(self.model, name) if value is None else str(value))

    def values(self) -> dict[str, str]:
        """The fields that are filled in."""
        return {n: e.text().strip() for n, e in self.edits.items() if e.text().strip()}

    def is_empty(self) -> bool:
        """No required field is filled in."""
        required = [n for n, info in self.model.model_fields.items() if info.is_required()]
        return not any(self.edits[n].text().strip() for n in required)

    def _edited(self) -> None:
        self.apply_suggestions(self.sender())

    def apply_suggestions(self, typed: object = None) -> None:
        for name, value in suggestions(self.connector, self.values()).items():
            if name in self.edits and self.edits[name] is not typed:
                self.edits[name].setText(value)
            elif name == "name":
                self.name_suggested.emit(value)


class BankAccessDialog(QDialog):
    def __init__(self, app: App, parent=None) -> None:
        super().__init__(parent)
        self.app = app
        self.language = app.settings.app.language
        self.setWindowTitle(self.tr("Bank access"))
        fit_to_screen(self, 900, 560)
        access = app.settings.app.bank_access
        self.logins: list[BankLogin] = [lg.model_copy(deep=True) for lg in access.logins]
        self.options: dict[str, dict] = {k: dict(v) for k, v in access.connectors.items()}
        self.connectors = {c.name: c for c in connections.connectors(app.settings)}
        self._current = -1
        self._suggested_name = ""
        self.fields: SettingsForm | None = None
        self.option_fields: SettingsForm | None = None
        with app.session() as s:
            self.accounts = {
                a.name: (a.id, a.kind.value) for a in lookups.accounts(s, by_name=False)
            }

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
        self.connector_choice = QComboBox()
        for c in self.connectors.values():
            self.connector_choice.addItem(c.title, c.name)
        self.connector_choice.currentIndexChanged.connect(self._connector_chosen)
        # one form: name, connection, the connector's fields (inserted), the secret
        self.head = head = QFormLayout()
        head.addRow(self.tr("Name"), self.name)
        head.addRow(self.tr("Connection"), self.connector_choice)
        # only a choice with more than one connector plugin
        head.setRowVisible(self.connector_choice, len(self.connectors) > 1)
        self.secret_label = QLabel()
        self.secret_note = QLabel(self.tr("Asked for at every fetch, never saved."))
        self.secret_note.setWordWrap(True)
        head.addRow(self.secret_label, self.secret_note)

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
        right.addLayout(head)
        right.addLayout(test)
        right.addWidget(QLabel(self.tr("Accounts")))
        right.addWidget(self.table, 1)
        right.addWidget(accounts_note)
        self.options_form = QFormLayout()

        box = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        box.accepted.connect(self._save)
        box.rejected.connect(self.reject)
        layout = QVBoxLayout(self)
        layout.addLayout(logins)
        layout.addWidget(self.detail, 1)
        layout.addLayout(self.options_form)
        layout.addWidget(box)

        for login in self.logins:
            self.login_list.addItem(login.name)
        if self.logins:
            self._select(0)
        else:
            self._add_login()

    # -- the connector's forms --

    def _connector(self, login: BankLogin) -> Connector | None:
        return self.connectors.get(login.connector)

    def _show_forms(self, login: BankLogin) -> None:
        """The login's settings form and its connector's options below."""
        connector = self._connector(login)
        if self.fields is not None and (self.fields.connector is not connector):
            self._store_options()
            for form in (self.fields, self.option_fields):
                if form is not None:
                    form.remove()
            self.fields = self.option_fields = None
        if connector is None:
            self.secret_label.setText("")
            self.test_status.setText(
                "⚠ "
                + self.tr("No plugin provides the connection “%1”.").replace("%1", login.connector)
            )
            return
        if self.fields is None:
            # after name and connection, before the secret
            self.fields = SettingsForm(
                connector, connector.login_model, self.language, self.head, 2
            )
            self.fields.name_suggested.connect(self._name_suggested)
            if connector.options_model is not None:
                self.option_fields = SettingsForm(
                    connector, connector.options_model, self.language, self.options_form, 0, True
                )
                self.option_fields.set_values(self.options.get(connector.name, {}))
        self.fields.set_values(login.settings)
        label = secret_label(self.app.settings, login)
        self.secret_label.setText(label)
        self.secret_label.setVisible(bool(label))
        self.secret_note.setVisible(bool(label))

    def _name_suggested(self, name: str) -> None:
        """The bank's name for a login the user has not named yet."""
        if self.name.text().strip() in ("", DEFAULT_NAME, self._suggested_name):
            self.name.setText(name)
            self._suggested_name = name

    def _store_options(self) -> None:
        if self.option_fields is not None:
            self.options[self.option_fields.connector.name] = self.option_fields.values()

    def _connector_chosen(self) -> None:
        if not 0 <= self._current < len(self.logins):
            return
        login = self.logins[self._current]
        name = self.connector_choice.currentData()
        if name and name != login.connector:
            login.connector, login.settings = name, {}
            self._show_forms(login)

    # -- logins --

    def _add_login(self) -> None:
        self._store()
        first = next(iter(self.connectors), "fints")
        login = BankLogin(connector=first, name=DEFAULT_NAME)
        self.logins.append(login)
        self.login_list.addItem(login.name)
        self.login_list.setCurrentIndex(len(self.logins) - 1)
        if self.fields is not None:  # the defaults may name a bank (FinTS: ING)
            self.fields.apply_suggestions()
            self._store()

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
        self.connector_choice.blockSignals(True)
        self.connector_choice.setCurrentIndex(
            max(self.connector_choice.findData(login.connector), 0)
        )
        self.connector_choice.blockSignals(False)
        self.test_status.clear()
        self._show_forms(login)
        self._fill_table(login.accounts)

    def _store(self) -> None:
        """The form back into the selected login."""
        if not 0 <= self._current < len(self.logins):
            return
        login = self.logins[self._current]
        login.name = self.name.text().strip() or "Bank"
        if self.fields is not None and self.fields.connector is self._connector(login):
            login.settings = self.fields.values()
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
        item.setToolTip(format_iban(link.bank_id))
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

        if not connections.is_fetched(link):
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
        connector = self.logins[self._current].connector
        with self.app.session() as s:
            edit.setDate(_qdate(connections.suggested_fetch_from(s, account_id, connector)))

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

    def _settings_problem(self, login: BankLogin) -> str | None:
        """Why the login's settings do not fit its connector (None: they do)."""
        connector = self._connector(login)
        if connector is None:
            return self.tr("No plugin provides the connection “%1”.").replace("%1", login.connector)
        try:
            connector.login_model.model_validate(login.settings)
            if connector.options_model is not None:
                connector.options_model.model_validate(self.options.get(connector.name, {}))
        except ValidationError as e:
            return self._describe(connector, e)
        return None

    def _describe(self, connector: Connector, error: ValidationError) -> str:
        """Each wrong field by its label in the form."""
        models = [connector.login_model, connector.options_model]
        fields = {n: i for m in models if m is not None for n, i in m.model_fields.items()}
        lines = []
        for e in error.errors():
            name = str(e["loc"][0]) if e["loc"] else ""
            info = fields.get(name)
            label = text(connector, info.title or name, self.language) if info else name
            if e["type"] in ("missing", "string_too_short"):
                message = self.tr("required")
            else:  # the connector's own message (a validator), in the app's language
                message = text(connector, error_message(e), self.language)
            lines.append(f"{label}: {message}")
        return "\n".join(lines)

    def _test(self) -> None:
        self._store()
        self._store_options()
        login = self.logins[self._current]
        problem = self._settings_problem(login)
        if problem is not None:
            QMessageBox.information(
                self,
                self.windowTitle(),
                self.tr("Please check the settings:") + "\n\n" + problem,
            )
            return
        secret = ""
        if label := secret_label(self.app.settings, login):
            secret = ask_secret(self, label, login)
            if secret is None:
                return
        settings, options = self.app.settings, self.options.get(login.connector, {})

        def job(confirm):
            with connections.connect(settings, login, secret, confirm, options) as session:
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
        if isinstance(error, LoginError):
            text = self.tr("The bank rejected the login.")
        elif isinstance(error, Offline):
            text = self.tr("The bank's server is not reachable.")
        elif isinstance(error, Cancelled):
            text = self.tr("Cancelled.")
        else:
            text = self.tr("Failed: %1").replace("%1", str(error))
        self.test_status.setText("⚠ " + text)

    def _offered(self, login: BankLogin, offered: list[RemoteAccount]) -> None:
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
                account = connections.find_account(s, a)
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
                    fetch_from=connections.suggested_fetch_from(
                        s, account.id if account else None, login.connector
                    ),
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

    # -- saving --

    def _is_empty(self, login: BankLogin) -> bool:
        """Added but nothing filled in: not saved."""
        connector = self._connector(login)
        if login.accounts or connector is None:
            return False
        required = [n for n, i in connector.login_model.model_fields.items() if i.is_required()]
        return not any(login.settings.get(n) for n in required)

    def _save(self) -> None:
        self._store()
        self._store_options()
        logins = [lg for lg in self.logins if not self._is_empty(lg)]
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
        for login in logins:
            if (problem := self._settings_problem(login)) is not None:
                QMessageBox.warning(
                    self,
                    self.windowTitle(),
                    self.tr("Please check the settings of %1:").replace("%1", login.name)
                    + "\n\n"
                    + problem,
                )
                return
        used = {lg.connector for lg in logins}
        options = {k: v for k, v in self.options.items() if k in used and v}
        access = BankAccessConfig(connectors=options, logins=logins)
        try:
            config_edit.save_bank_access(self.app.settings, access)
        except Exception as e:
            show_error(self, self.windowTitle(), e)
            return
        self.accept()
