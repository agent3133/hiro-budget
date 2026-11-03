"""Data folder (#29): settings, moving database/classifier/backups/view state, config."""

import shutil
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from hiro_budget.config import load_settings
from hiro_budget.services import backup, config_edit
from hiro_budget.services import data_folder as df
from hiro_budget.services.data_folder import DataFolderError


def counts(db: Path) -> dict[str, int]:
    with closing(sqlite3.connect(db)) as con:
        names = [
            r[0]
            for r in con.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
        ]
        return {n: con.execute(f'SELECT COUNT(*) FROM "{n}"').fetchone()[0] for n in names}


@pytest.fixture
def full_app(app):
    """An app with all four kinds of data files."""
    s = app.settings
    s.model_path.parent.mkdir(parents=True, exist_ok=True)
    s.model_path.write_bytes(b"model" * 100)
    app.ui_state.path.write_text('{"page": 2}', encoding="utf-8")
    backup.create_backup(s, "manual")
    backup.create_backup(s, "import")
    return app


# -- settings -------------------------------------------------------------------------


def test_without_data_folder_everything_stays_as_before(config_dir) -> None:
    s = load_settings(config_dir)
    root = config_dir.parent
    assert s.app.data_folder is None
    assert s.db_path == root / "data" / "test.db"
    assert s.model_path == root / "data" / "classifier.npz"
    assert s.data_dir == root / "data"
    assert backup.backup_dir(s) == root / "data" / "backups"


def test_data_folder_takes_the_file_names(config_dir, tmp_path) -> None:
    s = load_settings(config_dir)
    s.app.data_folder = str(tmp_path / "cloud")
    assert s.data_dir == tmp_path / "cloud"
    assert s.db_path == tmp_path / "cloud" / "test.db"
    assert s.model_path == tmp_path / "cloud" / "classifier.npz"
    s.app.data_folder = "elsewhere"  # relative: against the project root
    assert s.data_dir == config_dir.parent / "elsewhere"
    s.app.database.path = str(tmp_path / "abs" / "x.db")  # absolute stays absolute
    assert s.db_path == tmp_path / "abs" / "x.db"


def test_demo_config_has_no_data_folder() -> None:
    demo = load_settings(Path(__file__).parents[1] / "demo" / "config")
    assert demo.app.data_folder is None
    assert demo.db_path.parent == demo.data_dir


# -- moving ---------------------------------------------------------------------------


def test_move_to_an_empty_folder(full_app, tmp_path) -> None:
    s = full_app.settings
    old = df.data_files(s)
    before = counts(s.db_path)
    target = tmp_path / "OneDrive" / "Hiro Budget"
    steps = []
    result = df.move_data(s, target, steps.append)
    new = df.data_files(s, target)
    assert counts(new.database) == before and result.rows == sum(before.values()) > 0
    assert new.classifier.read_bytes() == old.classifier.read_bytes()
    assert new.ui_state.read_text(encoding="utf-8") == '{"page": 2}'
    assert result.backups == 2
    assert sorted(p.name for p in new.backups.iterdir()) == sorted(
        p.name for p in old.backups.iterdir()
    )
    assert set(result.old_files) == set(old.all())
    assert steps == ["database", "classifier.npz", "ui_state.json", "backups"]
    assert all(p.exists() for p in old.all())  # the old files stay until deleted
    assert load_settings(s.config_dir).app.data_folder is None  # config not touched


def test_only_existing_files_are_moved(app, tmp_path) -> None:
    result = df.move_data(app.settings, tmp_path / "new")
    assert result.old_files == [app.settings.db_path]  # no classifier or backups yet
    assert sorted(p.name for p in (tmp_path / "new").iterdir()) == ["test.db"]


def test_folder_with_a_database_is_recognized(full_app, tmp_path) -> None:
    s = full_app.settings
    other = tmp_path / "other"
    other.mkdir()
    shutil.copy2(s.db_path, other / s.db_path.name)
    assert df.holds_database(s, other)
    assert not df.holds_database(s, tmp_path)
    with pytest.raises(DataFolderError) as e:
        df.move_data(s, other)
    assert e.value.kind == "clash" and "test.db" in e.value.detail


@pytest.mark.parametrize(
    ("make", "kind"),
    [
        (lambda s, tmp: s.data_dir, "same"),
        (lambda s, tmp: s.data_dir / "sub", "inside"),
        (lambda s, tmp: (tmp / "file.txt", (tmp / "file.txt").write_text("x"))[0], "not_folder"),
    ],
)
def test_impossible_targets(app, tmp_path, make, kind) -> None:
    with pytest.raises(DataFolderError) as e:
        df.move_data(app.settings, make(app.settings, tmp_path))
    assert e.value.kind == kind


def test_unwritable_folder(app, tmp_path, monkeypatch) -> None:
    def refuse(self, data):
        raise PermissionError("Access denied")

    monkeypatch.setattr(Path, "write_bytes", refuse)
    with pytest.raises(DataFolderError) as e:
        df.move_data(app.settings, tmp_path / "locked")
    assert e.value.kind == "not_writable"


def test_disk_full(full_app, tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(df.shutil, "disk_usage", lambda p: shutil._ntuple_diskusage(1, 1, 10))
    with pytest.raises(DataFolderError) as e:
        df.move_data(full_app.settings, tmp_path / "small")
    assert e.value.kind == "no_space"


def test_a_failure_leaves_no_half_data_folder(full_app, tmp_path, monkeypatch) -> None:
    def broken(src, dst):
        Path(dst).mkdir()
        raise OSError("network folder went away")

    monkeypatch.setattr(df.shutil, "copytree", broken)
    target = tmp_path / "nas"
    with pytest.raises(DataFolderError) as e:
        df.move_data(full_app.settings, target)
    assert e.value.kind == "failed" and "went away" in e.value.detail
    assert list(target.iterdir()) == []  # database, classifier, ui_state removed again
    assert full_app.settings.db_path.exists()


def test_delete_files_removes_only_what_it_is_given(full_app) -> None:
    s = full_app.settings
    keep = s.data_dir / "my notes.txt"
    keep.write_text("mine", encoding="utf-8")
    old = df.data_files(s).existing()
    full_app.session_factory.kw["bind"].dispose()  # the app closed the database
    assert df.delete_files(old) == []
    assert not any(p.exists() for p in old)
    assert keep.exists()


# -- config.yaml ----------------------------------------------------------------------


def test_save_data_folder_round_trip(config_dir, tmp_path) -> None:
    s = load_settings(config_dir)
    text_before = (config_dir / "config.yaml").read_text(encoding="utf-8")
    config_edit.save_data_folder(s, tmp_path / "cloud")
    again = load_settings(config_dir)
    assert again.data_dir == tmp_path / "cloud"
    assert again.db_path == tmp_path / "cloud" / "test.db"
    assert again.app.database.path == "data/test.db"  # relative: kept, only the name counts
    config_edit.save_data_folder(again, None)
    assert load_settings(config_dir).app.data_folder is None
    assert (config_dir / "config.yaml").read_text(encoding="utf-8") == text_before


def test_absolute_paths_become_file_names(config_dir, tmp_path) -> None:
    cfg = config_dir / "config.yaml"
    old = (tmp_path / "old place").as_posix()
    text = cfg.read_text(encoding="utf-8")
    text = text.replace("path: data/test.db", f"path: {old}/my.db")
    text = text.replace("model_path: data/classifier.npz", f"model_path: {old}/m.npz")
    cfg.write_text(text, encoding="utf-8")
    s = load_settings(config_dir)
    assert s.db_path == tmp_path / "old place" / "my.db"
    config_edit.save_data_folder(s, tmp_path / "new")
    again = load_settings(config_dir)
    assert (again.app.database.path, again.app.ml.model_path) == ("my.db", "m.npz")
    assert again.db_path == tmp_path / "new" / "my.db"
    assert again.model_path == tmp_path / "new" / "m.npz"


def test_template_comments_survive(tmp_path) -> None:
    from hiro_budget.config import ensure_config_files

    cfg = tmp_path / "config"
    ensure_config_files(cfg)
    s = load_settings(cfg)
    config_edit.save_data_folder(s, tmp_path / "cloud")
    text = (cfg / "config.yaml").read_text(encoding="utf-8")
    assert text.startswith("# Main configuration.")
    assert "# UI language: de | en" in text
    assert load_settings(cfg).data_dir == tmp_path / "cloud"


def test_apply_restart_without_qt(app, tmp_path) -> None:
    """#78: the file work after a restart request is a service (testable without Qt)."""
    s = app.settings
    new = tmp_path / "new"
    df.move_data(s, new)
    old_files = df.data_files(s).existing()
    app.session_factory.kw["bind"].dispose()  # as finish_restart: release the database
    app.ui_state.path.write_text('{"written": "on close"}', encoding="utf-8")
    request = df.RestartRequest(
        delete=old_files,
        ui_state=(app.ui_state.path, new / "ui_state.json"),
        restore=tmp_path / "no such backup",
    )
    outcome = df.apply_restart(s, request)
    assert outcome.restore_error and outcome.delete_problems == []
    assert (new / "ui_state.json").read_text(encoding="utf-8") == '{"written": "on close"}'
    assert not any(p.exists() for p in old_files)
