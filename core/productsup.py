"""Productsup platform API client.

Two very different surfaces live here:

  * the platform endpoints (projects → sites → channels), used by the Browse
    tab to discover feeds;
  * the product stage endpoint, used by the comparison to ask "is this SKU in
    the channel, and if not, why".

Both go through one ``Client`` so the token, base URL and timeouts are applied
in exactly one place.
"""
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

import requests

from .errors import Cancelled, TokenError
from .redact import redact_token
from .settings import API_RETRY_BACKOFF, DEFAULT_BASE_URL, Settings

# Fields the stage endpoint must return for the "why was it skipped" report.
SKIP_FIELDS = ["___skipped_intermediate", "___skipped_export"]
MAX_URL_LENGTH = 8000
_METADATA_TIMEOUT = (5, 30)


@dataclass
class BatchResult:
    ok: bool
    products: List[Dict[str, Any]] = field(default_factory=list)
    status_code: Optional[int] = None
    request_url: str = ""
    error: str = ""
    retry_count: int = 0
    elapsed: float = 0.0


class StageClient:
    """What the runner needs from a stage API.

    Kept deliberately small: if a second provider ever has to be supported,
    it implements ``query_batch`` and gets its own factory entry, and the
    runner does not change.
    """

    def query_batch(self, site_id: str, channel_id: str, skus: Sequence[str],
                    id_column: str, cancel_event) -> BatchResult:
        raise NotImplementedError

    def close(self) -> None:
        pass

    def __enter__(self):
        return self

    def __exit__(self, *_exc) -> None:
        self.close()


class Client(StageClient):
    """Thin wrapper over the two Productsup APIs."""

    def __init__(self, token: str, base_url: str = DEFAULT_BASE_URL,
                 settings: Optional[Settings] = None):
        self.token = token
        self.base_url = (base_url or DEFAULT_BASE_URL).rstrip("/")
        self.settings = settings
        self._session = requests.Session()
        self._session.headers.update({"X-Auth-Token": token})

    def close(self) -> None:
        self._session.close()

    def __enter__(self) -> "Client":
        return self

    def __exit__(self, *_exc) -> None:
        self.close()

    # -- platform metadata ------------------------------------------------

    def projects(self) -> List[Dict[str, Any]]:
        return self._get_json("/platform/v2/projects", "Projects")

    def sites(self, project_id: str) -> List[Dict[str, Any]]:
        return self._get_json(f"/platform/v2/projects/{project_id}/sites", "Sites")

    def channels(self, site_id: str) -> List[Dict[str, Any]]:
        return self._get_json(f"/platform/v2/sites/{site_id}/channels", "Channels")

    def _get_json(self, path: str, key: str) -> List[Dict[str, Any]]:
        url = f"{self.base_url}{path}"
        try:
            response = self._session.get(url, timeout=_METADATA_TIMEOUT)
        except requests.RequestException as exc:
            raise ConnectionError(f"{type(exc).__name__}: {exc}") from exc

        if response.status_code in (401, 403):
            raise TokenError("Token is invalid or lacks permission.")
        if response.status_code >= 400:
            raise RuntimeError(f"HTTP {response.status_code} for {path}")
        try:
            payload = response.json()
        except ValueError as exc:
            raise RuntimeError(f"Invalid JSON from {path}: {exc}") from exc

        # The platform answers {"success": true, "Sites": [...]} — a request
        # that fails with 200 still carries success=false, and returning []
        # for it made a broken call look like an empty project.
        if payload.get("success") is False:
            detail = str(payload.get("message") or payload.get("error") or "").strip()
            raise RuntimeError(
                f"API reported failure for {path}" + (f": {detail}" if detail else "")
            )
        return payload.get(key) or []

    # -- product stage ----------------------------------------------------

    def query_batch(self, site_id: str, channel_id: str, skus: Sequence[str],
                    id_column: str, cancel_event) -> BatchResult:
        """Ask the channel stage about one batch of SKUs (serial, with retries).

        Retries cover timeouts, 429 and 5xx; 4xx are returned as failures
        without retrying because repeating them cannot help.
        """
        settings = self.settings
        url = f"{self.base_url}/product/v2/site/{site_id}/stage/channel/{channel_id}"
        requested = [id_column, *SKIP_FIELDS]
        params: List[Tuple[str, Any]] = [("filter", build_sku_filter(skus, id_column))]
        for index, name in enumerate(requested):
            params.append((f"fields[{index}]", name))
        params.append(("hidden", "1"))
        params.append(("limit", "1000"))

        max_retries = int(settings.tune("api_max_retries")) if settings else 2
        backoffs = API_RETRY_BACKOFF
        timeout = settings.api_timeouts if settings else (10, 60)

        attempt = 0
        last_error = ""
        last_status: Optional[int] = None
        last_url = redact_token(url)

        while True:
            if cancel_event.is_set():
                raise Cancelled()

            try:
                prepared = self._session.prepare_request(
                    requests.Request("GET", url, params=params))
            except Exception as exc:  # noqa: BLE001
                return BatchResult(False, [], None, last_url,
                                   redact_token(f"{type(exc).__name__}: {exc}"),
                                   attempt, 0.0)

            last_url = redact_token(prepared.url)
            started = time.time()

            try:
                response = self._session.send(prepared, timeout=timeout)
            except (requests.Timeout, requests.ConnectionError) as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                last_status = None
                if attempt >= max_retries:
                    return BatchResult(False, [], last_status, last_url,
                                       redact_token(last_error), attempt,
                                       time.time() - started)
                wait = backoffs[min(attempt, len(backoffs) - 1)]
                attempt += 1
                if cancel_event.wait(wait):
                    raise Cancelled()
                continue

            elapsed = time.time() - started
            status = response.status_code

            if status in (401, 403):
                raise TokenError("Token is invalid or lacks permission.")

            if status == 429 or status >= 500:
                last_status = status
                last_error = ("HTTP 429 Too Many Requests" if status == 429
                              else f"HTTP {status}")
                if attempt >= max_retries:
                    return BatchResult(False, [], status, last_url, last_error,
                                       attempt, elapsed)
                wait = backoffs[min(attempt, len(backoffs) - 1)]
                if status == 429:
                    wait = _retry_after(response, wait)
                attempt += 1
                if cancel_event.wait(wait):
                    raise Cancelled()
                continue

            if status >= 400:
                return BatchResult(
                    False, [], status, last_url,
                    redact_token(f"HTTP {status}: {response.text[:200]}"),
                    attempt, elapsed,
                )

            try:
                payload = response.json()
            except ValueError as exc:
                return BatchResult(False, [], status, last_url,
                                   redact_token(f"Invalid JSON response: {exc}"),
                                   attempt, elapsed)

            return BatchResult(True, payload.get("products") or [], status,
                               last_url, "", attempt, elapsed)


def build_sku_filter(skus: Sequence[str], id_column: str) -> str:
    """SQL-ish filter with single quotes escaped."""
    escaped = ["'" + str(sku).replace("'", "''") + "'" for sku in skus]
    return f"{id_column} IN (" + ",".join(escaped) + ")"


def estimate_request_length(base_url: str, site_id: str, channel_id: str,
                            skus: Sequence[str], id_column: str) -> int:
    """Upper bound on the prepared URL, used to split oversized batches."""
    path = f"/product/v2/site/{site_id}/stage/channel/{channel_id}"
    return (len(base_url) + len(path)
            + len(build_sku_filter(skus, id_column)) + 200)


def extract_feed_url(channel: Dict[str, Any]) -> Optional[str]:
    """First URL of the first active feed destination."""
    for dest in (channel.get("feed_destinations") or {}).values():
        if dest.get("active") and dest.get("urls"):
            return str(dest["urls"][0])
    return None


def channel_is_usable(channel: Dict[str, Any]) -> bool:
    return bool(channel.get("active")) and extract_feed_url(channel) is not None


def detect_url_format(url: str) -> str:
    path = url.split("?")[0].lower()
    return "xml" if path.endswith(".xml") else "csv"


def _retry_after(response, default: float) -> float:
    raw = response.headers.get("Retry-After")
    if raw is None:
        return default
    try:
        return max(float(raw), 0.0)
    except (TypeError, ValueError):
        return default


def make_client(credentials: Dict[str, str], settings: Optional[Settings] = None) -> StageClient:
    """Build the stage client for the configured provider.

    One entry point so the runner never constructs a concrete client itself.
    """
    provider = str(credentials.get("provider") or "productsup").lower()
    if provider != "productsup":
        raise ValueError(f"Unsupported provider: {provider}")
    return Client(
        credentials.get("token", ""),
        credentials.get("base_url", DEFAULT_BASE_URL),
        settings,
    )
