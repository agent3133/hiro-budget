"""A fresh setup with the shipped templates works as it is."""

from hiro_budget.app import App
from hiro_budget.config import CONFIG_FILES, load_settings
from hiro_budget.services import import_service


def test_first_start_creates_the_config_and_imports(tmp_path, statement_pdf):
    cfg = tmp_path / "config"  # does not exist yet: a new user's first start
    app = App(load_settings(cfg))
    assert sorted(p.name for p in cfg.iterdir()) == sorted(CONFIG_FILES)
    with app.session() as s:
        result = import_service.import_file(s, app.settings, statement_pdf)
    assert result.added > 0


def test_existing_files_are_kept(tmp_path):
    cfg = tmp_path / "config"
    cfg.mkdir()
    (cfg / "rules.yaml").write_text("rules: []\n", encoding="utf-8")
    settings = load_settings(cfg)
    assert settings.rules.rules == []  # not replaced by the template
    assert (cfg / "config.yaml").is_file() and (cfg / "categories.yaml").is_file()


def test_a_broken_configuration_is_shown_in_a_message_box(tmp_path, monkeypatch):
    from PySide6.QtWidgets import QMessageBox

    from hiro_budget.__main__ import main

    cfg = tmp_path / "config"
    load_settings(cfg)
    (cfg / "config.yaml").write_text("language: xx\n", encoding="utf-8")
    shown = []
    monkeypatch.setattr(QMessageBox, "critical", lambda *a: shown.append(a[2]))
    assert main(["--config-dir", str(cfg)]) == 2  # the GUI start
    assert "config.yaml" in shown[0] and "language" in shown[0]
    shown.clear()
    assert main(["--config-dir", str(cfg), "train"]) == 2  # a command: the console only
    assert shown == []
