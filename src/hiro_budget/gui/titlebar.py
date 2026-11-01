"""Own title bar in place of the Windows one (as Obsidian, Firefox).

The window keeps its native frame styles, so Windows still draws the shadow and rounded
corners and offers Aero Snap, the snap layouts on the maximize button, Win+arrow keys and
the system menu; only the caption area is handed to the app (WM_NCCALCSIZE) and the mouse
is told where the caption, the borders and the maximize button are (WM_NCHITTEST).

Off Windows (or headless in tests) the system title bar stays and this bar only carries
the sidebar button.
"""

from __future__ import annotations

import ctypes
import sys
from ctypes import wintypes

from PySide6.QtCore import QEvent, QPoint, QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QFont, QGuiApplication, QPainter
from PySide6.QtWidgets import QAbstractButton, QHBoxLayout, QMainWindow, QToolButton, QWidget

from hiro_budget.gui import theme
from hiro_budget.gui.navigation import icon_font

HEIGHT = 36

# Segoe Fluent Icons / Segoe MDL2 Assets
ICON_MINIMIZE = ""  # ChromeMinimize
ICON_MAXIMIZE = ""  # ChromeMaximize
ICON_RESTORE = ""  # ChromeRestore
ICON_CLOSE = ""  # ChromeClose

WM_NCCALCSIZE = 0x0083
WM_NCHITTEST = 0x0084
WM_NCMOUSEMOVE = 0x00A0
WM_NCLBUTTONDOWN = 0x00A1
WM_NCLBUTTONUP = 0x00A2
WM_NCLBUTTONDBLCLK = 0x00A3
WM_NCMOUSELEAVE = 0x02A2
WM_MOUSEMOVE = 0x0200
WM_XBUTTONUP = 0x020C
XBUTTON1, XBUTTON2 = 1, 2  # back, forward

HTCLIENT, HTCAPTION, HTMAXBUTTON = 1, 2, 9
HTLEFT, HTRIGHT, HTTOP, HTTOPLEFT, HTTOPRIGHT = 10, 11, 12, 13, 14
HTBOTTOM, HTBOTTOMLEFT, HTBOTTOMRIGHT = 15, 16, 17

GWL_STYLE = -16
WS_CAPTION, WS_THICKFRAME, WS_SYSMENU = 0x00C00000, 0x00040000, 0x00080000
WS_MINIMIZEBOX, WS_MAXIMIZEBOX = 0x00020000, 0x00010000
SWP_FRAMECHANGED = 0x0020
SWP_NOSIZE, SWP_NOMOVE, SWP_NOZORDER, SWP_NOACTIVATE = 0x0001, 0x0002, 0x0004, 0x0010
SM_CXSIZEFRAME, SM_CYSIZEFRAME, SM_CXPADDEDBORDER = 32, 33, 92
DWMWA_WINDOW_CORNER_PREFERENCE, DWMWCP_ROUND = 33, 2

RESIZE_BORDER = 5  # px inside the window edge that resize it (logical)


class MARGINS(ctypes.Structure):
    _fields_ = [
        ("cxLeftWidth", ctypes.c_int),
        ("cxRightWidth", ctypes.c_int),
        ("cyTopHeight", ctypes.c_int),
        ("cyBottomHeight", ctypes.c_int),
    ]


class NCCALCSIZE_PARAMS(ctypes.Structure):
    _fields_ = [("rgrc", wintypes.RECT * 3), ("lppos", ctypes.c_void_p)]


def native_frame_available() -> bool:
    """Only with the real Windows platform (not the headless one of the tests)."""
    return sys.platform == "win32" and QGuiApplication.platformName() == "windows"


def side_button(event_type, message) -> int | None:
    """-1 / 1: the back / forward button of the mouse was released over the window
    (WM_XBUTTONUP, also over child widgets: they have no window of their own)."""
    if sys.platform != "win32" or bytes(event_type) != b"windows_generic_MSG":
        return None
    msg = wintypes.MSG.from_address(int(message))
    if msg.message != WM_XBUTTONUP:
        return None
    button = (msg.wParam >> 16) & 0xFFFF
    return {XBUTTON1: -1, XBUTTON2: 1}.get(button)


def _signed_word(value: int) -> int:
    return ctypes.c_short(value & 0xFFFF).value


class CaptionButton(QAbstractButton):
    """Minimize / maximize / close, drawn as in Windows 11 (close turns red)."""

    def __init__(self, glyph: str, close: bool = False, parent=None) -> None:
        super().__init__(parent)
        self.glyph = glyph
        self.close_button = close
        self.hot = False  # hovered while Windows tracks the mouse (maximize button)
        self.setFixedSize(46, HEIGHT)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setAttribute(Qt.WidgetAttribute.WA_Hover)

    def set_hot(self, hot: bool) -> None:
        if hot != self.hot:
            self.hot = hot
            self.update()

    def paintEvent(self, event) -> None:
        t = theme.tokens()
        text = QColor(t["text"]) if t else self.palette().windowText().color()
        hover = self.hot or self.underMouse()
        painter = QPainter(self)
        if hover or self.isDown():
            if self.close_button:
                painter.fillRect(self.rect(), QColor("#C42B1C" if hover else "#C93F33"))
                text = QColor("#FFFFFF")
            else:
                shade = QColor(text)
                shade.setAlphaF(0.16 if self.isDown() else 0.09)
                painter.fillRect(self.rect(), shade)
        font = QFont(icon_font() or "")
        font.setPixelSize(10)
        painter.setFont(font)
        painter.setPen(text)
        painter.drawText(QRectF(self.rect()), Qt.AlignmentFlag.AlignCenter, self.glyph)
        painter.end()


class BadgeButton(QToolButton):
    """A title bar button with a dot in the accent colour when something is waiting."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.badge = False

    def set_badge(self, on: bool) -> None:
        if on != self.badge:
            self.badge = on
            self.update()

    def paintEvent(self, event) -> None:
        super().paintEvent(event)
        if not self.badge:
            return
        t = theme.tokens()
        color = QColor(t["accent"]) if t else self.palette().highlight().color()
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(color)
        icon = self.iconSize()
        top = (self.height() - icon.height()) / 2
        painter.drawEllipse(QPointF(8 + icon.width(), top + 1), 3.5, 3.5)  # by the icon
        painter.end()


class TitleBar(QWidget):
    """The strip at the top: ``left`` (the sidebar button), then the window buttons."""

    def __init__(self, window: QMainWindow, left: QWidget) -> None:
        super().__init__(window)
        self.window_ = window
        self.native = native_frame_available()
        self.setObjectName("titleBar")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground)
        self.setFixedHeight(HEIGHT)
        self.minimize = CaptionButton(ICON_MINIMIZE)
        self.maximize = CaptionButton(ICON_MAXIMIZE)
        self.close_ = CaptionButton(ICON_CLOSE, close=True)
        self.minimize.clicked.connect(window.showMinimized)
        self.maximize.clicked.connect(self.toggle_maximized)
        self.close_.clicked.connect(window.close)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(10, 0, 0, 0)
        layout.setSpacing(2)
        layout.addWidget(left, 0, Qt.AlignmentFlag.AlignVCenter)
        self.left = QHBoxLayout()  # add_left: next to the sidebar button
        self.left.setSpacing(2)
        layout.addLayout(self.left)
        layout.addStretch(1)
        self.center = QHBoxLayout()  # set_center: the search box
        layout.addLayout(self.center, 4)
        layout.addStretch(1)
        self.right = QHBoxLayout()  # add_right: before the window buttons
        self.right.setSpacing(4)
        layout.addLayout(self.right)
        layout.addSpacing(10)
        for button in (self.minimize, self.maximize, self.close_):
            layout.addWidget(button)
            button.setVisible(self.native)
        window.setMenuWidget(self)
        window.installEventFilter(self)
        self.restyle()

    def install(self) -> None:
        """Hide the Windows title bar; once the window passes its messages to
        ``native_event`` (the frame is calculated anew right here)."""
        if self.native:
            self.window_.setWindowFlag(Qt.WindowType.FramelessWindowHint)
            self._native_styles()

    def add_left(self, widget: QWidget) -> None:
        self.left.addWidget(widget, 0, Qt.AlignmentFlag.AlignVCenter)

    def set_center(self, widget: QWidget) -> None:
        self.center.addWidget(widget, 1, Qt.AlignmentFlag.AlignVCenter)

    def add_right(self, widget: QWidget) -> None:
        self.right.addWidget(widget, 0, Qt.AlignmentFlag.AlignVCenter)

    # -- look ----------------------------------------------------------------------

    def restyle(self) -> None:
        """Colours of the theme in use (also after switching it)."""
        t = theme.tokens()
        if t is None:
            ground, line = "palette(window)", "transparent"
            hover, text = "rgba(128, 128, 128, 0.12)", "palette(text)"
            warn, warn_text = "rgba(232, 163, 61, 0.22)", "palette(text)"
            field, field_border, focus = (
                "palette(base)",
                "rgba(128, 128, 128, 0.35)",
                "palette(highlight)",
            )
        else:
            ground, line, hover, text = t["panel"], t["border"], t["line"], t["text"]
            warn, warn_text = t["warn"], t["warn_text"]
            field, field_border, focus = t["input"], t["border"], t["accent"]
        self.setStyleSheet(
            f"QWidget#titleBar {{ background: {ground}; border-bottom: 1px solid {line}; }}"
            "QToolButton#sidebarToggle, QToolButton#titleButton {"
            f" background: transparent; color: {text}; border: none; border-radius: 6px;"
            " padding: 3px 8px; }"
            "QToolButton#sidebarToggle:hover, QToolButton#titleButton:hover {"
            f" background: {hover}; }}"
            f"QToolButton#titleUnsaved {{ background: {warn}; color: {warn_text};"
            " border: none; border-radius: 6px; padding: 3px 10px; font-weight: 600; }"
            f"QLineEdit#titleSearch {{ background: {field}; color: {text};"
            f" border: 1px solid {field_border}; border-radius: 8px; padding: 2px 10px;"
            " min-height: 20px; }"
            f"QLineEdit#titleSearch:focus {{ border-color: {focus}; }}"
        )
        for button in (self.minimize, self.maximize, self.close_):
            button.update()

    def toggle_maximized(self) -> None:
        w = self.window_
        w.showNormal() if w.isMaximized() else w.showMaximized()

    def eventFilter(self, obj, event) -> bool:
        if obj is self.window_ and event.type() == QEvent.Type.WindowStateChange:
            self.maximize.glyph = ICON_RESTORE if obj.isMaximized() else ICON_MAXIMIZE
            self.maximize.set_hot(False)
            self.maximize.update()
        return False

    # -- native frame ----------------------------------------------------------------

    def _hwnd(self) -> int:
        return int(self.window_.winId())

    def _native_styles(self) -> None:
        """Frame styles back on the frameless window: snap, animations, shadow."""
        user32, dwmapi = ctypes.windll.user32, ctypes.windll.dwmapi
        user32.GetWindowLongPtrW.restype = ctypes.c_ssize_t
        user32.GetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int]
        user32.SetWindowLongPtrW.restype = ctypes.c_ssize_t
        user32.SetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_ssize_t]
        hwnd = self._hwnd()
        style = user32.GetWindowLongPtrW(hwnd, GWL_STYLE)
        style |= WS_CAPTION | WS_THICKFRAME | WS_SYSMENU | WS_MINIMIZEBOX | WS_MAXIMIZEBOX
        user32.SetWindowLongPtrW(hwnd, GWL_STYLE, style)
        margins = MARGINS(0, 0, 1, 0)  # a hair of DWM frame: Windows draws the shadow
        dwmapi.DwmExtendFrameIntoClientArea(wintypes.HWND(hwnd), ctypes.byref(margins))
        corner = ctypes.c_int(DWMWCP_ROUND)  # Windows 11; ignored on Windows 10
        dwmapi.DwmSetWindowAttribute(
            wintypes.HWND(hwnd),
            DWMWA_WINDOW_CORNER_PREFERENCE,
            ctypes.byref(corner),
            ctypes.sizeof(corner),
        )
        flags = SWP_FRAMECHANGED | SWP_NOSIZE | SWP_NOMOVE | SWP_NOZORDER | SWP_NOACTIVATE
        user32.SetWindowPos(wintypes.HWND(hwnd), None, 0, 0, 0, 0, flags)

    def _frame_thickness(self, hwnd: int) -> tuple[int, int]:
        """Width of the (hidden) sizing frame in physical pixels, x and y."""
        user32 = ctypes.windll.user32
        try:
            dpi = user32.GetDpiForWindow(wintypes.HWND(hwnd))
            metric = lambda i: user32.GetSystemMetricsForDpi(i, dpi)  # noqa: E731
        except AttributeError:  # before Windows 10 1607
            metric = user32.GetSystemMetrics
        padded = metric(SM_CXPADDEDBORDER)
        return metric(SM_CXSIZEFRAME) + padded, metric(SM_CYSIZEFRAME) + padded

    def native_event(self, event_type, message) -> tuple[bool, int] | None:
        """Handle the frame messages of the window; None: leave the message to Qt."""
        if not self.native or bytes(event_type) != b"windows_generic_MSG":
            return None
        msg = wintypes.MSG.from_address(int(message))
        if msg.message == WM_NCCALCSIZE and msg.wParam:
            # the whole window is client area; maximized, Windows pushes the frame
            # off the screen edges, which must not cut off the content
            if ctypes.windll.user32.IsZoomed(msg.hWnd):
                params = NCCALCSIZE_PARAMS.from_address(msg.lParam)
                fx, fy = self._frame_thickness(msg.hWnd)
                r = params.rgrc[0]
                r.left, r.right = r.left + fx, r.right - fx
                r.top, r.bottom = r.top + fy, r.bottom - fy
            return True, 0
        if msg.message == WM_NCHITTEST:
            return True, self._hit_test(msg)
        if msg.message == WM_NCMOUSEMOVE:
            self.maximize.set_hot(msg.wParam == HTMAXBUTTON)
        elif msg.message in (WM_NCMOUSELEAVE, WM_MOUSEMOVE):
            self.maximize.set_hot(False)
        elif msg.message in (WM_NCLBUTTONDOWN, WM_NCLBUTTONDBLCLK) and msg.wParam == HTMAXBUTTON:
            self.maximize.setDown(True)
            return True, 0  # not the classic button tracking of Windows
        elif msg.message == WM_NCLBUTTONUP and msg.wParam == HTMAXBUTTON:
            self.maximize.setDown(False)
            self.toggle_maximized()
            return True, 0
        return None

    def _hit_test(self, msg) -> int:
        w = self.window_
        rect = wintypes.RECT()
        ctypes.windll.user32.GetWindowRect(msg.hWnd, ctypes.byref(rect))
        ratio = w.devicePixelRatioF()
        x = (_signed_word(msg.lParam) - rect.left) / ratio
        y = (_signed_word(msg.lParam >> 16) - rect.top) / ratio
        width, height = (rect.right - rect.left) / ratio, (rect.bottom - rect.top) / ratio
        if not w.isMaximized() and not w.isFullScreen():
            b = RESIZE_BORDER
            left, right = x < b, x >= width - b
            top, bottom = y < b, y >= height - b
            for edge, code in (
                (top and left, HTTOPLEFT),
                (top and right, HTTOPRIGHT),
                (bottom and left, HTBOTTOMLEFT),
                (bottom and right, HTBOTTOMRIGHT),
                (left, HTLEFT),
                (right, HTRIGHT),
                (top, HTTOP),
                (bottom, HTBOTTOM),
            ):
                if edge:
                    return code
        pos = self.mapFrom(w, QPoint(int(x), int(y)))
        if not self.rect().contains(pos):
            return HTCLIENT
        child = self.childAt(pos)
        if child is self.maximize:
            return HTMAXBUTTON  # Windows 11 shows the snap layouts
        if child is not None:  # buttons (and other controls) get the mouse
            return HTCLIENT
        return HTCAPTION  # drag, double-click, Aero Snap, system menu
