"""Fetching from the bank: the background thread, PIN and TAN prompts, and the fetch
button with its status in the title bar.

The thread only talks to the bank; the database is written on the GUI thread afterwards
(``services.bank.fetch.import_fetched``), like an import.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date

from PySide6.QtCore import QCoreApplication, QObject, Qt, QThread, QTimer, Signal
from PySide6.QtWidgets import QInputDialog, QLineEdit, QMessageBox, QToolButton, QWidget

from hiro_budget.app import App
from hiro_budget.config import BankAccountLink, BankLogin
from hiro_budget.gui.common import busy, learning_text, money
from hiro_budget.services import bank
from hiro_budget.services.backup import create_backup
from hiro_budget.services.bank import fetch
from hiro_budget.services.categorization.pipeline import train_classifier

log = logging.getLogger(__name__)

POLL_SECONDS = 2.0  # asking the bank again whether the app confirmation arrived


class BankWorker(QThread):
    """Runs ``job(confirmation)`` away from the GUI; is the job's ``Confirmation``."""

    confirm_in_app = Signal(str)  # the bank waits for a confirmation in its app
    tan_needed = Signal(str)  # the bank wants a TAN: answer with ``answer_tan``
    succeeded = Signal(object)
    failed = Signal(object)  # the exception

    def __init__(self, job: Callable[[bank.Confirmation], object], parent=None) -> None:
        super().__init__(parent)
        self.job = job
        self.cancelled = threading.Event()
        self._tan_ready = threading.Event()
        self._tan: str | None = None
        self._waiting = False

    def run(self) -> None:
        try:
            result = self.job(self)
        except Exception as e:  # reported on the GUI thread
            self.failed.emit(e)
        else:
            self.succeeded.emit(result)

    def cancel(self) -> None:
        self.cancelled.set()
        self.answer_tan(None)

    # -- Confirmation (called on the worker thread) --

    def wait_for_app(self, challenge: str) -> bool:
        if not self._waiting:
            self._waiting = True
            self.confirm_in_app.emit(challenge)
        return not self.cancelled.wait(POLL_SECONDS)

    def tan(self, challenge: str) -> str | None:
        self._tan_ready.clear()
        self.tan_needed.emit(challenge)
        self._tan_ready.wait()
        return None if self.cancelled.is_set() else self._tan

    def answer_tan(self, tan: str | None) -> None:
        self._tan = tan
        self._tan_ready.set()


def ask_pin(parent: QWidget | None, login: BankLogin, rejected: bool = False) -> str | None:
    """The PIN of a login (not stored); None: cancelled."""
    text = (
        QCoreApplication.translate("bank_fetch", "PIN for %1 (login %2):")
        .replace("%1", login.name)
        .replace("%2", login.login_id)
    )
    if rejected:
        text = (
            QCoreApplication.translate(
                "bank_fetch", "The bank rejected the PIN. Please enter it again."
            )
            + "\n\n"
            + text
        )
    pin, ok = QInputDialog.getText(
        parent,
        QCoreApplication.translate("bank_fetch", "Online banking"),
        text,
        QLineEdit.EchoMode.Password,
    )
    return pin if ok and pin else None


def ask_tan(parent: QWidget | None, worker: BankWorker, challenge: str) -> None:
    tan, ok = QInputDialog.getText(
        parent,
        QCoreApplication.translate("bank_fetch", "Online banking"),
        challenge or QCoreApplication.translate("bank_fetch", "TAN:"),
        QLineEdit.EchoMode.Normal,
    )
    if ok and tan.strip():
        worker.answer_tan(tan.strip())
    else:
        worker.cancel()


@dataclass
class _LoginJob:
    login: BankLogin
    starts: list[tuple[BankAccountLink, date | None]]
    pin: str = field(default="", repr=False)


def fetch_login(job: _LoginJob, product_id: str | None, confirm: bank.Confirmation):
    """Worker thread: bookings and balances of the login's fetched accounts.

    Returns ``(items, missing)``: ``(login, link, Fetched)`` per account, and the links the
    bank did not list."""
    items, missing = [], []
    with bank.connect(job.login, job.pin, product_id, confirm) as session:
        offered = {a.bank_id: a for a in session.accounts()}
        for link, start in job.starts:
            account = offered.get(link.bank_id)
            if account is None:
                missing.append(link)
                continue
            items.append((job.login, link, session.fetch(account, start)))
    return items, missing


class FetchControl(QObject):
    """The "Fetch" button and its status in the title bar.

    ``fetched`` is emitted after new data was imported (the window refreshes its pages).
    ``setup_needed`` when there is no bank login yet (the window opens the settings).
    """

    fetched = Signal()
    setup_needed = Signal()

    def __init__(self, app: App, window: QWidget) -> None:
        super().__init__(window)
        self.app = app
        self.window = window
        self.button = QToolButton()
        self.button.setObjectName("titleButton")
        self.button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.button.clicked.connect(self.start)
        self.status = QToolButton()
        self.status.setObjectName("titleButton")
        self.status.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.status.clicked.connect(self._show_details)
        self.status.hide()
        self.cancel_button = QToolButton()
        self.cancel_button.setObjectName("titleButton")
        self.cancel_button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.cancel_button.clicked.connect(self.cancel)
        self.cancel_button.hide()
        self._clear = QTimer(self, singleShot=True, interval=60_000)
        self._clear.timeout.connect(self._clear_status)
        self.worker: BankWorker | None = None
        self.details = ""
        self._queue: list[_LoginJob] = []
        self._current: _LoginJob | None = None
        self._items: list = []
        self._missing: list[BankAccountLink] = []
        self.retranslate()

    def retranslate(self) -> None:
        self.button.setText(self.tr("Fetch"))
        self.button.setToolTip(self.tr("Fetch new bookings and balances from the bank (FinTS)"))
        self.cancel_button.setText(self.tr("Cancel"))

    @property
    def running(self) -> bool:
        return self.worker is not None or self._current is not None

    # -- status --

    def _set_status(self, text: str, details: str = "", final: bool = False) -> None:
        self.status.setText(text)
        self.status.setToolTip(details or text)
        self.status.setVisible(bool(text))
        self.details = details
        if final:
            self._clear.start()
        else:
            self._clear.stop()

    def _clear_status(self) -> None:
        self._set_status("")

    def _show_details(self) -> None:
        if self.details:
            QMessageBox.information(self.window, self.tr("Fetch"), self.details)

    # -- flow --

    def start(self) -> None:
        if self.running:
            return
        logins = [lg for lg in self.app.settings.app.fints.logins if lg.accounts]
        if not logins:
            self.setup_needed.emit()
            return
        jobs = []
        with self.app.session() as s:
            for login in logins:
                starts = [
                    (link, fetch.fetch_start(s, link))
                    for link in login.accounts
                    if fetch.is_fetched(link)
                ]
                if starts:
                    jobs.append(_LoginJob(login, starts))
        if not jobs:
            self._set_status(self.tr("Nothing to fetch"), final=True)
            return
        self._queue, self._items, self._missing = jobs, [], []
        self._next()

    def _next(self, rejected: bool = False) -> None:
        if not self._queue:
            self._current = None
            self._import()
            return
        job = self._queue[0]
        pin = ask_pin(self.window, job.login, rejected)
        if pin is None:
            self._queue = []
            self._current = None
            self._set_status(self.tr("Fetch cancelled"), final=True)
            return
        job.pin = pin
        self._current = job
        self._set_status(self.tr("Fetching from %1…").replace("%1", job.login.name))
        product = self.app.settings.app.fints.product_id
        self.worker = BankWorker(lambda c, j=job: fetch_login(j, product, c), self)
        self.worker.confirm_in_app.connect(self._confirm_in_app)
        self.worker.tan_needed.connect(lambda ch: ask_tan(self.window, self.worker, ch))
        self.worker.succeeded.connect(lambda r: setattr(self, "_outcome", (True, r)))
        self.worker.failed.connect(lambda e: setattr(self, "_outcome", (False, e)))
        self.worker.finished.connect(self._worker_finished)
        self._outcome = (False, bank.BankError("no answer"))
        self.worker.start()

    def _worker_finished(self) -> None:
        """The thread has ended: only now the next one may start."""
        worker, self.worker = self.worker, None
        worker.deleteLater()
        ok, value = self._outcome
        if ok:
            self._login_done(value)
        else:
            self._login_failed(value)

    def _confirm_in_app(self, challenge: str) -> None:
        name = self._current.login.name if self._current else ""
        self._set_status(self.tr("Please confirm in the %1 app").replace("%1", name), challenge)
        self.cancel_button.show()

    def cancel(self) -> None:
        if self.worker is not None:
            self.worker.cancel()

    def shutdown(self) -> None:
        """The window closes: stop asking, wait for the bank's last answer."""
        self._queue = []
        if self.worker is not None:
            self.worker.cancel()
            self.worker.wait()

    def _login_done(self, result) -> None:
        self.cancel_button.hide()
        items, missing = result
        self._items += items
        self._missing += missing
        self._queue.pop(0)
        self._current.pin = ""
        self._next()

    def _login_failed(self, error: Exception) -> None:
        self.cancel_button.hide()
        job = self._current
        job.pin = ""
        if isinstance(error, bank.PinError):
            self._next(rejected=True)  # the same login again
            return
        self._queue = []
        self._current = None
        if isinstance(error, bank.Cancelled):
            self._set_status(self.tr("Fetch cancelled"), final=True)
        elif isinstance(error, bank.Offline):
            self._set_status(self.tr("Bank not reachable"), str(error), final=True)
        else:
            log.warning("fetch from %s failed: %s", job.login.name, error)
            self._set_status("⚠ " + self.tr("Fetch failed"), f"{job.login.name}: {error}")

    def _import(self) -> None:
        settings = self.app.settings
        if settings.app.backup.before_import:
            try:
                create_backup(settings, "fetch")
            except Exception as e:  # the fetch still goes in; the user sees why there is none
                log.warning("backup before fetch failed: %s", e)
        try:
            with self.app.session() as s:
                result = fetch.import_fetched(s, settings, self._items, retrain=False)
            if result.added and settings.app.ml.enabled:
                with busy(self._set_status, learning_text()), self.app.session() as s:
                    train_classifier(s, settings)
        except Exception as e:
            log.exception("importing the fetched bookings failed")
            self._set_status("⚠ " + self.tr("Fetch failed"), str(e))
            return
        finally:
            self._items = []
        self._report(result)
        if result.added or result.batch_ids:
            self.fetched.emit()

    def _report(self, result: fetch.FetchResult) -> None:
        lines = []
        for r in result.accounts:
            line = f"{r.account}: " + self.tr("%n new booking(s)", "", r.added)
            diff = r.balance_difference_cents
            if diff:
                line += " – " + self.tr("balance at the bank %1, in the app %2").replace(
                    "%1", money(r.bank_balance_cents)
                ).replace("%2", money(r.app_balance_cents))
            lines.append(line)
        for link in self._missing:
            lines.append(
                self.tr("%1: not offered by the bank (any more)").replace("%1", link.account)
            )
        text = "✓ " + self.tr("%n new booking(s)", "", result.added)
        if result.balance_differences:
            text = (
                "⚠ "
                + self.tr("%n new booking(s)", "", result.added)
                + " · "
                + self.tr("balance differs")
            )
        elif all(r.bank_balance_cents is not None for r in result.accounts):
            text += " · " + self.tr("balances match")
        if self._missing:
            text = "⚠ " + text.lstrip("✓⚠ ")
        self._set_status(text, "\n".join(lines), final=not result.balance_differences)
