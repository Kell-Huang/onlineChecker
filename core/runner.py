"""The run: download, compare, ask the API, write the report.

This is the only place that knows the order of operations. Everything it calls
is a pure function or a small client, which keeps the orchestration readable
and lets each step be tested on its own.
"""
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple
from urllib.parse import urlparse

from . import compare, feeds as feeds_module
from .download import download_file
from .errors import (Cancelled, ConfigError, ExcelError, GosError, TokenError)
from .models import Feed, Selection
from .productsup import estimate_request_length, make_client
from .progress import (PHASE_API, PHASE_COMPARE, PHASE_DOWNLOAD, PHASE_GOS,
                       PHASE_REPORT, Reporter)
from .redact import redact_token
from .report import SUMMARY_COLUMNS, write_report
from .settings import CACHE_DIR, Settings, ensure_dirs, load_countries, require_token

MAX_URL_LENGTH = 8000


@dataclass
class RunResult:
    success: bool = False
    cancelled: bool = False
    output_path: Optional[Path] = None
    error: str = ""
    error_kind: str = ""          # config | gos | token | excel | other
    summary: List[Dict[str, Any]] = field(default_factory=list)
    feed_failures: int = 0
    elapsed: float = 0.0


def run(selection: Selection, settings: Settings,
        credentials: Dict[str, str], reporter: Reporter,
        cancel_event) -> RunResult:
    """Execute one comparison run end to end."""
    started = time.time()
    result = RunResult()

    try:
        _run(selection, settings, credentials, reporter, cancel_event, result)
        result.success = True
    except Cancelled:
        result.cancelled = True
        reporter.log("WARNING", "Run cancelled.")
    except ConfigError as exc:
        result.error, result.error_kind = str(exc), "config"
    except TokenError as exc:
        result.error, result.error_kind = str(exc), "token"
    except GosError as exc:
        result.error, result.error_kind = str(exc), "gos"
    except ExcelError as exc:
        result.error, result.error_kind = str(exc), "excel"
    except Exception as exc:  # noqa: BLE001 - surface, never crash the app
        result.error = redact_token(f"{type(exc).__name__}: {exc}")
        result.error_kind = "other"
        reporter.log("ERROR", result.error)

    result.elapsed = time.time() - started
    if not result.success and not result.cancelled and result.error:
        reporter.log("ERROR", f"Run failed ({result.error_kind}): {result.error}")
    return result


def _run(selection: Selection, settings: Settings,
         credentials: Dict[str, str], reporter: Reporter,
         cancel_event, result: RunResult) -> None:
    gos = selection.gos
    feeds = selection.selected_feeds()
    if gos is None or not gos.url:
        raise ConfigError("No GOS channel configured. Set one in the Browse tab.")
    if not feeds:
        raise ConfigError("No comparison feed selected.")

    token = require_token(credentials)
    ensure_dirs(settings)
    # Only the download cache is wiped: it is regenerated every run. The output
    # directory is not touched, because deleting the previous report meant a
    # run that failed halfway left the user with nothing at all.
    _clean_dir(CACHE_DIR)

    aliases, country_table = load_countries()
    gos_path = CACHE_DIR / f"gos_{gos.channel_id or 'file'}.csv"

    # -- 1. GOS ---------------------------------------------------------
    reporter.progress(PHASE_GOS, 0.05, "GOS", "downloading", force=True)
    reporter.log("INFO", f"[GOS] Downloading {gos.channel_name or gos.channel_id}")
    ok, error, _content_type = download_file(
        gos.url, gos_path, settings, cancel_event, reporter, label="[GOS] ")
    if cancel_event.is_set():
        raise Cancelled()
    if not ok:
        raise GosError(f"GOS download failed: {error}")

    header, _encoding, delimiter = feeds_module.read_csv_header(gos_path)
    header_lookup = {column.strip(): column for column in header}
    if "id" not in header_lookup:
        raise GosError("The GOS file has no 'id' column.")

    needed: List[str] = ["id"]
    for feed in feeds:
        mapping = compare.gos_columns_for(
            feed.gos_columns, compare.country_columns_for(feed.country, country_table))
        for key in ("online", "stock", "price"):
            column = mapping.get(key)
            if column and column in header_lookup and column not in needed:
                needed.append(column)

    gos_df = compare.load_gos(gos_path, needed, delimiter)
    reporter.log("INFO", f"[GOS] {gos_df.height} unique rows.")
    reporter.progress(PHASE_GOS, 1.0, "GOS", f"{gos_df.height} rows", force=True)

    # -- 2. feeds -------------------------------------------------------
    paths, content_types, failed = _download_feeds(
        feeds, settings, cancel_event, reporter)
    result.feed_failures = len(failed)

    feed_ids: Dict[int, Optional[set]] = {}
    for index, feed in enumerate(feeds):
        if index not in paths:
            continue
        feed_format = feeds_module.detect_format(
            feed.url, feed.format, content_types.get(index))
        try:
            raw_ids, detected = feeds_module.read_feed_ids(paths[index], feed_format)
        except Exception as exc:  # noqa: BLE001
            reporter.log("ERROR", f"[{feed.country}] could not read ids: "
                                  f"{redact_token(str(exc))}")
            raw_ids, detected = [], None
        if detected is None:
            reporter.log("ERROR", f"[{feed.country}] no ID column found in "
                                  f"{paths[index].name}")
            feed_ids[index] = None
        else:
            feed_ids[index] = feeds_module.normalized_set(raw_ids)
            feed.id_column = feed.id_column or detected
            reporter.log("INFO", f"[{feed.country}] id column '{detected}', "
                                 f"{len(feed_ids[index])} unique ids.")

    # -- 3. per-country compare ----------------------------------------
    payloads: List[Dict[str, Any]] = []
    summary_rows: List[Dict[str, Any]] = []

    for index, feed in enumerate(feeds):
        if cancel_event.is_set():
            raise Cancelled()
        reporter.progress(PHASE_COMPARE, index / max(len(feeds), 1),
                          feed.country, f"{index + 1} / {len(feeds)}")

        if index not in paths:
            summary_rows.append(_blank_row(feed.country, "feed download failed"))
            continue
        if feed_ids.get(index) is None:
            summary_rows.append(_blank_row(feed.country, "ID column not detected"))
            continue

        mapping = compare.gos_columns_for(
            feed.gos_columns, compare.country_columns_for(feed.country, country_table))
        missing_keys = compare.invalid_columns(mapping, list(gos_df.columns))
        if missing_keys:
            reporter.log("ERROR", f"[{feed.country}] invalid GOS columns "
                                  f"{missing_keys} — country skipped.")
            summary_rows.append(_blank_row(feed.country, "invalid GOS column mapping"))
            continue

        online = compare.filter_online(
            gos_df, mapping["online"], mapping["stock"], mapping["price"],
            reporter, feed.country)
        missing_df = compare.missing_rows(online, feed_ids[index] or set())
        payloads.append({
            "feed": feed,
            "online_count": online.height,
            "feed_count": len(feed_ids[index] or set()),
            "missing_df": missing_df,
            "missing_count": missing_df.height,
        })

    reporter.progress(PHASE_COMPARE, 1.0, "", "compared", force=True)

    # -- 4. API ---------------------------------------------------------
    detail_rows, reason_rows, api_log_rows, failed_rows = _query_api(
        payloads, settings, credentials, cancel_event, reporter, summary_rows)

    # -- 5. report ------------------------------------------------------
    reporter.progress(PHASE_REPORT, 0.2, "Writing report", "", force=True)
    output_path = settings.output_dir / (
        f"online_status_{datetime.now():%Y%m%d_%H%M%S}.xlsx")
    write_report(output_path, {
        "Summary": summary_rows,
        "Sampled Details": detail_rows,
        "Skip Reason Distribution": reason_rows,
        "API Log": api_log_rows,
        "Failed Batches": failed_rows,
    })
    reporter.progress(PHASE_REPORT, 1.0, "Done", output_path.name, force=True)

    result.summary = summary_rows
    result.output_path = output_path
    reporter.log("INFO", f"Report written to {output_path}")


def _download_feeds(feeds: Sequence[Feed], settings: Settings, cancel_event,
                    reporter: Reporter) -> Tuple[Dict[int, Path], Dict[int, str],
                                                 List[int]]:
    """Download every selected feed concurrently."""
    paths: Dict[int, Path] = {}
    content_types: Dict[int, str] = {}
    failed: List[int] = []
    total = max(len(feeds), 1)
    completed = 0

    def fetch(index: int, feed: Feed):
        original = Path(urlparse(feed.url).path).name or f"feed_{feed.channel_id}.csv"
        destination = CACHE_DIR / f"{feed.country or 'xx'}_{feed.channel_id}_{original}"
        reporter.log("INFO", f"[{feed.country}] Downloading {feed.display_name}")
        ok, error, content_type = download_file(
            feed.url, destination, settings, cancel_event, reporter,
            label=f"[{feed.country}] ")
        return index, destination, ok, error, content_type

    with ThreadPoolExecutor(max_workers=settings.download_concurrency) as pool:
        futures = {pool.submit(fetch, index, feed): index
                   for index, feed in enumerate(feeds)}
        for future in as_completed(futures):
            index, destination, ok, error, content_type = future.result()
            completed += 1
            reporter.progress(PHASE_DOWNLOAD, completed / total,
                              f"{completed} / {total} files")
            if cancel_event.is_set():
                raise Cancelled()
            if ok:
                paths[index] = destination
                content_types[index] = content_type or ""
            else:
                failed.append(index)
                reporter.log("ERROR", f"[{feeds[index].country}] download failed: "
                                      f"{redact_token(error)}")
    return paths, content_types, failed


def _query_api(payloads: List[Dict[str, Any]], settings: Settings,
               credentials: Dict[str, str], cancel_event, reporter: Reporter,
               summary_rows: List[Dict[str, Any]]):
    """Sample, query the stage API, classify, and build the report rows."""
    detail_rows: List[Dict[str, Any]] = []
    reason_rows: List[Dict[str, Any]] = []
    api_log_rows: List[Dict[str, Any]] = []
    failed_rows: List[Dict[str, Any]] = []
    total = max(len(payloads), 1)

    with make_client(credentials, settings) as client:
        for position, payload in enumerate(payloads):
            if cancel_event.is_set():
                raise Cancelled()

            feed: Feed = payload["feed"]
            country = feed.country or f"feed_{position}"
            missing_df = payload["missing_df"]
            sampled = compare.sample_skus(
                missing_df, settings.max_per_country, settings.random_seed)

            returned: Dict[str, Dict[str, Any]] = {}
            covered: set = set()
            failed_skus: set = set()
            failed_batches = 0
            reason_counter: Counter = Counter()
            reason_text: Dict[str, str] = {}

            queue: List[List[str]] = [
                sampled[i:i + settings.api_batch_size]
                for i in range(0, len(sampled), settings.api_batch_size)
            ]
            batch_index = 0
            cursor = 0
            last_request_at = 0.0

            while cursor < len(queue):
                if cancel_event.is_set():
                    raise Cancelled()
                batch = queue[cursor]
                cursor += 1
                if not batch:
                    continue

                if (len(batch) > 1 and estimate_request_length(
                        credentials["base_url"], feed.site_id, feed.channel_id,
                        batch, feed.id_column or "id") > MAX_URL_LENGTH):
                    middle = len(batch) // 2
                    queue.insert(cursor, batch[middle:])
                    queue.insert(cursor, batch[:middle])
                    continue

                interval = settings.api_interval
                since = time.time() - last_request_at
                if last_request_at and since < interval:
                    if cancel_event.wait(interval - since):
                        raise Cancelled()

                batch_index += 1
                outcome = client.query_batch(
                    feed.site_id, feed.channel_id, batch,
                    feed.id_column or "id", cancel_event)
                last_request_at = time.time()

                covered.update(batch)
                api_log_rows.append({
                    "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    "country": country,
                    "batch_index": batch_index,
                    "sku_count": len(batch),
                    "request_url": outcome.request_url,
                    "status_code": outcome.status_code,
                    "response_count": len(outcome.products),
                    "elapsed_seconds": round(outcome.elapsed, 3),
                    "error_message": outcome.error,
                })

                if outcome.ok:
                    for product in outcome.products:
                        sku = str(product.get(feed.id_column or "id") or "").strip()
                        if sku:
                            returned[sku] = product
                else:
                    failed_batches += 1
                    failed_skus.update(batch)
                    failed_rows.append({
                        "country": country,
                        "batch_index": batch_index,
                        "sku_list": ",".join(batch),
                        "error_message": outcome.error,
                        "retry_count": outcome.retry_count,
                    })
                    reporter.log("ERROR", f"[{country}] batch {batch_index} "
                                          f"failed: {outcome.error}")

                reporter.progress(
                    PHASE_API,
                    (position + cursor / max(len(queue), 1)) / total,
                    country, f"batch {batch_index}")

            # -- classify ---------------------------------------------
            skipped = in_channel = not_in_channel = unchecked = 0
            for sku in sampled:
                normalized = sku.strip().lower()
                if sku in failed_skus:
                    continue
                if sku in returned:
                    product = returned[sku]
                    export_raw = str(product.get("___skipped_export") or "").strip()
                    intermediate_raw = str(
                        product.get("___skipped_intermediate") or "").strip()
                    is_skipped = bool(export_raw or intermediate_raw)
                    status = "skipped" if is_skipped else "in_channel"
                    raw = f"export={export_raw} | intermediate={intermediate_raw}"
                    readable, priority = format_skip_reason(export_raw, intermediate_raw)
                    if is_skipped:
                        skipped += 1
                        reason_counter[raw] += 1
                        reason_text.setdefault(raw, readable)
                    else:
                        in_channel += 1
                elif sku in covered:
                    status, raw, readable, priority = "not_in_channel", "", "", ""
                    not_in_channel += 1
                else:
                    status, raw, readable, priority = "unchecked", "", "", ""
                    unchecked += 1

                detail_rows.append({
                    "country": country, "sku": sku, "normalized_sku": normalized,
                    "status": status, "skip_reason_raw": raw,
                    "skip_reason_readable": readable, "priority": priority,
                })

            top = reason_counter.most_common(2)
            top_reason_1, pct_1 = _reason_pair(top, 0, skipped, reason_text)
            top_reason_2, pct_2 = _reason_pair(top, 1, skipped, reason_text)
            for raw, count in reason_counter.items():
                reason_rows.append({
                    "country": country,
                    "reason": reason_text.get(raw, raw),
                    "count": count,
                    "percentage": round(count / skipped * 100, 1) if skipped else 0.0,
                })

            summary_rows.append({
                "country": country,
                "gos_count": payload["online_count"],
                "feed_count": payload["feed_count"],
                "missing_count": payload["missing_count"],
                "sampled_count": len(sampled),
                "skipped_count": skipped,
                "in_channel_count": in_channel,
                "not_in_channel_count": not_in_channel,
                "failed_batch_count": failed_batches,
                "failed_sku_count": len(failed_skus),
                "unchecked_count": unchecked,
                "top_reason_1": top_reason_1,
                "top_reason_1_pct": pct_1,
                "top_reason_2": top_reason_2,
                "top_reason_2_pct": pct_2,
                "note": "" if payload["missing_count"] else "no missing SKUs",
            })

    return detail_rows, reason_rows, api_log_rows, failed_rows


def _reason_pair(top: Sequence[Tuple[str, int]], index: int, total: int,
                 texts: Dict[str, str]) -> Tuple[str, float]:
    """(readable reason, share of skipped) for the index-th most common."""
    if index >= len(top):
        return "", 0.0
    raw, count = top[index]
    return texts.get(raw, raw), round(count / total * 100, 1) if total else 0.0


def format_skip_reason(export_raw: str, intermediate_raw: str) -> Tuple[str, str]:
    """Turn ``priority::field::reason`` values into readable text + priority."""
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
    return "; ".join(parts), (str(min(priorities)) if priorities else "")


def _blank_row(country: str, note: str) -> Dict[str, Any]:
    """Summary row for a country that could not be compared."""
    row: Dict[str, Any] = {column: None for column in SUMMARY_COLUMNS}
    row["country"] = country
    row["note"] = note
    return row


def _clean_dir(path: Path) -> None:
    """Remove the previous run's files so stale downloads cannot be reused."""
    if not path.exists():
        return
    for item in path.iterdir():
        try:
            if item.is_dir():
                import shutil
                shutil.rmtree(item, ignore_errors=True)
            else:
                item.unlink()
        except OSError:
            pass
