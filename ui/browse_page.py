"""Browse: discover channels in Productsup and add them to the selection.

This is the only page that talks to the platform API, and it is deliberately
separate from the country page: discovery needs the network and a token,
selection does not. Lazy loading keeps the first screen instant — projects
load on open, sites when a project expands, channels when a site expands.
"""
from typing import Dict, List, Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView, QLabel, QMenu, QPushButton, QTreeWidget, QTreeWidgetItem,
    QVBoxLayout, QWidget,
)

from core.compare import infer_country
from core.models import Feed, GosFeed, Selection
from core.productsup import (channel_is_usable, detect_url_format,
                             extract_feed_url)

from .workers import MetadataWorker


class BrowsePage(QWidget):
    """Lazy project → site → channel tree."""

    feed_toggled = Signal(object, bool)   # Feed, selected
    gos_chosen = Signal(object)           # GosFeed
    log = Signal(str, str)

    def __init__(self, settings_provider, credentials_provider, parent=None):
        super().__init__(parent)
        self.setObjectName("BrowsePage")
        self._settings_provider = settings_provider
        self._credentials_provider = credentials_provider
        self._selection = Selection()
        self._countries: Dict[str, Dict[str, str]] = {}
        self._aliases: Dict[str, str] = {}
        self._workers: List[MetadataWorker] = []
        self._build_ui()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(10)

        bar = QWidget()
        row = QVBoxLayout(bar)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(6)

        buttons = QWidget()
        from PySide6.QtWidgets import QHBoxLayout
        buttons_row = QHBoxLayout(buttons)
        buttons_row.setContentsMargins(0, 0, 0, 0)

        self.refresh_btn = QPushButton("Reload projects")
        self.refresh_btn.setObjectName("secondaryButton")
        self.refresh_btn.clicked.connect(self.refresh)
        buttons_row.addWidget(self.refresh_btn)
        buttons_row.addStretch()

        self.hint = QLabel(
            "Double-click a channel to add it to the country page. "
            "Right-click to set it as the GOS channel."
        )
        self.hint.setObjectName("mutedLabel")
        self.hint.setWordWrap(True)
        buttons_row.addWidget(self.hint, stretch=2)

        row.addWidget(buttons)
        layout.addWidget(bar)

        self.tree = QTreeWidget()
        self.tree.setObjectName("browseTree")
        self.tree.setHeaderHidden(True)
        self.tree.setSelectionMode(QAbstractItemView.NoSelection)
        self.tree.setContextMenuPolicy(Qt.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self._on_context_menu)
        self.tree.itemExpanded.connect(self._on_expanded)
        self.tree.itemDoubleClicked.connect(self._on_double_click)
        layout.addWidget(self.tree, stretch=1)

        self.status = QLabel("Not loaded.")
        self.status.setObjectName("mutedLabel")
        layout.addWidget(self.status)

    # ------------------------------------------------------------------

    def set_selection(self, selection: Selection,
                      countries: Dict[str, Dict[str, str]],
                      aliases: Dict[str, str]) -> None:
        self._selection = selection
        self._countries = countries
        self._aliases = aliases
        self._refresh_stars()

    def refresh(self) -> None:
        credentials = self._credentials_provider()
        if not credentials.get("token"):
            self.status.setText("No token configured — add one in Settings.")
            return
        self.tree.clear()
        self.status.setText("Loading projects…")
        self._load("projects", "")

    def _load(self, kind: str, parent_id: str) -> None:
        credentials = self._credentials_provider()
        worker = MetadataWorker(kind, parent_id, credentials["token"],
                                credentials["base_url"],
                                self._settings_provider(), self)
        worker.loaded.connect(self._on_loaded)
        worker.failed.connect(self._on_failed)
        worker.finished.connect(lambda w=worker: self._workers.remove(w)
                                if w in self._workers else None)
        self._workers.append(worker)
        worker.start()

    # ------------------------------------------------------------------

    def _on_loaded(self, kind: str, parent_id: str, items: List[dict]) -> None:
        if kind == "projects":
            self.tree.clear()
            for project in items:
                item = QTreeWidgetItem([str(project.get("name") or project.get("id"))])
                item.setData(0, Qt.UserRole, ("project", str(project.get("id")), project))
                item.addChild(_placeholder())
                self.tree.addTopLevelItem(item)
            self.status.setText(f"{len(items)} project(s).")
            return

        parent = self._find(parent_id)
        if parent is None:
            return
        parent.takeChildren()

        if kind == "sites":
            for site in items:
                item = QTreeWidgetItem([str(site.get("name") or site.get("id"))])
                item.setData(0, Qt.UserRole, ("site", str(site.get("id")), site))
                item.addChild(_placeholder())
                parent.addChild(item)
            self.status.setText(f"{len(items)} site(s).")
            return

        usable = 0
        for channel in items:
            if not channel_is_usable(channel):
                continue
            usable += 1
            parent.addChild(self._channel_item(channel, parent))
        self.status.setText(f"{usable} usable channel(s).")

    def _channel_item(self, channel: dict, site_item: QTreeWidgetItem) -> QTreeWidgetItem:
        site_kind, site_id, site_data = site_item.data(0, Qt.UserRole)
        site_name = str(site_data.get("name") or "")
        project_item = site_item.parent()
        project_id = ""
        if project_item is not None:
            _k, project_id, _d = project_item.data(0, Qt.UserRole)

        url = extract_feed_url(channel) or ""
        feed = Feed(
            country="",
            site_id=str(site_id),
            channel_id=str(channel.get("id")),
            url=url,
            site_name=site_name,
            channel_name=str(channel.get("name") or ""),
            project_id=str(project_id or ""),
            format=detect_url_format(url),
        )

        item = QTreeWidgetItem([feed.display_name])
        item.setData(0, Qt.UserRole, ("channel", feed.key, feed, project_id))
        item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
        item.setCheckState(
            0, Qt.Checked if feed.key in self._selection.selected else Qt.Unchecked)
        item.setToolTip(0, url)
        if self._selection.gos and self._selection.gos.key == feed.key:
            item.setText(0, f"{feed.display_name}   (GOS)")
        return item

    def _on_expanded(self, item: QTreeWidgetItem) -> None:
        kind, item_id, _data = item.data(0, Qt.UserRole) or (None, None, None)
        if kind not in ("project", "site"):
            return
        if item.childCount() != 1 or item.child(0).data(0, Qt.UserRole):
            return          # already loaded
        self._load("sites" if kind == "project" else "channels", item_id)

    def _on_failed(self, kind: str, parent_id: str, message: str) -> None:
        self.status.setText(f"Failed to load {kind}: {message}")
        self.log.emit("ERROR", f"Failed to load {kind}: {message}")

    def _find(self, item_id: str) -> Optional[QTreeWidgetItem]:
        for index in range(self.tree.topLevelItemCount()):
            project = self.tree.topLevelItem(index)
            if _id_of(project) == item_id:
                return project
            for site_index in range(project.childCount()):
                site = project.child(site_index)
                if _id_of(site) == item_id:
                    return site
        return None

    # ------------------------------------------------------------------

    def _on_double_click(self, item: QTreeWidgetItem, _column: int) -> None:
        data = item.data(0, Qt.UserRole) or ()
        if not data or data[0] != "channel":
            return
        _kind, _key, feed, _project_id = data
        if not feed.country:
            feed.country = infer_country(feed.site_name, feed.channel_name,
                                         self._aliases, self._countries)
        selected = feed.key in self._selection.selected
        self.feed_toggled.emit(feed, not selected)

    def _on_context_menu(self, position) -> None:
        item = self.tree.itemAt(position)
        if item is None:
            return
        data = item.data(0, Qt.UserRole) or ()
        if not data or data[0] != "channel":
            return
        _kind, _key, feed, _project_id = data

        menu = QMenu(self)
        if self._selection.gos and self._selection.gos.key == feed.key:
            menu.addAction("Unset GOS").triggered.connect(
                lambda: self.gos_chosen.emit(None))
        else:
            menu.addAction("Set as GOS").triggered.connect(
                lambda: self.gos_chosen.emit(GosFeed(
                    site_id=feed.site_id, channel_id=feed.channel_id,
                    url=feed.url, site_name=feed.site_name,
                    channel_name=feed.channel_name, format=feed.format)))
        menu.addSeparator()
        if feed.key in self._selection.selected:
            menu.addAction("Remove from selection").triggered.connect(
                lambda: self.feed_toggled.emit(feed, False))
        else:
            menu.addAction("Add to selection").triggered.connect(
                lambda: self.feed_toggled.emit(feed, True))
        menu.exec(self.tree.viewport().mapToGlobal(position))

    def _refresh_stars(self) -> None:
        """Re-label channels after the GOS changes."""
        gos_key = self._selection.gos.key if self._selection.gos else ""
        for index in range(self.tree.topLevelItemCount()):
            project = self.tree.topLevelItem(index)
            for site_index in range(project.childCount()):
                site = project.child(site_index)
                for channel_index in range(site.childCount()):
                    channel = site.child(channel_index)
                    data = channel.data(0, Qt.UserRole) or ()
                    if not data or data[0] != "channel":
                        continue
                    _kind, key, feed, _pid = data
                    label = feed.display_name
                    if key == gos_key:
                        label += "   (GOS)"
                    channel.setText(0, label)
                    channel.setCheckState(
                        0, Qt.Checked if key in self._selection.selected
                        else Qt.Unchecked)


def _placeholder() -> QTreeWidgetItem:
    item = QTreeWidgetItem(["Loading…"])
    item.setForeground(0, Qt.gray)
    item.setData(0, Qt.UserRole, None)
    return item


def _id_of(item: QTreeWidgetItem) -> str:
    data = item.data(0, Qt.UserRole) or ()
    return data[1] if len(data) > 1 else ""
