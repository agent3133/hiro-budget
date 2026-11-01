"""The app icon, the "budget ring": the budget as a ring split into categories.

Three drawings for the sizes: large on parchment with the € sign (desktop, About), from
40 px down on the dark ground without it (taskbar), and at 16–20 px with a thicker ring
(title bars of the dialogs, the sidebar).
"""

from __future__ import annotations

import sys
from functools import cache
from pathlib import Path

from PySide6.QtCore import QRectF, QSize, Qt
from PySide6.QtGui import QIcon, QPainter, QPixmap
from PySide6.QtSvg import QSvgRenderer

ICON_DIR = Path(__file__).resolve().parent / "icons"
SIZES = (16, 20, 24, 32, 40, 48, 64, 128, 256)
APP_ID = "hirobudget.app"


def drawing(size: int) -> Path:
    """The SVG drawn for a pixel size."""
    if size <= 20:
        return ICON_DIR / "app-16.svg"
    if size <= 40:
        return ICON_DIR / "app-32.svg"
    return ICON_DIR / "app.svg"


def render(size: int) -> QPixmap:
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    QSvgRenderer(str(drawing(size))).render(painter, QRectF(0, 0, size, size))
    painter.end()
    return pixmap


@cache
def app_icon() -> QIcon:
    """All sizes in one icon; Qt picks the one closest to what it shows."""
    icon = QIcon()
    for size in SIZES:
        icon.addPixmap(render(size))
    return icon


def ring_pixmap(size: int, ratio: float) -> QPixmap:
    """The small ring for a label, sharp on scaled screens."""
    pixmap = app_icon().pixmap(QSize(size, size), ratio)
    pixmap.setDevicePixelRatio(ratio)
    return pixmap


def set_app_id() -> None:
    """Own taskbar entry and icon on Windows (else it is Python's)."""
    if sys.platform != "win32":
        return
    import ctypes

    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_ID)
    except (AttributeError, OSError):
        pass
