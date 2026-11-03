"""Settings → Plugins and plugin problems in the import dialog (issue #24, step 5)."""

from __future__ import annotations

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QMessageBox

from hiro_budget.config import load_settings
from hiro_budget.gui.plugins_dialog import COL_NAME, COL_ORIGIN, COL_STATUS, PluginsDialog
from hiro_budget.importers import plugins
from tests.test_plugins import importer_plugin, write_plugin

BUILTIN = [
    "fints",
    "ing_common",
    "ing_depot_pdf",
    "ing_giro_pdf",
    "ing_loan_pdf",
    "ing_umsatzanzeige_csv",
    "ing_umsatzanzeige_pdf",
    "paypal_csv",
    "revolut_csv",
    "revolut_pdf",
]


@pytest.fixture(autouse=True)
def fresh_registry():
    plugins.reset()
    yield
    plugins.reset()


def rows(dlg: PluginsDialog) -> list[tuple[str, str, str]]:
    t = dlg.table
    return [
        (t.item(r, COL_NAME).text(), t.item(r, COL_ORIGIN).text(), t.item(r, COL_STATUS).text())
        for r in range(t.rowCount())
    ]


def row_of(dlg: PluginsDialog, name: str) -> int:
    return next(r for r in range(dlg.table.rowCount()) if dlg.table.item(r, 0).text() == name)


def test_builtin_plugins_are_listed_as_active(qtbot, app):
    dlg = PluginsDialog(app)
    qtbot.addWidget(dlg)
    assert rows(dlg) == [(name, "built-in", "active") for name in BUILTIN]


def test_enable_a_user_plugin_with_code(qtbot, app, monkeypatch):
    importer_plugin(app.settings.plugins_dir, "my_bank_pdf", "my_bank_pdf")
    asked = []

    def question(*args):
        asked.append(args[2])
        return QMessageBox.StandardButton.Yes

    monkeypatch.setattr(QMessageBox, "question", question)
    dlg = PluginsDialog(app)
    qtbot.addWidget(dlg)
    assert rows(dlg)[0] == ("my_bank_pdf", "own", "off: contains program code")
    item = dlg.table.item(row_of(dlg, "my_bank_pdf"), COL_NAME)
    assert item.checkState() == Qt.CheckState.Unchecked
    item.setCheckState(Qt.CheckState.Checked)
    dlg._save()
    assert "my_bank_pdf" in asked[0] and "trust" in asked[0]
    assert load_settings(app.settings.config_dir).app.plugins.enabled == ["my_bank_pdf"]
    s = app.settings
    reg = plugins.registry(s.plugins_dir, s.app.plugins.enabled)
    assert [i.name for i in reg.importers()][:1] == ["my_bank_pdf"]


def test_declining_the_question_saves_nothing(qtbot, app, monkeypatch):
    importer_plugin(app.settings.plugins_dir, "my_bank_pdf")
    monkeypatch.setattr(QMessageBox, "question", lambda *a: QMessageBox.StandardButton.No)
    dlg = PluginsDialog(app)
    qtbot.addWidget(dlg)
    dlg.table.item(row_of(dlg, "my_bank_pdf"), COL_NAME).setCheckState(Qt.CheckState.Checked)
    dlg._save()
    assert dlg.result() == 0  # still open
    assert load_settings(app.settings.config_dir).app.plugins.enabled == []


def test_a_broken_user_plugin_shows_its_error(qtbot, app):
    write_plugin(
        app.settings.plugins_dir, "broken_csv", "name: broken_csv\nversion: 1\napi: 2\nformat: []\n"
    )
    dlg = PluginsDialog(app)
    qtbot.addWidget(dlg)
    name, origin, status = rows(dlg)[0]
    assert (name, origin) == ("broken_csv", "own")
    assert status.startswith("error: plugin.yaml") and "format" in status  # misspelt
    dlg.table.setCurrentCell(0, COL_NAME)
    assert str(app.settings.plugins_dir / "broken_csv") in dlg.detail.text()


def test_import_dialog_names_plugins_that_are_left_out(qtbot, app):
    from hiro_budget.gui.import_dialog import ImportDialog

    importer_plugin(app.settings.plugins_dir, "my_bank_pdf")
    write_plugin(app.settings.plugins_dir, "broken_csv", "name: other\nversion: '1'\napi: 2\n")
    dlg = ImportDialog(app)
    qtbot.addWidget(dlg)
    text = dlg.plugin_problems.text()
    assert not dlg.plugin_problems.isHidden()
    assert "Plugin broken_csv is left out: name 'other' differs from its folder" in text
    assert "Your plugins with program code are off: my_bank_pdf" in text


def test_import_dialog_without_problems_shows_no_notice(qtbot, app):
    from hiro_budget.gui.import_dialog import ImportDialog

    dlg = ImportDialog(app)
    qtbot.addWidget(dlg)
    assert dlg.plugin_problems.isHidden()


def test_read_again_makes_a_new_plugin_usable_without_restart(qtbot, app):
    from hiro_budget.services import import_service

    import_service.plugin_importers(app.settings)  # the import has read the plugins
    write_plugin(
        app.settings.plugins_dir,
        "my_bank_csv",
        "name: my_bank_csv\nversion: 1\napi: 2\nformats: [format.csv.yaml]\n",
        {
            "format.csv.yaml": "name: My Bank\ndetect: {header_has: [Tag, Betrag]}\n"
            "account: {name: My Bank}\ncolumns: {booking_date: Tag, amount: Betrag}\n"
        },
    )
    assert "csv:My Bank" not in [i.name for i in import_service.plugin_importers(app.settings)]
    dlg = PluginsDialog(app)
    qtbot.addWidget(dlg)
    dlg._reload()
    assert ("my_bank_csv", "own", "active") in rows(dlg)
    assert "csv:My Bank" in [i.name for i in import_service.plugin_importers(app.settings)]
