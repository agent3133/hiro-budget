"""Settings > Data folder… (#29): move with restart, open an existing folder, start-up."""

import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from PySide6.QtWidgets import QMessageBox

from hiro_budget.config import CONFIG_DIR_ENV, load_settings
from hiro_budget.gui import data_folder_dialog as dfd
from hiro_budget.gui.data_folder_dialog import DataFolderDialog
from hiro_budget.gui.main_window import MainWindow
from hiro_budget.services import backup
from hiro_budget.services import data_folder as df
from hiro_budget.services.data_folder import RestartRequest


@pytest.fixture(autouse=True)
def message_boxes(monkeypatch):
    shown: list[tuple[str, str]] = []

    def record(kind, answer):
        def box(parent, title, text, *a, **k):
            shown.append((kind, text))
            return answer

        return box

    monkeypatch.setattr(QMessageBox, "information", record("information", QMessageBox.Ok))
    monkeypatch.setattr(QMessageBox, "warning", record("warning", QMessageBox.Ok))
    monkeypatch.setattr(QMessageBox, "critical", record("critical", QMessageBox.Ok))
    monkeypatch.setattr(QMessageBox, "question", record("question", QMessageBox.Yes))
    return shown


@pytest.fixture
def restarts(monkeypatch):
    requests: list[RestartRequest] = []
    monkeypatch.setattr(MainWindow, "restart", lambda self, r: requests.append(r))
    return requests


@pytest.fixture
def window(qtbot, app):
    s = app.settings
    s.model_path.parent.mkdir(parents=True, exist_ok=True)
    s.model_path.write_bytes(b"model")
    backup.create_backup(s, "manual")
    w = MainWindow(app)
    qtbot.addWidget(w)
    return w


@pytest.fixture
def dialog(qtbot, window):
    d = DataFolderDialog(window.app, window)
    qtbot.addWidget(d)
    return d


def test_settings_menu_has_the_entry(window) -> None:
    texts = [a.text() for menu in window._menus for a in menu.actions()]
    assert "&Data folder…" in texts


def test_dialog_shows_the_folder_and_what_is_in_it(dialog, app) -> None:
    assert str(app.settings.data_dir) in dialog.path.text()
    assert "test.db" in dialog.contents.text()


def test_move_writes_the_config_and_restarts(
    dialog, app, tmp_path, qtbot, restarts, monkeypatch
) -> None:
    monkeypatch.setattr(dfd, "ask_restart_after_move", lambda parent, result, old: True)
    old = df.data_files(app.settings).existing()
    old_dir = app.settings.data_dir
    target = tmp_path / "OneDrive"
    assert dialog.change(target)
    qtbot.waitUntil(lambda: len(restarts) == 1)
    assert load_settings(app.settings.config_dir).data_dir == target
    assert (target / "test.db").is_file() and (target / "classifier.npz").is_file()
    request = restarts[0]
    assert set(request.delete) == set(old)
    assert request.ui_state == (old_dir / "ui_state.json", target / "ui_state.json")


def test_keeping_the_old_files(dialog, app, tmp_path, qtbot, restarts, monkeypatch) -> None:
    monkeypatch.setattr(dfd, "ask_restart_after_move", lambda parent, result, old: False)
    dialog.change(tmp_path / "nas")
    qtbot.waitUntil(lambda: len(restarts) == 1)
    assert restarts[0].delete == []


def test_declined_question_moves_nothing(dialog, app, tmp_path, monkeypatch, restarts) -> None:
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.No)
    assert not dialog.change(tmp_path / "new")
    assert not (tmp_path / "new").exists() and restarts == []


def test_unsaved_changes_first(dialog, window, tmp_path, message_boxes, monkeypatch) -> None:
    monkeypatch.setattr(MainWindow, "has_unsaved_changes", lambda self: True)
    assert not dialog.change(tmp_path / "new")
    assert message_boxes[-1][0] == "information" and not (tmp_path / "new").exists()


def test_errors_are_explained(dialog, app, message_boxes, restarts) -> None:
    assert not dialog.change(app.settings.data_dir / "inside")
    kind, text = message_boxes[-1]
    assert kind == "critical" and "inside the current data folder" in text
    assert load_settings(app.settings.config_dir).app.data_folder is None and restarts == []


def test_a_folder_with_a_database_is_opened(dialog, app, tmp_path, qtbot, restarts) -> None:
    other = tmp_path / "second pc"
    other.mkdir()
    shutil.copy2(app.settings.db_path, other / "test.db")
    assert dialog.change(other)
    qtbot.waitUntil(lambda: len(restarts) == 1)
    assert restarts[0] == RestartRequest(delete=[], ui_state=None)
    assert load_settings(app.settings.config_dir).data_dir == other
    assert sorted(p.name for p in other.iterdir()) == ["test.db"]  # nothing copied


def test_finish_restart(app, tmp_path, monkeypatch) -> None:
    started = []
    monkeypatch.setattr(subprocess, "Popen", lambda cmd, **k: started.append((cmd, k)))
    s = app.settings
    new = tmp_path / "new"
    df.move_data(s, new)
    old_files = df.data_files(s).existing()
    app.ui_state.path.write_text('{"written": "on close"}', encoding="utf-8")
    request = RestartRequest(delete=old_files, ui_state=(app.ui_state.path, new / "ui_state.json"))
    dfd.finish_restart(app, request)
    assert (new / "ui_state.json").read_text(encoding="utf-8") == '{"written": "on close"}'
    assert not any(p.exists() for p in old_files)
    ((cmd, kwargs),) = started
    assert cmd == [sys.executable, "-m", "hiro_budget"]
    assert kwargs["env"][CONFIG_DIR_ENV] == str(s.config_dir)


def test_restart_command_starts_this_app() -> None:
    out = subprocess.run(
        [*dfd.restart_command(), "--help"], capture_output=True, text=True, timeout=60
    )
    assert out.returncode == 0 and "hiro-budget" in out.stdout


# -- start-up ---------------------------------------------------------------------------


@pytest.fixture
def offline(config_dir, tmp_path):
    """A configuration whose data folder is not there (cloud drive offline)."""
    s = load_settings(config_dir)
    s.app.data_folder = str(tmp_path / "offline")
    return s


def test_missing_folder_quit(offline, monkeypatch) -> None:
    monkeypatch.setattr(dfd, "ask_missing", lambda s: "quit")
    assert not dfd.ensure_data_folder(offline)
    assert not (Path(offline.app.data_folder)).exists()  # no new empty database


def test_missing_folder_retry_after_it_came_back(offline, monkeypatch, tmp_path) -> None:
    answers = []

    def ask(s):
        answers.append("retry")
        folder = Path(s.app.data_folder)
        folder.mkdir()
        (folder / "test.db").write_bytes(b"")  # OneDrive has synced it now
        return "retry"

    monkeypatch.setattr(dfd, "ask_missing", ask)
    assert dfd.ensure_data_folder(offline)
    assert answers == ["retry"]


def test_missing_folder_choose(offline, monkeypatch, tmp_path, message_boxes) -> None:
    empty, moved = tmp_path / "empty", tmp_path / "moved"
    empty.mkdir()
    moved.mkdir()
    (moved / "test.db").write_bytes(b"")
    answers = iter(["choose", "choose"])
    folders = iter([empty, moved])
    monkeypatch.setattr(dfd, "ask_missing", lambda s: next(answers))
    monkeypatch.setattr(dfd, "choose_folder", lambda s: next(folders))
    assert dfd.ensure_data_folder(offline)
    assert "no Hiro Budget database" in message_boxes[-1][1]  # the empty one first
    assert load_settings(offline.config_dir).data_dir == moved


def test_no_data_folder_no_question(config_dir, monkeypatch) -> None:
    monkeypatch.setattr(dfd, "ask_missing", lambda s: pytest.fail("asked"))
    assert dfd.ensure_data_folder(load_settings(config_dir))  # first start: db is created


def test_finish_restart_restores_a_backup(app, statement_pdf, monkeypatch) -> None:
    # #48: the backup is copied back after the window closed, then the app starts again
    import sqlite3
    from contextlib import closing

    from sqlalchemy import text

    from hiro_budget.services import backup, import_service

    started = []
    monkeypatch.setattr(subprocess, "Popen", lambda cmd, **k: started.append(cmd))
    with app.session() as s:
        import_service.import_file(s, app.settings, statement_pdf)
    folder = backup.create_backup(app.settings, "manual")
    with app.session() as s:  # changed after the backup
        s.execute(text('delete from "transaction"'))
        s.commit()
    dfd.finish_restart(app, RestartRequest(delete=[], ui_state=None, restore=folder))
    with closing(sqlite3.connect(app.settings.db_path)) as db:
        assert db.execute('select count(*) from "transaction"').fetchone()[0] > 0
    assert len(started) == 1
