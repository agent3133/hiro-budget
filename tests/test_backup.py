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
