"""GUI bootstrap: QApplication, translations, main window."""

from __future__ import annotations

import logging
import sys
from pathlib import Path

from PySide6.QtCore import QCoreApplication, QLibraryInfo, QLocale, QTranslator
from PySide6.QtWidgets import QApplication, QMessageBox

from hiro_budget import logfile
from hiro_budget.config import Settings
from hiro_budget.gui import common
from hiro_budget.gui.app_icon import app_icon, set_app_id
from hiro_budget.gui.theme import apply_theme

I18N_DIR = Path(__file__).resolve().parent.parent / "i18n"

log = logging.getLogger(__name__)
_in_hook = False


def excepthook(kind, error, tb) -> None:
    """An exception no slot caught: into the log with its traceback, and a short message
    instead of only a line on a console the exe started from Explorer doesn't have (#79)."""
    global _in_hook
    if issubclass(kind, KeyboardInterrupt):
        sys.__excepthook__(kind, error, tb)
        return
    log.critical("unexpected error", exc_info=(kind, error, tb))
    if _in_hook or QApplication.instance() is None:
        return  # the message box itself failed, or no GUI yet
    _in_hook = True
    try:
        where = logfile.log_path()
        text = QCoreApplication.translate("Main", "An unexpected error occurred:\n%1").replace(
            "%1", str(error) or kind.__name__
        )
        if where is not None:
            text += "\n\n" + QCoreApplication.translate(
                "Main", "Details are in the log file:\n%1"
            ).replace("%1", str(where))
        QMessageBox.critical(None, QCoreApplication.translate("Main", "Unexpected error"), text)
    finally:
        _in_hook = False


def install_translators(qapp: QApplication, language: str) -> list[QTranslator]:
    """Load the app translation plus Qt's own (standard dialogs, buttons)."""
    translators = []
    app_tr = QTranslator(qapp)
    if app_tr.load(str(I18N_DIR / f"app_{language}.qm")):
        qapp.installTranslator(app_tr)
        translators.append(app_tr)
    qt_tr = QTranslator(qapp)
    qt_dir = QLibraryInfo.path(QLibraryInfo.LibraryPath.TranslationsPath)
    if qt_tr.load(f"qtbase_{language}", qt_dir):
        qapp.installTranslator(qt_tr)
        translators.append(qt_tr)
    QLocale.setDefault(QLocale(language))
    return translators


def run_gui(settings: Settings) -> int:
    from hiro_budget.app import App
    from hiro_budget.gui.main_window import MainWindow

    set_app_id()  # before the first window
    qapp = QApplication.instance() or QApplication(sys.argv)
    qapp.setApplicationName("Hiro Budget")
    qapp.setWindowIcon(app_icon())  # taskbar, Alt+Tab, dialogs, About
    apply_theme(qapp, settings.app.theme)  # Windows 11 look, light/dark mode, accent
    language = settings.app.language
    common.set_language(language)
    install_translators(qapp, language)

    from hiro_budget.gui.data_folder_dialog import ensure_data_folder, finish_restart

    if not ensure_data_folder(settings):  # a data folder in the cloud may be offline
        return 1
    logfile.open_log_file(settings)
    sys.excepthook = excepthook
    try:
        app = App(settings)
    except Exception as e:
        QMessageBox.critical(None, "Error", f"Cannot open database:\n{e}")
        return 1
    window = MainWindow(app)
    window.show()
    code = qapp.exec()
    if app.restart is not None:  # the data folder was moved
        finish_restart(app, app.restart)
    return code
