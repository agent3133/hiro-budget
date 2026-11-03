"""Help > User guide (#51): the workflows guide in the UI language, in a window."""

import pytest
from PySide6.QtCore import QUrl
from PySide6.QtWidgets import QMessageBox

from hiro_budget.gui import guide
from hiro_budget.gui.guide import GuideWindow
from hiro_budget.gui.main_window import MainWindow


@pytest.fixture(autouse=True)
def no_message_boxes(monkeypatch):
    for name in ("information", "warning", "critical"):
        monkeypatch.setattr(QMessageBox, name, lambda *a, **k: QMessageBox.StandardButton.Ok)
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.No)


@pytest.mark.parametrize(
    ("language", "heading"),
    [("de", "Arbeiten mit der App"), ("en", "Working with the app")],
)
def test_guide_in_the_ui_language(qtbot, language, heading) -> None:
    w = GuideWindow(language)
    qtbot.addWidget(w)
    assert w.browser.source().fileName() == f"workflows.{language}.md"
    assert w.browser.toPlainText().startswith(heading)
    assert not w.isModal()


def test_links_between_the_guides_stay_in_the_window(qtbot) -> None:
    w = GuideWindow("de")
    qtbot.addWidget(w)
    w.browser.setSource(QUrl("workflows.en.md"))  # as a click on "English: …" does
    assert w.browser.toPlainText().startswith("Working with the app")


def test_unknown_language_falls_back_to_english() -> None:
    assert guide.guide_file("fr").name == "workflows.en.md"


def test_help_menu_opens_one_guide_window(qtbot, app) -> None:
    window = MainWindow(app)
    qtbot.addWidget(window)
    action = next(a for menu in window._menus for a in menu.actions() if a.text() == "&User guide")
    action.trigger()
    first = window._guide_window
    assert first.isVisible()
    action.trigger()
    assert window._guide_window is first
    first.close()


def _images(browser) -> list:
    """The image formats in the guide's document."""
    found, block = [], browser.document().begin()
    while block.isValid():
        it = block.begin()
        while not it.atEnd():
            char_format = it.fragment().charFormat()
            if char_format.isImageFormat():
                found.append(char_format.toImageFormat())
            it += 1
        block = block.next()
    return found


@pytest.mark.parametrize("language", ["de", "en"])
def test_screenshots_are_found_and_fit_the_window(qtbot, language) -> None:
    """The guide's screenshots (#63) load, and none is wider than the text: Qt's Markdown
    shows an image at its own size, 1782 px for a page taken at 125 %."""
    w = GuideWindow(language)
    qtbot.addWidget(w)
    w.resize(860, 820)
    w.show()
    qtbot.waitExposed(w)
    images = _images(w.browser)
    assert len(images) >= 10
    for image in images:
        assert f"images/workflows/{language}/" in image.name()
        assert 0 < image.width() < w.browser.viewport().width()
    assert w.browser.horizontalScrollBar().maximum() == 0
