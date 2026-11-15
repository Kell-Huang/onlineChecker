"""Main window: four tabs, one toolbar, one progress bar.

Tabs, in the order they matter:

    Countries   what to run (the new page, default)
    Browse      where feeds come from (needs a token)
    Results     the summary table after a run
    Log         everything the run said, for when a number looks wrong

The Charts tab from the previous version is gone: the summary table carries
the same numbers without a second place to look.
"""
from pathlib import Path

from PySide6.QtCore import QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QHBoxLayout, QLabel, QMainWindow, QMessageBox, QPlainTextEdit,
    QProgressBar, QPushButton, QTabWidget, QVBoxLayout, QWidget,
)

from core.models import Feed, Selection
from core.progress import ProgressUpdate
from core.settings import (BASE_DIR, load_countries, load_credentials,
                           load_selection, load_settings, save_credentials,
                           save_selection, save_settings)

from .browse_page import BrowsePage
from .country_page import CountryPage
from .dialogs import SettingsDialog, show_message
from .icons import make_icon
from .results_page import ResultsPage
from .theme import app_icon
from .workers import RunWorker

MAX_LOG_LINES = 5000


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setObjectName("MainWindow")
        self.setWindowTitle("Online Status Checker")
        self.setWindowIcon(app_icon())
        self.resize(1120, 760)
        self.setMinimumSize(940, 620)

        self._settings = load_settings()
        self._credentials = load_credentials()
        self._selection: Selection = load_selection()
        self._aliases, self._countries = load_countries()
        self._worker = None
        self._last_output: Path = self._settings.output_dir

        self._build_ui()
        self._refresh_pages()
        self._log("INFO", "Ready.")

    # ------------------------------------------------------------------
    # UI
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        central = QWidget()
        central.setObjectName("Central")
        self.setCentralWidget(central)

        outer = QVBoxLayout(central)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        outer.addWidget(self._build_toolbar())

        self.tabs = QTabWidget()
        self.tabs.setObjectName("MainTabs")
        self.country_page = CountryPage()
        self.browse_page = BrowsePage(lambda: self._settings,
                                      lambda: self._credentials)
        self.results_page = ResultsPage()
        self.log_view = QPlainTextEdit()
        self.log_view.setObjectName("logTextEdit")
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumBlockCount(MAX_LOG_LINES)

        self.tabs.addTab(self.country_page, "Countries")
        self.tabs.addTab(self.browse_page, "Browse")
        self.tabs.addTab(self.results_page, "Results")
        self.tabs.addTab(self.log_view, "Log")
        outer.addWidget(self.tabs, stretch=1)

        outer.addWidget(self._build_progress())

        self.country_page.selection_changed.connect(self._on_selection_changed)
        self.browse_page.feed_toggled.connect(self._on_feed_toggled)
        self.browse_page.gos_chosen.connect(self._on_gos_chosen)
        self.browse_page.log.connect(self._log)
        self.results_page.open_btn.clicked.connect(self._open_output_dir)

    def _build_toolbar(self) -> QWidget:
        bar = QWidget()
        bar.setObjectName("ActionBar")
        layout = QHBoxLayout(bar)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(8)

        self.settings_btn = QPushButton("Settings")
        self.settings_btn.setObjectName("secondaryButton")
        self.settings_btn.clicked.connect(self._on_settings)
        layout.addWidget(self.settings_btn)

        self.summary_label = QLabel()
        self.summary_label.setObjectName("toolbarSummary")
        layout.addWidget(self.summary_label, stretch=1)

        self.cancel_btn = QPushButton("Cancel")
        self.cancel_btn.setObjectName("cancelButton")
        self.cancel_btn.setIcon(make_icon("stop", "#b42318"))
        self.cancel_btn.setEnabled(False)
        self.cancel_btn.clicked.connect(self._on_cancel)
        layout.addWidget(self.cancel_btn)

        self.start_btn = QPushButton("Start Comparison")
        self.start_btn.setObjectName("primaryButton")
        self.start_btn.setIcon(make_icon("play", "#ffffff"))
        self.start_btn.setMinimumWidth(170)
        self.start_btn.clicked.connect(self._on_start)
        layout.addWidget(self.start_btn)
        return bar

    def _build_progress(self) -> QWidget:
        bar = QWidget()
        bar.setObjectName("ProgressBar")
        layout = QHBoxLayout(bar)
        layout.setContentsMargins(12, 8, 12, 10)
        layout.setSpacing(10)

        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.progress.setTextVisible(False)
        layout.addWidget(self.progress, stretch=1)

        self.status_label = QLabel("Ready")
        self.status_label.setObjectName("progressLabel")
        self.status_label.setMinimumWidth(260)
        layout.addWidget(self.status_label)
        return bar

    # ------------------------------------------------------------------
    # State
    # ------------------------------------------------------------------

    def _refresh_pages(self) -> None:
        self.country_page.set_selection(self._selection, self._countries)
        self.browse_page.set_selection(self._selection, self._countries,
                                       self._aliases)
        self._update_summary()

    def _update_summary(self) -> None:
        chosen = len(self._selection.selected)
        total = len(self._selection.feeds)
        gos = self._selection.gos.channel_name if self._selection.gos else "none"
        self.summary_label.setText(
            f"{chosen} of {total} feeds selected   ·   GOS: {gos}")
        self.start_btn.setEnabled(bool(chosen) and self._worker is None)

    def _persist(self) -> None:
        save_selection(self._selection)

    # ------------------------------------------------------------------
    # Page signals
    # ------------------------------------------------------------------

    def _on_selection_changed(self) -> None:
        self._selection.selected = self.country_page.selected_keys()
        self._persist()
        self._update_summary()

    def _on_feed_toggled(self, feed: Feed, selected: bool) -> None:
        if selected:
            self._selection.add_feed(feed, select=True)
            self._log("INFO", f"Added feed {feed.country or feed.display_name} "
                              f"({feed.channel_name}).")
        else:
            self._selection.remove_feed(feed.key)
            self._log("INFO", f"Removed feed {feed.display_name}.")
        self._persist()
        self._refresh_pages()

    def _on_gos_chosen(self, gos) -> None:
        self._selection.gos = gos
        self._persist()
        self._refresh_pages()
        if gos is None:
            self._log("INFO", "GOS channel cleared.")
        else:
            self._log("INFO", f"GOS set to {gos.channel_name or gos.channel_id}.")

    def _on_settings(self) -> None:
        dialog = SettingsDialog(self, self._settings, self._credentials["token"])
        if dialog.exec() != SettingsDialog.Accepted:
            return
        token, output, max_per_country, concurrency, seed = dialog.values()
        self._credentials["token"] = token
        save_credentials(token, self._credentials["base_url"])
        self._settings.max_per_country = max_per_country
        self._settings.download_concurrency = concurrency
        self._settings.random_seed = seed
        if output:
            path = Path(output)
            # Relative paths are resolved against the project root, not the
            # working directory the app happened to be launched from.
            self._settings.output_dir = path if path.is_absolute() \
                else BASE_DIR / path
        save_settings(self._settings)
        self._update_summary()
        self._log("INFO", "Settings saved.")

    # ------------------------------------------------------------------
    # Run
    # ------------------------------------------------------------------

    def _on_start(self) -> None:
        if self._worker is not None:
            return
        if not self._selection.gos:
            show_message(self, "No GOS channel",
                         "Set a channel as GOS in the Browse tab first.",
                         QMessageBox.Warning)
            return
        if not self._selection.selected:
            show_message(self, "Nothing selected",
                         "Tick at least one country before starting.",
                         QMessageBox.Warning)
            return
        if not self._credentials.get("token"):
            show_message(self, "No token",
                         "Add a Productsup token in Settings.",
                         QMessageBox.Warning)
            return

        self.progress.setValue(0)
        self.status_label.setText("Starting…")
        self.start_btn.setEnabled(False)
        self.cancel_btn.setEnabled(True)
        self._set_pages_enabled(False)
        self.results_page.clear()

        self._worker = RunWorker(self._selection, self._settings,
                                 dict(self._credentials), self)
        self._worker.progress.connect(self._on_progress)
        self._worker.log.connect(self._log)
        self._worker.finished_ok.connect(self._on_finished)
        self._worker.failed.connect(self._on_failed)
        self._worker.cancelled.connect(self._on_cancelled)
        self._worker.finished.connect(self._worker.deleteLater)
        self._worker.start()

    def _on_cancel(self) -> None:
        if self._worker is None:
            return
        self._worker.request_stop()
        self.cancel_btn.setEnabled(False)
        self.status_label.setText("Cancelling…")

    def _on_progress(self, update: ProgressUpdate) -> None:
        self.progress.setValue(update.percent)
        text = update.label
        if update.detail:
            text = f"{text} — {update.detail}" if text else update.detail
        self.status_label.setText(f"{update.percent}%  {text}".strip())

    def _on_finished(self, result) -> None:
        self._finish_run()
        self.progress.setValue(100)
        self.status_label.setText(f"Done in {result.elapsed:.0f}s")
        self.results_page.set_rows(result.summary)
        if result.output_path:
            self._last_output = Path(result.output_path).parent
        self.tabs.setCurrentWidget(self.results_page)

    def _on_failed(self, message: str) -> None:
        self._finish_run()
        self.status_label.setText("Failed")
        self._log("ERROR", message)
        first_line = message.strip().splitlines()[0] if message.strip() else "Unknown error"
        show_message(self, "Run failed", first_line, QMessageBox.Critical)

    def _on_cancelled(self) -> None:
        self._finish_run()
        self.status_label.setText("Cancelled")
        self._log("WARNING", "Run cancelled.")

    def _finish_run(self) -> None:
        self._worker = None
        self.cancel_btn.setEnabled(False)
        self._set_pages_enabled(True)
        self.start_btn.setEnabled(bool(self._selection.selected))

    def _set_pages_enabled(self, enabled: bool) -> None:
        """Editing the selection mid-run would not affect the run, only confuse."""
        self.country_page.setEnabled(enabled)
        self.browse_page.setEnabled(enabled)

    # ------------------------------------------------------------------

    def _open_output_dir(self) -> None:
        if self._last_output and Path(self._last_output).exists():
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(self._last_output)))

    def _log(self, level: str, message: str) -> None:
        self.log_view.appendPlainText(f"[{level:<7}] {message}")

    def closeEvent(self, event) -> None:
        if self._worker is not None:
            self._worker.request_stop()
            self._worker.wait(3000)
        super().closeEvent(event)
