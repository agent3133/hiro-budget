"""Help > User guide: the workflows guide (docs/workflows.<language>.md) in a window.

The guide ships with the app: as package data in ``hiro_budget/guide/`` (wheel and exe,
see pyproject.toml and packaging/build_exe.py); from a source checkout it is read from
``docs/``. Links between the Markdown files open in the same window, web links in the
browser.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QUrl
from PySide6.QtGui import QImage, QPixmap, QTextCursor, QTextDocument
from PySide6.QtWidgets import QDialog, QDialogButtonBox, QTextBrowser, QVBoxLayout

from hiro_budget.gui.common import fit_to_screen

PACKAGE = Path(__file__).resolve().parents[1]
#: where the guide is looked for: shipped with the package, then the source checkout
FOLDERS = (PACKAGE / "guide", PACKAGE.parents[1] / "docs")


def guide_file(language: str) -> Path | None:
    """The guide in ``language`` (else English, else German), None if not found."""
    for lang in (language, "en", "de"):
        for folder in FOLDERS:
            path = folder / f"workflows.{lang}.md"
            if path.is_file():
                return path
    return None


class GuideBrowser(QTextBrowser):
    """Screenshots at most as wide as the text: Qt's Markdown has no image width (inline
    HTML is dropped), and the shots are wider than the window (1782 px at 125 %)."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.sourceChanged.connect(self.fit_images)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self.fit_images()

    def _natural_size(self, name: str) -> tuple[float, float] | None:
        """An image's logical size: taken at 125 %, it is shown at its size in the app."""
        resource = self.document().resource(QTextDocument.ResourceType.ImageResource, QUrl(name))
        if isinstance(resource, QPixmap):
            resource = resource.toImage()
        elif not isinstance(resource, QImage):
            resource = QImage.fromData(resource) if resource else QImage()
        if resource.isNull():
            return None
        ratio = self.devicePixelRatioF() or 1.0
        return resource.width() / ratio, resource.height() / ratio

    def _indent(self, block) -> float:
        """How far a block is indented: images in a list item have less room."""
        doc = self.document()
        fmt = block.blockFormat()
        indent = fmt.leftMargin() + fmt.indent() * doc.indentWidth()
        if block.textList() is not None:
            indent += block.textList().format().indent() * doc.indentWidth()
        return indent

    def fit_images(self) -> None:
        doc = self.document()
        room = self.viewport().width() - 2 * doc.documentMargin() - 8
        if room <= 0:
            return
        images = []  # (position, length, format, room): collected first, then changed
        block = doc.begin()
        while block.isValid():
            indent = self._indent(block)
            it = block.begin()
            while not it.atEnd():
                fragment = it.fragment()
                if fragment.charFormat().isImageFormat():
                    images.append(
                        (fragment.position(), fragment.length(), fragment.charFormat(), indent)
                    )
                it += 1
            block = block.next()
        for position, length, char_format, indent in images:
            image = char_format.toImageFormat()
            size = self._natural_size(image.name())
            if size is None or room - indent <= 0:
                continue
            width = min(size[0], room - indent)
            if abs(image.width() - width) < 1:
                continue
            image.setWidth(width)
            image.setHeight(size[1] * width / size[0])
            cursor = QTextCursor(doc)
            cursor.setPosition(position)
            cursor.setPosition(position + length, QTextCursor.MoveMode.KeepAnchor)
            cursor.setCharFormat(image)


class GuideWindow(QDialog):
    """A window beside the app, not blocking it."""

    def __init__(self, language: str, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle(self.tr("User guide"))
        self.setModal(False)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 12)
        self.browser = GuideBrowser()
        self.browser.setOpenExternalLinks(True)
        self.browser.setFrameShape(QTextBrowser.Shape.NoFrame)
        self.browser.document().setDocumentMargin(16)
        layout.addWidget(self.browser)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.close)
        buttons.setContentsMargins(0, 0, 12, 0)
        layout.addWidget(buttons)
        path = guide_file(language)
        if path is None:
            self.browser.setPlainText(self.tr("The user guide was not found."))
        else:
            self.browser.setSource(QUrl.fromLocalFile(str(path)))  # .md: read as Markdown
        fit_to_screen(self, 860, 820)
