"""Regenerate the application icon assets from ``assets/app_icon.svg``.

Run from anywhere::

    python tools/make_icon.py

Produces:

  - ``assets/app_icon.ico``       multi-size Windows icon (16-256 px)
  - ``assets/app_icon_256.png``   256 px preview, used as the fallback

The ICO is written by hand rather than through an image library: Vista and
later accept PNG-compressed entries, so the container is just a header plus
one directory entry per size. That keeps the build free of Pillow.
"""
import struct
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from PySide6.QtCore import QByteArray, QBuffer, QIODevice, Qt
from PySide6.QtGui import QGuiApplication, QImage, QPainter
from PySide6.QtSvg import QSvgRenderer

SVG_PATH = ROOT / "assets" / "app_icon.svg"
ICO_PATH = ROOT / "assets" / "app_icon.ico"
PNG_PATH = ROOT / "assets" / "app_icon_256.png"

# 16 and 24 are the title bar / list view sizes, 32-48 the shell, 256 the
# store and file-preview size. Windows picks the closest match per context.
SIZES = (16, 24, 32, 48, 64, 128, 256)


def render(svg: bytes, size: int) -> QImage:
    renderer = QSvgRenderer(QByteArray(svg))
    if not renderer.isValid():
        raise SystemExit(f"{SVG_PATH.name} is not a valid SVG")

    image = QImage(size, size, QImage.Format_ARGB32)
    image.fill(Qt.transparent)
    painter = QPainter(image)
    painter.setRenderHint(QPainter.Antialiasing, True)
    renderer.render(painter)
    painter.end()
    return image


def png_bytes(image: QImage) -> bytes:
    buffer = QBuffer()
    buffer.open(QIODevice.WriteOnly)
    image.save(buffer, "PNG")
    return bytes(buffer.data())


def build_ico(entries) -> bytes:
    """Pack ``[(size, png_bytes), ...]`` into an ICO container."""
    header = struct.pack("<HHH", 0, 1, len(entries))
    offset = len(header) + 16 * len(entries)
    directory = b""
    payload = b""
    for size, data in entries:
        # 256 is encoded as 0: the width/height fields are single bytes.
        directory += struct.pack(
            "<BBBBHHII", size % 256, size % 256, 0, 0, 1, 32, len(data), offset,
        )
        payload += data
        offset += len(data)
    return header + directory + payload


def main() -> int:
    if not SVG_PATH.exists():
        raise SystemExit(f"missing {SVG_PATH}")

    app = QGuiApplication.instance() or QGuiApplication([])  # noqa: F841
    svg = SVG_PATH.read_bytes()

    entries = [(size, png_bytes(render(svg, size))) for size in SIZES]
    ICO_PATH.write_bytes(build_ico(entries))
    render(svg, 256).save(str(PNG_PATH), "PNG")

    print(f"wrote {ICO_PATH.relative_to(ROOT)} "
          f"({ICO_PATH.stat().st_size:,} bytes, sizes {', '.join(map(str, SIZES))})")
    print(f"wrote {PNG_PATH.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
