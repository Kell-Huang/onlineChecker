"""Stylesheet loading, app icon, and the few colours Python paints with.

The visual system lives in ``styles/main.qss``; this module only exposes what
code needs: the loader that resolves ``@ASSETS@`` to an absolute path, the
window icon, and the status colours used by table cells and the summary badges.
"""
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parent.parent
ASSETS_DIR = APP_ROOT / "assets"
STYLE_PATH = APP_ROOT / "styles" / "main.qss"
ICON_PATH = ASSETS_DIR / "app_icon.ico"
ICON_PNG_PATH = ASSETS_DIR / "app_icon_256.png"

TEXT_MUTED = "#98a2b3"
PRIMARY = "#2f6fed"

SUCCESS = "#0f7b3f"
WARNING = "#b54708"
DANGER = "#b42318"

def load_stylesheet() -> str:
    """Read main.qss and resolve the ``@ASSETS@`` token."""
    try:
        text = STYLE_PATH.read_text(encoding="utf-8")
    except OSError:
        return ""
    return text.replace("@ASSETS@", ASSETS_DIR.as_posix())


def app_icon():
    """Window / taskbar icon, or a null QIcon when the assets are missing."""
    from PySide6.QtGui import QIcon

    for path in (ICON_PATH, ICON_PNG_PATH):
        if path.exists():
            icon = QIcon(str(path))
            if not icon.isNull():
                return icon
    return QIcon()
