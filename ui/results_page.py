"""Results: the per-country summary, in the app.

Opening the Excel to answer "how many are missing in Germany?" is a detour
when the numbers are already in memory. This page shows the six numbers that
matter per country and nothing else — the workbook still holds the sampled
detail, the API log and the failed batches for when something needs digging.
"""
from typing import Any, Dict, List

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QAbstractItemView, QHBoxLayout, QHeaderView, QLabel, QPushButton,
    QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from .theme import DANGER, TEXT_MUTED, WARNING

COLUMNS = [
    ("country", "Country", 110, Qt.AlignLeft),
    ("gos_count", "Online", 110, Qt.AlignRight),
    ("not_in_channel_count", "Not in GMC", 120, Qt.AlignRight),
    ("skipped_count", "Skipped", 100, Qt.AlignRight),
    ("top_reason_1", "Top reason", 320, Qt.AlignLeft),
    ("top_reason_1_pct", "Share", 80, Qt.AlignRight),
]


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
        # Header text must sit over the data it labels: Qt left-aligns header
        # items by default, so a right-aligned number column ended up with its
        # heading pinned to the far left.
        for index, (_key, _title, _width, align) in enumerate(COLUMNS):
            header_item = self.table.horizontalHeaderItem(index)
            if header_item is not None:
                header_item.setTextAlignment(align | Qt.AlignVCenter)
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(28)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.setShowGrid(False)
        self.table.setAlternatingRowColors(True)
        header = self.table.horizontalHeader()
        header.setHighlightSections(False)
        header.setStretchLastSection(False)
        for index, (_key, _title, width, _align) in enumerate(COLUMNS):
            self.table.setColumnWidth(index, width)
        header.setSectionResizeMode(4, QHeaderView.ResizeMode.Stretch)
        layout.addWidget(self.table, stretch=1)

    # ------------------------------------------------------------------

    def set_rows(self, rows: List[Dict[str, Any]]) -> None:
        self._rows = list(rows)
        self.table.setRowCount(len(self._rows))

        for row_index, row in enumerate(self._rows):
            for column, (key, _title, _width, align) in enumerate(COLUMNS):
                item = QTableWidgetItem(_cell_text(row.get(key)))
                item.setTextAlignment(align | Qt.AlignVCenter)
                if key == "not_in_channel_count" and (row.get(key) or 0) > 0:
                    item.setForeground(QColor(DANGER))
                elif key == "skipped_count" and (row.get(key) or 0) > 0:
                    item.setForeground(QColor(WARNING))
                elif key in ("top_reason_1",) and not row.get(key):
                    item.setForeground(QColor(TEXT_MUTED))
                self.table.setItem(row_index, column, item)

        self.totals_label.setText(_totals_text(self._rows))
        self.open_btn.setEnabled(bool(self._rows))

    def clear(self) -> None:
        self._rows = []
        self.table.setRowCount(0)
        self.totals_label.setText("No run yet.")
        self.open_btn.setEnabled(False)


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
        return "No run yet."
    online = sum(int(row.get("gos_count") or 0) for row in rows)
    missing = sum(int(row.get("not_in_channel_count") or 0) for row in rows)
    skipped = sum(int(row.get("skipped_count") or 0) for row in rows)
    countries = len(rows)
    return (f"{countries} countries  ·  {online:,} online  ·  "
            f"{missing:,} not in GMC  ·  {skipped:,} skipped")
