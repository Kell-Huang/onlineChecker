"""Country selection: the page this refactor exists for.

The old flow made you expand project → site → channel in a lazy tree to see
what was ticked, because checkbox state was only painted when a site's
channels finished loading. With 34 feeds across 32 sites that is 34 clicks to
change your mind.

This page is flat and country-first:

  - one row per country, one child row per feed (a country may have several);
  - ticking a country ticks all of its feeds, and a country shows the
    indeterminate state when only some of its feeds are ticked;
  - the header checkbox is the only bulk control: it shows whether everything
    visible is ticked and toggles it, so a search narrows what it touches;
  - every change is persisted immediately by the owner.

Feeds are only listed from the saved selection: the page never calls the API,
which is what makes it usable before a token is even configured.
"""
from typing import Dict, List

from PySide6.QtCore import QRect, QSize, Qt, Signal
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import (
    QAbstractItemView, QHBoxLayout, QHeaderView, QLabel, QLineEdit, QStyle,
    QStyleOptionHeader, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget,
)

from core.compare import country_columns_for
from core.models import Feed, Selection

from .theme import DANGER, SUCCESS, WARNING


class CheckableHeader(QHeaderView):
    """A tree header whose first section carries a tri-state checkbox.

    Clicking it toggles every *visible* row, which is the same scope the
    removed Select all / Select none buttons had — but the control now shows
    the current state as well as changing it, and there is one of it instead
    of three.
    """

    toggled = Signal()

    _BOX = 14
    _PAD = 6

    def __init__(self, orientation, parent=None):
        super().__init__(orientation, parent)
        self.setSectionsClickable(True)
        self.setHighlightSections(False)
        self._state = Qt.Unchecked
        self.setToolTip("Select or clear every visible row")

    def set_check_state(self, state) -> None:
        if state != self._state:
            self._state = state
            self.viewport().update()

    def check_state(self):
        return self._state

    def _box_rect(self, section_rect: QRect) -> QRect:
        return QRect(
            section_rect.left() + self._PAD,
            section_rect.top() + (section_rect.height() - self._BOX) // 2,
            self._BOX,
            self._BOX,
        )

    def paintSection(self, painter: QPainter, rect: QRect, logical_index: int) -> None:
        if logical_index != 0:
            super().paintSection(painter, rect, logical_index)
            return

        painter.save()
        option = QStyleOptionHeader()
        self.initStyleOption(option)
        option.rect = rect
        self.style().drawControl(QStyle.CE_Header, option, painter, self)

        self._paint_box(painter, self._box_rect(rect))
        title = self.model().headerData(0, Qt.Horizontal) or ""
        painter.setPen(QColor("#667085"))
        painter.drawText(rect.adjusted(self._BOX + self._PAD * 2, 0, 0, 0),
                         Qt.AlignLeft | Qt.AlignVCenter, str(title))
        painter.restore()

    def _paint_box(self, painter: QPainter, box: QRect) -> None:
        painter.setRenderHint(QPainter.Antialiasing, True)
        checked = self._state != Qt.Unchecked
        painter.setPen(QPen(QColor("#2f6fed" if checked else "#d0d5dd"), 1))
        painter.setBrush(QColor("#2f6fed" if checked else "#ffffff"))
        painter.drawRoundedRect(box.adjusted(0, 0, -1, -1), 4, 4)

        if self._state == Qt.Checked:
            path = QPainterPath()
            path.moveTo(box.left() + 3.4, box.center().y() + 0.2)
            path.lineTo(box.left() + 5.9, box.center().y() + 2.8)
            path.lineTo(box.left() + 10.8, box.center().y() - 2.6)
            painter.setPen(QPen(QColor("#ffffff"), 1.8, Qt.SolidLine,
                                Qt.RoundCap, Qt.RoundJoin))
            painter.setBrush(Qt.NoBrush)
            painter.drawPath(path)
        elif self._state == Qt.PartiallyChecked:
            painter.setPen(QPen(QColor("#ffffff"), 1.8, Qt.SolidLine, Qt.RoundCap))
            painter.drawLine(box.left() + 3, box.center().y(),
                             box.right() - 3, box.center().y())

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.LeftButton:
            rect = QRect(self.sectionViewportPosition(0), 0,
                         self.sectionSize(0), self.height())
            if self._box_rect(rect).adjusted(-4, -4, 4, 4).contains(event.pos()):
                self.toggled.emit()
                return
        super().mousePressEvent(event)

    def sizeHint(self) -> QSize:
        size = super().sizeHint()
        return QSize(size.width() + self._BOX + self._PAD, size.height())


class CountryPage(QWidget):
    """Flat country/feed checklist with bulk actions."""

    selection_changed = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("CountryPage")
        self._selection = Selection()
        self._countries: Dict[str, Dict[str, str]] = {}
        self._updating = False
        self._build_ui()

    # ------------------------------------------------------------------
    # Construction
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(10)

        bar = QHBoxLayout()
        bar.setSpacing(8)

        # No Select all / none buttons: the header checkbox owns both, and it
        # shows the state as well as changing it.
        self.search_edit = QLineEdit()
        self.search_edit.setObjectName("searchEdit")
        self.search_edit.setPlaceholderText("Filter country or feed…")
        self.search_edit.setClearButtonEnabled(True)
        self.search_edit.textChanged.connect(self._apply_filter)
        bar.addWidget(self.search_edit, stretch=1)

        self.count_label = QLabel("0 selected")
        self.count_label.setObjectName("mutedLabel")
        bar.addWidget(self.count_label)
        layout.addLayout(bar)

        self.tree = QTreeWidget()
        self.tree.setObjectName("countryTree")
        self.header = CheckableHeader(Qt.Horizontal, self.tree)
        self.header.toggled.connect(self._toggle_visible)
        self.tree.setHeader(self.header)
        self.tree.setColumnCount(3)
        self.tree.setHeaderLabels(["Country / Feed", "Site", "Status"])
        self.tree.setRootIsDecorated(True)
        self.tree.setUniformRowHeights(True)
        self.tree.setSelectionMode(QAbstractItemView.NoSelection)
        self.tree.setEditTriggers(QAbstractItemView.NoEditTriggers)
        header = self.tree.header()
        header.setStretchLastSection(False)
        # Site names are the long values, so that column takes the slack. A
        # stretched name column left a wide gap between the feed and its site.
        header.setSectionResizeMode(0, header.ResizeMode.Interactive)
        header.setSectionResizeMode(1, header.ResizeMode.Stretch)
        header.setSectionResizeMode(2, header.ResizeMode.Interactive)
        self.tree.setColumnWidth(0, 380)
        self.tree.setColumnWidth(2, 150)
        self.tree.itemChanged.connect(self._on_item_changed)
        layout.addWidget(self.tree, stretch=1)

        self.empty_label = QLabel(
            "No feeds configured yet. Open the Browse tab, pick a channel and "
            "tick it — it will show up here."
        )
        self.empty_label.setObjectName("mutedLabel")
        self.empty_label.setWordWrap(True)
        layout.addWidget(self.empty_label)

    # ------------------------------------------------------------------
    # Model
    # ------------------------------------------------------------------

    def set_selection(self, selection: Selection,
                      countries: Dict[str, Dict[str, str]]) -> None:
        """Render the page from a selection."""
        self._selection = selection
        self._countries = countries
        self._rebuild()

    def selected_keys(self) -> List[str]:
        return list(self._selection.selected)

    def _rebuild(self) -> None:
        self._updating = True
        self.tree.clear()

        grouped: Dict[str, List[Feed]] = {}
        for feed in self._selection.feeds:
            grouped.setdefault(feed.country or "—", []).append(feed)

        chosen = set(self._selection.selected)
        for country in sorted(grouped):
            feeds = grouped[country]
            # The count belongs next to the country, not under "Site".
            country_item = QTreeWidgetItem(
                [f"{country.upper()}   ({_count_text(feeds)})", "", ""])
            # No ItemIsAutoTristate: Qt would set the parent to
            # PartiallyChecked while a child is changing, and the change
            # handler below would read that as "unchecked" and clear every
            # feed under the country. The parent state is managed explicitly.
            country_item.setFlags(country_item.flags() | Qt.ItemIsUserCheckable)
            country_item.setData(0, Qt.UserRole, ("country", country))
            self.tree.addTopLevelItem(country_item)

            for feed in feeds:
                status, colour = _feed_status(feed, self._countries)
                feed_item = QTreeWidgetItem([feed.display_name, feed.site_name, status])
                feed_item.setFlags(feed_item.flags() | Qt.ItemIsUserCheckable)
                feed_item.setData(0, Qt.UserRole, ("feed", feed.key))
                feed_item.setCheckState(
                    0, Qt.Checked if feed.key in chosen else Qt.Unchecked)
                feed_item.setForeground(2, QColor(colour))
                if feed.site_name:
                    feed_item.setToolTip(0, feed.url)
                country_item.addChild(feed_item)

            country_item.setCheckState(0, _tristate(feeds, chosen))

        self._updating = False
        self._update_count()
        self._update_header_state()
        self.empty_label.setVisible(not self._selection.feeds)
        self.tree.setVisible(bool(self._selection.feeds))
        self._apply_filter(self.search_edit.text())

    # ------------------------------------------------------------------
    # Interaction
    # ------------------------------------------------------------------

    def _on_item_changed(self, item: QTreeWidgetItem, _column: int) -> None:
        if self._updating:
            return
        kind, value = item.data(0, Qt.UserRole) or (None, None)
        state = item.checkState(0)
        if kind == "country" and state == Qt.PartiallyChecked:
            # Only reachable from our own parent sync, never from a click.
            return
        chosen = state == Qt.Checked

        self._updating = True
        try:
            if kind == "country":
                for index in range(item.childCount()):
                    item.child(index).setCheckState(
                        0, Qt.Checked if chosen else Qt.Unchecked)
            elif kind == "feed":
                parent = item.parent()
                if parent is not None:
                    parent.setCheckState(0, _tristate_from_children(parent))
        finally:
            self._updating = False

        self._sync_selection()
        self._update_count()
        self._update_header_state()
        self.selection_changed.emit()

    def _sync_selection(self) -> None:
        """Rebuild ``selection.selected`` from the tree, keeping feed order."""
        ticked = set()
        for index in range(self.tree.topLevelItemCount()):
            country_item = self.tree.topLevelItem(index)
            for child_index in range(country_item.childCount()):
                child = country_item.child(child_index)
                if child.checkState(0) == Qt.Checked:
                    _kind, key = child.data(0, Qt.UserRole)
                    ticked.add(key)
        self._selection.selected = [feed.key for feed in self._selection.feeds
                                    if feed.key in ticked]

    def _bulk(self, checked: bool) -> None:
        """Apply to visible rows only, so a filter narrows the action."""
        self._updating = True
        try:
            for index in range(self.tree.topLevelItemCount()):
                country_item = self.tree.topLevelItem(index)
                if country_item.isHidden():
                    continue
                for child_index in range(country_item.childCount()):
                    child = country_item.child(child_index)
                    if child.isHidden():
                        continue
                    child.setCheckState(0, Qt.Checked if checked else Qt.Unchecked)
                country_item.setCheckState(0, _tristate_from_children(country_item))
        finally:
            self._updating = False
        self._sync_selection()
        self._update_count()
        self._update_header_state()
        self.selection_changed.emit()

    def _toggle_visible(self) -> None:
        """Header click: clear when everything visible is ticked, else select."""
        self._bulk(self.header.check_state() != Qt.Checked)

    def _update_header_state(self) -> None:
        """Header checkbox reflects the visible rows only."""
        total = 0
        ticked = 0
        for index in range(self.tree.topLevelItemCount()):
            country_item = self.tree.topLevelItem(index)
            if country_item.isHidden():
                continue
            for child_index in range(country_item.childCount()):
                child = country_item.child(child_index)
                if child.isHidden():
                    continue
                total += 1
                if child.checkState(0) == Qt.Checked:
                    ticked += 1

        if total == 0 or ticked == 0:
            state = Qt.Unchecked
        elif ticked == total:
            state = Qt.Checked
        else:
            state = Qt.PartiallyChecked
        self.header.set_check_state(state)

    def _apply_filter(self, text: str) -> None:
        needle = (text or "").strip().lower()
        for index in range(self.tree.topLevelItemCount()):
            country_item = self.tree.topLevelItem(index)
            country_match = needle in country_item.text(0).lower()
            visible_children = 0
            for child_index in range(country_item.childCount()):
                child = country_item.child(child_index)
                match = (not needle or country_match
                         or needle in child.text(0).lower()
                         or needle in child.text(1).lower())
                child.setHidden(not match)
                visible_children += 1 if match else 0
            country_item.setHidden(visible_children == 0)
        self._update_header_state()

    def _update_count(self) -> None:
        total = len(self._selection.feeds)
        chosen = len(self._selection.selected)
        self.count_label.setText(f"{chosen} of {total} selected")


def _count_text(feeds: List[Feed]) -> str:
    return f"{len(feeds)} feed" + ("" if len(feeds) == 1 else "s")


def _tristate(feeds: List[Feed], chosen: set):
    ticked = sum(1 for feed in feeds if feed.key in chosen)
    if ticked == 0:
        return Qt.Unchecked
    if ticked == len(feeds):
        return Qt.Checked
    return Qt.PartiallyChecked


def _tristate_from_children(item: QTreeWidgetItem):
    states = [item.child(index).checkState(0) for index in range(item.childCount())]
    if not states or all(state == Qt.Unchecked for state in states):
        return Qt.Unchecked
    if all(state == Qt.Checked for state in states):
        return Qt.Checked
    return Qt.PartiallyChecked


def _feed_status(feed: Feed, countries: Dict[str, Dict[str, str]]):
    """(label, colour) describing whether this feed can run."""
    if not feed.url:
        return "No URL", DANGER
    mapping = dict(country_columns_for(feed.country, countries))
    mapping.update({k: v for k, v in (feed.gos_columns or {}).items() if v})
    # The GOS header is not available here, so only check that the mapping is
    # complete; the run validates the names against the real header.
    if [key for key in ("online", "stock", "price") if not mapping.get(key)]:
        return "Needs GOS columns", WARNING
    return "Ready", SUCCESS
