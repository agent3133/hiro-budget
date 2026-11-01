"""Application look.

- "system", "light", "dark": Qt's Windows 11 style; light/dark follows Windows or the
  setting, the accent colour follows Windows. The stylesheet only covers group boxes
  (rounded cards with the title above) and tab bars (accent-coloured bar under the
  selected tab); everything else is drawn by the native style.
- "obsidian": the warm "Obsidian" design (cream ground, aubergine ink, violet accent,
  pastel page colours) on the Fusion style, which follows a custom palette; buttons,
  fields and tables get rounded shapes from a stylesheet. Views that draw their own
  colours (budget pills, charts, the sidebar) ask ``tokens()``.
- "obsidian_dark": the same design on dark grounds (its own token set).
"""

from __future__ import annotations

import ctypes
import sys
import tempfile
from pathlib import Path

from PySide6.QtCore import QEvent, QObject, Qt
from PySide6.QtGui import QColor, QFont, QFontDatabase, QPalette
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QDoubleSpinBox,
    QSpinBox,
    QStyledItemDelegate,
    QStyleFactory,
    QWidget,
)

SCHEMES = {
    "system": Qt.ColorScheme.Unknown,  # follow Windows
    "light": Qt.ColorScheme.Light,
    "dark": Qt.ColorScheme.Dark,
    "obsidian": Qt.ColorScheme.Light,
    "obsidian_dark": Qt.ColorScheme.Dark,
}

# The "Obsidian" design (Claude Design canvas "Familien-Finanzanalyse – Obsidian Theme")
OBSIDIAN = {
    "window": "#F1E9DF",  # page ground
    "panel": "#E8DFD3",  # sidebar, buttons, table headers
    "card": "#EDE4D9",
    "input": "#F6F0E8",  # fields, tables, chart cards
    "group_row": "#EBE2D7",
    "border": "#DCD1C3",
    "line": "#E1D7CA",
    "text": "#3D3654",
    "muted": "#625A75",
    "heading": "#4B3F72",
    "accent": "#5B4F8C",
    "accent_soft": "#CEC1D4",
    "link": "#3F7478",
    "pos": "#D5E3D6",
    "pos_text": "#2D5A43",
    "neg": "#F0D0CB",
    "neg_text": "#8A3329",
    "warn": "#F3DEB5",
    "warn_text": "#74501A",
    "notice": "#F4DFBE",
    "notice_text": "#6B4916",
    "info": "#D3E2E1",
    "info_text": "#2C5558",
    "disabled": "#A39BB3",
    "on_accent": "#FFFFFF",  # text on accent-filled buttons
    "bevel_light": "#FFFFFF",  # palette Light/Dark (frames drawn by the style)
    "bevel_dark": "#B9AE9F",
    "income": "#3F7478",
    "expense": "#E3A35A",
    # one per page of the sidebar, and repeated for the budget groups
    "pages": ["#E4B9B4", "#CEC1D4", "#EFCB9E", "#ADC4C3", "#9FB3B6"],
}

# Obsidian at night: the same aubergine character on dark grounds, light ink, a lighter
# violet accent and muted versions of the pastel page colours.
OBSIDIAN_DARK = {
    "window": "#1E1B25",
    "panel": "#26222E",
    "card": "#2A2633",
    "input": "#211E29",
    "group_row": "#2A2633",
    "border": "#3A3547",
    "line": "#322E3D",
    "text": "#E7E2EE",
    "muted": "#A9A1B8",
    "heading": "#D2C6F2",
    "accent": "#9384D6",
    "accent_soft": "#463D66",
    "link": "#7FBCC0",
    "pos": "#233D2D",
    "pos_text": "#9DD5AF",
    "neg": "#4A2723",
    "neg_text": "#F2A99E",
    "warn": "#47391C",
    "warn_text": "#F1D08F",
    "notice": "#433520",
    "notice_text": "#EFD3A2",
    "info": "#1F393A",
    "info_text": "#A7D4D6",
    "disabled": "#6E6780",
    "on_accent": "#16131C",
    "bevel_light": "#3A3547",
    "bevel_dark": "#141118",
    "income": "#6FB5BA",
    "expense": "#E9A96A",
    "pages": ["#5B3F46", "#4B4159", "#5D4B34", "#3F5453", "#3B4B4E"],
}

STYLE = """
QGroupBox {
    border: 1px solid rgba(128, 128, 128, 0.30);
    border-radius: 8px;
    margin-top: 1.4em;
    padding: 10px 8px 8px 8px;
}
/* Tabs as in Windows 11 apps: no frames, an accent-coloured bar under the selected tab */
QTabWidget::pane {
    border: none;
}
QTabBar::tab {
    background: transparent;
    border: none;
    border-bottom: 3px solid transparent;
    padding: 6px 12px 4px 12px;
    margin-right: 2px;
    color: palette(text);
}
QTabBar::tab:hover {
    background: rgba(128, 128, 128, 0.12);
    border-top-left-radius: 4px;
    border-top-right-radius: 4px;
}
QTabBar::tab:selected {
    border-bottom: 3px solid palette(highlight);
}
QGroupBox::title {
    subcontrol-origin: margin;
    subcontrol-position: top left;
    left: 2px;
    padding: 0 2px;
    font-weight: 600;
}
/* page building blocks (widgets.py) */
QFrame#detailCard {
    border: 1px solid rgba(128, 128, 128, 0.30);
    border-radius: 8px;
}
QLabel[banner] {
    border-radius: 8px; padding: 8px 12px;
    background: rgba(128, 128, 128, 0.12);
}
QLabel[banner="success"] { background: rgba(46, 158, 91, 0.16); }
QLabel[banner="warning"] { background: rgba(232, 163, 61, 0.22); }
QLabel[banner="error"] { background: rgba(217, 83, 79, 0.18); }
QFrame#dateRangePopup { border: 1px solid rgba(128, 128, 128, 0.45); }
"""

OBSIDIAN_STYLE = """
QPushButton {
    background: {panel}; color: {text};
    border: 1px solid {border}; border-radius: 8px;
    padding: 5px 12px; min-height: 22px;
}
QPushButton:hover { background: {line}; }
QPushButton:pressed { background: {border}; }
QPushButton:default {
    background: {accent}; color: {on_accent}; border-color: {accent}; font-weight: 600;
}
QPushButton:disabled { color: {disabled}; background: {card}; border-color: {line}; }
QLineEdit, QComboBox, QDateEdit, QSpinBox, QDoubleSpinBox, QTextBrowser, QPlainTextEdit {
    background: {input}; color: {text};
    border: 1px solid {border}; border-radius: 8px; padding: 4px 8px;
    selection-background-color: {accent_soft}; selection-color: {text};
}
QLineEdit:focus, QComboBox:focus, QDateEdit:focus, QSpinBox:focus, QDoubleSpinBox:focus {
    border-color: {accent};
}
QTableView, QTreeView, QListView {
    background: {input}; alternate-background-color: {card};
    border: 1px solid {border}; border-radius: 4px;
    selection-background-color: {accent_soft}; selection-color: {text};
}
QHeaderView::section {
    background: {panel}; color: {muted}; font-weight: 500;
    border: none; border-bottom: 1px solid {border}; border-right: 1px solid {line};
    padding: 6px 8px;
}
/* top and bottom strips in the sidebar colour, as Obsidian's tab strip */
QMenuBar {
    background: {panel}; color: {text};
    border-bottom: 1px solid {border}; padding: 3px 4px;
}
QMenuBar::item:selected { background: {line}; border-radius: 4px; }
QMenu { background: {input}; color: {text}; border: 1px solid {border}; padding: 4px; }
QMenu::item { padding: 5px 24px 5px 20px; border-radius: 4px; }
QMenu::item:selected { background: {accent_soft}; }
QToolTip { background: {text}; color: {window}; border: none; padding: 4px 6px; }
QStatusBar { background: {panel}; color: {muted}; border-top: 1px solid {border}; }
QGroupBox { border-color: {border}; }
QGroupBox::title { color: {heading}; }
/* Tabs as in Obsidian: a darker strip; the selected tab is a card with rounded top
   corners that runs into the page below */
QTabBar { background: {panel}; }
QTabBar::tab {
    color: {muted};
    background: transparent;
    border: none;
    border-top-left-radius: 10px;
    border-top-right-radius: 10px;
    padding: 8px 22px 8px 22px;
    margin: 6px 2px 0 2px;
}
QTabBar::tab:hover { background: {line}; }
QTabBar::tab:selected {
    background: {window};
    color: {text};
    border: 1px solid {border};
    border-bottom: none;
}
QTabBar::tab:first { margin-left: 8px; }
QCheckBox::indicator {
    width: 13px; height: 13px;
    background: {input}; border: 1px solid {muted}; border-radius: 3px;
}
QCheckBox::indicator:checked {
    background: {accent}; border: 1px solid {accent}; border-radius: 3px;
    image: url({check});
}
/* Number fields: amounts without arrow buttons (typing, the arrow keys and the wheel
   still change them), other numbers with two small chevrons on the right; the text
   inset is set by _Watcher */
QDoubleSpinBox::up-button, QDoubleSpinBox::down-button {
    width: 0; border: none; background: transparent;
}
QSpinBox { padding-right: 26px; }
QSpinBox::up-button, QSpinBox::down-button {
    subcontrol-origin: border; width: 20px;
    border: none; border-radius: 4px; background: transparent;
}
QSpinBox::up-button { subcontrol-position: top right; margin: 3px 4px 0 0; }
QSpinBox::down-button { subcontrol-position: bottom right; margin: 0 4px 3px 0; }
QSpinBox::up-button:hover, QSpinBox::down-button:hover { background: {line}; }
QSpinBox::up-arrow { image: url({chevron_up}); width: 10px; height: 10px; }
QSpinBox::down-arrow { image: url({chevron}); width: 10px; height: 10px; }
/* Drop-downs as in the design: a soft button with a chevron (in forms: a field), the
   open list a rounded card below it with roomy rows */
QComboBox {
    background: {panel}; color: {text};
    border: 1px solid {border}; border-radius: 8px;
    padding: 4px 28px 4px 10px; min-height: 22px;
    combobox-popup: 0;
}
QComboBox:hover { background: {line}; }
QComboBox:focus, QComboBox:on { border-color: {accent}; }
QComboBox:editable, QFrame#detailCard QComboBox, QDialog QComboBox {
    background: {input};
}
QComboBox:disabled { color: {disabled}; background: {card}; border-color: {line}; }
QAbstractItemView QComboBox {  /* editor in a table cell: room for the text */
    padding: 0 20px 0 6px; border-radius: 4px; background: {input};
}
QComboBox::drop-down, QDateEdit::drop-down {
    subcontrol-origin: padding; subcontrol-position: center right;
    width: 24px; border: none; background: transparent;
}
QComboBox::down-arrow, QDateEdit::down-arrow {
    image: url({chevron}); width: 12px; height: 12px;
}
QComboBox::down-arrow:on { top: 1px; }
QDateEdit { padding-right: 28px; }
QComboBox QAbstractItemView {
    background: {input}; color: {text};
    border: 1px solid {border}; border-radius: 8px;
    padding: 4px; outline: 0;
    selection-background-color: {accent_soft}; selection-color: {text};
}
QComboBox QAbstractItemView::item {
    min-height: 26px; padding: 0 8px; border-radius: 6px;
}
QComboBox QAbstractItemView::item:hover { background: {line}; }
QComboBox QAbstractItemView::item:selected { background: {accent_soft}; color: {text}; }
QComboBox QAbstractItemView::item:disabled { color: {heading}; }
/* the period button (date_range.py) and its popup with presets */
QPushButton#dateRange { padding: 5px 12px; }
QFrame#dateRangePopup { background: {input}; border: 1px solid {border}; }
QPushButton#preset { background: {card}; border-color: {line}; }
QPushButton#preset:hover { background: {accent_soft}; }
QFrame#dateRangePopup QDateEdit { background: {input}; }
/* page building blocks (widgets.py) */
QLabel#pageTitle, QLabel#panelHeading { color: {heading}; }
QLabel#caption, QLabel#propName { color: {muted}; }
QFrame#detailCard {
    background: {card}; border: 1px solid {border}; border-radius: 14px;
}
QFrame#detailCard QGroupBox { background: {panel}; border-radius: 10px; }
QLabel[banner] { border-radius: 10px; padding: 8px 12px; }
QLabel[banner="info"] { background: {info}; color: {info_text}; }
QLabel[banner="success"] { background: {pos}; color: {pos_text}; }
QLabel[banner="warning"] { background: {notice}; color: {notice_text}; }
QLabel[banner="error"] { background: {neg}; color: {neg_text}; }
"""

_active = "system"
_default_font: QFont | None = None


def active() -> str:
    return _active


def tokens() -> dict | None:
    """Colours of the custom theme in use, None for the native look."""
    return {"obsidian": OBSIDIAN, "obsidian_dark": OBSIDIAN_DARK}.get(_active)


def _palette(t: dict) -> QPalette:
    p = QPalette()
    roles = {
        QPalette.ColorRole.Window: t["window"],
        QPalette.ColorRole.WindowText: t["text"],
        QPalette.ColorRole.Base: t["input"],
        QPalette.ColorRole.AlternateBase: t["card"],
        QPalette.ColorRole.Text: t["text"],
        QPalette.ColorRole.Button: t["panel"],
        QPalette.ColorRole.ButtonText: t["text"],
        QPalette.ColorRole.Highlight: t["accent_soft"],
        QPalette.ColorRole.HighlightedText: t["text"],
        QPalette.ColorRole.Accent: t["accent"],
        QPalette.ColorRole.Link: t["link"],
        QPalette.ColorRole.ToolTipBase: t["text"],
        QPalette.ColorRole.ToolTipText: t["window"],
        QPalette.ColorRole.PlaceholderText: t["muted"],
        QPalette.ColorRole.Light: t["bevel_light"],
        QPalette.ColorRole.Midlight: t["line"],
        QPalette.ColorRole.Mid: t["border"],
        QPalette.ColorRole.Dark: t["bevel_dark"],
    }
    for role, color in roles.items():
        p.setColor(role, QColor(color))
    for role in (
        QPalette.ColorRole.Text,
        QPalette.ColorRole.WindowText,
        QPalette.ColorRole.ButtonText,
    ):
        p.setColor(QPalette.ColorGroup.Disabled, role, QColor(t["disabled"]))
    return p


FONTS_DIR = Path(__file__).parent / "fonts"  # Inter (SIL Open Font License, see there)
_fonts_loaded = False


def _load_fonts() -> None:
    """Register the bundled fonts with Qt (once), so the design's font is there even
    when it is not installed in Windows."""
    global _fonts_loaded
    if not _fonts_loaded:
        for path in sorted(FONTS_DIR.glob("*.ttf")):
            QFontDatabase.addApplicationFont(str(path))
        _fonts_loaded = True


def _font(family_wanted: str) -> QFont:
    _load_fonts()
    font = QFont(_default_font)
    if family_wanted in QFontDatabase.families():
        font.setFamily(family_wanted)
    return font


# Title bar colours (Windows 11 DWM; ignored by older Windows and other systems)
DWMWA_BORDER_COLOR, DWMWA_CAPTION_COLOR, DWMWA_TEXT_COLOR = 34, 35, 36
DWMWA_COLOR_DEFAULT = 0xFFFFFFFF


def _colorref(color: str) -> int:
    c = QColor(color)
    return c.red() | c.green() << 8 | c.blue() << 16


def style_title_bar(window: QWidget) -> None:
    """Windows draws the title bar: give it the theme's strip colour, or hand it back."""
    if sys.platform != "win32" or not window.isWindow():
        return
    t = tokens()
    values = {
        DWMWA_CAPTION_COLOR: _colorref(t["panel"]) if t else DWMWA_COLOR_DEFAULT,
        DWMWA_TEXT_COLOR: _colorref(t["text"]) if t else DWMWA_COLOR_DEFAULT,
        DWMWA_BORDER_COLOR: _colorref(t["border"]) if t else DWMWA_COLOR_DEFAULT,
    }
    try:
        set_attribute = ctypes.windll.dwmapi.DwmSetWindowAttribute
        hwnd = ctypes.c_void_p(int(window.winId()))
        for attribute, value in values.items():
            v = ctypes.c_uint(value)
            set_attribute(hwnd, attribute, ctypes.byref(v), ctypes.sizeof(v))
    except (AttributeError, OSError):  # no DWM (e.g. Windows 10 without the attributes)
        pass


class _Watcher(QObject):
    """Colours the title bar of every window (main window, dialogs) when it is shown;
    gives drop-downs a list painter that follows the stylesheet (the default one of the
    Fusion style ignores the padding and rounding of the rows)."""

    def eventFilter(self, obj, event):
        kind = event.type()
        if kind == QEvent.Type.Show and isinstance(obj, QWidget) and obj.isWindow():
            style_title_bar(obj)
        elif (
            kind == QEvent.Type.Polish
            and isinstance(obj, QComboBox)
            and not isinstance(obj.itemDelegate(), QStyledItemDelegate)
        ):
            obj.setItemDelegate(QStyledItemDelegate(obj))
        elif kind == QEvent.Type.Polish and isinstance(obj, (QSpinBox, QDoubleSpinBox)):
            # the stylesheet's padding does not reach the text of number fields: the
            # same inset as in the other fields (Obsidian), none in the Windows look
            obj.lineEdit().setTextMargins(8 if tokens() else 0, 0, 0, 0)
        return False


_watcher: _Watcher | None = None


def _watch(qapp: QApplication) -> None:
    global _watcher
    if _watcher is None:
        _watcher = _Watcher(qapp)
        qapp.installEventFilter(_watcher)
    for window in qapp.topLevelWidgets():  # windows already open (switching the theme)
        if window.isVisible():
            style_title_bar(window)


_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24" '
    'fill="none" stroke="{color}" stroke-width="{width}" stroke-linecap="round" '
    'stroke-linejoin="round"><path d="{path}"/></svg>'
)
# line drawings for the stylesheet: name -> (path, stroke width)
SHAPES = {
    "chevron": ("M6 9l6 6 6-6", 2),
    "chevron-up": ("M6 15l6-6 6 6", 2),
    "check": ("M5 12.5l4.5 4.5L19 7.5", 3),
}


def _image(shape: str, color: str) -> str:
    """Path of a line image in the given colour (a stylesheet needs a file)."""
    folder = Path(tempfile.gettempdir()) / "hiro_budget"
    folder.mkdir(exist_ok=True)
    path = folder / f"{shape}-{color.lstrip('#').lower()}.svg"
    if not path.exists():
        d, width = SHAPES[shape]
        svg = _SVG.replace("{color}", color).replace("{width}", str(width))
        path.write_text(svg.replace("{path}", d), encoding="utf-8")
    return path.as_posix()


def apply_theme(qapp: QApplication, theme: str = "system") -> None:
    """Style, colours and light/dark mode ("system": as set in Windows); also while
    running."""
    global _active, _default_font
    if _default_font is None:
        _default_font = QFont(qapp.font())
    _active = theme if theme in SCHEMES else "system"
    qapp.styleHints().setColorScheme(SCHEMES[_active])
    t = tokens()
    if t is not None:
        qapp.setStyle("Fusion")  # follows the palette everywhere
        qapp.setPalette(_palette(t))
        qapp.setFont(_font("Inter"))  # the design's font, when it is installed
        style = (
            OBSIDIAN_STYLE.replace("{chevron}", _image("chevron", t["muted"]))
            .replace("{chevron_up}", _image("chevron-up", t["muted"]))
            .replace("{check}", _image("check", t["on_accent"]))
        )
        for key, value in t.items():
            if isinstance(value, str):
                style = style.replace("{" + key + "}", value)
        qapp.setStyleSheet(STYLE + style)
        _watch(qapp)
        return
    # Setting the style (again) after the colour scheme gives every existing widget the
    # new palette; otherwise views keep parts of the old one when switching at runtime.
    keys = QStyleFactory.keys()
    qapp.setStyle("windows11" if "windows11" in keys else "Fusion")
    qapp.setPalette(qapp.style().standardPalette())
    qapp.setFont(_default_font)
    qapp.setStyleSheet(STYLE)
    _watch(qapp)
