"""The Excel report.

Sheets are described by ``SHEETS`` rather than written out one by one, so
adding a sheet is a spec entry plus a key in the dict the runner passes in —
no new code path, no new argument in five places.

Sheet order is part of the contract with whoever reads these files:

  1. Summary                  one row per country
  2. Sampled Details          one row per sampled SKU
  3. Skip Reason Distribution one row per (country, reason)
  4. API Log                  one row per request
  5. Failed Batches           one row per failed request, with the SKU list

A failure deletes the partial file, so a truncated workbook is never mistaken
for a finished one.
"""
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Mapping, Sequence

from openpyxl import Workbook
from openpyxl.utils import get_column_letter

from .errors import ExcelError

SUMMARY_COLUMNS = [
    "country", "gos_count", "feed_count", "missing_count", "sampled_count",
    "skipped_count", "in_channel_count", "not_in_channel_count",
    "failed_batch_count", "failed_sku_count", "unchecked_count",
    "top_reason_1", "top_reason_1_pct", "top_reason_2", "top_reason_2_pct",
    "note",
]

DETAIL_COLUMNS = [
    "country", "sku", "normalized_sku", "status", "skip_reason_raw",
    "skip_reason_readable", "priority",
]

REASON_COLUMNS = ["country", "reason", "count", "percentage"]

API_LOG_COLUMNS = [
    "timestamp", "country", "batch_index", "sku_count", "request_url",
    "status_code", "response_count", "elapsed_seconds", "error_message",
]

FAILED_BATCH_COLUMNS = [
    "country", "batch_index", "sku_list", "error_message", "retry_count",
]


@dataclass(frozen=True)
class SheetSpec:
    title: str
    columns: Sequence[str]


SHEETS = (
    SheetSpec("Summary", SUMMARY_COLUMNS),
    SheetSpec("Sampled Details", DETAIL_COLUMNS),
    SheetSpec("Skip Reason Distribution", REASON_COLUMNS),
    SheetSpec("API Log", API_LOG_COLUMNS),
    SheetSpec("Failed Batches", FAILED_BATCH_COLUMNS),
)

_MAX_COLUMN_WIDTH = 60


def write_report(path: Path,
                 sheets: Mapping[str, Sequence[Dict[str, Any]]]) -> None:
    """Write the workbook, raising ``ExcelError`` on any failure.

    ``sheets`` maps a sheet title to its rows. Titles that are not in
    ``SHEETS`` are still written (after the known ones), with columns taken
    from the first row — that keeps ad-hoc sheets possible without editing
    this module.
    """
    path = Path(path)
    try:
        workbook = Workbook()
        first = True
        for spec in SHEETS:
            rows = sheets.get(spec.title) or []
            worksheet = workbook.active if first else workbook.create_sheet(spec.title)
            worksheet.title = spec.title
            first = False
            _write_sheet(worksheet, spec.columns, rows)

        known = {spec.title for spec in SHEETS}
        for title, rows in sheets.items():
            if title in known or not rows:
                continue
            worksheet = workbook.create_sheet(title)
            _write_sheet(worksheet, tuple(rows[0].keys()), rows)

        path.parent.mkdir(parents=True, exist_ok=True)
        workbook.save(path)
    except Exception as exc:  # noqa: BLE001
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass
        raise ExcelError(str(exc)) from exc


def _write_sheet(worksheet, headers: Sequence[str],
                 rows: Sequence[Dict[str, Any]]) -> None:
    """Headers plus dict rows, with widths sized to the content."""
    worksheet.append(list(headers))
    for row in rows:
        worksheet.append([row.get(column) for column in headers])

    for index, header in enumerate(headers, start=1):
        longest = len(str(header))
        for row in rows:
            value = row.get(header)
            if value is not None:
                longest = max(longest, len(str(value)))
        # Cap the width: a 200-SKU failed-batch cell should not stretch the
        # sheet to the width of the screen.
        worksheet.column_dimensions[get_column_letter(index)].width = min(
            max(10, longest + 2), _MAX_COLUMN_WIDTH
        )
