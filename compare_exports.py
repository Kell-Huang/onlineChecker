#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
onlineStatusChecker CLI - SFCC x Productsup Feed Diff Checker.

Execution script. Downloads the Global Online Status (GOS) file and the
per-country comparison feeds, computes the missing SKUs, queries the
Productsup channel stage API and writes a five-sheet Excel report.

The Productsup token is read ONLY from config/credentials.json.
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import logging
import random
import re
import shutil
import sys
import threading
import time
import xml.etree.ElementTree as ET
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from typing import (
    Any,
    Callable,
    Dict,
    Iterable,
    List,
    Optional,
    Sequence,
    Tuple,
)
from urllib.parse import urlparse

import chardet
import polars as pl
import requests
from openpyxl import Workbook
from openpyxl.utils import get_column_letter

# ---------------------------------------------------------------------------
# Naming / constants
# ---------------------------------------------------------------------------

APP_NAME = "onlineStatusChecker"
CLI_DESCRIPTION = "onlineStatusChecker CLI"
SESSION_VERSION = "8.1"

BASE_DIR = Path(__file__).resolve().parent
CONFIG_DIR = BASE_DIR / "config"
COUNTRY_COLUMNS_FILE = CONFIG_DIR / "country_columns.json"
CREDENTIALS_FILE = CONFIG_DIR / "credentials.json"
APP_CONFIG_FILE = CONFIG_DIR / "app_config.json"

DEFAULT_BASE_URL = "https://platform-api.productsup.io"

DEFAULT_APP_CONFIG: Dict[str, Any] = {
    "api": {
        "timeout_connect": 10,
        "timeout_read": 60,
        "batch_size": 200,
        "batch_interval_seconds": 1.0,
        "max_retries": 2,
        "retry_backoff_seconds": [2, 4],
    },
    "download": {
        "concurrency": 5,
        "max_retries": 3,
        "retry_backoff_seconds": [2, 4, 8],
        "timeout_connect": 10,
        "timeout_read": 60,
        "chunk_size_bytes": 65536,
    },
    "sampling": {"max_per_country": 1000, "random_seed": 42},
    "paths": {
        "output_dir": "output",
        "logs_dir": "logs",
        "api_file_savings": "api_file_savings",
        "preview_dir": "api_file_savings/_preview",
    },
}

# CLI exit codes (section 11.2)
EXIT_SUCCESS = 0
EXIT_CONFIG_ERROR = 1
EXIT_GOS_ERROR = 2
EXIT_FEED_ERROR = 3
EXIT_CANCELLED = 4
EXIT_EXCEL_ERROR = 5
EXIT_UNKNOWN = 6

# GOS / feed parsing
ONLINE_TRUTHY = {"true", "1", "yes", "y"}
ID_PRIORITY = ["id", "sku", "product_id", "productid", "gtin", "ean"]
PREVIEW_ROWS = 500
PREVIEW_MIN_NEWLINES = 600
MAX_URL_LENGTH = 8000

SUMMARY_COLUMNS = [
    "country",
    "gos_count",
    "feed_count",
    "missing_count",
    "sampled_count",
    "skipped_count",
    "in_channel_count",
    "not_in_channel_count",
    "failed_batch_count",
    "failed_sku_count",
    "unchecked_count",
    "top_reason_1",
    "top_reason_1_pct",
    "top_reason_2",
    "top_reason_2_pct",
    "note",
]

DETAIL_COLUMNS = [
    "country",
    "sku",
    "normalized_sku",
    "status",
    "skip_reason_raw",
    "skip_reason_readable",
    "priority",
]

REASON_COLUMNS = ["country", "reason", "count", "percentage"]

API_LOG_COLUMNS = [
    "timestamp",
    "country",
    "batch_index",
    "sku_count",
    "request_url",
    "status_code",
    "response_count",
    "elapsed_seconds",
    "error_message",
]

FAILED_BATCH_COLUMNS = [
    "country",
    "batch_index",
    "sku_list",
    "error_message",
    "retry_count",
]

# Fields always requested, excluding the ID column (provided per-feed).
API_SKIP_FIELDS = ["___skipped_intermediate", "___skipped_export"]

# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class ConfigError(Exception):
    """Raised when configuration or credentials are missing/invalid."""


class TokenError(Exception):
    """Raised on HTTP 401/403 - token invalid or lacking permission."""


class GosError(Exception):
    """Raised when the GOS file cannot be obtained or parsed."""


class ExcelError(Exception):
    """Raised when the Excel report cannot be written."""


class _Cancelled(Exception):
    """Internal marker for cooperative cancellation."""


# ---------------------------------------------------------------------------
# Logging / redaction
# ---------------------------------------------------------------------------

_LOGGER = logging.getLogger("online_status_checker")
_LOG_FILE: Optional[Path] = None

_TOKEN_HEADER_RE = re.compile(r"(X-Auth-Token\s*:\s*)\S+", re.IGNORECASE)
_TOKEN_QUERY_RE = re.compile(r"(\btoken=)[^&\s]+", re.IGNORECASE)

_LEVELS = {
    "DEBUG": logging.DEBUG,
    "INFO": logging.INFO,
    "WARNING": logging.WARNING,
    "ERROR": logging.ERROR,
    "CRITICAL": logging.CRITICAL,
}


def redact_token(text: str) -> str:
    """Replace any auth token occurrence with '***' (logs, errors, Excel)."""
    if text is None:
        return ""
    text = str(text)
    text = _TOKEN_HEADER_RE.sub(r"\1***", text)
    text = _TOKEN_QUERY_RE.sub(r"\1***", text)
    return text


def setup_logging(logs_dir: Path) -> Path:
    """Configure file + console logging once. Returns the log file path."""
    global _LOG_FILE
    logs_dir.mkdir(parents=True, exist_ok=True)
    if _LOG_FILE is not None:
        return _LOG_FILE

    log_path = (
        logs_dir / f"online_status_checker_{datetime.now():%Y%m%d_%H%M%S}.log"
    )
    formatter = logging.Formatter("[%(asctime)s] [%(levelname)s] %(message)s")

    file_handler = logging.FileHandler(log_path, encoding="utf-8")
    file_handler.setFormatter(formatter)
    file_handler.setLevel(logging.DEBUG)

    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setFormatter(formatter)
    stream_handler.setLevel(logging.INFO)

    _LOGGER.setLevel(logging.DEBUG)
    _LOGGER.addHandler(file_handler)
    _LOGGER.addHandler(stream_handler)
    _LOGGER.propagate = False
    _LOG_FILE = log_path
    return log_path


class Reporter:
    """Fan-out helper: module logger + optional UI callbacks."""

    def __init__(
        self,
        progress_callback: Optional[Callable[[int, str], None]] = None,
        log_callback: Optional[Callable[[str, str], None]] = None,
    ) -> None:
        self._progress = progress_callback
        self._log = log_callback

    def log(self, level: str, message: str) -> None:
        level = (level or "INFO").upper()
        safe = redact_token(message)
        _LOGGER.log(_LEVELS.get(level, logging.INFO), safe)
        if self._log is not None:
            try:
                self._log(level, safe)
            except Exception:  # never let UI callback break the run
                pass

    def progress(self, percent: int, message: str = "") -> None:
        if self._progress is None:
            return
        try:
            self._progress(int(percent), redact_token(message))
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------


def load_json(path: Path) -> dict:
    with Path(path).open("r", encoding="utf-8") as handle:
        return json.load(handle)


def load_credentials(path: Optional[Path] = None) -> Dict[str, str]:
    """Read the Productsup token. Raises ConfigError when unusable."""
    path = Path(path) if path else CREDENTIALS_FILE
    if not path.exists():
        raise ConfigError(
            "Cannot start: config/credentials.json is missing or productsup.token "
            "is empty. Format: client_id:client_secret."
        )
    try:
        data = load_json(path)
    except Exception as exc:
        raise ConfigError(f"Cannot read {path.name}: {exc}") from exc

    productsup = data.get("productsup") or {}
    token = str(productsup.get("token") or "").strip()
    if not token or ":" not in token:
        raise ConfigError(
            "Cannot start: config/credentials.json is missing or productsup.token "
            "is empty. Format: client_id:client_secret."
        )
    base_url = (
        str(productsup.get("base_url") or DEFAULT_BASE_URL).strip().rstrip("/")
    )
    return {"token": token, "base_url": base_url}


def load_app_config(path: Optional[Path] = None) -> Dict[str, Any]:
    """Load app_config.json merged on top of built-in defaults."""
    path = Path(path) if path else APP_CONFIG_FILE
    merged: Dict[str, Any] = {
        k: dict(v) for k, v in DEFAULT_APP_CONFIG.items()
    }
    if path.exists():
        try:
            data = load_json(path)
            for section, values in (data or {}).items():
                if isinstance(values, dict) and isinstance(
                    merged.get(section), dict
                ):
                    merged[section].update(values)
                else:
                    merged[section] = values
        except Exception as exc:
            _LOGGER.warning("Failed to read %s, using defaults: %s", path, exc)
    return merged


def load_country_columns(
    path: Optional[Path] = None,
) -> Tuple[Dict[str, Dict[str, str]], Dict[str, str]]:
    """
    Return (country_columns, aliases).

    The '_aliases' key is stripped out and returned separately. It maps
    non-standard tokens (e.g. 'uae', 'ksa') to standard country codes.
    """
    path = Path(path) if path else COUNTRY_COLUMNS_FILE
    if not path.exists():
        _LOGGER.warning("country_columns.json not found at %s", path)
        return {}, {}
    try:
        data = load_json(path)
    except Exception as exc:
        _LOGGER.warning("Failed to read country_columns.json: %s", exc)
        return {}, {}
    aliases = data.pop("_aliases", None) or {}
    return data, aliases


# ---------------------------------------------------------------------------
# Generic helpers
# ---------------------------------------------------------------------------


def _sanitize_filename(name: str) -> str:
    """Make a filename Windows-safe."""
    name = re.sub(r'[\\/:*?"<>|]', "_", str(name))
    name = name.strip().strip(".")
    return name or "file"


def _safe_unlink(path: Path) -> None:
    try:
        if path.exists():
            path.unlink()
    except OSError:
        pass


def _clean_directory(path: Path, keep: Optional[Iterable[str]] = None) -> None:
    """Remove all entries of `path` except the names listed in `keep`."""
    keep_set = set(keep or [])
    if not path.exists():
        path.mkdir(parents=True, exist_ok=True)
        return
    for child in path.iterdir():
        if child.name in keep_set:
            continue
        try:
            if child.is_dir():
                shutil.rmtree(child)
            else:
                child.unlink()
        except OSError as exc:
            _LOGGER.warning("Could not remove %s: %s", child, exc)


def _localname(tag: str) -> str:
    """Strip an XML namespace prefix: '{ns}id' -> 'id'."""
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def _detect_format(
    url: str,
    declared: Optional[str],
    content_type: Optional[str] = None,
) -> str:
    """
    Detect feed format: explicit declaration > Content-Type > URL suffix.
    Content-Type verification takes precedence over the URL suffix so that
    mislabelled URLs are still parsed correctly.
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

    path = urlparse(url).path.lower()
    return "xml" if path.endswith(".xml") else "csv"


# ---------------------------------------------------------------------------
# Number parsing
# ---------------------------------------------------------------------------

_NUMBER_CLEAN_RE = re.compile(r"[^0-9,.\-]")


def _parse_number(raw: Any) -> Tuple[float, bool]:
    """
    Parse a stock/price value. Returns (value, parsed_ok).

    Empty values return (0.0, True) - they are legitimately "not set".
    Unparseable values return (0.0, False) so the caller can warn.
    """
    if raw is None:
        return 0.0, True
    text = str(raw).strip()
    if not text:
        return 0.0, True
    cleaned = _NUMBER_CLEAN_RE.sub("", text)
    if not cleaned or cleaned in {"-", ".", ",", "-.", "-,"}:
        return 0.0, False

    if "," in cleaned and "." in cleaned:
        # The right-most separator is the decimal separator.
        if cleaned.rfind(",") > cleaned.rfind("."):
            cleaned = cleaned.replace(".", "").replace(",", ".")
        else:
            cleaned = cleaned.replace(",", "")
    elif "," in cleaned:
        parts = cleaned.split(",")
        # A single group of exactly 3 digits after the comma = thousands.
        if len(parts) == 2 and len(parts[1]) == 3 and parts[1].isdigit():
            cleaned = cleaned.replace(",", "")
        else:
            cleaned = cleaned.replace(",", ".")

    try:
        return float(cleaned), True
    except ValueError:
        return 0.0, False


def _make_number_parser(label: str, counters: Dict[str, int]):
    """Build a map_elements-compatible parser that counts failures."""

    def _parse(raw: Any) -> float:
        value, ok = _parse_number(raw)
        if not ok:
            counters[label] = counters.get(label, 0) + 1
        return value

    return _parse


# ---------------------------------------------------------------------------
# Downloads
# ---------------------------------------------------------------------------


def download_file(
    url: str,
    dest: Path,
    download_cfg: Dict[str, Any],
    cancel_event: threading.Event,
    reporter: Reporter,
    label: str = "",
) -> Tuple[bool, str, Optional[str]]:
    """
    Stream-download `url` into `dest` atomically (.part then rename).

    `label` is a short prefix such as "[de] " that is prepended to every
    progress and retry log line, so concurrent downloads remain readable.

    Progress is reported at most once every 2 seconds per attempt. Speed is
    the average over the last reporting interval, not the whole download.

    Returns (ok, error_message, content_type). error_message is
    "cancelled" when the user cancelled the run.
    """
    attempts = int(download_cfg.get("max_retries", 3))
    backoffs = list(download_cfg.get("retry_backoff_seconds", [2, 4, 8]))
    chunk_size = int(download_cfg.get("chunk_size_bytes", 65536))
    timeout = (
        int(download_cfg.get("timeout_connect", 10)),
        int(download_cfg.get("timeout_read", 60)),
    )

    dest.parent.mkdir(parents=True, exist_ok=True)
    part_path = dest.with_suffix(dest.suffix + ".part")
    last_error = ""

    for attempt in range(attempts):
        if cancel_event.is_set():
            _safe_unlink(part_path)
            return False, "cancelled", None

        session = requests.Session()
        try:
            with session.get(url, stream=True, timeout=timeout) as response:
                if response.status_code in (401, 403):
                    _safe_unlink(part_path)
                    return (
                        False,
                        f"HTTP {response.status_code} (access denied)",
                        None,
                    )
                response.raise_for_status()

                content_type = response.headers.get("Content-Type") or ""

                # Parse Content-Length once; used for the progress percentage
                # and the final size check. Missing or invalid => skip both.
                expected_bytes = 0
                raw_expected = response.headers.get("Content-Length")
                if raw_expected is not None:
                    try:
                        expected_bytes = int(raw_expected)
                    except (TypeError, ValueError):
                        expected_bytes = 0

                written = 0
                last_report_time = time.time()
                last_report_bytes = 0
                with part_path.open("wb") as handle:
                    for chunk in response.iter_content(chunk_size=chunk_size):
                        if cancel_event.is_set():
                            raise _Cancelled()
                        if not chunk:
                            continue
                        handle.write(chunk)
                        written += len(chunk)

                        now = time.time()
                        elapsed_since_report = now - last_report_time
                        if elapsed_since_report >= 2.0:
                            speed = (
                                written - last_report_bytes
                            ) / elapsed_since_report
                            if expected_bytes > 0:
                                pct = 100.0 * written / expected_bytes
                                reporter.log(
                                    "INFO",
                                    f"{label}{written / 1e6:.1f} / "
                                    f"{expected_bytes / 1e6:.1f} MB ({pct:.0f}%) "
                                    f"@ {speed / 1e6:.2f} MB/s",
                                )
                            else:
                                reporter.log(
                                    "INFO",
                                    f"{label}{written / 1e6:.1f} MB "
                                    f"@ {speed / 1e6:.2f} MB/s",
                                )
                            last_report_time = now
                            last_report_bytes = written

            if written == 0:
                raise IOError("downloaded file is empty")
            if expected_bytes > 0 and expected_bytes != written:
                raise IOError(
                    f"size mismatch (expected {expected_bytes}, got {written})"
                )

            part_path.replace(dest)
            return True, "", content_type

        except _Cancelled:
            _safe_unlink(part_path)
            return False, "cancelled", None
        except (
            Exception
        ) as exc:  # noqa: BLE001 - one failure must not break the run
            _safe_unlink(part_path)
            last_error = f"{type(exc).__name__}: {exc}"
            if attempt < attempts - 1:
                wait = backoffs[min(attempt, len(backoffs) - 1)]
                reporter.log(
                    "WARNING",
                    f"{label}Download attempt {attempt + 1}/{attempts} failed for "
                    f"{url}: {redact_token(last_error)}. Retrying in {wait}s.",
                )
                if cancel_event.wait(wait):
                    return False, "cancelled", None
        finally:
            session.close()

    return False, last_error, None


def download_gos_preview(
    url: str,
    dest: Path,
    download_cfg: Dict[str, Any],
    cancel_event: threading.Event,
    reporter: Reporter,
) -> Tuple[Optional[List[str]], Optional[str]]:
    """
    Stream only the first ~500 rows of a huge GOS file.

    Returns (header_columns, delimiter) or (None, None) on failure.
    """
    chunk_size = int(download_cfg.get("chunk_size_bytes", 65536))
    timeout = (
        int(download_cfg.get("timeout_connect", 10)),
        int(download_cfg.get("timeout_read", 60)),
    )

    dest.parent.mkdir(parents=True, exist_ok=True)
    session = requests.Session()
    buffer = bytearray()
    try:
        with session.get(url, stream=True, timeout=timeout) as response:
            response.raise_for_status()
            for chunk in response.iter_content(chunk_size=chunk_size):
                if cancel_event.is_set():
                    raise _Cancelled()
                if not chunk:
                    continue
                buffer.extend(chunk)
                if buffer.count(b"\n") >= PREVIEW_MIN_NEWLINES:
                    break
    except _Cancelled:
        raise
    except Exception as exc:  # noqa: BLE001
        reporter.log(
            "ERROR", f"GOS preview download failed: {redact_token(str(exc))}"
        )
        return None, None
    finally:
        session.close()

    # Drop a possibly truncated trailing line.
    last_newline = buffer.rfind(b"\n")
    if last_newline == -1:
        reporter.log(
            "ERROR", "GOS preview download returned no complete line."
        )
        return None, None

    raw = bytes(buffer[: last_newline + 1])
    encoding = chardet.detect(raw[:65536]).get("encoding") or "utf-8"
    text = raw.decode(encoding, errors="replace")

    sample = "\n".join(text.splitlines()[:10])
    try:
        delimiter = csv.Sniffer().sniff(sample, delimiters=",;\t|").delimiter
    except csv.Error:
        delimiter = ","

    rows: List[List[str]] = []
    reader = csv.reader(io.StringIO(text), delimiter=delimiter)
    for index, row in enumerate(reader):
        if index == 0:
            rows.append([cell.strip().lstrip("\ufeff") for cell in row])
            continue
        if len(rows) > PREVIEW_ROWS:
            break
        rows.append(row)

    if not rows:
        reporter.log("ERROR", "GOS preview contains no header row.")
        return None, None

    try:
        with dest.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.writer(
                handle, delimiter=delimiter, lineterminator="\n"
            )
            writer.writerows(rows)
    except OSError as exc:
        reporter.log("ERROR", f"Could not write GOS preview: {exc}")
        return None, None

    header = rows[0]
    reporter.log(
        "INFO",
        f"GOS preview written to {dest.name} ({len(rows) - 1} rows, encoding "
        f"{encoding}, delimiter '{delimiter}').",
    )
    return header, delimiter


# ---------------------------------------------------------------------------
# CSV / XML inspection
# ---------------------------------------------------------------------------


def read_csv_header(path: Path) -> Tuple[List[str], str, str]:
    """Return (header_columns, encoding, delimiter) for a CSV file."""
    with path.open("rb") as handle:
        raw = handle.read(65536)
    encoding = chardet.detect(raw).get("encoding") or "utf-8"
    text = raw.decode(encoding, errors="replace")

    sample = "\n".join(text.splitlines()[:10])
    try:
        delimiter = csv.Sniffer().sniff(sample, delimiters=",;\t|").delimiter
    except csv.Error:
        delimiter = ","

    reader = csv.reader(io.StringIO(text), delimiter=delimiter)
    header = next(reader, [])
    header = [cell.strip().lstrip("\ufeff") for cell in header]
    return header, encoding, delimiter


def detect_id_column(columns: Sequence[str]) -> Optional[str]:
    """Auto-detect the ID column using the documented priority order."""
    lookup: Dict[str, str] = {}
    for column in columns:
        key = column.strip().lower()
        key = key.split(":")[-1]  # strip namespace prefix (g:id -> id)
        lookup.setdefault(key, column)
    for candidate in ID_PRIORITY:
        if candidate in lookup:
            return lookup[candidate]
    return None


def read_feed_ids_csv(path: Path) -> Tuple[List[str], Optional[str]]:
    """Read only the ID column of a CSV comparison feed."""
    header, _, delimiter = read_csv_header(path)
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
    return [str(v).strip() for v in values if v is not None], id_column


def read_feed_ids_xml(path: Path) -> Tuple[List[str], Optional[str]]:
    """
    Stream an XML feed with iterparse and collect product IDs only.

    Supports both common Google Merchant Center shapes:
      - <products><product>...</product></products>
      - <rss><channel><item>...</item></channel></rss>

    The tag used for the ID is determined from the first product element
    that carries one of the priority candidates.

    To keep memory bounded on large feeds, each processed item is cleared
    and detached from its parent as soon as its end event fires. Without
    detaching, the parent would keep a reference to every item ever seen
    and memory would grow linearly with the number of products.
    """
    ids: List[str] = []
    chosen: Optional[str] = None
    # Stack of currently open elements, used to reach the parent of a
    # finished item so the cleared element can be dropped from it.
    open_elements: List[Any] = []

    try:
        for event, element in ET.iterparse(str(path), events=("start", "end")):
            if event == "start":
                open_elements.append(element)
                continue

            # event == "end": drop self from the stack before handling.
            if open_elements and open_elements[-1] is element:
                open_elements.pop()

            if _localname(element.tag) not in ("product", "item"):
                continue

            candidates: Dict[str, str] = {}
            for child in element:
                child_tag = _localname(child.tag).strip().lower()
                if child_tag in ID_PRIORITY and (child.text or "").strip():
                    candidates.setdefault(
                        child_tag, (child.text or "").strip()
                    )

            if chosen is None:
                for candidate in ID_PRIORITY:
                    if candidate in candidates:
                        chosen = candidate
                        break

            if chosen and candidates.get(chosen):
                ids.append(candidates[chosen])

            element.clear()
            # Detach from the parent so the cleared element can be freed.
            # `del parent[-1]` is O(1); `parent.remove(element)` is O(n)
            # and would become quadratic across the whole feed.
            if open_elements:
                parent = open_elements[-1]
                if len(parent) > 0 and parent[-1] is element:
                    del parent[-1]
    except ET.ParseError as exc:
        _LOGGER.warning("XML parse error in %s: %s", path, exc)

    return ids, chosen


def read_feed_ids(
    path: Path, feed_format: str
) -> Tuple[List[str], Optional[str]]:
    """Dispatch to the CSV or XML reader."""
    if feed_format == "xml":
        return read_feed_ids_xml(path)
    return read_feed_ids_csv(path)


# ---------------------------------------------------------------------------
# Productsup API
# ---------------------------------------------------------------------------


def _build_sku_filter(skus: Sequence[str], id_column: str) -> str:
    """Build the SQL-syntax filter, escaping single quotes."""
    escaped = ["'" + str(sku).replace("'", "''") + "'" for sku in skus]
    return f"{id_column} IN (" + ",".join(escaped) + ")"


def _estimate_request_length(
    base_url: str,
    site_id: str,
    channel_id: str,
    skus: Sequence[str],
    id_column: str,
) -> int:
    """Cheap upper-bound estimate of the prepared request URL length."""
    path = f"/product/v2/site/{site_id}/stage/channel/{channel_id}"
    return (
        len(base_url)
        + len(path)
        + len(_build_sku_filter(skus, id_column))
        + 200
    )


def _query_batch(
    session: requests.Session,
    base_url: str,
    token: str,
    site_id: str,
    channel_id: str,
    skus: Sequence[str],
    id_column: str,
    api_cfg: Dict[str, Any],
    cancel_event: threading.Event,
) -> Dict[str, Any]:
    """
    Query the channel stage for one batch of SKUs (serial, with retries).

    The `id_column` name is the field used both for filtering and for
    reading back the SKU value. It comes from the feed's ID Column setting.

    Returns a dict:
      ok, products, status_code, request_url, error, retry_count, elapsed
    """
    url = f"{base_url}/product/v2/site/{site_id}/stage/channel/{channel_id}"
    requested_fields = [id_column, *API_SKIP_FIELDS]
    params: List[Tuple[str, Any]] = [
        ("filter", _build_sku_filter(skus, id_column))
    ]
    for index, field_name in enumerate(requested_fields):
        params.append((f"fields[{index}]", field_name))
    params.append(("hidden", "1"))
    params.append(("limit", "1000"))
    headers = {"X-Auth-Token": token}

    max_retries = int(api_cfg.get("max_retries", 2))
    backoffs = list(api_cfg.get("retry_backoff_seconds", [2, 4]))
    timeout = (
        int(api_cfg.get("timeout_connect", 10)),
        int(api_cfg.get("timeout_read", 60)),
    )

    attempt = 0
    last_error = ""
    last_status: Optional[int] = None
    last_url = redact_token(url)

    while True:
        if cancel_event.is_set():
            raise _Cancelled()

        try:
            prepared = session.prepare_request(
                requests.Request("GET", url, params=params, headers=headers)
            )
        except Exception as exc:  # noqa: BLE001
            return {
                "ok": False,
                "products": [],
                "status_code": None,
                "request_url": last_url,
                "error": redact_token(f"{type(exc).__name__}: {exc}"),
                "retry_count": attempt,
                "elapsed": 0.0,
            }

        last_url = redact_token(prepared.url)
        started = time.time()

        try:
            response = session.send(prepared, timeout=timeout)
        except TokenError:
            raise
        except (requests.Timeout, requests.ConnectionError) as exc:
            # Timeouts are treated like 5xx.
            last_error = f"{type(exc).__name__}: {exc}"
            last_status = None
            if attempt >= max_retries:
                return {
                    "ok": False,
                    "products": [],
                    "status_code": last_status,
                    "request_url": last_url,
                    "error": redact_token(last_error),
                    "retry_count": attempt,
                    "elapsed": time.time() - started,
                }
            wait = backoffs[min(attempt, len(backoffs) - 1)]
            attempt += 1
            if cancel_event.wait(wait):
                raise _Cancelled()
            continue

        elapsed = time.time() - started
        status = response.status_code

        if status in (401, 403):
            raise TokenError("Token is invalid or lacks permission.")

        if status == 429:
            last_status = status
            last_error = "HTTP 429 Too Many Requests"
            if attempt >= max_retries:
                return {
                    "ok": False,
                    "products": [],
                    "status_code": status,
                    "request_url": last_url,
                    "error": last_error,
                    "retry_count": attempt,
                    "elapsed": elapsed,
                }
            retry_after = response.headers.get("Retry-After")
            wait = backoffs[min(attempt, len(backoffs) - 1)]
            try:
                if retry_after is not None:
                    wait = max(float(retry_after), 0.0)
            except ValueError:
                pass
            attempt += 1
            if cancel_event.wait(wait):
                raise _Cancelled()
            continue

        if status >= 500:
            last_status = status
            last_error = f"HTTP {status}"
            if attempt >= max_retries:
                return {
                    "ok": False,
                    "products": [],
                    "status_code": status,
                    "request_url": last_url,
                    "error": last_error,
                    "retry_count": attempt,
                    "elapsed": elapsed,
                }
            wait = backoffs[min(attempt, len(backoffs) - 1)]
            attempt += 1
            if cancel_event.wait(wait):
                raise _Cancelled()
            continue

        if status >= 400:
            # Other 4xx: log, do not retry.
            return {
                "ok": False,
                "products": [],
                "status_code": status,
                "request_url": last_url,
                "error": redact_token(f"HTTP {status}: {response.text[:200]}"),
                "retry_count": attempt,
                "elapsed": elapsed,
            }

        try:
            payload = response.json()
        except ValueError as exc:
            return {
                "ok": False,
                "products": [],
                "status_code": status,
                "request_url": last_url,
                "error": redact_token(f"Invalid JSON response: {exc}"),
                "retry_count": attempt,
                "elapsed": elapsed,
            }

        products = payload.get("products") or []
        return {
            "ok": True,
            "products": products,
            "status_code": status,
            "request_url": last_url,
            "error": "",
            "retry_count": attempt,
            "elapsed": elapsed,
        }


# ---------------------------------------------------------------------------
# Skip reason formatting
# ---------------------------------------------------------------------------


def format_skip_reason(
    export_raw: str, intermediate_raw: str
) -> Tuple[str, str]:
    """
    Convert raw 'priority::field::reason' values into a readable string
    plus the smallest numeric priority found (or "" when none parsed).
    """
    parts: List[str] = []
    priorities: List[int] = []

    for raw in (export_raw or "", intermediate_raw or ""):
        if not raw:
            continue
        for item in str(raw).split(","):
            item = item.strip()
            if not item:
                continue
            segments = item.split("::")
            if len(segments) >= 3:
                priority, field = segments[0].strip(), segments[1].strip()
                reason = "::".join(segments[2:]).strip()
                parts.append(f'"{field}" {reason} (priority: {priority})')
                try:
                    priorities.append(int(priority))
                except ValueError:
                    pass
            else:
                parts.append(item)

    readable = "; ".join(parts)
    priority = str(min(priorities)) if priorities else ""
    return readable, priority


# ---------------------------------------------------------------------------
# Excel output
# ---------------------------------------------------------------------------


def _write_sheet(
    worksheet, headers: Sequence[str], rows: Sequence[Dict[str, Any]]
) -> None:
    """Write headers + dict rows and auto-size columns."""
    worksheet.append(list(headers))
    for row in rows:
        worksheet.append([row.get(column) for column in headers])

    for index, _ in enumerate(headers, start=1):
        longest = len(str(headers[index - 1]))
        for row in rows:
            value = row.get(headers[index - 1])
            if value is not None:
                longest = max(longest, len(str(value)))
        worksheet.column_dimensions[get_column_letter(index)].width = min(
            longest + 2, 60
        )


def write_excel(
    output_path: Path,
    summary_rows: Sequence[Dict[str, Any]],
    detail_rows: Sequence[Dict[str, Any]],
    reason_rows: Sequence[Dict[str, Any]],
    api_log_rows: Sequence[Dict[str, Any]],
    failed_rows: Sequence[Dict[str, Any]],
) -> None:
    """Write the five-sheet report. Raises ExcelError on failure."""
    try:
        workbook = Workbook()
        summary_ws = workbook.active
        summary_ws.title = "Summary"
        _write_sheet(summary_ws, SUMMARY_COLUMNS, summary_rows)

        _write_sheet(
            workbook.create_sheet("Sampled Details"),
            DETAIL_COLUMNS,
            detail_rows,
        )
        _write_sheet(
            workbook.create_sheet("Skip Reason Distribution"),
            REASON_COLUMNS,
            reason_rows,
        )
        _write_sheet(
            workbook.create_sheet("API Log"), API_LOG_COLUMNS, api_log_rows
        )
        _write_sheet(
            workbook.create_sheet("Failed Batches"),
            FAILED_BATCH_COLUMNS,
            failed_rows,
        )

        output_path.parent.mkdir(parents=True, exist_ok=True)
        workbook.save(output_path)
    except Exception as exc:  # noqa: BLE001
        _safe_unlink(output_path)
        raise ExcelError(str(exc)) from exc


# ---------------------------------------------------------------------------
# Main orchestration
# ---------------------------------------------------------------------------


def _blank_summary_row(country: str, note: str) -> Dict[str, Any]:
    row = {column: None for column in SUMMARY_COLUMNS}
    row["country"] = country
    row["note"] = note
    return row


def _gos_filtered_frame(
    gos_df: pl.DataFrame,
    online_column: str,
    stock_column: str,
    price_column: str,
    reporter: Reporter,
    country: str,
) -> pl.DataFrame:
    """
    Apply the GOS filter for one country.

    online truthy AND stock > 0 AND price > 0. Returns a frame with
    'sku' (original) and 'normalized_sku' (strip + lower), deduplicated.
    """
    subset = gos_df.select(["id", online_column, stock_column, price_column])

    subset = subset.with_columns(
        pl.col(online_column)
        .cast(pl.Utf8, strict=False)
        .fill_null("")
        .str.strip_chars()
        .str.to_lowercase()
        .alias("_online")
    )
    # Cheap pre-filter before the Python-level numeric parsing.
    subset = subset.filter(pl.col("_online").is_in(list(ONLINE_TRUTHY)))
    if subset.height == 0:
        return pl.DataFrame(schema={"sku": pl.Utf8, "normalized_sku": pl.Utf8})

    counters: Dict[str, int] = {}
    stock_parser = _make_number_parser(stock_column, counters)
    price_parser = _make_number_parser(price_column, counters)

    subset = subset.with_columns(
        [
            pl.col(stock_column)
            .cast(pl.Utf8, strict=False)
            .map_elements(stock_parser, return_dtype=pl.Float64)
            .alias("_stock"),
            pl.col(price_column)
            .cast(pl.Utf8, strict=False)
            .map_elements(price_parser, return_dtype=pl.Float64)
            .alias("_price"),
        ]
    )
    subset = subset.filter((pl.col("_stock") > 0) & (pl.col("_price") > 0))

    for label, count in counters.items():
        if count:
            reporter.log(
                "WARNING",
                f"[{country}] {count} value(s) in column '{label}' could not be "
                f"parsed as numbers and were treated as 0.",
            )

    return subset.select(
        [
            pl.col("id").alias("sku"),
            pl.col("id")
            .str.strip_chars()
            .str.to_lowercase()
            .alias("normalized_sku"),
        ]
    ).unique(subset=["normalized_sku"], keep="first")


def _normalized_feed_set(raw_ids: Iterable[str]) -> set:
    return {
        str(value).strip().lower() for value in raw_ids if str(value).strip()
    }


def run_comparison(
    session_config: dict,
    credentials: dict,
    progress_callback: Optional[Callable[[int, str], None]] = None,
    log_callback: Optional[Callable[[str, str], None]] = None,
    cancel_event: Optional[threading.Event] = None,
) -> dict:
    """
    Execute the whole comparison workflow.

    Returns:
      {
        "success": bool,
        "cancelled": bool,
        "output_path": str | None,
        "error": str | None,
        "error_kind": str | None,      # config|gos|token|excel|other
        "summary": list[dict],
        "feed_failures": int,
      }
    """
    reporter = Reporter(progress_callback, log_callback)
    cancel_event = cancel_event or threading.Event()

    result: Dict[str, Any] = {
        "success": False,
        "cancelled": False,
        "output_path": None,
        "error": None,
        "error_kind": None,
        "summary": [],
        "feed_failures": 0,
    }

    app_cfg = load_app_config()
    paths_cfg = app_cfg["paths"]
    setup_logging(BASE_DIR / paths_cfg["logs_dir"])

    summary_rows: List[Dict[str, Any]] = []
    detail_rows: List[Dict[str, Any]] = []
    reason_rows: List[Dict[str, Any]] = []
    api_log_rows: List[Dict[str, Any]] = []
    failed_rows: List[Dict[str, Any]] = []

    try:
        # -- validate session -------------------------------------------------
        version = str(session_config.get("version") or "").strip()
        if version != SESSION_VERSION:
            reporter.log(
                "WARNING",
                f"Session version '{version or 'unknown'}' differs from expected "
                f"'{SESSION_VERSION}'. Continuing anyway.",
            )

        gos_cfg = session_config.get("gos") or {}
        feeds_cfg = [f for f in (session_config.get("feeds") or []) if f]
        if not gos_cfg:
            raise ConfigError("Session config does not define a GOS channel.")
        if not feeds_cfg:
            raise ConfigError(
                "Session config does not define any comparison feed."
            )

        token = credentials.get("token") or ""
        base_url = (credentials.get("base_url") or DEFAULT_BASE_URL).rstrip(
            "/"
        )
        if not token:
            raise ConfigError("Missing Productsup token.")

        api_cfg = app_cfg["api"]
        download_cfg = app_cfg["download"]
        sampling_cfg = session_config.get("sampling") or app_cfg["sampling"]
        max_per_country = int(sampling_cfg.get("max_per_country", 1000))
        batch_size = int(
            sampling_cfg.get("batch_size", api_cfg.get("batch_size", 200))
        )
        random_seed = int(sampling_cfg.get("random_seed", 42))

        savings_dir = BASE_DIR / paths_cfg["api_file_savings"]
        preview_dir = BASE_DIR / paths_cfg["preview_dir"]
        output_dir = BASE_DIR / paths_cfg["output_dir"]

        # -- clean working directories (preview folder is preserved) ----------
        _clean_directory(output_dir)
        _clean_directory(savings_dir, keep={"_preview"})
        preview_dir.mkdir(parents=True, exist_ok=True)

        # -- deduplicate feeds by (site_id, channel_id) -----------------------
        seen_keys = set()
        feeds: List[Dict[str, Any]] = []
        for feed in feeds_cfg:
            key = (str(feed.get("site_id")), str(feed.get("channel_id")))
            if key in seen_keys:
                continue
            seen_keys.add(key)
            feeds.append(feed)

        # -- 0-10%: download and read the GOS file ----------------------------
        reporter.progress(1, "Downloading GOS file")
        reporter.log("INFO", "[GOS] Downloading Global Online Status file")
        gos_path = savings_dir / f"gos_{gos_cfg.get('channel_id')}.csv"
        ok, error, _ = download_file(
            gos_cfg.get("url", ""),
            gos_path,
            download_cfg,
            cancel_event,
            reporter,
            label="[GOS] ",
        )
        if cancel_event.is_set():
            raise _Cancelled()
        if not ok:
            raise GosError(f"GOS download failed: {error}")

        reporter.progress(4, "Reading GOS file")
        header, _, gos_delimiter = read_csv_header(gos_path)
        header_lookup = {column.strip(): column for column in header}
        if "id" not in header_lookup:
            raise GosError("GOS file must contain an 'id' column.")

        needed_columns: List[str] = ["id"]
        for feed in feeds:
            mapping = feed.get("gos_columns") or {}
            for key in ("online", "stock", "price"):
                column = mapping.get(key)
                if (
                    column
                    and column in header_lookup
                    and column not in needed_columns
                ):
                    needed_columns.append(column)

        gos_df = pl.read_csv(
            gos_path,
            separator=gos_delimiter,
            columns=needed_columns,
            schema_overrides={column: pl.Utf8 for column in needed_columns},
            infer_schema_length=500,
            encoding="utf8-lossy",
            truncate_ragged_lines=True,
        )
        gos_df = gos_df.with_columns(
            pl.col("id")
            .cast(pl.Utf8, strict=False)
            .fill_null("")
            .str.strip_chars()
            .alias("id")
        )
        gos_df = gos_df.filter(pl.col("id") != "")
        gos_df = gos_df.unique(subset=["id"], keep="first")
        reporter.log("INFO", f"GOS loaded: {gos_df.height} unique rows.")
        reporter.progress(10, "GOS loaded")

        # -- 10-40%: download comparison feeds --------------------------------
        feed_paths: Dict[int, Path] = {}
        feed_content_types: Dict[int, Optional[str]] = {}
        failed_feed_indexes: List[int] = []

        def _download_feed(
            index: int, feed: Dict[str, Any]
        ) -> Tuple[int, Path, bool, str, Optional[str]]:
            url = feed.get("url", "")
            feed_country = str(feed.get("country") or "?")
            channel_name = str(
                feed.get("channel_name") or feed.get("channel_id") or ""
            )
            label = f"[{feed_country}] "
            reporter.log("INFO", f"{label}Downloading feed: {channel_name}")

            original = _sanitize_filename(
                Path(urlparse(url).path).name
                or f"feed_{feed.get('channel_id')}.csv"
            )
            destination = savings_dir / (
                f"{feed.get('country', 'xx')}_{feed.get('channel_id')}_{original}"
            )
            ok_inner, error_inner, content_type = download_file(
                url,
                destination,
                download_cfg,
                cancel_event,
                reporter,
                label=label,
            )
            return index, destination, ok_inner, error_inner, content_type

        total_feeds = max(len(feeds), 1)
        completed = 0
        with ThreadPoolExecutor(
            max_workers=int(download_cfg.get("concurrency", 5))
        ) as pool:
            futures = {
                pool.submit(_download_feed, index, feed): index
                for index, feed in enumerate(feeds)
            }
            for future in as_completed(futures):
                index, destination, ok_inner, error_inner, content_type = (
                    future.result()
                )
                completed += 1
                reporter.progress(
                    10 + int(30 * completed / total_feeds),
                    f"{completed} / {total_feeds} files",
                )
                if cancel_event.is_set():
                    raise _Cancelled()
                feed_country = str(feeds[index].get("country") or "?")
                if ok_inner:
                    feed_paths[index] = destination
                    feed_content_types[index] = content_type
                    reporter.log(
                        "INFO",
                        f"[{feed_country}] Feed downloaded: {destination.name}",
                    )
                else:
                    failed_feed_indexes.append(index)
                    reporter.log(
                        "ERROR",
                        f"[{feed_country}] feed download failed: "
                        f"{redact_token(error_inner)}",
                    )
        result["feed_failures"] = len(failed_feed_indexes)

        # -- detect ID columns ------------------------------------------------
        feed_ids: Dict[int, Optional[set]] = {}
        for index, feed in enumerate(feeds):
            if index not in feed_paths:
                continue
            path = feed_paths[index]
            feed_format = _detect_format(
                feed.get("url", ""),
                feed.get("format"),
                feed_content_types.get(index),
            )
            try:
                raw_ids, detected = read_feed_ids(path, feed_format)
            except Exception as exc:  # noqa: BLE001
                reporter.log(
                    "ERROR",
                    f"[{feed.get('country')}] failed to read feed ids: "
                    f"{redact_token(str(exc))}",
                )
                raw_ids, detected = [], None

            if detected is None:
                reporter.log(
                    "ERROR",
                    f"[{feed.get('country')}] ID column could not be detected in "
                    f"{path.name}.",
                )
                feed_ids[index] = None
            else:
                feed_ids[index] = _normalized_feed_set(raw_ids)
                reporter.log(
                    "INFO",
                    f"[{feed.get('country')}] id column '{detected}', "
                    f"{len(feed_ids[index])} unique ids.",
                )

        # -- 40-60%: per-country join -----------------------------------------
        country_payloads: List[Dict[str, Any]] = []
        total_countries = max(len(feeds), 1)

        for index, feed in enumerate(feeds):
            country = str(feed.get("country") or "").strip() or f"feed_{index}"
            reporter.progress(
                40 + int(20 * index / total_countries), f"{country}"
            )

            if index not in feed_paths:
                summary_rows.append(
                    _blank_summary_row(country, "feed download failed")
                )
                continue
            if feed_ids.get(index) is None:
                summary_rows.append(
                    _blank_summary_row(country, "id column not detected")
                )
                continue

            mapping = feed.get("gos_columns") or {}
            columns = [
                mapping.get("online"),
                mapping.get("stock"),
                mapping.get("price"),
            ]
            invalid_names = [
                name
                for name, column in zip(("online", "stock", "price"), columns)
                if not column or column not in gos_df.columns
            ]
            if invalid_names:
                reporter.log(
                    "ERROR",
                    f"[{country}] invalid column mapping {invalid_names} - "
                    f"country skipped.",
                )
                summary_rows.append(
                    _blank_summary_row(country, "invalid column mapping")
                )
                continue

            gos_filtered = _gos_filtered_frame(
                gos_df, columns[0], columns[1], columns[2], reporter, country
            )
            gos_count = gos_filtered.height

            feed_set = feed_ids[index] or set()
            feed_df = pl.DataFrame(
                {"normalized_sku": sorted(feed_set)},
                schema={"normalized_sku": pl.Utf8},
            )
            missing_df = gos_filtered.join(
                feed_df, on="normalized_sku", how="anti"
            )
            missing_count = missing_df.height

            country_payloads.append(
                {
                    "country": country,
                    "feed": feed,
                    "gos_count": gos_count,
                    "feed_count": len(feed_set),
                    "missing_count": missing_count,
                    "missing_df": missing_df,
                }
            )

        # -- 60-90%: sampling + API queries -----------------------------------
        total_payloads = max(len(country_payloads), 1)

        with requests.Session() as api_session:
            for position, payload in enumerate(country_payloads):
                if cancel_event.is_set():
                    raise _Cancelled()

                country = payload["country"]
                gos_count = payload["gos_count"]
                feed_count = payload["feed_count"]
                missing_count = payload["missing_count"]
                missing_df = payload["missing_df"]
                id_column = str(payload["feed"].get("id_column") or "id")
                notes: List[str] = []

                reporter.progress(
                    60 + int(30 * position / total_payloads), f"{country}"
                )

                if missing_count == 0:
                    notes.append("no missing SKUs")

                sample_size = min(missing_count, max_per_country)
                sampled_skus: List[str] = []
                if sample_size > 0:
                    candidates = sorted(
                        {
                            str(sku)
                            for sku in missing_df.get_column("sku").to_list()
                        }
                    )
                    rng = random.Random(random_seed)
                    if sample_size >= len(candidates):
                        sampled_skus = candidates
                    else:
                        sampled_skus = rng.sample(candidates, sample_size)

                sampled_count = len(sampled_skus)

                returned: Dict[str, Dict[str, Any]] = {}
                covered: set = set()
                failed_skus: set = set()
                failed_batch_count = 0
                reason_counter: Counter = Counter()

                queue: List[List[str]] = [
                    sampled_skus[i : i + batch_size]
                    for i in range(0, len(sampled_skus), batch_size)
                ]
                batch_index = 0
                queue_position = 0
                last_request_time = 0.0

                while queue_position < len(queue):
                    if cancel_event.is_set():
                        raise _Cancelled()

                    batch = queue[queue_position]
                    queue_position += 1
                    if not batch:
                        continue

                    # Shrink the batch dynamically when the URL would be too long.
                    if (
                        len(batch) > 1
                        and _estimate_request_length(
                            base_url,
                            str(payload["feed"].get("site_id")),
                            str(payload["feed"].get("channel_id")),
                            batch,
                            id_column,
                        )
                        > MAX_URL_LENGTH
                    ):
                        middle = len(batch) // 2
                        queue.insert(queue_position, batch[middle:])
                        queue.insert(queue_position, batch[:middle])
                        continue

                    # Serial requests with a minimum 1s interval.
                    interval = float(
                        api_cfg.get("batch_interval_seconds", 1.0)
                    )
                    elapsed_since = time.time() - last_request_time
                    if last_request_time and elapsed_since < interval:
                        if cancel_event.wait(interval - elapsed_since):
                            raise _Cancelled()

                    batch_index += 1
                    try:
                        outcome = _query_batch(
                            api_session,
                            base_url,
                            token,
                            str(payload["feed"].get("site_id")),
                            str(payload["feed"].get("channel_id")),
                            batch,
                            id_column,
                            api_cfg,
                            cancel_event,
                        )
                    except TokenError:
                        raise
                    last_request_time = time.time()

                    covered.update(batch)
                    api_log_rows.append(
                        {
                            "timestamp": datetime.now().strftime(
                                "%Y-%m-%d %H:%M:%S"
                            ),
                            "country": country,
                            "batch_index": batch_index,
                            "sku_count": len(batch),
                            "request_url": outcome["request_url"],
                            "status_code": outcome["status_code"],
                            "response_count": len(outcome["products"]),
                            "elapsed_seconds": round(outcome["elapsed"], 3),
                            "error_message": outcome["error"],
                        }
                    )

                    if outcome["ok"]:
                        for product in outcome["products"]:
                            sku = str(product.get(id_column) or "").strip()
                            if sku:
                                returned[sku] = product
                    else:
                        failed_batch_count += 1
                        failed_skus.update(batch)
                        failed_rows.append(
                            {
                                "country": country,
                                "batch_index": batch_index,
                                "sku_list": ",".join(batch),
                                "error_message": outcome["error"],
                                "retry_count": outcome["retry_count"],
                            }
                        )
                        reporter.log(
                            "ERROR",
                            f"[{country}] batch {batch_index} failed: {outcome['error']}",
                        )

                    reporter.progress(
                        60
                        + int(
                            30
                            * (
                                position
                                + (queue_position / max(len(queue), 1))
                            )
                            / total_payloads
                        ),
                        f"{country} - batch {batch_index}",
                    )

                # -- classification -------------------------------------------
                # Failed-batch SKUs are excluded from every other count and
                # only appear on the Failed Batches sheet (per spec 6.3.1).
                skipped_count = 0
                in_channel_count = 0
                not_in_channel_count = 0
                unchecked_count = 0

                for sku in sampled_skus:
                    normalized = sku.strip().lower()
                    if sku in failed_skus:
                        continue
                    if sku in returned:
                        product = returned[sku]
                        export_raw = str(
                            product.get("___skipped_export") or ""
                        ).strip()
                        intermediate_raw = str(
                            product.get("___skipped_intermediate") or ""
                        ).strip()
                        status = (
                            "skipped"
                            if (export_raw or intermediate_raw)
                            else "in_channel"
                        )
                        skip_raw = f"export={export_raw} | intermediate={intermediate_raw}"
                        readable, priority = format_skip_reason(
                            export_raw, intermediate_raw
                        )
                        if status == "skipped":
                            skipped_count += 1
                            reason_counter[skip_raw] += 1
                        else:
                            in_channel_count += 1
                    elif sku in covered:
                        status = "not_in_channel"
                        skip_raw = ""
                        readable = ""
                        priority = ""
                        not_in_channel_count += 1
                    else:
                        status = "unchecked"
                        skip_raw = ""
                        readable = ""
                        priority = ""
                        unchecked_count += 1

                    detail_rows.append(
                        {
                            "country": country,
                            "sku": sku,
                            "normalized_sku": normalized,
                            "status": status,
                            "skip_reason_raw": skip_raw,
                            "skip_reason_readable": readable,
                            "priority": priority,
                        }
                    )

                failed_sku_count = len(failed_skus)

                # Defensive invariant check.
                total_classified = (
                    skipped_count
                    + in_channel_count
                    + not_in_channel_count
                    + failed_sku_count
                    + unchecked_count
                )
                if total_classified != sampled_count:
                    reporter.log(
                        "WARNING",
                        f"[{country}] classification mismatch: sampled={sampled_count}, "
                        f"classified={total_classified}.",
                    )

                denominator = sampled_count if sampled_count else 0
                top_reasons = reason_counter.most_common(2)
                top_1, top_1_count = (
                    top_reasons[0] if len(top_reasons) > 0 else ("", 0)
                )
                top_2, top_2_count = (
                    top_reasons[1] if len(top_reasons) > 1 else ("", 0)
                )

                for reason, count in reason_counter.items():
                    reason_rows.append(
                        {
                            "country": country,
                            "reason": reason,
                            "count": count,
                            "percentage": (
                                round(100.0 * count / denominator, 2)
                                if denominator
                                else 0.0
                            ),
                        }
                    )

                summary_rows.append(
                    {
                        "country": country,
                        "gos_count": gos_count,
                        "feed_count": feed_count,
                        "missing_count": missing_count,
                        "sampled_count": sampled_count,
                        "skipped_count": skipped_count,
                        "in_channel_count": in_channel_count,
                        "not_in_channel_count": not_in_channel_count,
                        "failed_batch_count": failed_batch_count,
                        "failed_sku_count": failed_sku_count,
                        "unchecked_count": unchecked_count,
                        "top_reason_1": top_1,
                        "top_reason_1_pct": (
                            round(100.0 * top_1_count / denominator, 2)
                            if denominator
                            else 0.0
                        ),
                        "top_reason_2": top_2,
                        "top_reason_2_pct": (
                            round(100.0 * top_2_count / denominator, 2)
                            if denominator
                            else 0.0
                        ),
                        "note": "; ".join(notes),
                    }
                )

        # -- 90-100%: write the Excel report ----------------------------------
        if cancel_event.is_set():
            raise _Cancelled()

        reporter.progress(90, "Writing")
        output_path = output_dir / (
            f"online_status_checker_{datetime.now():%Y%m%d_%H%M%S}.xlsx"
        )
        write_excel(
            output_path,
            summary_rows,
            detail_rows,
            reason_rows,
            api_log_rows,
            failed_rows,
        )
        reporter.progress(100, "Done")
        reporter.log("INFO", f"Report written to {output_path}")

        result["success"] = True
        result["output_path"] = str(output_path)
        result["summary"] = summary_rows

    except _Cancelled:
        result["cancelled"] = True
        result["error"] = "Cancelled by user"
        reporter.log("WARNING", "Cancelled by user.")
    except TokenError as exc:
        result["error"] = redact_token(str(exc))
        result["error_kind"] = "token"
        reporter.log("ERROR", result["error"])
    except ConfigError as exc:
        result["error"] = redact_token(str(exc))
        result["error_kind"] = "config"
        reporter.log("ERROR", result["error"])
    except GosError as exc:
        result["error"] = redact_token(str(exc))
        result["error_kind"] = "gos"
        reporter.log("ERROR", result["error"])
    except ExcelError as exc:
        result["error"] = redact_token(str(exc))
        result["error_kind"] = "excel"
        reporter.log("ERROR", f"Report writing failed: {result['error']}")
    except Exception as exc:  # noqa: BLE001 - never crash the caller
        result["error"] = redact_token(f"{type(exc).__name__}: {exc}")
        result["error_kind"] = "other"
        reporter.log("ERROR", result["error"])
        _LOGGER.exception("Unexpected error in run_comparison")

    return result


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="compare_exports.py", description=CLI_DESCRIPTION
    )
    parser.add_argument(
        "session",
        nargs="?",
        default=str(CONFIG_DIR / "last_session.json"),
        help="Path to the session configuration JSON file.",
    )
    args = parser.parse_args(argv)

    app_cfg = load_app_config()
    setup_logging(BASE_DIR / app_cfg["paths"]["logs_dir"])

    try:
        session_config = load_json(Path(args.session))
        credentials = load_credentials()
    except ConfigError as exc:
        _LOGGER.error(redact_token(str(exc)))
        return EXIT_CONFIG_ERROR
    except Exception as exc:  # noqa: BLE001
        _LOGGER.error(
            "Failed to load configuration: %s", redact_token(str(exc))
        )
        return EXIT_CONFIG_ERROR

    cancel_event = threading.Event()

    def _progress(percent: int, message: str) -> None:
        _LOGGER.info("[%3d%%] %s", percent, message)

    try:
        result = run_comparison(
            session_config,
            credentials,
            progress_callback=_progress,
            log_callback=None,
            cancel_event=cancel_event,
        )
    except KeyboardInterrupt:
        cancel_event.set()
        _LOGGER.warning("Interrupted by user.")
        return EXIT_CANCELLED

    if result.get("cancelled"):
        return EXIT_CANCELLED

    if not result.get("success"):
        kind = result.get("error_kind")
        if kind == "config":
            return EXIT_CONFIG_ERROR
        if kind in ("gos", "token"):
            return EXIT_GOS_ERROR
        if kind == "excel":
            return EXIT_EXCEL_ERROR
        return EXIT_UNKNOWN

    if result.get("feed_failures"):
        _LOGGER.error(
            "%d comparison feed(s) failed to download.",
            result["feed_failures"],
        )
        return EXIT_FEED_ERROR

    return EXIT_SUCCESS


if __name__ == "__main__":
    sys.exit(main())
