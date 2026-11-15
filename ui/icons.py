"""Inline SVG icons.

Kept as SVG source rather than files so they stay crisp at any DPI and can be
re-tinted per role (white on the primary button, grey on secondary, red on the
destructive action).
"""
from PySide6.QtCore import QByteArray, Qt
from PySide6.QtGui import QIcon, QPainter, QPixmap

try:
    from PySide6.QtSvg import QSvgRenderer
except ImportError:                                     # pragma: no cover
    QSvgRenderer = None


_TEMPLATE = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 16 16">{body}</svg>'

_BODIES = {
    "play": '<path d="M5.6 3.3a.6.6 0 0 1 .9-.5l7 4.7a.6.6 0 0 1 0 1l-7 4.7a.6.6 0 0 1-.9-.5z" fill="{c}"/>',
    "stop": '<rect x="4.4" y="4.4" width="7.2" height="7.2" rx="1.4" fill="{c}"/>',
    "folder": (
        '<path d="M2.4 4.6c0-.6.5-1.1 1.1-1.1h2.3l1.2 1.4h4.5c.6 0 1.1.5 1.1 1.1v5.4'
        'c0 .6-.5 1.1-1.1 1.1H3.5c-.6 0-1.1-.5-1.1-1.1z" fill="none" stroke="{c}" '
        'stroke-width="1.4" stroke-linejoin="round"/>'
    ),
    "star": (
        '<path d="M8 2.4l1.7 3.6 3.9.5-2.9 2.7.8 3.9L8 11.2l-3.5 1.9.8-3.9L2.4 6.5'
        'l3.9-.5z" fill="{c}"/>'
    ),
    "alert": (
        '<path d="M8 2.5 14.3 13.3H1.7z" fill="none" stroke="{c}" stroke-width="1.5" '
        'stroke-linejoin="round"/>'
        '<path d="M8 6.5v3.2" fill="none" stroke="{c}" stroke-width="1.6" '
        'stroke-linecap="round"/><circle cx="8" cy="11.8" r="0.9" fill="{c}"/>'
    ),
    "error": (
        '<circle cx="8" cy="8" r="6" fill="none" stroke="{c}" stroke-width="1.5"/>'
        '<path d="M5.9 5.9 10.1 10.1M10.1 5.9 5.9 10.1" fill="none" stroke="{c}" '
        'stroke-width="1.6" stroke-linecap="round"/>'
    ),
    "question": (
        '<circle cx="8" cy="8" r="6" fill="none" stroke="{c}" stroke-width="1.5"/>'
        '<path d="M6.2 6.4a1.9 1.9 0 1 1 2.4 1.8c-.5.2-.7.6-.7 1.1v.3" fill="none" '
        'stroke="{c}" stroke-width="1.5" stroke-linecap="round"/>'
        '<circle cx="8" cy="11.7" r="0.85" fill="{c}"/>'
    ),
    "info": (
        '<circle cx="8" cy="8" r="6" fill="none" stroke="{c}" stroke-width="1.5"/>'
        '<path d="M8 7.4v3.5" fill="none" stroke="{c}" stroke-width="1.6" '
        'stroke-linecap="round"/><circle cx="8" cy="5.1" r="0.9" fill="{c}"/>'
    ),
}


def make_pixmap(name: str, color: str = "#ffffff", size: int = 16) -> QPixmap:
    """Render one icon to a transparent pixmap (null when unknown)."""
    body = _BODIES.get(name)
    if body is None or QSvgRenderer is None:
        return QPixmap()

    svg = _TEMPLATE.format(body=body.format(c=color)).encode("utf-8")
    renderer = QSvgRenderer(QByteArray(svg))
    if not renderer.isValid():
        return QPixmap()

    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.Antialiasing, True)
    renderer.render(painter)
    painter.end()
    return pixmap


def make_icon(name: str, color: str = "#ffffff", size: int = 16) -> QIcon:
    """Build a QIcon for ``name``; null when the glyph or plugin is missing."""
    icon = QIcon()
    for scale in (1, 2):
        pixmap = make_pixmap(name, color, size * scale)
        if pixmap.isNull():
            return QIcon()
        icon.addPixmap(pixmap)
    return icon
