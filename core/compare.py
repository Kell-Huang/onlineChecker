"""The comparison itself: GOS rows in, per-country counts out.

The shape of the question is simple — "which products are online for this
country but missing from its Google Merchant Center feed?" — but the input
needs care:

  * the GOS export is one wide CSV with a column pair per country;
  * "online" is a truthy string (true/1/yes/y) and stock/price must be > 0;
  * IDs are compared case-insensitively and whitespace-trimmed, because the
    GOS export and the GMC feed disagree about both.

Everything is done in Polars except the numeric parsing, which has to tolerate
currency symbols and European decimal commas.
"""
import re
from typing import Dict, List, Sequence, Tuple

import polars as pl

from .progress import Reporter

ONLINE_TRUTHY = {"true", "1", "yes", "y"}
_NUMBER_CLEAN_RE = re.compile(r"[^0-9,.\-]")


def parse_number(raw) -> Tuple[float, bool]:
    """Parse a stock/price cell. Returns (value, parsed_ok).

    Handles ``1,234.50``, ``1.234,50``, ``€ 12.00`` and blanks. A blank is
    treated as 0 (not online), which is why the filter is ``> 0``.
    """
    if raw is None:
        return 0.0, False
    text = str(raw).strip()
    if not text:
        return 0.0, False

    cleaned = _NUMBER_CLEAN_RE.sub("", text)
    if not cleaned or cleaned in {"-", ".", ","}:
        return 0.0, False

    if "," in cleaned and "." in cleaned:
        # Whichever separator comes last is the decimal one.
        if cleaned.rfind(",") > cleaned.rfind("."):
            cleaned = cleaned.replace(".", "").replace(",", ".")
        else:
            cleaned = cleaned.replace(",", "")
    elif "," in cleaned:
        head, _, tail = cleaned.rpartition(",")
        cleaned = (head.replace(",", "") + "." + tail) if len(tail) <= 2 \
            else cleaned.replace(",", "")

    try:
        return float(cleaned), True
    except ValueError:
        return 0.0, False


def load_gos(path, columns: Sequence[str], delimiter: str) -> pl.DataFrame:
    """Read the GOS export, keeping only id + the columns a run needs."""
    frame = pl.read_csv(
        path,
        separator=delimiter,
        columns=list(columns),
        schema_overrides={column: pl.Utf8 for column in columns},
        infer_schema_length=500,
        encoding="utf8-lossy",
        truncate_ragged_lines=True,
    )
    frame = frame.with_columns(
        pl.col("id").cast(pl.Utf8, strict=False).fill_null("")
        .str.strip_chars().alias("id")
    )
    frame = frame.filter(pl.col("id") != "")
    return frame.unique(subset=["id"], keep="first")


def filter_online(gos_df: pl.DataFrame, online_column: str,
                  stock_column: str, price_column: str,
                  reporter: Reporter, country: str) -> pl.DataFrame:
    """Rows where online is truthy AND stock > 0 AND price > 0.

    Returns a frame with ``sku`` (as written) and ``normalized_sku``
    (trimmed, lowercased), one row per SKU.
    """
    subset = gos_df.select(["id", online_column, stock_column, price_column])
    subset = subset.with_columns(
        pl.col(online_column).cast(pl.Utf8, strict=False).fill_null("")
        .str.strip_chars().str.to_lowercase().alias("_online")
    )
    subset = subset.filter(pl.col("_online").is_in(list(ONLINE_TRUTHY)))
    if subset.height == 0:
        return pl.DataFrame(schema={"sku": pl.Utf8, "normalized_sku": pl.Utf8})

    unparsed: Dict[str, int] = {}
    stock = _numeric_column(subset, stock_column, unparsed)
    price = _numeric_column(subset, price_column, unparsed)
    subset = subset.with_columns([stock.alias("_stock"), price.alias("_price")])
    subset = subset.filter((pl.col("_stock") > 0) & (pl.col("_price") > 0))

    for column, count in unparsed.items():
        if count:
            reporter.log(
                "WARNING",
                f"[{country}] {count} value(s) in '{column}' could not be read "
                f"as a number and were treated as 0.",
            )

    return subset.select([
        pl.col("id").alias("sku"),
        pl.col("id").str.strip_chars().str.to_lowercase().alias("normalized_sku"),
    ]).unique(subset=["normalized_sku"], keep="first")


def _numeric_column(frame: pl.DataFrame, column: str,
                     counters: Dict[str, int]) -> pl.Series:
    """Cast a stock/price column to floats, parsing only what Polars cannot.

    Polars casts "9.99", "5" and "-3" natively in Rust. Values like
    "€ 12,50" or "1.234,50" come back null, and only those rows are handed to
    the Python parser — on a real export that is a few percent of the rows
    instead of every one of them.
    """
    raw = (pl.col(column).cast(pl.Utf8, strict=False).fill_null("")
           .str.strip_chars())
    prepared = frame.select([
        raw.alias("_text"),
        raw.cast(pl.Float64, strict=False).alias("_value"),
    ])
    texts = prepared.get_column("_text")
    values = prepared.get_column("_value")

    # A null is either "not a plain number" or "empty". Empty is not an error:
    # it simply means the product is not online.
    needs_python = values.is_null() & (texts != "")
    if bool(needs_python.any()):
        text_list = texts.to_list()
        number_list = values.to_list()
        for index in needs_python.arg_true().to_list():
            parsed, ok = parse_number(text_list[index])
            number_list[index] = parsed
            if not ok:
                counters[column] = counters.get(column, 0) + 1
        values = pl.Series("_value", number_list, dtype=pl.Float64)

    return values.fill_null(0.0)


def missing_rows(online_df: pl.DataFrame, feed_ids: set) -> pl.DataFrame:
    """SKUs that are online but absent from the feed (anti-join)."""
    feed_df = pl.DataFrame(
        {"normalized_sku": sorted(feed_ids)},
        schema={"normalized_sku": pl.Utf8},
    )
    return online_df.join(feed_df, on="normalized_sku", how="anti")


def sample_skus(missing_df: pl.DataFrame, limit: int, seed: int) -> List[str]:
    """Deterministic sample of missing SKUs, sorted for a stable report."""
    if missing_df.height == 0 or limit <= 0:
        return []
    candidates = sorted({str(sku) for sku in missing_df.get_column("sku").to_list()})
    if limit >= len(candidates):
        return candidates
    # Local RNG so concurrent runs cannot influence each other's sample.
    import random
    return random.Random(seed).sample(candidates, limit)


def country_columns_for(country: str, table: Dict[str, Dict[str, str]]) -> Dict[str, str]:
    """GOS columns for a country code, tolerating case and separators."""
    key = (country or "").strip().lower().replace("_", "-")
    return dict(table.get(key) or {})


def gos_columns_for(feed_columns: Dict[str, str],
                    fallback: Dict[str, str]) -> Dict[str, str]:
    """Per-feed GOS columns win over the country table."""
    merged = dict(fallback)
    for key, value in (feed_columns or {}).items():
        if value:
            merged[key] = value
    return merged


def invalid_columns(mapping: Dict[str, str],
                    gos_columns: Sequence[str]) -> List[str]:
    """Names of the required keys that are missing or absent from the GOS."""
    present = set(gos_columns)
    missing = []
    for key in ("online", "stock", "price"):
        column = mapping.get(key)
        if not column or column not in present:
            missing.append(key)
    return missing


def infer_country(site_name: str, channel_name: str,
                  aliases: Dict[str, str],
                  known: Dict[str, Dict[str, str]]) -> str:
    """Best-effort country code for a channel being added from Browse.

    Only used when a feed enters the selection; feeds restored from the saved
    file keep whatever country they were stored with, so a wrong guess never
    silently rewrites an existing setup.
    """
    text = f"{site_name or ''} {channel_name or ''}".lower()
    if not text.strip():
        return ""

    # Explicit aliases first (uae -> ae, england -> uk).
    for alias, code in aliases.items():
        if re.search(rf"(?<![a-z]){re.escape(alias)}(?![a-z])", text):
            return code

    # Then the longest known code that appears as a whole token. Longest
    # first so "be-nl" wins over "be".
    for code in sorted(known, key=len, reverse=True):
        if re.search(rf"(?<![a-z0-9]){re.escape(code)}(?![a-z0-9])", text):
            return code
    return ""
