"""Settings > Data folder… (#29): show the folder, move database, classifier, backups and
view state to another one (then restart), or open a data folder that exists already. Also
the start-up question when a configured data folder is not reachable."""

from __future__ import annotations

import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QCoreApplication, Qt, QTimer, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from hiro_budget import logfile
from hiro_budget.app import App
from hiro_budget.config import CONFIG_DIR_ENV, ConfigError, Settings
from hiro_budget.gui.common import busy, fmt_number, show_error
from hiro_budget.gui.widgets import caption
from hiro_budget.services import config_edit
from hiro_budget.services import data_folder as df
from hiro_budget.services.data_folder import DataFolderError, RestartRequest


def default_dir(settings: Settings) -> Path:
    """Where the data is without ``data_folder``: the folder of database.path."""
    return settings.resolve(settings.app.database.path).parent


def _megabytes(size: int) -> str:
    return fmt_number(size / 1e6, 1, 1)


class DataFolderDialog(QDialog):
    def __init__(self, app: App, window, parent: QWidget | None = None) -> None:
        super().__init__(parent or window)
        self.app = app
        self.window_ = window  # the main window: unsaved changes, restart
        self.setWindowTitle(self.tr("Data folder"))
        layout = QVBoxLayout(self)
        layout.addWidget(caption(self.tr("Data folder")))
        self.path = QLabel()
        self.path.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.path.setWordWrap(True)
        layout.addWidget(self.path)
        self.contents = QLabel()
        self.contents.setWordWrap(True)
        layout.addWidget(self.contents)
        note = QLabel(
            self.tr(
                "Database, classifier, backups and view settings are kept together in this "
                "folder. Put it in a OneDrive, Nextcloud or NAS folder to have it backed up "
                "with your other files. The configuration stays in %1."
            ).replace("%1", str(app.settings.config_dir))
        )
        note.setWordWrap(True)
        layout.addWidget(note)
        row = QHBoxLayout()
        self.open_button = QPushButton(self.tr("Open in Explorer"))
        self.open_button.clicked.connect(self._open)
        self.change_button = QPushButton(self.tr("Change…"))
        self.change_button.clicked.connect(lambda: self.change())
        for button in (self.open_button, self.change_button):
            button.setAutoDefault(False)  # Enter does not open Explorer
        row.addWidget(self.open_button)
        row.addWidget(self.change_button)
        row.addStretch(1)
        layout.addLayout(row)
        layout.addStretch(1)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.resize(560, 260)
        self._fill()

    def _fill(self) -> None:
        s = self.app.settings
        self.path.setText(f"<b>{s.data_dir}</b>")
        files = df.data_files(s)
        backups = (
            len([p for p in files.backups.iterdir() if p.is_dir()]) if files.backups.is_dir() else 0
        )
        size = sum(df.size_of(p) for p in files.existing())
        self.contents.setText(
            self.tr("Database %1, %n backup(s), %2 MB in all", "", backups)
            .replace("%1", s.db_path.name)
            .replace("%2", _megabytes(size))
        )

    def _open(self) -> None:
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.app.settings.data_dir)))

    def message(self, e: DataFolderError) -> str:
        texts = {
            "same": self.tr("This is already the data folder."),
            "inside": self.tr("The new folder must not be inside the current data folder."),
            "not_folder": self.tr("%1 is not a folder."),
            "clash": self.tr(
                "The folder already holds files of the same names (%1). Choose an empty "
                "folder, or a folder with a Hiro Budget database to open it."
            ),
            "not_writable": self.tr("The folder cannot be written to (%1)."),
            "no_space": self.tr("Not enough free space (needed / free: %1)."),
            "damaged": self.tr("The copy of the database is damaged. Nothing was changed."),
            "differs": self.tr("The copy of %1 differs from the original. Nothing was changed."),
            "failed": self.tr("Copying failed (%1). Nothing was changed."),
        }
        return texts.get(e.kind, str(e)).replace("%1", e.detail)

    def change(self, folder: Path | None = None) -> bool:
        """Choose a folder and move the data there (or open the database in it); True when
        the app restarts."""
        s = self.app.settings
        if folder is None:
            chosen = QFileDialog.getExistingDirectory(
                self, self.tr("Choose the data folder"), str(s.data_dir)
            )
            if not chosen:
                return False
            folder = Path(chosen)
        folder = folder.resolve()
        if folder != s.data_dir.resolve() and df.holds_database(s, folder):
            return self._open_existing(folder)
        if self.window_.has_unsaved_changes():
            QMessageBox.information(
                self,
                self.tr("Data folder"),
                self.tr("Save or revert the unsaved changes first."),
            )
            return False
        size = sum(df.size_of(p) for p in df.data_files(s).existing())
        answer = QMessageBox.question(
            self,
            self.tr("Move the data"),
            self.tr(
                "Copy database, classifier and backups (%1 MB) to\n%2?\n\n"
                "The copies are checked; then the app restarts and uses the new folder."
            )
            .replace("%1", _megabytes(size))
            .replace("%2", str(folder)),
        )
        if answer != QMessageBox.StandardButton.Yes:
            return False
        try:
            with busy(self.window_.show_status, self.tr("Copying the data…")):
                result = df.move_data(s, folder)
        except DataFolderError as e:
            show_error(self, self.tr("Move the data"), self.message(e))
            return False
        old_dir = s.data_dir
        try:
            config_edit.save_data_folder(s, None if folder == default_dir(s) else folder)
        except (ConfigError, OSError) as e:
            df.delete_files(df.data_files(s, folder).existing())  # back to the old folder
            show_error(self, self.tr("Move the data"), e)
            return False
        delete = ask_restart_after_move(self, result, old_dir)
        self._restart(
            RestartRequest(
                delete=result.old_files if delete else [],
                ui_state=(old_dir / df.UI_STATE, folder / df.UI_STATE),
            )
        )
        return True

    def _open_existing(self, folder: Path) -> bool:
        s = self.app.settings
        db = folder / s.db_path.name
        changed = datetime.fromtimestamp(db.stat().st_mtime).strftime("%d.%m.%Y %H:%M")
        answer = QMessageBox.question(
            self,
            self.tr("Open a data folder"),
            self.tr(
                "This folder already holds a Hiro Budget database (%1, changed %2).\n\n"
                "Use it instead of the current one? The data in the current folder stays "
                "where it is. The app restarts."
            )
            .replace("%1", db.name)
            .replace("%2", changed),
        )
        if answer != QMessageBox.StandardButton.Yes:
            return False
        try:
            config_edit.save_data_folder(s, None if folder == default_dir(s) else folder)
        except (ConfigError, OSError) as e:
            show_error(self, self.tr("Open a data folder"), e)
            return False
        self._restart(RestartRequest(delete=[], ui_state=None))
        return True

    def _restart(self, request: RestartRequest) -> None:
        """Close this dialog, then the window (from the main event loop, not this one)."""
        self.accept()
        QTimer.singleShot(0, lambda: self.window_.restart(request))


def ask_restart_after_move(parent: QWidget, result: df.MoveResult, old_dir: Path) -> bool:
    """The move is done: say so, offer to delete the old files; True to delete them."""
    box = QMessageBox(parent)
    box.setIcon(QMessageBox.Icon.Information)
    box.setWindowTitle(QCoreApplication.translate("DataFolder", "Data moved"))
    box.setText(
        QCoreApplication.translate(
            "DataFolder",
            "Copied and checked: the database (%1 rows), the classifier, %2 backups and the "
            "view settings.\n\nThe app restarts now and uses\n%3",
        )
        .replace("%1", fmt_number(result.rows))
        .replace("%2", str(result.backups))
        .replace("%3", str(result.target))
    )
    check = QCheckBox(
        QCoreApplication.translate("DataFolder", "Delete the old files in %1").replace(
            "%1", str(old_dir)
        )
    )
    box.setCheckBox(check)
    box.addButton(
        QCoreApplication.translate("DataFolder", "Restart now"),
        QMessageBox.ButtonRole.AcceptRole,
    )
    box.exec()
    return check.isChecked()


# -- restart and start-up -----------------------------------------------------------------


def restart_command() -> list[str]:
    """The command that starts this app again: the exe, or Python with the package."""
    if "__compiled__" in globals():  # Nuitka build
        return [str(Path(sys.argv[0]).resolve())]
    return [sys.executable, "-m", "hiro_budget"]


def finish_restart(app: App, request: RestartRequest) -> None:
    """After the window closed: release the database, carry over the view state written on
    closing, delete the old files if asked, start the app again on the same configuration."""
    app.session_factory.kw["bind"].dispose()
    logfile.close_log_file()  # the new process writes the log (maybe in another folder)
    outcome = df.apply_restart(app.settings, request)
    if outcome.restore_error:
        QMessageBox.critical(
            None,
            QCoreApplication.translate("DataFolder", "Restore backup"),
            QCoreApplication.translate(
                "DataFolder", "The backup could not be restored:\n%1"
            ).replace("%1", outcome.restore_error),
        )
    if outcome.delete_problems:
        QMessageBox.warning(
            None,
            QCoreApplication.translate("DataFolder", "Data folder"),
            QCoreApplication.translate(
                "DataFolder", "Some old files could not be deleted:\n%1"
            ).replace("%1", "\n".join(outcome.delete_problems)),
        )
    env = dict(os.environ)
    env[CONFIG_DIR_ENV] = str(app.settings.config_dir)  # also keeps a demo's copy
    flags = 0
    if sys.platform == "win32":
        flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    subprocess.Popen(restart_command(), env=env, cwd=os.getcwd(), creationflags=flags)


def ask_missing(settings: Settings) -> str:
    """The data folder (or its database) is not there: "retry", "choose" or "quit"."""
    box = QMessageBox()
    box.setIcon(QMessageBox.Icon.Warning)
    box.setWindowTitle(QCoreApplication.translate("DataFolder", "Data folder not available"))
    box.setText(
        QCoreApplication.translate(
            "DataFolder",
            "The data folder\n%1\nor the database %2 in it cannot be found.\n\n"
            "If it is in a cloud folder or on a network drive, make sure it is available "
            "and retry, or choose the folder where the data is now.",
        )
        .replace("%1", str(settings.data_dir))
        .replace("%2", settings.db_path.name)
    )
    retry = box.addButton(
        QCoreApplication.translate("DataFolder", "Retry"), QMessageBox.ButtonRole.AcceptRole
    )
    choose = box.addButton(
        QCoreApplication.translate("DataFolder", "Choose folder…"),
        QMessageBox.ButtonRole.ActionRole,
    )
    box.addButton(
        QCoreApplication.translate("DataFolder", "Quit"), QMessageBox.ButtonRole.RejectRole
    )
    box.exec()
    clicked = box.clickedButton()
    return "retry" if clicked is retry else "choose" if clicked is choose else "quit"


def choose_folder(settings: Settings) -> Path | None:
    chosen = QFileDialog.getExistingDirectory(
        None,
        QCoreApplication.translate("DataFolder", "Choose the data folder"),
        str(settings.data_dir.parent if settings.data_dir.parent.exists() else Path.home()),
    )
    return Path(chosen) if chosen else None


def ensure_data_folder(settings: Settings) -> bool:
    """At start: a configured data folder must hold the database; never create a new empty
    one there silently. False: the user quit."""
    while settings.app.data_folder and not settings.db_path.is_file():
        answer = ask_missing(settings)
        if answer == "quit":
            return False
        if answer == "retry":
            continue
        folder = choose_folder(settings)
        if folder is None:
            continue
        if not df.holds_database(settings, folder):
            QMessageBox.information(
                None,
                QCoreApplication.translate("DataFolder", "Data folder"),
                QCoreApplication.translate(
                    "DataFolder", "There is no Hiro Budget database (%1) in this folder."
                ).replace("%1", settings.db_path.name),
            )
            continue
        folder = folder.resolve()
        try:
            config_edit.save_data_folder(
                settings, None if folder == default_dir(settings) else folder
            )
        except (ConfigError, OSError) as e:
            show_error(None, QCoreApplication.translate("DataFolder", "Data folder"), e)
            return False
    return True
