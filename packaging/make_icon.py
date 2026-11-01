"""Write ``app.ico`` (the budget ring) for the exe and its shortcuts.

The sizes Windows asks for, each drawn from the SVG meant for it (``gui/app_icon.py``),
stored as PNG images in one ICO file. Run: ``uv run python packaging/make_icon.py``.
"""

from __future__ import annotations

import struct
import sys
from pathlib import Path

from PySide6.QtCore import QBuffer, QIODevice
from PySide6.QtGui import QGuiApplication

from hiro_budget.gui.app_icon import render

SIZES = (16, 20, 24, 32, 40, 48, 64, 128, 256)
OUT = Path(__file__).resolve().parent / "app.ico"


def png(size: int) -> bytes:
    buffer = QBuffer()
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    render(size).save(buffer, "PNG")
    return bytes(buffer.data())


def ico(images: list[tuple[int, bytes]]) -> bytes:
    """ICONDIR, one ICONDIRENTRY per image, then the PNG data (0 in a size means 256)."""
    header = struct.pack("<HHH", 0, 1, len(images))
    offset = len(header) + 16 * len(images)
    entries, data = b"", b""
    for size, image in images:
        side = 0 if size >= 256 else size
        entries += struct.pack("<BBBBHHII", side, side, 0, 0, 1, 32, len(image), offset)
        offset += len(image)
        data += image
    return header + entries + data


def main() -> None:
    app = QGuiApplication(sys.argv[:1])  # noqa: F841  (painting needs one)
    OUT.write_bytes(ico([(size, png(size)) for size in SIZES]))
    print(f"{OUT} ({OUT.stat().st_size} bytes, sizes {', '.join(map(str, SIZES))})")


if __name__ == "__main__":
    main()
