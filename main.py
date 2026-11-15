"""Online Status Checker — entry point.

Responsibilities, in order: install the exception hook, build the QApplication
with the app palette, load the stylesheet, set the window icon, show the main
window. Anything that can fail here is reported on stderr and in a dialog
rather than as a bare traceback.
"""
import sys
import traceback
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parent

try:
    from PySide6.QtGui import QColor, QPalette
    from PySide6.QtWidgets import QApplication, QMessageBox

    from ui.dialogs import show_message
    from ui.main_window import MainWindow
    from ui.theme import app_icon, load_stylesheet
except ModuleNotFoundError as exc:
    sys.stderr.write(
        f"Missing dependency: {exc.name}\n\n"
        "Install the project requirements first:\n"
        f'    "{sys.executable}" -m pip install -r '
        f'"{APP_ROOT / "requirements.txt"}"\n'
    )
    raise SystemExit(1) from exc


def _install_excepthook() -> None:
    """Surface uncaught exceptions on stderr instead of dying silently."""
    def hook(exc_type, exc_value, exc_tb):
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc_value, exc_tb)
            return
        sys.stderr.write("Unhandled exception:\n")
        traceback.print_exception(exc_type, exc_value, exc_tb)
    sys.excepthook = hook


def _build_palette() -> QPalette:
    """Baseline palette; QSS overrides most of it, this covers native dialogs."""
    role = QPalette.ColorRole
    group = QPalette.ColorGroup
    palette = QPalette()
    palette.setColor(role.Window, QColor("#f0f2f5"))
    palette.setColor(role.WindowText, QColor("#1a1a2e"))
    palette.setColor(role.Base, QColor("#ffffff"))
    palette.setColor(role.AlternateBase, QColor("#fafbfc"))
    palette.setColor(role.Text, QColor("#1a1a2e"))
    palette.setColor(role.Button, QColor("#f0f0f0"))
    palette.setColor(role.ButtonText, QColor("#1a1a2e"))
    palette.setColor(role.Highlight, QColor("#2f6fed"))
    palette.setColor(role.HighlightedText, QColor("#ffffff"))
    palette.setColor(group.Disabled, role.Text, QColor("#98a2b3"))
    palette.setColor(group.Disabled, role.ButtonText, QColor("#98a2b3"))
    return palette


def main() -> int:
    _install_excepthook()

    app = QApplication(sys.argv)
    app.setApplicationName("Online Status Checker")
    app.setOrganizationName("Online Status Checker")
    app.setStyle("Fusion")
    app.setPalette(_build_palette())
    app.setWindowIcon(app_icon())

    stylesheet = load_stylesheet()
    if stylesheet:
        app.setStyleSheet(stylesheet)

    try:
        window = MainWindow()
    except Exception as exc:  # noqa: BLE001
        traceback.print_exc()
        show_message(None, "Startup Error",
                     f"Failed to initialise the application:\n\n{exc}",
                     QMessageBox.Critical)
        return 1

    window.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
