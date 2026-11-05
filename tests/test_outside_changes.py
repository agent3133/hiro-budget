"""Writes of the command line / an agent (#85): a backup before the first one, no lock
errors between processes, and the open window loads what changed."""

from __future__ import annotations

import sqlite3
import threading
import time
from datetime import date

import pytest
from PySide6.QtWidgets import QMessageBox
from sqlalchemy import select

from hiro_budget import ops
from hiro_budget.app import App
from hiro_budget.db.models import Account
from hiro_budget.gui.main_window import MainWindow
from hiro_budget.ops import OpContext, OpError
from hiro_budget.services import backup
from hiro_budget.services import transactions as txs
from hiro_budget.services.transactions import ManualEntry


def call(ctx, op_name, /, **args):
    return ops.call(ctx, op_name, args)


def make_booking(app, payee="Shop", cents=-2000) -> int:
    with app.session() as s:
        acc = s.scalar(select(Account.id).where(Account.name == "Bargeld"))
        entry = ManualEntry(acc, date(2026, 3, 5), cents, payee)
        return txs.create_manual(s, app.settings, entry)


# --- backup before the first write -----------------------------------------------------


def test_one_backup_for_two_writes_none_for_reads(app):
    ctx = OpContext(app, source="cli")
    tx = make_booking(app)
    call(ctx, "list_bookings")
    call(ctx, "get_budget")
    assert backup.list_backups(app.settings) == []
    call(ctx, "set_booking_note", id=tx, text="a")
    call(ctx, "set_booking_note", id=tx, text="b")
    (made,) = backup.list_backups(app.settings)
    assert made.reason == "cli"
    call(OpContext(app, source="mcp"), "set_booking_note", id=tx, text="c")  # a new session
    assert [b.reason for b in backup.list_backups(app.settings)] == ["mcp", "cli"]


def test_backup_can_be_switched_off(app):
    app.settings.app.backup.before_write = False
    tx = make_booking(app)
    call(OpContext(app), "set_booking_note", id=tx, text="a")
    assert backup.list_backups(app.settings) == []


def test_failed_backup_stops_the_write(app, monkeypatch):
    tx = make_booking(app)

    def broken(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(backup, "create_backup", broken)
    with pytest.raises(OpError, match="backup before the change failed.*disk full"):
        call(OpContext(app), "set_booking_note", id=tx, text="a")
    assert call(OpContext(app), "get_booking", id=tx).notes is None


def test_import_makes_its_own_backup_only(app, statement_pdf):
    call(OpContext(app, source="cli"), "import_files", paths=[str(statement_pdf)])
    assert [b.reason for b in backup.list_backups(app.settings)] == ["import"]


# --- two processes on one database -----------------------------------------------------


def test_a_write_waits_for_the_lock_of_another_connection(app):
    tx = make_booking(app)
    holder = sqlite3.connect(app.settings.db_path, isolation_level=None, check_same_thread=False)
    holder.execute("BEGIN IMMEDIATE")  # another process is writing
    release = threading.Timer(1.0, lambda: holder.execute("COMMIT"))
    release.start()
    started = time.perf_counter()
    try:
        call(OpContext(app), "set_booking_note", id=tx, text="waited")
    finally:
        release.join()
        holder.close()
    assert time.perf_counter() - started >= 0.8  # it waited for the other write
    assert call(OpContext(app), "get_booking", id=tx).notes == "waited"


# --- the open window ---------------------------------------------------------------------


class Boxes(list):
    """Every question the window asks; ``answer`` is what the user says."""

    answer = QMessageBox.StandardButton.No


@pytest.fixture
def boxes(monkeypatch) -> Boxes:
    asked = Boxes()

    def question(parent, title, text, *args, **kwargs):
        asked.append(text)
        return asked.answer

    for name in ("information", "warning", "critical"):
        monkeypatch.setattr(QMessageBox, name, lambda *a, **k: QMessageBox.StandardButton.Ok)
    monkeypatch.setattr(QMessageBox, "question", question)
    return asked


@pytest.fixture
def window(qtbot, app, boxes):
    w = MainWindow(app)
    qtbot.addWidget(w)
    w.isActiveWindow = lambda: True  # headless windows are never "active"
    return w


@pytest.fixture
def other(config_dir) -> OpContext:
    """The command line / an agent: another App on the same files."""
    return OpContext(App.from_config_dir(config_dir), source="mcp")


def category_shown(window, tx_id: int) -> str:
    row = next(r for r in window.transactions.model.rows() if r.id == tx_id)
    return row.splits[0].category_key or ""


def test_change_from_outside_is_loaded(window, other):
    tx = make_booking(window.app)
    window.show_page(window.PAGES.index("transactions"))
    assert category_shown(window, tx) != "lebensmittel.supermarkt"
    call(other, "categorize_bookings", ids=[tx], category="lebensmittel.supermarkt")
    window._poll_outside_changes()
    assert category_shown(window, tx) == "lebensmittel.supermarkt"
    assert "change(s) by the command line or an agent loaded" in window.statusBar().currentMessage()


def test_changed_configuration_is_reloaded(window, other):
    window.show_page(window.PAGES.index("config_view"))
    call(other, "create_category", group="lebensmittel", name="Eis")
    assert "lebensmittel.eis" not in window.app.settings.categories.full_keys()
    window._poll_outside_changes()
    assert "lebensmittel.eis" in window.app.settings.categories.full_keys()


def test_no_reload_when_nothing_changed_or_window_inactive(window, other, monkeypatch):
    seen = []
    monkeypatch.setattr(window, "refresh_all", lambda: seen.append(1))
    window._poll_outside_changes()
    assert seen == []
    tx = make_booking(window.app)
    call(other, "set_booking_note", id=tx, text="x")
    window.isActiveWindow = lambda: False
    window._poll_outside_changes()
    assert seen == []
    window.isActiveWindow = lambda: True
    window._poll_outside_changes()  # back in the window: now
    assert seen == [1]
    window._poll_outside_changes()  # and only once
    assert seen == [1]


def test_own_writes_do_not_trigger_a_reload(window, monkeypatch):
    seen = []
    monkeypatch.setattr(window, "refresh_all", lambda: seen.append(1))
    tx = make_booking(window.app)
    with window.app.session() as s:
        txs.set_notes(s, tx, "from the window")
    window._poll_outside_changes()
    assert seen == []


def test_unsaved_edits_are_asked_about_once(window, other, boxes, monkeypatch):
    seen = []
    monkeypatch.setattr(window, "refresh_all", lambda: seen.append(1))
    monkeypatch.setattr(window, "has_unsaved_changes", lambda: True)
    tx = make_booking(window.app)
    call(other, "set_booking_note", id=tx, text="1")
    window._poll_outside_changes()  # the answer is No: keep my edits
    window._poll_outside_changes()  # not asked again for the same change
    assert len(boxes) == 1 and "unsaved edits" in boxes[0] and seen == []
    assert "not loaded (unsaved edits)" in window.statusBar().currentMessage()
    boxes.answer = QMessageBox.StandardButton.Yes
    call(other, "set_booking_note", id=tx, text="2")
    window._poll_outside_changes()
    assert len(boxes) == 2 and seen == [1]


def test_poll_is_cheap(window):
    started = time.perf_counter()
    for _ in range(50):
        window._latest_change_id()
    per_query_ms = (time.perf_counter() - started) / 50 * 1000
    assert per_query_ms < 5  # measured on the demo database in the issue; here only a bound
