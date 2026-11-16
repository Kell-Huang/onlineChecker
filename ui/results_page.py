"""Results: the per-country summary, in the app.

Opening the Excel to answer "how many are missing in Germany?" is a detour
when the numbers are already in memory. This page shows the numbers that
matter per country and nothing else — the workbook still holds the sampled
detail, the API log and the failed batches for when something needs digging.

Three of the columns are whole-population counts (online, in the feed, missing
from the feed) and two come from the sample the API was asked about (skipped,
and why). The sampled ones say so in their header tooltip; mixing the two
without saying which is which is how "3 missing, 1 skipped" gets misread as a
ratio of the same set.

The skip reasons are drawn as text plus a share bar in the same cell. They
were two columns once, but the reason column has to stretch (it holds the
longest strings) and stretching it pushed each bar hundreds of pixels away
from the reason it belongs to. One column, two lines, reason and bar adjacent.
"""
from typing import Any, Dict, List, Sequence, Tuple

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QPainter
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QHBoxLayout, QHeaderView, QLabel,
    QPushButton, QStyle, QStyledItemDelegate, QStyleOptionViewItem,
    QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from .theme import DANGER, TEXT_MUTED, WARNING

COLUMNS = [
    ("country", "Country", 100, Qt.AlignLeft),
    ("gos_count", "SFCC Online", 110, Qt.AlignRight),
    ("feed_count", "Google Shopping", 145, Qt.AlignRight),
    ("missing_count", "Not in Google Shopping", 180, Qt.AlignRight),
    ("skipped_count", "Skipped", 90, Qt.AlignRight),
    ("top_reason_1", "Skip reason", 0, Qt.AlignLeft),      # 0 = stretch
]

# The first three counts are over every product; the last two are over the
# sample. Without saying so, "3 missing, 1 skipped" reads as a ratio of the
# same set — it is not.
HEADER_TIPS = {
    "gos_count": "Products online in SFCC for this country\n"
                 "(online = true, stock > 0, price > 0).",
    "feed_count": "Unique IDs in this country's Google Merchant Center feed.",
    "missing_count": "Online in SFCC but absent from the Google Merchant "
                     "Center feed.",
    "skipped_count": "Of the sampled missing SKUs, how many are in the channel "
                     "but skipped for export.",
    "top_reason_1": "The two most common skip reasons, each with its share of "
                    "the skipped set.",
}

REASON_COLUMN = len(COLUMNS) - 1

ROW_HEIGHT = 46
_LINE_TOP = 3
_LINE_HEIGHT = 19

# Room reserved on the right of the reason cell for the bar and its number.
_BAR_WIDTH = 110
_VALUE_WIDTH = 48


class SkipReasonDelegate(QStyledItemDelegate):
    """Two reasons per row: text on the left, share bar and percentage right.

    The bar answers "how much of the skipped set is this?" without a second
    column, and keeps the number next to the reason it measures. The fill is
    a light amber so the dark percentage stays readable where they meet.
    """

    TRACK = "#e9edf2"
    FILL = "#f7d9a8"
    TEXT = "#1f2937"
    SECOND = "#98a2b3"
    VALUE = "#475467"

    def paint(self, painter: QPainter, option: QStyleOptionViewItem, index) -> None:
        opt = QStyleOptionViewItem(option)
        self.initStyleOption(opt, index)
        opt.text = ""                       # this delegate draws everything

        style = opt.widget.style() if opt.widget else QApplication.style()
        style.drawControl(QStyle.CE_ItemViewItem, opt, painter, opt.widget)

        entries: Sequence[Tuple[str, float]] = index.data(Qt.UserRole) or ()
        painter.save()
        painter.setRenderHint(QPainter.Antialiasing, True)
        metrics = painter.fontMetrics()

        if not entries:
            # Nothing was skipped, so there is no reason and no share to draw.
            painter.setPen(QColor(self.SECOND))
            painter.drawText(option.rect.adjusted(10, 0, -10, 0),
                             Qt.AlignLeft | Qt.AlignVCenter, "—")
            painter.restore()
            return

        for line, (reason, share) in enumerate(list(entries)[:2]):
            band = option.rect.adjusted(0, _LINE_TOP + line * _LINE_HEIGHT, 0, 0)
            band.setHeight(_LINE_HEIGHT)

            text_rect = band.adjusted(10, 0, -(_BAR_WIDTH + _VALUE_WIDTH + 6), 0)
            painter.setPen(QColor(self.TEXT if line == 0 else self.SECOND))
            if reason:
                painter.drawText(
                    text_rect, Qt.AlignLeft | Qt.AlignVCenter,
                    metrics.elidedText(str(reason), Qt.ElideRight,
                                       max(20, text_rect.width())),
                )
            elif line == 0:
                painter.drawText(text_rect, Qt.AlignLeft | Qt.AlignVCenter, "—")

            if not reason:
                continue

            track = QRectF(band).adjusted(
                band.width() - _BAR_WIDTH - _VALUE_WIDTH - 6, 4,
                -_VALUE_WIDTH - 6, -4)
            painter.setPen(Qt.NoPen)
            painter.setBrush(QColor(self.TRACK))
            painter.drawRoundedRect(track, 3, 3)

            if share > 0:
                filled = QRectF(track)
                filled.setWidth(track.width() * min(1.0, share / 100.0))
                painter.setBrush(QColor(self.FILL))
                painter.drawRoundedRect(filled, 3, 3)

            painter.setPen(QColor(self.VALUE))
            painter.drawText(band.adjusted(0, 0, -8, 0),
                             Qt.AlignRight | Qt.AlignVCenter, f"{share:.1f}%")

        painter.restore()


class ResultsPage(QWidget):
    """Summary table plus a one-line total."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("ResultsPage")
        self._rows: List[Dict[str, Any]] = []
        self._build_ui()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(10)

        bar = QHBoxLayout()
        self.totals_label = QLabel("No run yet.")
        self.totals_label.setObjectName("totalsLabel")
        bar.addWidget(self.totals_label, stretch=1)
        self.open_btn = QPushButton("Open report folder")
        self.open_btn.setObjectName("secondaryButton")
        self.open_btn.setEnabled(False)
        bar.addWidget(self.open_btn)
        layout.addLayout(bar)

        self.table = QTableWidget(0, len(COLUMNS))
        self.table.setObjectName("summaryTable")
        self.table.setHorizontalHeaderLabels([title for _k, title, _w, _a in COLUMNS])
        # Header text must sit over the data it labels: Qt centres header items
        # by default, which left the numeric headings over nothing.
        for index, (key, _title, _width, align) in enumerate(COLUMNS):
            header_item = self.table.horizontalHeaderItem(index)
            if header_item is not None:
                header_item.setTextAlignment(align | Qt.AlignVCenter)
                header_item.setToolTip(HEADER_TIPS.get(key, ""))

        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(ROW_HEIGHT)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.setShowGrid(False)
        self.table.setAlternatingRowColors(True)

        header = self.table.horizontalHeader()
        header.setHighlightSections(False)
        header.setStretchLastSection(False)
        for index, (_key, _title, width, _align) in enumerate(COLUMNS):
            if width:
                self.table.setColumnWidth(index, width)
        header.setSectionResizeMode(REASON_COLUMN, QHeaderView.ResizeMode.Stretch)

        self.table.setItemDelegateForColumn(
            REASON_COLUMN, SkipReasonDelegate(self.table))
        layout.addWidget(self.table, stretch=1)

    # ------------------------------------------------------------------

    def set_rows(self, rows: List[Dict[str, Any]]) -> None:
        self._rows = list(rows)
        self.table.setRowCount(len(self._rows))

        for row_index, row in enumerate(self._rows):
            for column, (key, _title, _width, align) in enumerate(COLUMNS):
                item = QTableWidgetItem(_cell_text(row.get(key)))
                item.setTextAlignment(align | Qt.AlignVCenter)
                if key == "top_reason_1":
                    item.setData(Qt.UserRole, _reason_pairs(row))
                self.table.setItem(row_index, column, item)

        self.totals_label.setText(_totals_text(self._rows))
        self.open_btn.setEnabled(bool(self._rows))

    def clear(self) -> None:
        self._rows = []
        self.table.setRowCount(0)
        self.totals_label.setText("No run yet.")
        self.open_btn.setEnabled(False)


def _reason_pairs(row: Dict[str, Any]) -> List[Tuple[str, float]]:
    """The top two reasons with their share of the skipped set."""
    pairs: List[Tuple[str, float]] = []
    for reason_key, share_key in (("top_reason_1", "top_reason_1_pct"),
                                  ("top_reason_2", "top_reason_2_pct")):
        reason = row.get(reason_key) or ""
        if not reason:
            continue
        pairs.append((str(reason), float(row.get(share_key) or 0.0)))
    return pairs


def _cell_text(value: Any) -> str:
    if value is None or value == "":
        return "—"
    if isinstance(value, float):
        return f"{value:.1f}%"
    if isinstance(value, int):
        return f"{value:,}"
    return str(value)


def _totals_text(rows: List[Dict[str, Any]]) -> str:
    if not rows:
        return "No countries were compared."
    online = sum(int(row.get("gos_count") or 0) for row in rows)
    in_feed = sum(int(row.get("feed_count") or 0) for row in rows)
    missing = sum(int(row.get("missing_count") or 0) for row in rows)
    skipped = sum(int(row.get("skipped_count") or 0) for row in rows)
    countries = len(rows)
    return (f"{countries} countries  ·  {online:,} online  ·  "
            f"{in_feed:,} in Google Shopping  ·  {missing:,} missing  ·  "
            f"{skipped:,} skipped")
