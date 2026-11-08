"""Рисует значок приложения (ICO со всеми размерами) тем же кодом, что и значок в трее."""
import os
import struct
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PySide6.QtCore import QBuffer, QIODevice  # noqa: E402
from PySide6.QtGui import QGuiApplication  # noqa: E402

from jarvis.gui.theme import make_icon  # noqa: E402

SIZES = (16, 24, 32, 48, 64, 128, 256)


def png_bytes(icon, size: int) -> bytes:
    buf = QBuffer()
    buf.open(QIODevice.OpenModeFlag.WriteOnly)
    icon.pixmap(size, size).save(buf, "PNG")
    return bytes(buf.data())


def main(out: str) -> None:
    app = QGuiApplication([])
    icon = make_icon("listening", 512)
    images = [(s, png_bytes(icon, s)) for s in SIZES]
    header = struct.pack("<HHH", 0, 1, len(images))
    offset = 6 + 16 * len(images)
    entries, blobs = b"", b""
    for size, data in images:
        dim = 0 if size >= 256 else size
        entries += struct.pack("<BBBBHHII", dim, dim, 0, 0, 1, 32, len(data), offset + len(blobs))
        blobs += data
    Path(out).write_bytes(header + entries + blobs)
    print(f"{out}: {len(images)} размеров")
    del app


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "packaging/jarvis.ico")
