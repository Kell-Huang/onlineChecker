#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
onlineStatusChecker UI - SFCC x Productsup Feed Diff Checker.

PySide6 desktop application. Loads the Productsup project list on demand,
lets the user mark one channel as GOS and check comparison feeds, then
runs the comparison in a worker thread and displays charts.

Comparison feeds are held in `_active_feeds`, which is populated from
`config/last_session.json` at startup and updated by UI interactions.
The tree is a convenience view; it is not required for a run.

Token is read ONLY from config/credentials.json.
"""

from __future__ import annotations

import json
import logging
import re
import sys
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import requests
from PySide6.QtCharts import (
    QBarCategoryAxis,
    QBarSeries,
    QBarSet,
    QChart,
    QChartView,
    QValueAxis,
)
from PySide6.QtCore import Qt, QThread, QTimer, Signal
from PySide6.QtGui import QBrush, QColor, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

import compare_exports
from compare_exports import (
    Reporter,
    download_gos_preview,
    load_app_config,
    load_country_columns,
    load_credentials,
    read_csv_header,
    redact_token,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

BASE_DIR = Path(__file__).resolve().parent
CONFIG_DIR = BASE_DIR / "config"
SESSION_PATH = CONFIG_DIR / "last_session.json"

SESSION_VERSION = "8.1"
LOG_VIEW_MAX_BLOCKS = 5000

MULTI_LANG_SUFFIXES = {"de", "fr", "nl", "en", "ar"}
MULTI_COUNTRY_PREFIXES = {"ch", "be", "ca", "ae", "sa"}
NORMALIZATION = {"gb": "uk", "uk": "uk", "co.uk": "uk", "com.au": "au"}

_BUSINESS_TAG_RE = re.compile(r"\s*[([]\s*[^)\]]*\s*[)\]]")
_NEW_MP_SUFFIX_RE = re.compile(r"\s+new\s*-\s*mp\s*$", re.IGNORECASE)
_ATTRIBUTE_SUFFIX_RE = re.compile(
    r"\.attribute(?:-part\d+)?\s*$", re.IGNORECASE
)
_URL_CHANNEL_RE = re.compile(r"/channel/([^/?#]+)")

CHART_BG = "#FFFFFF"
PLOT_BG = "#FAFAFA"
PRIMARY_BAR = "#1D1D1F"
SECONDARY_BAR = "#C7C7CC"
REASON_COLORS = ["#2E7D32", "#EF6C00", "#00838F", "#C62828"]

_LOGGER = logging.getLogger("online_status_checker.ui")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _normalize_country(token: str, aliases: Dict[str, str]) -> str:
    """
    Normalize a country token using the aliases table and the built-in
    NORMALIZATION map. Handles both single-token ('uae') and multi-lang
    ('uae-ar') forms.
    """
    if not token:
        return ""
    lowered = token.lower().strip()
    if lowered in aliases:
        return aliases[lowered]
    if "-" in lowered:
        prefix, _, lang = lowered.partition("-")
        mapped = aliases.get(prefix) or NORMALIZATION.get(prefix) or prefix
        return f"{mapped}-{lang}"
    return aliases.get(lowered) or NORMALIZATION.get(lowered, lowered)


def infer_country(
    site_name: str, aliases: Optional[Dict[str, str]] = None
) -> str:
    """
    Multi-language country inference (see spec 7.8).

    Also strips common business suffixes before matching: parenthesised
    tags, ' New - MP' (marketplace variant), and '.Attribute*' (intermediate
    layer). Both 'fr New - MP' and 'fr.Attribute' therefore resolve to 'fr'.
    """
    if not site_name:
        return ""
    cleaned = _BUSINESS_TAG_RE.sub("", site_name).strip()
    cleaned = _NEW_MP_SUFFIX_RE.sub("", cleaned).strip()
    cleaned = _ATTRIBUTE_SUFFIX_RE.sub("", cleaned).strip()
    parts = cleaned.lower().split(".")
    if len(parts) < 2:
        return ""
    last = parts[-1]
    second = parts[-2]
    alias_map = aliases or {}
    if last in MULTI_LANG_SUFFIXES and second in MULTI_COUNTRY_PREFIXES:
        return _normalize_country(f"{second}-{last}", alias_map)
    return _normalize_country(last, alias_map)


def _refine_country_by_channel(country: str, channel_name: str) -> str:
    """
    Refine an ambiguous country code ('ch' or 'be') into its language
    variant (ch-de / ch-fr / be-nl / be-fr) using the channel name.

    Only a channel name that explicitly contains '<country>-<lang>' or
    '<country> <lang>' (separator being space, dash, underscore or dot)
    triggers refinement. Anything else leaves the country unchanged so
    the user can pick the correct variant in Advanced Settings.
    """
    if country not in ("ch", "be"):
        return country
    if not channel_name:
        return country
    cleaned = _BUSINESS_TAG_RE.sub("", channel_name).strip()
    pattern = rf"\b{country}[\s\-_.]+(de|fr|nl)\b"
    match = re.search(pattern, cleaned, re.IGNORECASE)
    if not match:
        return country
    return f"{country}-{match.group(1).lower()}"


def infer_gos_columns(
    country: str, country_columns: Dict[str, Dict[str, str]]
) -> Dict[str, str]:
    entry = country_columns.get(country) or {}
    return {
        "online": entry.get("online", ""),
        "stock": entry.get("stock", ""),
        "price": entry.get("price", ""),
    }


def make_star_icon() -> QIcon:
    pixmap = QPixmap(16, 16)
    pixmap.fill(Qt.transparent)
    painter = QPainter(pixmap)
    painter.setPen(QColor("#FFB300"))
    font = painter.font()
    font.setPointSize(13)
    painter.setFont(font)
    painter.drawText(pixmap.rect(), Qt.AlignCenter, "\u2605")
    painter.end()
    return QIcon(pixmap)


def extract_feed_url(channel: Dict[str, Any]) -> Optional[str]:
    """Return the first URL of the first active feed destination."""
    for dest in (channel.get("feed_destinations") or {}).values():
        if dest.get("active") and dest.get("urls"):
            return str(dest["urls"][0])
    return None


def channel_is_usable(channel: Dict[str, Any]) -> bool:
    if not channel.get("active"):
        return False
    return extract_feed_url(channel) is not None


def detect_url_format(url: str) -> str:
    path = url.split("?")[0].lower()
    return "xml" if path.endswith(".xml") else "csv"


def make_placeholder_item() -> QTreeWidgetItem:
    """Sub-item shown until a node's children are loaded."""
    item = QTreeWidgetItem(["Loading..."])
    item.setData(0, Qt.UserRole, {"type": "placeholder"})
    return item


# ---------------------------------------------------------------------------
# API workers
# ---------------------------------------------------------------------------


class ProjectsLoaderWorker(QThread):
    """Fetch the top-level project list only."""

    loadedSignal = Signal(list)
    errorSignal = Signal(str)
    logSignal = Signal(str, str)

    def __init__(
        self, token: str, base_url: str, parent: Optional[QWidget] = None
    ) -> None:
        super().__init__(parent)
        self._token = token
        self._base_url = base_url.rstrip("/")

    def _log(self, level: str, message: str) -> None:
        self.logSignal.emit(level, redact_token(message))

    def run(self) -> None:
        try:
            with requests.Session() as session:
                self._log("INFO", f"Contacting {self._base_url}")
                self._log("INFO", "Loading projects...")
                response = session.get(
                    f"{self._base_url}/platform/v2/projects",
                    headers={"X-Auth-Token": self._token},
                    timeout=(5, 30),
                )
                if response.status_code in (401, 403):
                    raise PermissionError(
                        "Token is invalid or lacks permission."
                    )
                if response.status_code >= 400:
                    raise RuntimeError(f"HTTP {response.status_code}")
                projects = response.json().get("Projects") or []
                self._log("INFO", f"Found {len(projects)} project(s).")
                self.loadedSignal.emit(projects)
        except PermissionError as exc:
            self.errorSignal.emit(redact_token(str(exc)))
        except Exception as exc:  # noqa: BLE001
            self.errorSignal.emit(redact_token(f"{type(exc).__name__}: {exc}"))


class SitesLoaderWorker(QThread):
    """Fetch the sites of a single project."""

    loadedSignal = Signal(str, list)
    errorSignal = Signal(str, str)

    def __init__(
        self,
        project_id: str,
        token: str,
        base_url: str,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self._project_id = project_id
        self._token = token
        self._base_url = base_url.rstrip("/")

    def run(self) -> None:
        try:
            with requests.Session() as session:
                url = f"{self._base_url}/platform/v2/projects/{self._project_id}/sites"
                response = session.get(
                    url,
                    headers={"X-Auth-Token": self._token},
                    timeout=(5, 30),
                )
                if response.status_code in (401, 403):
                    raise PermissionError(
                        "Token is invalid or lacks permission."
                    )
                if response.status_code >= 400:
                    raise RuntimeError(f"HTTP {response.status_code}")
                sites = response.json().get("Sites") or []
                self.loadedSignal.emit(self._project_id, sites)
        except Exception as exc:  # noqa: BLE001
            self.errorSignal.emit(
                self._project_id, redact_token(f"{type(exc).__name__}: {exc}")
            )


class ChannelsLoaderWorker(QThread):
    """Fetch the channels of a single site."""

    loadedSignal = Signal(str, list)
    errorSignal = Signal(str, str)

    def __init__(
        self,
        site_id: str,
        token: str,
        base_url: str,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self._site_id = site_id
        self._token = token
        self._base_url = base_url.rstrip("/")

    def run(self) -> None:
        try:
            with requests.Session() as session:
                url = f"{self._base_url}/platform/v2/sites/{self._site_id}/channels"
                response = session.get(
                    url,
                    headers={"X-Auth-Token": self._token},
                    timeout=(5, 30),
                )
                if response.status_code in (401, 403):
                    raise PermissionError(
                        "Token is invalid or lacks permission."
                    )
                if response.status_code >= 400:
                    raise RuntimeError(f"HTTP {response.status_code}")
                channels = response.json().get("Channels") or []
                self.loadedSignal.emit(self._site_id, channels)
        except Exception as exc:  # noqa: BLE001
            self.errorSignal.emit(
                self._site_id, redact_token(f"{type(exc).__name__}: {exc}")
            )


class GosPreviewWorker(QThread):
    """Download the first 500 rows of GOS and detect its columns."""

    finishedSignal = Signal(bool, str, list)

    def __init__(
        self,
        url: str,
        channel_id: str,
        preview_dir: Path,
        download_cfg: Dict[str, Any],
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self._url = url
        self._channel_id = channel_id
        self._preview_dir = preview_dir
        self._cfg = download_cfg

    def run(self) -> None:
        dest = self._preview_dir / f"gos_preview_{self._channel_id}.csv"
        reporter = Reporter()
        cancel_event = threading.Event()
        try:
            columns, _ = download_gos_preview(
                self._url, dest, self._cfg, cancel_event, reporter
            )
        except Exception as exc:  # noqa: BLE001
            self.finishedSignal.emit(False, redact_token(str(exc)), [])
            return
        if not columns:
            self.finishedSignal.emit(
                False, "Unable to download GOS preview.", []
            )
            return
        self.finishedSignal.emit(True, "", list(columns))


class OnlineStatusCheckerWorker(QThread):
    """Run compare_exports.run_comparison in a dedicated thread."""

    progressSignal = Signal(int, str)
    logSignal = Signal(str, str)
    finishedSignal = Signal(dict)

    def __init__(
        self,
        session_config: dict,
        credentials: dict,
        cancel_event: threading.Event,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self._session = session_config
        self._credentials = credentials
        self._cancel = cancel_event

    def run(self) -> None:
        try:
            result = compare_exports.run_comparison(
                self._session,
                self._credentials,
                progress_callback=lambda p, m: self.progressSignal.emit(
                    int(p), m
                ),
                log_callback=lambda lvl, msg: self.logSignal.emit(lvl, msg),
                cancel_event=self._cancel,
            )
        except Exception as exc:  # noqa: BLE001
            result = {
                "success": False,
                "cancelled": False,
                "output_path": None,
                "error": redact_token(f"{type(exc).__name__}: {exc}"),
                "error_kind": "other",
                "summary": [],
                "feed_failures": 0,
            }
        self.finishedSignal.emit(result)


# ---------------------------------------------------------------------------
# Advanced Settings dialog
# ---------------------------------------------------------------------------


class AdvancedSettingsDialog(QDialog):
    """
    Table editor for country code, GOS column mapping and ID column.

    Each feed entry is a mutable dict:
      {key, site_name, channel_name, country, gos_columns, id_column}

    When `gos_preview_columns` is non-empty, values that are absent from the
    GOS file are marked invalid (dynamic property 'invalid=true').

    Changing a Country Code triggers automatic refill of the three GOS
    columns, but only when their current values still match the previous
    country's defaults. User-edited values are preserved.
    """

    def __init__(
        self,
        feeds: List[Dict[str, Any]],
        gos_column_pool: List[str],
        country_column_map: Dict[str, Dict[str, str]],
        country_aliases: Optional[Dict[str, str]] = None,
        gos_preview_columns: Optional[Sequence[str]] = None,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Advanced Settings")
        self.setObjectName("advancedSettingsDialog")
        self.resize(1100, 480)

        self._feeds = feeds
        self._country_map = country_column_map
        self._country_aliases = country_aliases or {}
        self._preview_columns = {
            str(c).strip() for c in (gos_preview_columns or [])
        }

        # Merge built-in mappings into the dropdown pool.
        pool: List[str] = list(gos_column_pool)
        for entry in country_column_map.values():
            for value in entry.values():
                if value and value not in pool:
                    pool.append(value)
        self._gos_pool = sorted(pool)

        # Row -> previous default GOS columns; used to detect user edits.
        self._row_defaults: Dict[int, Dict[str, str]] = {}
        self._suppress_country_signal = False

        self._table = QTableWidget(len(feeds), 8, self)
        self._table.setObjectName("settingsTable")
        self._table.setHorizontalHeaderLabels(
            [
                "Country",
                "Site",
                "Channel",
                "Country Code",
                "Online Column",
                "Stock Column",
                "Price Column",
                "ID Column",
            ]
        )
        self._table.horizontalHeader().setSectionResizeMode(
            QHeaderView.Stretch
        )
        self._table.verticalHeader().setVisible(False)
        self._table.setEditTriggers(QTableWidget.NoEditTriggers)

        for row, feed in enumerate(feeds):
            self._fill_row(row, feed)

        buttons = QDialogButtonBox(self)
        reset_button = buttons.addButton(
            "Reset to Auto", QDialogButtonBox.ResetRole
        )
        save_button = buttons.addButton("Save", QDialogButtonBox.AcceptRole)
        reset_button.setObjectName("resetButton")
        save_button.setObjectName("saveButton")
        save_button.setProperty("role", "primaryButton")
        reset_button.clicked.connect(self._on_reset)
        save_button.clicked.connect(self.accept)

        layout = QVBoxLayout(self)
        layout.addWidget(self._table)
        layout.addWidget(buttons)

    # -- validity ----------------------------------------------------------

    def _is_invalid_country(self, value: str) -> bool:
        return not value

    def _is_invalid_gos_column(self, value: str) -> bool:
        if not value:
            return False
        if not self._preview_columns:
            return False
        return value not in self._preview_columns

    # -- cells -------------------------------------------------------------

    def _readonly_cell(self, text: str, row: int, column: int) -> None:
        item = QTableWidgetItem(text or "")
        item.setFlags(Qt.ItemIsEnabled)
        self._table.setItem(row, column, item)

    def _combo_cell(
        self,
        row: int,
        column: int,
        items: Sequence[str],
        current: str,
        invalid: bool = False,
    ) -> QComboBox:
        combo = QComboBox(self._table)
        combo.setEditable(True)
        combo.addItem("")
        seen = {""}
        for entry in items:
            if entry and entry not in seen:
                seen.add(entry)
                combo.addItem(entry)
        combo.setCurrentText(current or "")
        combo.setProperty("invalid", bool(invalid))
        self._table.setCellWidget(row, column, combo)
        return combo

    def _fill_row(self, row: int, feed: Dict[str, Any]) -> None:
        self._readonly_cell(feed.get("country", ""), row, 0)
        self._readonly_cell(feed.get("site_name", ""), row, 1)
        self._readonly_cell(feed.get("channel_name", ""), row, 2)

        country = feed.get("country", "")
        countries = sorted(self._country_map.keys())
        country_combo = self._combo_cell(
            row,
            3,
            countries,
            country,
            invalid=self._is_invalid_country(country),
        )
        country_combo.currentTextChanged.connect(
            lambda text, r=row: self._on_country_changed(r, text)
        )

        gos_cols = feed.get("gos_columns") or {}
        online = gos_cols.get("online", "")
        stock = gos_cols.get("stock", "")
        price = gos_cols.get("price", "")
        self._combo_cell(
            row,
            4,
            self._gos_pool,
            online,
            invalid=self._is_invalid_gos_column(online),
        )
        self._combo_cell(
            row,
            5,
            self._gos_pool,
            stock,
            invalid=self._is_invalid_gos_column(stock),
        )
        self._combo_cell(
            row,
            6,
            self._gos_pool,
            price,
            invalid=self._is_invalid_gos_column(price),
        )

        id_pool = ["id", "sku", "product_id", "productid", "gtin", "ean"]
        id_value = feed.get("id_column") or ""
        self._combo_cell(row, 7, id_pool, id_value, invalid=not id_value)

        self._row_defaults[row] = infer_gos_columns(country, self._country_map)

    def _combo_text(self, row: int, column: int) -> str:
        widget = self._table.cellWidget(row, column)
        if isinstance(widget, QComboBox):
            return widget.currentText().strip()
        return ""

    # -- actions -----------------------------------------------------------

    def _on_country_changed(self, row: int, new_country: str) -> None:
        """
        Refill GOS columns when a valid Country Code is chosen and the
        current values still match the previous country's defaults.
        """
        if self._suppress_country_signal:
            return
        new_country = (new_country or "").strip()
        if new_country not in self._country_map:
            return

        old_defaults = self._row_defaults.get(row) or {}
        new_defaults = infer_gos_columns(new_country, self._country_map)

        for column_idx, key in ((4, "online"), (5, "stock"), (6, "price")):
            widget = self._table.cellWidget(row, column_idx)
            if not isinstance(widget, QComboBox):
                continue
            current = widget.currentText().strip()
            if current == (old_defaults.get(key) or ""):
                widget.setCurrentText(new_defaults.get(key, ""))

        self._row_defaults[row] = new_defaults

    def _on_reset(self) -> None:
        self._suppress_country_signal = True
        try:
            for row, feed in enumerate(self._feeds):
                country = infer_country(
                    feed.get("site_name", ""), self._country_aliases
                )
                country = _refine_country_by_channel(
                    country, feed.get("channel_name", "")
                )
                cols = infer_gos_columns(country, self._country_map)
                id_value = feed.get("id_column") or "id"

                countries = sorted(self._country_map.keys())
                country_combo = self._combo_cell(
                    row, 3, countries, country, invalid=not country
                )
                country_combo.currentTextChanged.connect(
                    lambda text, r=row: self._on_country_changed(r, text)
                )

                online = cols.get("online", "")
                stock = cols.get("stock", "")
                price = cols.get("price", "")
                self._combo_cell(
                    row,
                    4,
                    self._gos_pool,
                    online,
                    invalid=self._is_invalid_gos_column(online),
                )
                self._combo_cell(
                    row,
                    5,
                    self._gos_pool,
                    stock,
                    invalid=self._is_invalid_gos_column(stock),
                )
                self._combo_cell(
                    row,
                    6,
                    self._gos_pool,
                    price,
                    invalid=self._is_invalid_gos_column(price),
                )
                self._combo_cell(
                    row,
                    7,
                    ["id", "sku", "product_id", "productid", "gtin", "ean"],
                    id_value,
                    invalid=False,
                )

                self._row_defaults[row] = dict(cols)
        finally:
            self._suppress_country_signal = False

    def result_data(self) -> List[Dict[str, Any]]:
        """Return the table content as a list of updated feed dicts."""
        updated: List[Dict[str, Any]] = []
        for row, feed in enumerate(self._feeds):
            entry = dict(feed)
            entry["country"] = self._combo_text(row, 3)
            entry["gos_columns"] = {
                "online": self._combo_text(row, 4),
                "stock": self._combo_text(row, 5),
                "price": self._combo_text(row, 6),
            }
            entry["id_column"] = self._combo_text(row, 7) or None
            updated.append(entry)
        return updated


# ---------------------------------------------------------------------------
# Main window
# ---------------------------------------------------------------------------


class OnlineStatusCheckerMainWindow(QMainWindow):
    """Main application window."""

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("onlineStatusChecker")
        self.setObjectName("mainWindow")
        self.resize(1280, 800)

        # -- state ---------------------------------------------------------
        self._app_cfg: Dict[str, Any] = load_app_config()
        self._preview_dir: Path = (
            BASE_DIR / self._app_cfg["paths"]["preview_dir"]
        )
        self._country_columns, self._country_aliases = load_country_columns()
        self._credentials: Dict[str, str] = {}
        self._credentials_ok: bool = False
        self._tree_data: List[Dict[str, Any]] = []

        # GOS channel: normalized dict, may come from session or the tree.
        self._gos_channel: Optional[Dict[str, Any]] = None
        self._gos_columns_pool: List[str] = []
        self._gos_preview_columns: List[str] = []

        # Active comparison feeds: (site_id, channel_id) -> feed config dict.
        # Single source of truth for what will be run. Only entries with a
        # non-empty URL are ever inserted.
        self._active_feeds: Dict[Tuple[str, str], Dict[str, Any]] = {}

        # Lazy-load workers
        self._projects_worker: Optional[ProjectsLoaderWorker] = None
        self._sites_workers: Dict[str, SitesLoaderWorker] = {}
        self._channels_workers: Dict[str, ChannelsLoaderWorker] = {}
        self._preview_worker: Optional[GosPreviewWorker] = None
        self._comparison_worker: Optional[OnlineStatusCheckerWorker] = None
        self._cancel_event: Optional[threading.Event] = None

        self._result_summary: List[Dict[str, Any]] = []
        self._result_reasons: List[Dict[str, Any]] = []
        self._multi_countries: List[str] = []
        self._current_country: Optional[str] = None
        self._chart_scale: str = "count"

        self._star_icon = make_star_icon()

        self._build_ui()
        self._load_credentials()

    # -- UI construction ---------------------------------------------------

    def _build_ui(self) -> None:
        central = QWidget(self)
        central.setObjectName("centralWidget")
        self.setCentralWidget(central)

        # Toolbar
        toolbar = QWidget(central)
        toolbar.setObjectName("toolbarWidget")
        toolbar_layout = QHBoxLayout(toolbar)
        toolbar_layout.setContentsMargins(8, 8, 8, 8)

        self._advanced_button = QPushButton("Advanced Settings", toolbar)
        self._advanced_button.setObjectName("advancedSettingsButton")
        self._advanced_button.setProperty("secondary", "true")
        self._advanced_button.clicked.connect(self._on_advanced_settings)

        self._start_button = QPushButton("Start Comparison", toolbar)
        self._start_button.setObjectName("startButton")
        self._start_button.setProperty("role", "primaryButton")
        self._start_button.clicked.connect(self._on_start_comparison)

        self._cancel_button = QPushButton("Cancel", toolbar)
        self._cancel_button.setObjectName("cancelButton")
        self._cancel_button.setProperty("secondary", "true")
        self._cancel_button.setEnabled(False)
        self._cancel_button.clicked.connect(self._on_cancel)

        toolbar_layout.addWidget(self._advanced_button)
        toolbar_layout.addStretch(1)
        toolbar_layout.addWidget(self._start_button)
        toolbar_layout.addWidget(self._cancel_button)

        # Tree (left pane)
        self._tree = QTreeWidget(central)
        self._tree.setObjectName("channelTree")
        self._tree.setHeaderHidden(True)
        self._tree.setContextMenuPolicy(Qt.CustomContextMenu)
        self._tree.customContextMenuRequested.connect(
            self._on_tree_context_menu
        )
        self._tree.itemChanged.connect(self._on_item_changed)
        self._tree.itemDoubleClicked.connect(self._on_item_double_clicked)
        self._tree.itemExpanded.connect(self._on_item_expanded)

        # Log view (right pane, Log tab)
        self._log_view = QPlainTextEdit(central)
        self._log_view.setObjectName("logTextEdit")
        self._log_view.setReadOnly(True)
        self._log_view.setMaximumBlockCount(LOG_VIEW_MAX_BLOCKS)

        # Charts panel (right pane, Charts tab)
        self._charts_panel = QWidget(central)
        self._charts_panel.setObjectName("chartsPanel")
        charts_layout = QVBoxLayout(self._charts_panel)
        charts_layout.setContentsMargins(8, 8, 8, 8)
        charts_layout.setSpacing(6)

        scale_row = QWidget(self._charts_panel)
        scale_row.setObjectName("scaleToggleRow")
        scale_layout = QHBoxLayout(scale_row)
        scale_layout.setContentsMargins(0, 0, 0, 0)
        self._count_button = QPushButton("Count", scale_row)
        self._count_button.setObjectName("countToggleButton")
        self._count_button.setProperty("flat", True)
        self._count_button.setProperty("active", True)
        self._count_button.clicked.connect(
            lambda: self._set_chart_scale("count")
        )
        self._percent_button = QPushButton("Percent", scale_row)
        self._percent_button.setObjectName("percentToggleButton")
        self._percent_button.setProperty("flat", True)
        self._percent_button.setProperty("active", False)
        self._percent_button.clicked.connect(
            lambda: self._set_chart_scale("percent")
        )
        scale_layout.addStretch(1)
        scale_layout.addWidget(self._count_button)
        scale_layout.addWidget(self._percent_button)
        scale_layout.addStretch(1)

        self._multi_chart_view = QChartView(self._charts_panel)
        self._multi_chart_view.setObjectName("multiCountryChart")
        self._multi_chart_view.setRenderHint(QPainter.Antialiasing)
        self._multi_chart_view.setMinimumHeight(220)

        self._reason_chart_view = QChartView(self._charts_panel)
        self._reason_chart_view.setObjectName("reasonChart")
        self._reason_chart_view.setRenderHint(QPainter.Antialiasing)
        self._reason_chart_view.setMinimumHeight(220)

        charts_layout.addWidget(scale_row)
        charts_layout.addWidget(self._multi_chart_view, 1)
        charts_layout.addWidget(self._reason_chart_view, 1)

        # Right pane: tab widget with Log + Charts
        self._right_tabs = QTabWidget(central)
        self._right_tabs.setObjectName("rightTabs")
        self._right_tabs.addTab(self._log_view, "Log")
        self._right_tabs.addTab(self._charts_panel, "Charts")

        # Left / right splitter
        self._splitter = QSplitter(Qt.Horizontal, central)
        self._splitter.setObjectName("mainSplitter")
        self._splitter.addWidget(self._tree)
        self._splitter.addWidget(self._right_tabs)
        self._splitter.setStretchFactor(0, 65)
        self._splitter.setStretchFactor(1, 35)
        self._splitter.setSizes([830, 450])

        # Progress
        self._progress_bar = QProgressBar(central)
        self._progress_bar.setObjectName("progressBar")
        self._progress_bar.setRange(0, 100)
        self._progress_bar.setValue(0)

        self._status_label = QLabel("0%", central)
        self._status_label.setObjectName("progressLabel")

        progress_row = QWidget(central)
        progress_row.setObjectName("progressRow")
        progress_layout = QHBoxLayout(progress_row)
        progress_layout.setContentsMargins(8, 0, 8, 8)
        progress_layout.addWidget(self._progress_bar, 1)
        progress_layout.addWidget(self._status_label)

        # Central layout
        layout = QVBoxLayout(central)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        layout.addWidget(toolbar)
        layout.addWidget(self._splitter, 1)
        layout.addWidget(progress_row)

        self._show_empty_chart(self._multi_chart_view, "No data yet")
        self._show_empty_chart(self._reason_chart_view, "No data yet")

        self._update_start_button()

    # -- credentials -------------------------------------------------------

    def _load_credentials(self) -> None:
        try:
            self._credentials = load_credentials()
            self._credentials_ok = True
            self._append_log("INFO", "Credentials loaded.")
        except Exception as exc:  # noqa: BLE001
            self._credentials_ok = False
            self._append_log("ERROR", redact_token(str(exc)))
            QMessageBox.warning(
                self,
                "Cannot start",
                "Cannot start: config/credentials.json is missing or "
                "productsup.token is empty. Format: client_id:client_secret.",
            )
        self._update_start_button()

    # -- tree loading ------------------------------------------------------

    def load_tree(self) -> None:
        if not self._credentials_ok:
            self._append_log("ERROR", "Credentials missing, cannot load tree.")
            return
        self._status_label.setText("Loading projects...")
        self._projects_worker = ProjectsLoaderWorker(
            self._credentials.get("token", ""),
            self._credentials.get("base_url", ""),
            self,
        )
        self._projects_worker.loadedSignal.connect(self._on_projects_loaded)
        self._projects_worker.errorSignal.connect(self._on_projects_error)
        self._projects_worker.logSignal.connect(self._append_log)
        self._projects_worker.start()

    def _on_projects_loaded(self, projects: List[Dict[str, Any]]) -> None:
        self._tree_data = projects
        self._build_project_tree(projects)
        self._status_label.setText("Ready")
        QTimer.singleShot(100, self._restore_session)

    def _on_projects_error(self, message: str) -> None:
        self._status_label.setText("Error")
        self._append_log("ERROR", message)
        QMessageBox.warning(self, "Cannot load channels", message)

    def _build_project_tree(self, projects: List[Dict[str, Any]]) -> None:
        self._tree.blockSignals(True)
        self._tree.clear()
        for project in projects:
            project_item = QTreeWidgetItem(
                [project.get("name") or "(unnamed)"]
            )
            project_item.setData(
                0,
                Qt.UserRole,
                {
                    "type": "project",
                    "data": project,
                    "loaded": False,
                    "loading": False,
                },
            )
            project_item.addChild(make_placeholder_item())
            self._tree.addTopLevelItem(project_item)
        self._tree.blockSignals(False)
        self._tree.collapseAll()

    def _on_item_expanded(self, item: QTreeWidgetItem) -> None:
        data = item.data(0, Qt.UserRole) or {}
        item_type = data.get("type")
        if item_type == "project":
            if not data.get("loaded") and not data.get("loading"):
                project_data = data.get("data") or {}
                self._load_sites_for_project(str(project_data.get("id")))
        elif item_type == "site":
            if not data.get("loaded") and not data.get("loading"):
                site_data = data.get("data") or {}
                self._load_channels_for_site(str(site_data.get("id")))

    # -- project -> sites --------------------------------------------------

    def _load_sites_for_project(self, project_id: str) -> None:
        if project_id in self._sites_workers:
            return

        project_item = self._find_project_item(project_id)
        if project_item is None:
            return
        data = project_item.data(0, Qt.UserRole) or {}
        if data.get("loaded"):
            return
        data["loading"] = True
        project_item.setData(0, Qt.UserRole, data)
        if project_item.childCount() > 0:
            placeholder = project_item.child(0)
            placeholder_data = placeholder.data(0, Qt.UserRole) or {}
            if placeholder_data.get("type") == "placeholder":
                placeholder.setText(0, "Loading sites...")

        worker = SitesLoaderWorker(
            project_id,
            self._credentials.get("token", ""),
            self._credentials.get("base_url", ""),
            self,
        )
        worker.loadedSignal.connect(self._on_sites_loaded)
        worker.errorSignal.connect(self._on_sites_error)
        self._sites_workers[project_id] = worker
        worker.start()

    def _on_sites_loaded(
        self, project_id: str, sites: List[Dict[str, Any]]
    ) -> None:
        worker = self._sites_workers.pop(project_id, None)
        if worker is not None:
            worker.deleteLater()

        project_item = self._find_project_item(project_id)
        if project_item is None:
            return

        data = project_item.data(0, Qt.UserRole) or {}
        data["loading"] = False
        data["loaded"] = True
        project_item.setData(0, Qt.UserRole, data)

        self._tree.blockSignals(True)
        project_item.takeChildren()
        project_name = (data.get("data") or {}).get("name") or ""
        for site in sites:
            site_item = QTreeWidgetItem([site.get("title") or "(unnamed)"])
            site_item.setData(
                0,
                Qt.UserRole,
                {
                    "type": "site",
                    "data": site,
                    "loaded": False,
                    "loading": False,
                },
            )
            site_item.addChild(make_placeholder_item())
            project_item.addChild(site_item)
        self._tree.blockSignals(False)

        self._append_log(
            "INFO",
            f"Loaded {len(sites)} site(s) for project '{project_name}'.",
        )

    def _on_sites_error(self, project_id: str, error: str) -> None:
        worker = self._sites_workers.pop(project_id, None)
        if worker is not None:
            worker.deleteLater()

        project_item = self._find_project_item(project_id)
        if project_item is not None:
            data = project_item.data(0, Qt.UserRole) or {}
            data["loading"] = False
            project_item.setData(0, Qt.UserRole, data)
            project_item.takeChildren()
            error_item = QTreeWidgetItem([f"Error: {error}"])
            error_item.setForeground(0, QBrush(QColor("#C62828")))
            error_item.setData(0, Qt.UserRole, {"type": "error"})
            project_item.addChild(error_item)

        self._append_log(
            "ERROR", f"Failed to load sites for project {project_id}: {error}"
        )

    # -- site -> channels --------------------------------------------------

    def _load_channels_for_site(self, site_id: str) -> None:
        if site_id in self._channels_workers:
            return

        site_item = self._find_site_item(site_id)
        if site_item is None:
            return
        data = site_item.data(0, Qt.UserRole) or {}
        if data.get("loaded"):
            return
        data["loading"] = True
        site_item.setData(0, Qt.UserRole, data)
        if site_item.childCount() > 0:
            placeholder = site_item.child(0)
            placeholder_data = placeholder.data(0, Qt.UserRole) or {}
            if placeholder_data.get("type") == "placeholder":
                placeholder.setText(0, "Loading channels...")

        worker = ChannelsLoaderWorker(
            site_id,
            self._credentials.get("token", ""),
            self._credentials.get("base_url", ""),
            self,
        )
        worker.loadedSignal.connect(self._on_channels_loaded)
        worker.errorSignal.connect(self._on_channels_error)
        self._channels_workers[site_id] = worker
        worker.start()

    def _on_channels_loaded(
        self, site_id: str, channels: List[Dict[str, Any]]
    ) -> None:
        worker = self._channels_workers.pop(site_id, None)
        if worker is not None:
            worker.deleteLater()

        site_item = self._find_site_item(site_id)
        if site_item is None:
            return

        data = site_item.data(0, Qt.UserRole) or {}
        data["loading"] = False
        data["loaded"] = True
        site_item.setData(0, Qt.UserRole, data)

        site_data = data.get("data") or {}
        site_title = site_data.get("title") or ""

        # project_id is read from the parent project item; needed when
        # persisting the session so subsequent launches can locate sites.
        parent_item = site_item.parent()
        parent_data = (
            (parent_item.data(0, Qt.UserRole) or {}) if parent_item else {}
        )
        parent_payload = parent_data.get("data") or {}
        project_id = str(parent_payload.get("id") or "")

        usable_count = 0
        self._tree.blockSignals(True)
        site_item.takeChildren()
        for channel in channels:
            if not channel_is_usable(channel):
                continue
            url = extract_feed_url(channel) or ""
            channel_data = {
                "channel_id": str(channel.get("id")),
                "channel_name": channel.get("name") or "",
                "site_id": site_id,
                "site_name": site_title,
                "project_id": project_id,
                "url": url,
                "format": detect_url_format(url),
            }
            channel_item = QTreeWidgetItem(
                [channel_data["channel_name"] or "(unnamed)"]
            )
            channel_item.setFlags(
                channel_item.flags() | Qt.ItemIsUserCheckable
            )
            channel_item.setData(
                0, Qt.UserRole, {"type": "channel", "data": channel_data}
            )

            key = (site_id, channel_data["channel_id"])
            if key in self._active_feeds:
                # Refresh the stored entry with the latest API data; the
                # session snapshot may be stale (URL or name changed).
                entry = self._active_feeds[key]
                entry["site_name"] = channel_data["site_name"]
                entry["project_id"] = channel_data["project_id"]
                entry["channel_name"] = channel_data["channel_name"]
                entry["url"] = channel_data["url"]
                entry["format"] = channel_data["format"]
                channel_item.setCheckState(0, Qt.Checked)
            else:
                channel_item.setCheckState(0, Qt.Unchecked)

            if (
                self._gos_channel
                and self._channel_key(self._gos_channel) == key
            ):
                channel_item.setIcon(0, self._star_icon)

            site_item.addChild(channel_item)
            usable_count += 1
        self._tree.blockSignals(False)

        self._append_log(
            "INFO",
            f"Loaded {usable_count}/{len(channels)} usable channel(s) for site "
            f"'{site_title}'.",
        )

    def _on_channels_error(self, site_id: str, error: str) -> None:
        worker = self._channels_workers.pop(site_id, None)
        if worker is not None:
            worker.deleteLater()

        site_item = self._find_site_item(site_id)
        if site_item is not None:
            data = site_item.data(0, Qt.UserRole) or {}
            data["loading"] = False
            site_item.setData(0, Qt.UserRole, data)
            site_item.takeChildren()
            error_item = QTreeWidgetItem([f"Error: {error}"])
            error_item.setForeground(0, QBrush(QColor("#C62828")))
            error_item.setData(0, Qt.UserRole, {"type": "error"})
            site_item.addChild(error_item)

        self._append_log(
            "ERROR", f"Failed to load channels for site {site_id}: {error}"
        )

    # -- session restore ---------------------------------------------------

    def _restore_session(self) -> None:
        """
        Read config/last_session.json and populate `_active_feeds` and
        `_gos_channel` directly. No site or channel is fetched here; the
        tree is populated lazily only when the user expands it.
        """
        if not SESSION_PATH.exists():
            return
        try:
            with SESSION_PATH.open("r", encoding="utf-8") as handle:
                session = json.load(handle)
        except Exception as exc:  # noqa: BLE001
            self._append_log(
                "WARNING", f"Failed to read last_session.json: {exc}"
            )
            return

        version = str(session.get("version") or "").strip()
        if version != SESSION_VERSION:
            self._append_log(
                "WARNING",
                f"Session version '{version or 'unknown'}' differs from expected "
                f"'{SESSION_VERSION}'. Continuing anyway.",
            )

        # GOS
        gos_cfg = session.get("gos") or {}
        if gos_cfg:
            channel = self._session_cfg_to_channel(gos_cfg)
            if channel.get("channel_id") and channel.get("url"):
                self._gos_channel = channel
                self._load_gos_preview_from_disk_or_download(channel)
                self._append_log(
                    "INFO",
                    f"Restored GOS: {channel.get('channel_name')}",
                )

        # Comparison feeds. Entries without a usable URL are dropped, since
        # they cannot be downloaded during the comparison run.
        restored = 0
        skipped = 0
        for feed_cfg in session.get("feeds") or []:
            channel = self._session_cfg_to_feed(feed_cfg)
            key = self._channel_key(channel)
            if not key[0] or not key[1] or not channel.get("url"):
                skipped += 1
                continue
            self._active_feeds[key] = channel
            restored += 1

        if restored or self._gos_channel is not None:
            self._append_log(
                "INFO",
                f"Session loaded: {restored} feed(s) and "
                f"{'1 GOS' if self._gos_channel else 'no GOS'}.",
            )
        if skipped:
            self._append_log(
                "WARNING",
                f"{skipped} session feed(s) skipped (missing site_id, "
                f"channel_id or url).",
            )

        self._mark_gos_in_tree()
        self._update_start_button()

    def _session_cfg_to_channel(self, cfg: Dict[str, Any]) -> Dict[str, Any]:
        """Normalize a session GOS block into a channel dict."""
        channel_id = str(cfg.get("channel_id") or "")
        url = str(cfg.get("url") or "")
        if not channel_id and url:
            match = _URL_CHANNEL_RE.search(url)
            if match:
                channel_id = match.group(1)
        return {
            "channel_id": channel_id,
            "channel_name": str(cfg.get("channel_name") or ""),
            "site_id": str(cfg.get("site_id") or ""),
            "site_name": str(cfg.get("site_name") or ""),
            "project_id": str(cfg.get("project_id") or ""),
            "url": url,
            "format": str(cfg.get("format") or "csv"),
        }

    def _session_cfg_to_feed(self, cfg: Dict[str, Any]) -> Dict[str, Any]:
        """Normalize a session feed entry into the `_active_feeds` shape."""
        channel_id = str(cfg.get("channel_id") or "")
        url = str(cfg.get("url") or "")
        if not channel_id and url:
            match = _URL_CHANNEL_RE.search(url)
            if match:
                channel_id = match.group(1)
        return {
            "country": str(cfg.get("country") or ""),
            "site_id": str(cfg.get("site_id") or ""),
            "site_name": str(cfg.get("site_name") or ""),
            "project_id": str(cfg.get("project_id") or ""),
            "channel_id": channel_id,
            "channel_name": str(cfg.get("channel_name") or ""),
            "url": url,
            "format": str(cfg.get("format") or "csv"),
            "id_column": cfg.get("id_column"),
            "gos_columns": dict(cfg.get("gos_columns") or {}),
        }

    # -- tree lookup helpers -----------------------------------------------

    def _find_project_item(self, project_id: str) -> Optional[QTreeWidgetItem]:
        for i in range(self._tree.topLevelItemCount()):
            item = self._tree.topLevelItem(i)
            data = item.data(0, Qt.UserRole) or {}
            if data.get("type") != "project":
                continue
            project_data = data.get("data") or {}
            if str(project_data.get("id")) == str(project_id):
                return item
        return None

    def _find_site_item(self, site_id: str) -> Optional[QTreeWidgetItem]:
        for i in range(self._tree.topLevelItemCount()):
            project_item = self._tree.topLevelItem(i)
            for j in range(project_item.childCount()):
                site_item = project_item.child(j)
                data = site_item.data(0, Qt.UserRole) or {}
                if data.get("type") != "site":
                    continue
                site_data = data.get("data") or {}
                if str(site_data.get("id")) == str(site_id):
                    return site_item
        return None

    def _channel_key(self, channel: Dict[str, Any]) -> Tuple[str, str]:
        site_id = str(channel.get("site_id") or "")
        channel_id = str(channel.get("channel_id") or "")
        if not channel_id:
            url = channel.get("url") or ""
            match = _URL_CHANNEL_RE.search(url)
            if match:
                channel_id = match.group(1)
        return (site_id, channel_id)

    def _iter_channel_items(self):
        for i in range(self._tree.topLevelItemCount()):
            project_item = self._tree.topLevelItem(i)
            for j in range(project_item.childCount()):
                site_item = project_item.child(j)
                site_data = site_item.data(0, Qt.UserRole) or {}
                if site_data.get("type") != "site":
                    continue
                for k in range(site_item.childCount()):
                    channel_item = site_item.child(k)
                    channel_data = channel_item.data(0, Qt.UserRole) or {}
                    if channel_data.get("type") == "channel":
                        yield channel_item

    def _find_channel_item(
        self, site_id: str, channel_id: str
    ) -> Optional[QTreeWidgetItem]:
        for item in self._iter_channel_items():
            data = item.data(0, Qt.UserRole) or {}
            channel = data.get("data") or {}
            if str(channel.get("site_id")) == str(site_id) and str(
                channel.get("channel_id")
            ) == str(channel_id):
                return item
        return None

    # -- selection / active feeds ------------------------------------------

    def _add_to_active_feeds(
        self, channel: Dict[str, Any]
    ) -> Optional[Dict[str, Any]]:
        """
        Register a channel as an active comparison feed and return the
        entry. Returns None when the channel is unusable (no channel_id
        or no feed URL), so that callers can roll back any UI change.

        Country inference runs `infer_country` and then refines ambiguous
        codes ('ch', 'be') using the channel name.
        """
        key = self._channel_key(channel)
        if not key[1]:
            return None

        entry = self._active_feeds.get(key)
        if entry is not None:
            return entry

        url = str(channel.get("url") or "")
        if not url:
            return None

        country = infer_country(
            channel.get("site_name", ""), self._country_aliases
        )
        country = _refine_country_by_channel(
            country, channel.get("channel_name", "")
        )
        gos_cols = infer_gos_columns(country, self._country_columns)
        entry = {
            "country": country,
            "site_id": str(channel.get("site_id") or ""),
            "site_name": channel.get("site_name", ""),
            "project_id": str(channel.get("project_id") or ""),
            "channel_id": str(channel.get("channel_id") or ""),
            "channel_name": channel.get("channel_name") or "",
            "url": url,
            "format": channel.get("format", "csv"),
            "id_column": None,
            "gos_columns": gos_cols,
        }
        self._active_feeds[key] = entry
        return entry

    def _set_channel_checked(
        self, channel: Dict[str, Any], checked: bool
    ) -> None:
        """Set the tree checkbox without triggering the itemChanged handler."""
        item = self._find_channel_item(
            channel.get("site_id"), channel.get("channel_id")
        )
        if item is None:
            return
        self._tree.blockSignals(True)
        item.setCheckState(0, Qt.Checked if checked else Qt.Unchecked)
        self._tree.blockSignals(False)

    def _on_item_changed(self, item: QTreeWidgetItem, _column: int) -> None:
        data = item.data(0, Qt.UserRole) or {}
        if data.get("type") != "channel":
            return
        channel = data.get("data") or {}
        key = self._channel_key(channel)
        checked = item.checkState(0) == Qt.Checked

        # GOS wins over comparison feed.
        if (
            checked
            and self._gos_channel is not None
            and self._channel_key(self._gos_channel) == key
        ):
            self._tree.blockSignals(True)
            item.setCheckState(0, Qt.Unchecked)
            self._tree.blockSignals(False)
            QMessageBox.information(
                self,
                "Channel is GOS",
                "This channel is set as GOS and cannot be a comparison feed.",
            )
            return

        if checked:
            if self._add_to_active_feeds(channel) is None:
                # Roll back the checkbox when the channel cannot be used.
                self._tree.blockSignals(True)
                item.setCheckState(0, Qt.Unchecked)
                self._tree.blockSignals(False)
        else:
            self._active_feeds.pop(key, None)

        self._update_start_button()

    # -- context menu / dialogs --------------------------------------------

    def _on_tree_context_menu(self, position) -> None:
        item = self._tree.itemAt(position)
        if item is None:
            return
        data = item.data(0, Qt.UserRole) or {}
        if data.get("type") != "channel":
            return
        channel = data.get("data") or {}
        key = self._channel_key(channel)

        menu = QMenu(self)
        if (
            self._gos_channel is not None
            and self._channel_key(self._gos_channel) == key
        ):
            action = menu.addAction("Unset GOS")
            action.triggered.connect(self._on_unset_gos)
        else:
            action = menu.addAction("Set as GOS")
            action.triggered.connect(
                lambda _=False, c=channel: self._on_set_gos(c)
            )

        menu.exec(self._tree.viewport().mapToGlobal(position))

    def _on_item_double_clicked(
        self, item: QTreeWidgetItem, _column: int
    ) -> None:
        data = item.data(0, Qt.UserRole) or {}
        if data.get("type") != "channel":
            return
        channel = data.get("data") or {}
        key = self._channel_key(channel)

        if (
            self._gos_channel is not None
            and self._channel_key(self._gos_channel) == key
        ):
            return

        if key not in self._active_feeds:
            self._tree.blockSignals(True)
            item.setCheckState(0, Qt.Checked)
            self._tree.blockSignals(False)
            if self._add_to_active_feeds(channel) is None:
                self._tree.blockSignals(True)
                item.setCheckState(0, Qt.Unchecked)
                self._tree.blockSignals(False)
                self._update_start_button()
                return
            self._update_start_button()

        self._open_advanced_settings(single_key=key)

    # -- GOS handling ------------------------------------------------------

    def _on_set_gos(self, channel: Dict[str, Any]) -> None:
        if (
            self._preview_worker is not None
            and self._preview_worker.isRunning()
        ):
            QMessageBox.information(
                self, "Please wait", "GOS preview is still downloading."
            )
            return

        if not channel.get("url"):
            QMessageBox.warning(
                self,
                "Invalid GOS channel",
                "Selected channel has no feed URL and cannot be used as GOS.",
            )
            return

        self._status_label.setText("Downloading GOS preview...")
        self._append_log(
            "INFO",
            f"Fetching GOS preview for {channel.get('channel_name')}...",
        )
        self._preview_worker = GosPreviewWorker(
            channel.get("url", ""),
            str(channel.get("channel_id")),
            self._preview_dir,
            self._app_cfg["download"],
            self,
        )
        self._preview_worker.finishedSignal.connect(
            lambda ok, err, cols, c=channel: self._on_gos_preview_done(
                c, ok, err, cols
            )
        )
        self._preview_worker.start()

    def _on_gos_preview_done(
        self,
        channel: Dict[str, Any],
        ok: bool,
        error: str,
        columns: List[str],
    ) -> None:
        self._preview_worker = None
        self._status_label.setText("Ready")

        if not ok:
            self._append_log("ERROR", f"GOS preview failed: {error}")
            QMessageBox.warning(
                self,
                "Unable to download GOS preview",
                "Unable to download GOS preview. Please check the network or re-select the GOS channel.",
            )
            return
        if "id" not in columns:
            QMessageBox.warning(
                self,
                "Invalid GOS file",
                "GOS file must contain an 'id' column. Please select a different channel.",
            )
            return

        # Remove the previous GOS preview file when switching channels.
        if self._gos_channel is not None:
            old_id = str(self._gos_channel.get("channel_id"))
            new_id = str(channel.get("channel_id"))
            if old_id != new_id:
                old_preview = self._preview_dir / f"gos_preview_{old_id}.csv"
                try:
                    if old_preview.exists():
                        old_preview.unlink()
                except OSError:
                    pass

        # Normalize incoming channel (may be a tree dict or session dict).
        self._gos_channel = {
            "channel_id": str(channel.get("channel_id") or ""),
            "channel_name": str(
                channel.get("channel_name") or channel.get("name") or ""
            ),
            "site_id": str(channel.get("site_id") or ""),
            "site_name": str(channel.get("site_name") or ""),
            "project_id": str(channel.get("project_id") or ""),
            "url": str(channel.get("url") or ""),
            "format": str(channel.get("format") or "csv"),
        }
        self._gos_preview_columns = list(columns)
        self._gos_columns_pool = list(columns)
        self._mark_gos_in_tree()

        # GOS wins over comparison feed.
        key = self._channel_key(self._gos_channel)
        if key in self._active_feeds:
            self._active_feeds.pop(key, None)
            self._set_channel_checked(self._gos_channel, False)

        self._refresh_gos_columns()
        self._append_log(
            "INFO", f"GOS set to {self._gos_channel.get('channel_name')}."
        )
        self._update_start_button()

    def _on_unset_gos(self) -> None:
        if self._gos_channel is not None:
            channel_id = str(self._gos_channel.get("channel_id"))
            preview = self._preview_dir / f"gos_preview_{channel_id}.csv"
            try:
                if preview.exists():
                    preview.unlink()
            except OSError:
                pass
        self._gos_channel = None
        self._gos_preview_columns = []
        self._gos_columns_pool = []
        self._mark_gos_in_tree()
        self._refresh_gos_columns()
        self._append_log("INFO", "GOS unset.")
        self._update_start_button()

    def _mark_gos_in_tree(self) -> None:
        for item in self._iter_channel_items():
            data = item.data(0, Qt.UserRole) or {}
            channel = data.get("data") or {}
            if self._gos_channel and self._channel_key(
                channel
            ) == self._channel_key(self._gos_channel):
                item.setIcon(0, self._star_icon)
            else:
                item.setIcon(0, QIcon())

    def _refresh_gos_columns(self) -> None:
        """Refresh the GOS column pool: built-in mapping UNION preview columns."""
        pool: List[str] = []
        for entry in self._country_columns.values():
            for value in entry.values():
                if value and value not in pool:
                    pool.append(value)
        for column in self._gos_preview_columns:
            if column and column not in pool:
                pool.append(column)
        self._gos_columns_pool = sorted(pool)

    # -- Advanced Settings -------------------------------------------------

    def _on_advanced_settings(self) -> None:
        self._open_advanced_settings(single_key=None)

    def _open_advanced_settings(
        self, single_key: Optional[Tuple[str, str]]
    ) -> None:
        if single_key is not None:
            keys = [single_key]
        else:
            keys = list(self._active_feeds.keys())

        if not keys:
            QMessageBox.information(
                self,
                "No comparison feeds",
                "Please select at least one comparison feed first.",
            )
            return

        feeds: List[Dict[str, Any]] = []
        for key in keys:
            settings = self._active_feeds.get(key)
            if settings is None:
                continue
            feeds.append(
                {
                    "key": key,
                    "site_name": settings.get("site_name", ""),
                    "channel_name": settings.get("channel_name", ""),
                    "country": settings.get("country", ""),
                    "gos_columns": dict(settings.get("gos_columns") or {}),
                    "id_column": settings.get("id_column"),
                }
            )

        if not feeds:
            QMessageBox.information(
                self,
                "No comparison feeds",
                "Please select at least one comparison feed first.",
            )
            return

        dialog = AdvancedSettingsDialog(
            feeds,
            self._gos_columns_pool,
            self._country_columns,
            self._country_aliases,
            self._gos_preview_columns,
            self,
        )
        if dialog.exec() != QDialog.Accepted:
            return

        for entry in dialog.result_data():
            key = entry.get("key")
            if key is None:
                continue
            settings = self._active_feeds.get(key)
            if settings is None:
                continue
            settings["country"] = entry.get("country", "")
            settings["gos_columns"] = entry.get("gos_columns") or {}
            settings["id_column"] = entry.get("id_column")

        self._append_log("INFO", "Advanced settings updated.")
        self._update_start_button()

    # -- GOS preview (disk or re-download) ---------------------------------

    def _load_gos_preview_from_disk_or_download(
        self, channel: Dict[str, Any]
    ) -> None:
        if (
            self._preview_worker is not None
            and self._preview_worker.isRunning()
        ):
            return

        channel_id = str(channel.get("channel_id"))
        if not channel_id:
            return

        preview = self._preview_dir / f"gos_preview_{channel_id}.csv"
        if preview.exists():
            try:
                header, _, _ = read_csv_header(preview)
                self._gos_preview_columns = list(header)
                self._refresh_gos_columns()
                return
            except Exception as exc:  # noqa: BLE001
                self._append_log(
                    "WARNING", f"Failed to read GOS preview: {exc}"
                )

        url = str(channel.get("url") or "")
        if not url:
            self._append_log(
                "WARNING",
                "GOS preview missing and channel has no URL to re-download.",
            )
            return

        self._append_log("INFO", "Re-downloading GOS preview...")
        worker = GosPreviewWorker(
            url,
            channel_id,
            self._preview_dir,
            self._app_cfg["download"],
            self,
        )
        worker.finishedSignal.connect(
            lambda ok, err, cols, c=channel: self._on_gos_preview_done(
                c, ok, err, cols
            )
        )
        self._preview_worker = worker
        worker.start()

    # -- Log output --------------------------------------------------------

    def _append_log(self, level: str, message: str) -> None:
        timestamp = datetime.now().strftime("%H:%M:%S")
        self._log_view.appendPlainText(
            f"[{timestamp}] [{level}] {redact_token(message)}"
        )

    # -- Start / Cancel ----------------------------------------------------

    def _update_start_button(self) -> None:
        has_feeds = bool(self._active_feeds)
        has_gos = self._gos_channel is not None
        self._start_button.setEnabled(
            self._credentials_ok
            and has_feeds
            and has_gos
            and self._comparison_worker is None
        )

    def _on_cancel(self) -> None:
        if self._cancel_event is not None:
            self._cancel_event.set()
            self._append_log("INFO", "Cancellation requested.")
            self._cancel_button.setEnabled(False)

    def _on_start_comparison(self) -> None:
        if not self._credentials_ok:
            QMessageBox.warning(
                self,
                "Cannot start",
                "config/credentials.json is missing or invalid.",
            )
            return

        if self._gos_channel is None:
            QMessageBox.information(
                self,
                "No GOS selected",
                "Please select a GOS and comparison feeds first.",
            )
            return

        if not self._active_feeds:
            QMessageBox.information(
                self,
                "No comparison feeds",
                "Please select a GOS and comparison feeds first.",
            )
            return

        feeds_cfg = self._build_feeds_config()
        if not feeds_cfg:
            QMessageBox.warning(
                self,
                "No usable feeds",
                "No comparison feed has a valid feed URL.",
            )
            return

        session_config = {
            "version": SESSION_VERSION,
            "gos": self._build_gos_config(),
            "sampling": {
                "max_per_country": 1000,
                "batch_size": 200,
                "random_seed": 42,
            },
            "country_mapping": dict(NORMALIZATION),
            "feeds": feeds_cfg,
        }

        try:
            CONFIG_DIR.mkdir(parents=True, exist_ok=True)
            with SESSION_PATH.open("w", encoding="utf-8") as handle:
                json.dump(session_config, handle, indent=2, ensure_ascii=False)
            self._append_log("INFO", f"Session saved to {SESSION_PATH.name}.")
        except Exception as exc:  # noqa: BLE001
            self._append_log("WARNING", f"Failed to save session: {exc}")

        self._progress_bar.setValue(0)
        self._status_label.setText("0%")
        self._result_summary = []
        self._result_reasons = []
        self._show_empty_chart(self._multi_chart_view, "No data yet")
        self._show_empty_chart(self._reason_chart_view, "No data yet")

        # Switch to Log tab so the user can watch the run.
        self._right_tabs.setCurrentWidget(self._log_view)

        self._cancel_event = threading.Event()
        self._comparison_worker = OnlineStatusCheckerWorker(
            session_config, self._credentials, self._cancel_event, self
        )
        self._comparison_worker.progressSignal.connect(self._on_progress)
        self._comparison_worker.logSignal.connect(self._on_log_message)
        self._comparison_worker.finishedSignal.connect(
            self._on_comparison_finished
        )

        self._start_button.setEnabled(False)
        self._cancel_button.setEnabled(True)
        self._comparison_worker.start()

    def _build_gos_config(self) -> Dict[str, Any]:
        channel = self._gos_channel or {}
        return {
            "site_id": str(channel.get("site_id")),
            "site_name": channel.get("site_name", ""),
            "project_id": str(channel.get("project_id") or ""),
            "channel_id": str(channel.get("channel_id")),
            "channel_name": channel.get("channel_name", ""),
            "url": channel.get("url", ""),
            "format": channel.get("format", "csv"),
            "id_column": "id",
        }

    def _build_feeds_config(self) -> List[Dict[str, Any]]:
        feeds: List[Dict[str, Any]] = []
        for settings in self._active_feeds.values():
            url = str(settings.get("url") or "")
            channel_id = str(settings.get("channel_id") or "")
            if not url or not channel_id:
                continue
            feeds.append(
                {
                    "country": settings.get("country", ""),
                    "site_id": str(settings.get("site_id", "")),
                    "site_name": settings.get("site_name", ""),
                    "project_id": str(settings.get("project_id", "")),
                    "channel_id": channel_id,
                    "channel_name": settings.get("channel_name", ""),
                    "url": url,
                    "format": settings.get("format", "csv"),
                    "id_column": settings.get("id_column") or None,
                    "gos_columns": dict(settings.get("gos_columns") or {}),
                }
            )
        return feeds

    # -- progress / logs / completion --------------------------------------

    def _on_progress(self, percent: int, message: str) -> None:
        self._progress_bar.setValue(int(percent))
        if message:
            self._status_label.setText(f"{percent}%  {message}")
        else:
            self._status_label.setText(f"{percent}%")
        if percent >= 90:
            self._cancel_button.setEnabled(False)

    def _on_log_message(self, level: str, message: str) -> None:
        self._append_log(level, message)

    def _on_comparison_finished(self, result: Dict[str, Any]) -> None:
        self._comparison_worker = None
        self._cancel_event = None
        self._cancel_button.setEnabled(False)
        self._update_start_button()

        if result.get("cancelled"):
            self._status_label.setText("Cancelled")
            self._append_log("WARNING", "Comparison cancelled.")
            return

        if not result.get("success"):
            error = result.get("error") or "Unknown error."
            self._status_label.setText("Failed")
            self._append_log("ERROR", error)
            kind = result.get("error_kind")
            title = "Comparison failed"
            if kind == "excel":
                title = "Report writing failed"
            QMessageBox.warning(self, title, redact_token(error))
            return

        self._status_label.setText("Done")
        output_path = result.get("output_path")
        if output_path:
            # "Report written to" is already logged by compare_exports.
            self._reload_reason_rows(output_path)

        self._result_summary = list(result.get("summary") or [])
        if not output_path:
            self._result_reasons = []
        self._build_charts()

        # Switch to the Charts tab so the user sees the results.
        self._right_tabs.setCurrentWidget(self._charts_panel)

        if result.get("feed_failures"):
            QMessageBox.warning(
                self,
                "Feed download failed",
                f"{result['feed_failures']} comparison feed(s) failed to download. "
                f"See the API Log sheet for details.",
            )

    # -- charts ------------------------------------------------------------

    def _show_empty_chart(self, view: QChartView, text: str) -> None:
        chart = QChart()
        chart.setBackgroundBrush(QBrush(QColor(CHART_BG)))
        chart.setTitle(text)
        chart.legend().setVisible(False)
        view.setChart(chart)

    def _build_charts(self) -> None:
        if not self._result_summary:
            self._show_empty_chart(self._multi_chart_view, "No data yet")
            self._show_empty_chart(self._reason_chart_view, "No data yet")
            return
        self._build_multi_chart()
        ranked = sorted(
            self._result_summary,
            key=lambda r: (
                -(r.get("missing_count") or 0),
                str(r.get("country", "")),
            ),
        )
        if ranked:
            self._update_reason_chart(str(ranked[0].get("country", "")))

    def _set_chart_scale(self, scale: str) -> None:
        self._chart_scale = scale
        self._count_button.setProperty("active", scale == "count")
        self._percent_button.setProperty("active", scale == "percent")
        for button in (self._count_button, self._percent_button):
            button.style().unpolish(button)
            button.style().polish(button)
        if self._result_summary:
            self._build_multi_chart()

    def _build_multi_chart(self) -> None:
        rows = sorted(
            self._result_summary,
            key=lambda r: (
                -(r.get("missing_count") or 0),
                str(r.get("country", "")),
            ),
        )
        countries = [str(row.get("country", "")) for row in rows]
        self._multi_countries = countries

        skipped_set = QBarSet("Skipped")
        skipped_set.setColor(QColor(PRIMARY_BAR))
        missing_set = QBarSet("Missing")
        missing_set.setColor(QColor(SECONDARY_BAR))

        max_value = 0.0
        for row in rows:
            sampled = float(row.get("sampled_count") or 0)
            gos = float(row.get("gos_count") or 0)
            skipped = float(row.get("skipped_count") or 0)
            missing = float(row.get("missing_count") or 0)

            if self._chart_scale == "percent":
                skipped_value = (
                    (100.0 * skipped / sampled) if sampled > 0 else 0.0
                )
                missing_value = (100.0 * missing / gos) if gos > 0 else 0.0
            else:
                skipped_value = skipped
                missing_value = missing

            skipped_set.append(skipped_value)
            missing_set.append(missing_value)
            max_value = max(max_value, skipped_value, missing_value)

        series = QBarSeries()
        series.append(skipped_set)
        series.append(missing_set)
        series.clicked.connect(self._on_multi_bar_clicked)

        chart = QChart()
        chart.setTitle("Missing vs Skipped by Country")
        chart.setBackgroundBrush(QBrush(QColor(CHART_BG)))
        chart.setPlotAreaBackgroundBrush(QBrush(QColor(PLOT_BG)))
        chart.setPlotAreaBackgroundVisible(True)
        chart.addSeries(series)

        axis_x = QBarCategoryAxis()
        axis_x.append(countries)
        chart.addAxis(axis_x, Qt.AlignBottom)
        series.attachAxis(axis_x)

        axis_y = QValueAxis()
        axis_y.setRange(0, max(1.0, max_value * 1.15))
        axis_y.setLabelFormat(
            "%d" if self._chart_scale == "count" else "%.0f%%"
        )
        chart.addAxis(axis_y, Qt.AlignLeft)
        series.attachAxis(axis_y)

        self._multi_chart_view.setChart(chart)

    def _on_multi_bar_clicked(self, index: int, _barset) -> None:
        if 0 <= index < len(self._multi_countries):
            self._update_reason_chart(self._multi_countries[index])

    def _update_reason_chart(self, country: str) -> None:
        self._current_country = country

        reasons = [
            r for r in self._result_reasons if str(r.get("country")) == country
        ]
        if not reasons:
            self._show_empty_chart(
                self._reason_chart_view, f"No data for {country}"
            )
            return

        reasons = sorted(
            reasons,
            key=lambda r: (-(r.get("count") or 0), str(r.get("reason", ""))),
        )[:10]

        reason_bar_set = QBarSet("Count")
        reason_bar_set.setColor(QColor(REASON_COLORS[0]))
        categories: List[str] = []
        values: List[float] = []
        for entry in reasons:
            categories.append(str(entry.get("reason", ""))[:40])
            value = float(entry.get("count") or 0)
            values.append(value)
            reason_bar_set.append(value)

        series = QBarSeries()
        series.append(reason_bar_set)

        chart = QChart()
        chart.setTitle(f"Skip Reasons - {country}")
        chart.setBackgroundBrush(QBrush(QColor(CHART_BG)))
        chart.setPlotAreaBackgroundBrush(QBrush(QColor(PLOT_BG)))
        chart.setPlotAreaBackgroundVisible(True)
        chart.addSeries(series)
        chart.legend().setVisible(False)

        axis_x = QBarCategoryAxis()
        axis_x.append(categories)
        chart.addAxis(axis_x, Qt.AlignBottom)
        series.attachAxis(axis_x)

        axis_y = QValueAxis()
        axis_y.setRange(0, max(1.0, (max(values) if values else 0.0) * 1.15))
        chart.addAxis(axis_y, Qt.AlignLeft)
        series.attachAxis(axis_y)

        self._reason_chart_view.setChart(chart)

    # -- populate reasons from the report ----------------------------------

    def _reload_reason_rows(self, output_path: str) -> None:
        """Read the Skip Reason Distribution sheet back from the report."""
        self._result_reasons = []
        workbook = None
        try:
            from openpyxl import load_workbook

            workbook = load_workbook(output_path, read_only=True)
            if "Skip Reason Distribution" not in workbook.sheetnames:
                return
            sheet = workbook["Skip Reason Distribution"]
            rows = list(sheet.iter_rows(values_only=True))
            if not rows:
                return

            header = [str(c) if c is not None else "" for c in rows[0]]
            index = {name: header.index(name) for name in header}

            result: List[Dict[str, Any]] = []
            for row in rows[1:]:
                if not row or row[0] is None:
                    continue
                result.append(
                    {
                        "country": row[index.get("country", 0)],
                        "reason": row[index.get("reason", 1)],
                        "count": row[index.get("count", 2)] or 0,
                        "percentage": row[index.get("percentage", 3)] or 0.0,
                    }
                )
            self._result_reasons = result
        except Exception as exc:  # noqa: BLE001
            self._append_log("WARNING", f"Failed to reload reasons: {exc}")
        finally:
            if workbook is not None:
                try:
                    workbook.close()
                except Exception:  # noqa: BLE001
                    pass


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def _load_stylesheet(app: QApplication) -> None:
    candidates = [Path("style.qss"), BASE_DIR / "style.qss"]
    for candidate in candidates:
        if candidate.exists():
            try:
                with candidate.open("r", encoding="utf-8") as handle:
                    app.setStyleSheet(handle.read())
                return
            except Exception as exc:  # noqa: BLE001
                _LOGGER.warning("Failed to load %s: %s", candidate, exc)
    _LOGGER.warning("style.qss not found; starting without stylesheet.")


def main() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName("onlineStatusChecker")
    _load_stylesheet(app)

    window = OnlineStatusCheckerMainWindow()
    window.show()
    window.load_tree()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
