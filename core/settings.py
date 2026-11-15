"""Configuration: three small files, plus one secret.

    config/credentials.json   {"token": "..."}                     (never commit)
    config/settings.json      four keys you might actually change
    config/countries.json     country -> GOS column names
    config/selection.json     written by the app: GOS + feeds + ticked subset

The previous layout nested timeouts, retry counts, backoff arrays, chunk sizes
and internal cache directories under an ``app_config.json`` nobody edited, and
kept the selection inside a 569-line session file that repeated every site and
channel name once per feed. Those tuning values are constants here now; if one
ever needs overriding, add an ``advanced`` object to settings.json with any of
the keys in ``ADVANCED_DEFAULTS``.

Everything is read through this module so the rest of the code never touches
a path or a JSON key directly.
"""
import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .errors import ConfigError
from .models import Feed, GosFeed, Selection

BASE_DIR = Path(__file__).resolve().parent.parent
CONFIG_DIR = BASE_DIR / "config"

CREDENTIALS_FILE = CONFIG_DIR / "credentials.json"
SETTINGS_FILE = CONFIG_DIR / "settings.json"
COUNTRIES_FILE = CONFIG_DIR / "countries.json"
SELECTION_FILE = CONFIG_DIR / "selection.json"

# Read-only fallback for users upgrading from the single-file version.
LEGACY_SESSION_FILE = CONFIG_DIR / "last_session.json"

CACHE_DIR = BASE_DIR / ".cache"
LOGS_DIR = BASE_DIR / "logs"

DEFAULT_BASE_URL = "https://platform-api.productsup.io"
SELECTION_VERSION = 2

# Tuning constants. Deliberately not configurable from the file: every value
# here is a "you would only make it worse" knob.
API_TIMEOUT = (10, 60)
API_BATCH_SIZE = 200
API_BATCH_INTERVAL = 1.0
API_MAX_RETRIES = 2
API_RETRY_BACKOFF = (2.0, 4.0)

DOWNLOAD_TIMEOUT = (10, 60)
DOWNLOAD_MAX_RETRIES = 3
DOWNLOAD_RETRY_BACKOFF = (2.0, 4.0, 8.0)
DOWNLOAD_CHUNK_BYTES = 65536

ADVANCED_DEFAULTS: Dict[str, Any] = {
    "api_batch_size": API_BATCH_SIZE,
    "api_batch_interval": API_BATCH_INTERVAL,
    "api_max_retries": API_MAX_RETRIES,
    "api_timeout_connect": API_TIMEOUT[0],
    "api_timeout_read": API_TIMEOUT[1],
    "download_max_retries": DOWNLOAD_MAX_RETRIES,
    "download_timeout_connect": DOWNLOAD_TIMEOUT[0],
    "download_timeout_read": DOWNLOAD_TIMEOUT[1],
    "download_chunk_bytes": DOWNLOAD_CHUNK_BYTES,
}

DEFAULT_SETTINGS: Dict[str, Any] = {
    "output_dir": "output",
    "max_per_country": 1000,
    "random_seed": 42,
    "download_concurrency": 5,
}


@dataclass
class Settings:
    """The four settings a user may change, plus optional overrides."""
    output_dir: Path
    max_per_country: int = 1000
    random_seed: int = 42
    download_concurrency: int = 5
    advanced: Dict[str, Any] = field(default_factory=dict)

    # -- derived tuning values -------------------------------------------

    def tune(self, key: str):
        return self.advanced.get(key, ADVANCED_DEFAULTS[key])

    @property
    def api_batch_size(self) -> int:
        return max(1, int(self.tune("api_batch_size")))

    @property
    def api_interval(self) -> float:
        return max(0.0, float(self.tune("api_batch_interval")))

    @property
    def api_timeouts(self) -> Tuple[int, int]:
        return (int(self.tune("api_timeout_connect")),
                int(self.tune("api_timeout_read")))

    @property
    def download_timeouts(self) -> Tuple[int, int]:
        return (int(self.tune("download_timeout_connect")),
                int(self.tune("download_timeout_read")))

    @property
    def download_retries(self) -> int:
        return max(0, int(self.tune("download_max_retries")))

    @property
    def download_chunk_bytes(self) -> int:
        return max(1024, int(self.tune("download_chunk_bytes")))


# ---------------------------------------------------------------------------
# File helpers
# ---------------------------------------------------------------------------

def _read_json(path: Path) -> Optional[dict]:
    """Return the parsed object, or None when the file is missing/unreadable."""
    if not path.exists():
        return None
    try:
        with path.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _write_json(path: Path, data: dict) -> None:
    """Write atomically so a crash cannot leave a half-written config."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(data, handle, indent=2, ensure_ascii=False)
        handle.write("\n")
    os.replace(temporary, path)


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

def load_settings(create: bool = True) -> Settings:
    """Read settings.json, writing the defaults out on first run."""
    data = _read_json(SETTINGS_FILE)
    if data is None:
        data = dict(DEFAULT_SETTINGS)
        if create:
            _write_json(SETTINGS_FILE, data)

    output_name = str(data.get("output_dir") or DEFAULT_SETTINGS["output_dir"])
    output_dir = Path(output_name)
    if not output_dir.is_absolute():
        output_dir = BASE_DIR / output_dir

    advanced = data.get("advanced")
    return Settings(
        output_dir=output_dir,
        max_per_country=int(data.get("max_per_country", 1000)),
        random_seed=int(data.get("random_seed", 42)),
        download_concurrency=max(1, int(data.get("download_concurrency", 5))),
        advanced=dict(advanced) if isinstance(advanced, dict) else {},
    )


def save_settings(settings: Settings) -> None:
    data = dict(DEFAULT_SETTINGS)
    try:
        data["output_dir"] = str(settings.output_dir.relative_to(BASE_DIR))
    except ValueError:
        data["output_dir"] = str(settings.output_dir)
    data["max_per_country"] = settings.max_per_country
    data["random_seed"] = settings.random_seed
    data["download_concurrency"] = settings.download_concurrency
    if settings.advanced:
        data["advanced"] = settings.advanced
    _write_json(SETTINGS_FILE, data)


# ---------------------------------------------------------------------------
# Credentials
# ---------------------------------------------------------------------------

def load_credentials() -> Dict[str, str]:
    """Return {'token': ..., 'base_url': ...}; token may be empty."""
    data = _read_json(CREDENTIALS_FILE) or {}
    productsup = data.get("productsup")
    if isinstance(productsup, dict):
        data = productsup
    return {
        "token": str(data.get("token") or "").strip(),
        "base_url": (str(data.get("base_url") or DEFAULT_BASE_URL).strip()
                     or DEFAULT_BASE_URL),
    }


def save_credentials(token: str, base_url: str = DEFAULT_BASE_URL) -> None:
    _write_json(CREDENTIALS_FILE, {"token": token.strip(), "base_url": base_url})


# ---------------------------------------------------------------------------
# Country -> GOS column table
# ---------------------------------------------------------------------------

def load_countries() -> Tuple[Dict[str, str], Dict[str, Dict[str, str]]]:
    """Return (aliases, {country: {online, stock, price}})."""
    data = _read_json(COUNTRIES_FILE) or {}
    aliases = data.get("_aliases") or {}
    table = {
        str(country): {str(k): str(v) for k, v in columns.items()}
        for country, columns in data.items()
        if not str(country).startswith("_") and isinstance(columns, dict)
    }
    return ({str(k): str(v) for k, v in aliases.items()}, table)


def country_columns(country: str,
                    table: Dict[str, Dict[str, str]]) -> Dict[str, str]:
    """GOS columns for a country code, tolerating case and separators."""
    if not country:
        return {}
    key = country.strip().lower().replace("_", "-")
    return dict(table.get(key) or {})


# ---------------------------------------------------------------------------
# Selection (the saved state)
# ---------------------------------------------------------------------------

def load_selection() -> Selection:
    """Read selection.json, migrating the old last_session.json on first run."""
    data = _read_json(SELECTION_FILE)
    if data is None:
        migrated = _migrate_legacy_session()
        if migrated is not None:
            save_selection(migrated)
            return migrated
        return Selection()

    gos_data = data.get("gos") or {}
    gos = GosFeed.from_dict(gos_data) if gos_data.get("url") else None

    feeds: List[Feed] = []
    seen = set()
    for entry in data.get("feeds") or []:
        feed = Feed.from_dict(entry)
        if not feed.key or feed.key in seen:
            continue
        seen.add(feed.key)
        feeds.append(feed)

    selected = [str(key) for key in (data.get("selected") or [])
                if str(key) in seen]
    return Selection(gos=gos, feeds=feeds, selected=selected)


def save_selection(selection: Selection) -> None:
    _write_json(SELECTION_FILE, {
        "version": SELECTION_VERSION,
        "gos": selection.gos.to_dict() if selection.gos else {},
        "feeds": [feed.to_dict() for feed in selection.feeds],
        "selected": list(selection.selected),
    })


def _migrate_legacy_session() -> Optional[Selection]:
    """Convert the single-file version's session into the new shape.

    The old file only stored the feeds that were selected for that run, so the
    migration keeps all of them as both "configured" and "ticked" — that is
    exactly what the user saw last time.
    """
    legacy = _read_json(LEGACY_SESSION_FILE)
    if legacy is None:
        return None

    gos_data = legacy.get("gos") or {}
    gos = GosFeed.from_dict(gos_data) if gos_data.get("url") else None

    feeds: List[Feed] = []
    seen = set()
    for entry in legacy.get("feeds") or []:
        feed = Feed.from_dict(entry)
        if not feed.key or feed.key in seen:
            continue
        seen.add(feed.key)
        feeds.append(feed)

    return Selection(
        gos=gos,
        feeds=feeds,
        selected=[feed.key for feed in feeds],
    )


def ensure_dirs(settings: Settings) -> None:
    """Create the directories a run writes into."""
    settings.output_dir.mkdir(parents=True, exist_ok=True)
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    LOGS_DIR.mkdir(parents=True, exist_ok=True)


def require_token(credentials: Dict[str, str]) -> str:
    token = (credentials.get("token") or "").strip()
    if not token:
        raise ConfigError(
            "No Productsup token. Put it in config/credentials.json as "
            '{"token": "..."}.'
        )
    return token
