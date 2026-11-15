"""Streaming downloads with retries, cancellation and progress logging.

Every file is written to ``<name>.part`` and renamed on success, so a killed
run never leaves a truncated CSV that later looks like a valid feed with
missing products.

Progress is logged at most every two seconds per file: with five concurrent
downloads and 64 KB chunks, logging every chunk would produce thousands of
lines a second and drown the UI.
"""
import time
from pathlib import Path
from typing import Optional, Tuple

import requests

from .errors import Cancelled
from .progress import Reporter
from .redact import redact_token
from .settings import DOWNLOAD_RETRY_BACKOFF, Settings

_REPORT_INTERVAL = 2.0


def _safe_unlink(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


def format_bytes(count: float) -> str:
    """Human-readable size, e.g. 12.4 MB."""
    for unit in ("B", "KB", "MB", "GB"):
        if abs(count) < 1024.0 or unit == "GB":
            return f"{count:.1f} {unit}" if unit != "B" else f"{int(count)} B"
        count /= 1024.0
    return f"{count:.1f} GB"


def download_file(url: str, dest: Path, settings: Settings, cancel_event,
                  reporter: Reporter, label: str = "") -> Tuple[bool, str, Optional[str]]:
    """Download ``url`` into ``dest``.

    Returns ``(ok, error_message, content_type)``. ``error_message`` is
    "cancelled" when the run was stopped, so callers can tell a real failure
    from an abort.
    """
    attempts = settings.download_retries
    timeouts = settings.download_timeouts
    chunk_size = settings.download_chunk_bytes

    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    part_path = dest.with_suffix(dest.suffix + ".part")
    last_error = ""

    for attempt in range(attempts):
        if cancel_event.is_set():
            _safe_unlink(part_path)
            return False, "cancelled", None

        session = requests.Session()
        try:
            with session.get(url, stream=True, timeout=timeouts) as response:
                if response.status_code in (401, 403):
                    _safe_unlink(part_path)
                    return False, f"HTTP {response.status_code} (access denied)", None
                response.raise_for_status()

                content_type = response.headers.get("Content-Type") or ""
                expected = _content_length(response)
                written = 0
                last_report = time.monotonic()
                last_bytes = 0

                with part_path.open("wb") as handle:
                    for chunk in response.iter_content(chunk_size=chunk_size):
                        if cancel_event.is_set():
                            raise Cancelled()
                        if not chunk:
                            continue
                        handle.write(chunk)
                        written += len(chunk)

                        now = time.monotonic()
                        elapsed = now - last_report
                        if elapsed >= _REPORT_INTERVAL:
                            speed = (written - last_bytes) / elapsed
                            reporter.log("INFO", _progress_line(
                                label, written, expected, speed))
                            last_report = now
                            last_bytes = written

            if written == 0:
                raise IOError("downloaded file is empty")
            if expected > 0 and expected != written:
                raise IOError(
                    f"size mismatch (expected {expected}, got {written})"
                )

            part_path.replace(dest)
            reporter.log("INFO", f"{label}Downloaded {dest.name} "
                                 f"({format_bytes(written)})")
            return True, "", content_type

        except Cancelled:
            _safe_unlink(part_path)
            return False, "cancelled", None
        except Exception as exc:  # noqa: BLE001 - one file must not kill the run
            _safe_unlink(part_path)
            last_error = f"{type(exc).__name__}: {exc}"
            if attempt < attempts - 1:
                wait = DOWNLOAD_RETRY_BACKOFF[min(attempt, len(DOWNLOAD_RETRY_BACKOFF) - 1)]
                reporter.log(
                    "WARNING",
                    f"{label}Attempt {attempt + 1}/{attempts} failed for {url}: "
                    f"{redact_token(last_error)}. Retrying in {wait:.0f}s.",
                )
                if cancel_event.wait(wait):
                    return False, "cancelled", None
        finally:
            session.close()

    return False, last_error, None


def _content_length(response) -> int:
    raw = response.headers.get("Content-Length")
    if raw is None:
        return 0
    try:
        return int(raw)
    except (TypeError, ValueError):
        return 0


def _progress_line(label: str, written: int, expected: int, speed: float) -> str:
    if expected > 0:
        percent = 100.0 * written / expected
        return (f"{label}{format_bytes(written)} / {format_bytes(expected)} "
                f"({percent:.0f}%) @ {format_bytes(speed)}/s")
    return f"{label}{format_bytes(written)} @ {format_bytes(speed)}/s"
