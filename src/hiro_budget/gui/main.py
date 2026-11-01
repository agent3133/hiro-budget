"""GUI bootstrap: QApplication, translations, main window."""

from __future__ import annotations

import sys
from pathlib import Path

from PySide6.QtCore import QLibraryInfo, QLocale, QTranslator
from PySide6.QtWidgets import QApplication, QMessageBox

from hiro_budget.config import Settings
from hiro_budget.gui import common
from hiro_budget.gui.app_icon import app_icon, set_app_id
from hiro_budget.gui.theme import apply_theme

I18N_DIR = Path(__file__).resolve().parent.parent / "i18n"


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

    try:
        app = App(settings)
    except Exception as e:
        QMessageBox.critical(None, "Error", f"Cannot open database:\n{e}")
        return 1
    window = MainWindow(app)
    window.show()
    return qapp.exec()
