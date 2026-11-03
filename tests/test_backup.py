import sqlite3
from contextlib import closing

from hiro_budget.services import backup, import_service
from hiro_budget.services import transactions as txs


def test_backup_copies_database_and_config(app, statement_pdf) -> None:
    with app.session() as s:
        import_service.import_file(s, app.settings, statement_pdf)
        count = len(txs.list_transactions(s))
    folder = backup.create_backup(app.settings, "manual")
    assert folder.parent == app.settings.db_path.parent / "backups"
    copy = folder / app.settings.db_path.name
    with closing(sqlite3.connect(copy)) as db:  # a complete, readable database
        assert db.execute('select count(*) from "transaction"').fetchone()[0] == count
    names = {p.name for p in (folder / "config").iterdir()}
    assert names == {"config.yaml", "categories.yaml", "rules.yaml"}
    [only] = backup.list_backups(app.settings)
    assert only.path == folder and only.reason == "manual"


def test_old_backups_are_pruned(app) -> None:
    app.settings.app.backup.keep = 3
    made = [backup.create_backup(app.settings, "import") for _ in range(5)]
    kept = backup.list_backups(app.settings)
    assert [b.path for b in kept] == list(reversed(made))[:3]  # the newest
    stranger = backup.backup_dir(app.settings) / "my notes"
    stranger.mkdir()  # folders not made by the app stay
    backup.create_backup(app.settings, "manual")
    assert stranger.exists() and len(backup.list_backups(app.settings)) == 3


def test_import_dialog_backs_up_first(qtbot, app, statement_pdf, monkeypatch) -> None:
    from PySide6.QtWidgets import QMessageBox

    monkeypatch.setattr(QMessageBox, "information", lambda *a, **k: None)
    from hiro_budget.gui.import_dialog import ImportDialog

    dialog = ImportDialog(app, paths=[statement_pdf])
    qtbot.addWidget(dialog)
    dialog._import()
    reasons = [b.reason for b in backup.list_backups(app.settings)]
    assert reasons == ["import"]
    app.settings.app.backup.before_import = False
    dialog._import()
    assert len(backup.list_backups(app.settings)) == 1


# --- restoring (#48) --------------------------------------------------------------------


def test_restore_brings_back_database_and_config_but_keeps_the_data_location(
    app, statement_pdf
) -> None:
    from hiro_budget.config import load_yaml, save_yaml

    old = backup.create_backup(app.settings, "manual")  # empty database
    with app.session() as s:
        import_service.import_file(s, app.settings, statement_pdf)
    rules = app.settings.rules_file.read_text(encoding="utf-8")
    app.settings.rules_file.write_text(rules + "\n# changed after the backup\n", "utf-8")
    doc = load_yaml(app.settings.config_file)
    doc["data_folder"] = str(app.settings.data_dir)  # the data was moved meanwhile
    save_yaml(app.settings.config_file, doc)
    [b] = [b for b in backup.list_backups(app.settings) if b.path == old]
    assert backup.backup_bookings(app.settings, b) == 0
    assert backup.backup_size(b) > 0

    app.session_factory.kw["bind"].dispose()  # as after the window closed
    backup.restore_backup(app.settings, old)
    with closing(sqlite3.connect(app.settings.db_path)) as db:
        assert db.execute('select count(*) from "transaction"').fetchone()[0] == 0
    assert "changed after the backup" not in app.settings.rules_file.read_text("utf-8")
    assert load_yaml(app.settings.config_file)["data_folder"] == str(app.settings.data_dir)


def test_restore_dialog_backs_up_first_and_asks_for_a_restart(qtbot, app, monkeypatch) -> None:
    from PySide6.QtWidgets import QMessageBox

    from hiro_budget.gui.restore_dialog import RestoreDialog
    from hiro_budget.services.data_folder import RestartRequest

    app.settings.app.backup.keep = 2
    oldest = backup.create_backup(app.settings, "import")
    backup.create_backup(app.settings, "manual")
    dialog = RestoreDialog(app)
    qtbot.addWidget(dialog)
    assert dialog.list.topLevelItemCount() == 2
    dialog.list.setCurrentItem(dialog.list.topLevelItem(1))  # the oldest
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.No)
    dialog._restore()
    assert dialog.request is None and len(backup.list_backups(app.settings)) == 2
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.Yes)
    dialog._restore()
    assert dialog.request == RestartRequest(delete=[], ui_state=None, restore=oldest)
    reasons = [b.reason for b in backup.list_backups(app.settings)]
    assert reasons[0] == "restore" and oldest.exists()  # not pruned although keep = 2


def test_file_menu_restore_restarts(qtbot, app, monkeypatch) -> None:
    from hiro_budget.gui import restore_dialog
    from hiro_budget.gui.main_window import MainWindow
    from hiro_budget.services.data_folder import RestartRequest

    folder = backup.create_backup(app.settings, "manual")
    request = RestartRequest(delete=[], ui_state=None, restore=folder)

    def chosen(self):
        self.request = request
        return 1

    monkeypatch.setattr(restore_dialog.RestoreDialog, "exec", chosen)
    w = MainWindow(app)
    qtbot.addWidget(w)
    restarts = []
    monkeypatch.setattr(w, "restart", restarts.append)
    action = next(a for menu in w._menus for a in menu.actions() if a.text() == "&Restore backup…")
    action.trigger()
    qtbot.waitUntil(lambda: restarts == [request])
