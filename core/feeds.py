"""Reading product IDs out of a comparison feed.

Only the ID column is needed from a feed, never the whole row: a Google
Merchant Center export can be hundreds of megabytes and the comparison is a
set membership test. The CSV reader asks Polars for one column; the XML reader
streams with ``iterparse`` and detaches each element as it finishes, so memory
stays flat instead of growing with the number of products.
"""
import csv
import io
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence, Tuple
from urllib.parse import urlparse
from xml.etree import ElementTree as ET

import chardet
import polars as pl

# Documented priority order for the ID column.
ID_PRIORITY = ["id", "sku", "product_id", "productid", "gtin", "ean"]

_HEADER_SNIFF_BYTES = 65536
_DELIMITERS = ",;\t|"


def detect_format(url: str, declared: Optional[str] = None,
                  content_type: Optional[str] = None) -> str:
    """Decide between csv and xml.

    An explicit declaration wins; otherwise the response Content-Type beats the
    URL suffix, because a ``.csv`` link that actually serves XML should still
    be parsed as XML.
    """
    declared = (declared or "").strip().lower()
    if declared in ("csv", "xml"):
        return declared

    if content_type:
        lower = content_type.lower()
        if "xml" in lower:
            return "xml"
        if "csv" in lower or "text/" in lower:
            return "csv"

    return "xml" if urlparse(url).path.lower().endswith(".xml") else "csv"


def read_csv_header(path: Path) -> Tuple[List[str], str, str]:
    """Return (columns, encoding, delimiter) without reading the whole file."""
    with Path(path).open("rb") as handle:
        raw = handle.read(_HEADER_SNIFF_BYTES)

    detected = chardet.detect(raw).get("encoding") or "utf-8"
    text = raw.decode(detected, errors="replace")

    sample = "\n".join(text.splitlines()[:10])
    try:
        delimiter = csv.Sniffer().sniff(sample, delimiters=_DELIMITERS).delimiter
    except csv.Error:
        delimiter = ","

    reader = csv.reader(io.StringIO(text), delimiter=delimiter)
    header = next(reader, [])
    header = [cell.strip().lstrip("\ufeff") for cell in header]
    return header, detected, delimiter


def detect_id_column(columns: Sequence[str]) -> Optional[str]:
    """Pick the ID column, tolerating namespaces (``g:id`` -> ``id``)."""
    lookup: Dict[str, str] = {}
    for column in columns:
        key = column.strip().lower().split(":")[-1]
        lookup.setdefault(key, column)
    for candidate in ID_PRIORITY:
        if candidate in lookup:
            return lookup[candidate]
    return None


# Readers are registered by format name so a new feed type (json, tsv) is a
# function plus one line here, not another branch inside the dispatcher.
READERS: Dict[str, Callable[[Path], Tuple[List[str], Optional[str]]]] = {}


def register_reader(feed_format: str, reader) -> None:
    READERS[feed_format.strip().lower()] = reader


def read_feed_ids(path: Path, feed_format: str) -> Tuple[List[str], Optional[str]]:
    """Return (raw ids, id column name or None when undetectable).

    Unknown formats fall back to CSV, which is what the previous
    ``if format == "xml" else csv`` behaviour did.
    """
    reader = READERS.get((feed_format or "").strip().lower(), _read_ids_csv)
    return reader(path)


def _read_ids_csv(path: Path) -> Tuple[List[str], Optional[str]]:
    header, _encoding, delimiter = read_csv_header(path)
    id_column = detect_id_column(header)
    if id_column is None:
        return [], None

    frame = pl.read_csv(
        path,
        separator=delimiter,
        columns=[id_column],
        schema_overrides={id_column: pl.Utf8},
        infer_schema_length=500,
        encoding="utf8-lossy",
        truncate_ragged_lines=True,
    )
    values = frame.get_column(id_column).cast(pl.Utf8, strict=False).to_list()
    return [str(value).strip() for value in values if value is not None], id_column


def _localname(tag: str) -> str:
    """Strip an XML namespace: ``{ns}id`` -> ``id``."""
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def _read_ids_xml(path: Path) -> Tuple[List[str], Optional[str]]:
    """Stream ``<product>`` / ``<item>`` elements and collect their IDs.

    Supports both shapes Google Merchant Center exports use:
    ``<products><product>`` and ``<rss><channel><item>``.

    Each finished element is cleared and detached from its parent. Clearing
    alone is not enough: the parent keeps a reference, so memory would still
    grow linearly across the feed.
    """
    ids: List[str] = []
    chosen: Optional[str] = None
    open_elements: List[ET.Element] = []

    try:
        for event, element in ET.iterparse(str(path), events=("start", "end")):
            if event == "start":
                open_elements.append(element)
                continue

            if open_elements and open_elements[-1] is element:
                open_elements.pop()

            if _localname(element.tag) not in ("product", "item"):
                continue

            candidates: Dict[str, str] = {}
            for child in element:
                # Handle both a real namespace ({ns}id) and a plain prefix
                # (g:id), which is what Google Merchant Center exports use.
                tag = _localname(child.tag).strip().lower().split(":")[-1]
                text = (child.text or "").strip()
                if tag in ID_PRIORITY and text:
                    candidates.setdefault(tag, text)

            if chosen is None:
                for candidate in ID_PRIORITY:
                    if candidate in candidates:
                        chosen = candidate
                        break

            if chosen and candidates.get(chosen):
                ids.append(candidates[chosen])

            element.clear()
            if open_elements:
                parent = open_elements[-1]
                # del parent[-1] is O(1); parent.remove(element) is O(n) and
                # would make the whole parse quadratic.
                if len(parent) and parent[-1] is element:
                    del parent[-1]
    except ET.ParseError:
        # A truncated export should still yield whatever was parsed so far.
        pass

    return ids, chosen


def normalized_set(raw_ids) -> set:
    """Feed IDs as a lowercase set, which is what the comparison joins on."""
    return {str(value).strip().lower() for value in raw_ids if str(value).strip()}


register_reader("csv", _read_ids_csv)
register_reader("xml", _read_ids_xml)
