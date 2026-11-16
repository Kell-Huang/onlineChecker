"""Core tests: config, feed parsing, comparison maths, report writing.

No network, no Qt — these run in milliseconds and cover the parts that
silently produce wrong numbers if they regress.
"""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import compare, feeds, settings as settings_module
from core.models import Feed, GosFeed, Selection
from core.report import SUMMARY_COLUMNS, write_report


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

@pytest.fixture
def config_dir(tmp_path, monkeypatch):
    """Point the config layer at a throwaway directory."""
    monkeypatch.setattr(settings_module, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(settings_module, "SETTINGS_FILE", tmp_path / "settings.json")
    monkeypatch.setattr(settings_module, "COUNTRIES_FILE", tmp_path / "countries.json")
    monkeypatch.setattr(settings_module, "SELECTION_FILE", tmp_path / "selection.json")
    monkeypatch.setattr(settings_module, "CREDENTIALS_FILE", tmp_path / "credentials.json")
    monkeypatch.setattr(settings_module, "LEGACY_SESSION_FILE", tmp_path / "last_session.json")
    monkeypatch.setattr(settings_module, "BASE_DIR", tmp_path)
    return tmp_path


def test_settings_file_is_written_with_defaults_on_first_run(config_dir):
    loaded = settings_module.load_settings()
    assert loaded.max_per_country == 1000
    written = json.loads((config_dir / "settings.json").read_text(encoding="utf-8"))
    # Four keys, flat. No nested sections, no tuning values.
    assert set(written) == {"output_dir", "max_per_country", "sample_seed",
                            "download_concurrency"}
    # A fresh seed every run by default: re-checking the same thousand SKUs
    # every time is the thing this setting exists to avoid.
    assert written["sample_seed"] == "random"
    assert loaded.random_seed is None


def test_settings_advanced_block_is_optional(config_dir):
    (config_dir / "settings.json").write_text(
        json.dumps({"output_dir": "out", "max_per_country": 50,
                    "advanced": {"api_batch_size": 50}}), encoding="utf-8")
    loaded = settings_module.load_settings()
    assert loaded.max_per_country == 50
    assert loaded.api_batch_size == 50
    # Untouched keys still fall back to the code constants.
    assert loaded.api_interval == settings_module.API_BATCH_INTERVAL


def test_a_pinned_seed_round_trips(config_dir):
    (config_dir / "settings.json").write_text(
        json.dumps({"output_dir": "out", "sample_seed": 42}), encoding="utf-8")
    loaded = settings_module.load_settings()
    assert loaded.random_seed == 42

    settings_module.save_settings(loaded)
    written = json.loads((config_dir / "settings.json").read_text(encoding="utf-8"))
    assert written["sample_seed"] == 42


def test_garbage_seed_falls_back_to_random(config_dir):
    (config_dir / "settings.json").write_text(
        json.dumps({"output_dir": "out", "sample_seed": "whatever"}),
        encoding="utf-8")
    assert settings_module.load_settings().random_seed is None


def test_legacy_session_is_migrated_once(config_dir):
    legacy = {
        "version": "8.1",
        "gos": {"site_id": "1", "channel_id": "9", "url": "http://x/gos.csv",
                "channel_name": "GOS"},
        "feeds": [
            {"country": "uk", "site_id": "2", "channel_id": "10",
             "url": "http://x/uk.csv", "channel_name": "UK feed",
             "project_id": "77"},
            {"country": "de", "site_id": "3", "channel_id": "11",
             "url": "http://x/de.csv", "channel_name": "DE feed"},
        ],
    }
    (config_dir / "last_session.json").write_text(json.dumps(legacy), encoding="utf-8")

    selection = settings_module.load_selection()
    assert selection.gos.channel_name == "GOS"
    assert [feed.country for feed in selection.feeds] == ["uk", "de"]
    # project_id is part of the session and must survive the migration: losing
    # it silently would break any later "refresh this feed from the API".
    assert selection.feeds[0].project_id == "77"
    # The old file only listed the feeds that were selected, so all stay ticked.
    assert selection.selected == ["2/10", "3/11"]
    assert (config_dir / "selection.json").exists()

    # A second load reads the new file, not the legacy one.
    (config_dir / "last_session.json").unlink()
    again = settings_module.load_selection()
    assert again.selected == selection.selected


def test_selection_round_trips_and_drops_unknown_keys(config_dir):
    selection = Selection(
        gos=GosFeed(site_id="1", channel_id="2", url="http://x/g.csv"),
        feeds=[Feed(country="uk", site_id="2", channel_id="10",
                    url="http://x/uk.csv", gos_columns={"online": "ca_online_uk"})],
        selected=["2/10", "9/99"],       # 9/99 is not in feeds
    )
    settings_module.save_selection(selection)

    loaded = settings_module.load_selection()
    assert len(loaded.feeds) == 1
    assert loaded.feeds[0].gos_columns == {"online": "ca_online_uk"}
    assert loaded.selected == ["2/10"]


def test_credentials_accept_both_shapes(config_dir):
    (config_dir / "credentials.json").write_text(
        json.dumps({"productsup": {"token": "abc"}}), encoding="utf-8")
    assert settings_module.load_credentials()["token"] == "abc"

    (config_dir / "credentials.json").write_text(
        json.dumps({"token": "xyz"}), encoding="utf-8")
    assert settings_module.load_credentials()["token"] == "xyz"


def test_country_columns_tolerate_separators():
    table = {"be-nl": {"online": "a"}, "uk": {"online": "b"}}
    assert compare.country_columns_for("BE_NL", table) == {"online": "a"}
    assert compare.country_columns_for("uk ", table) == {"online": "b"}
    assert compare.country_columns_for("", table) == {}


# ---------------------------------------------------------------------------
# Feeds
# ---------------------------------------------------------------------------

def test_id_column_detection_uses_priority_order():
    assert feeds.detect_id_column(["g:id", "title"]) == "g:id"
    assert feeds.detect_id_column(["sku", "id"]) == "id"
    assert feeds.detect_id_column(["ean"]) == "ean"
    assert feeds.detect_id_column(["title", "price"]) is None


def test_csv_ids_are_read_with_semicolons_and_bom(tmp_path):
    path = tmp_path / "feed.csv"
    path.write_text("\ufeffid;title\nA1;x\nA2;y\n", encoding="utf-8")
    ids, column = feeds.read_feed_ids(path, "csv")
    assert column == "id"
    assert ids == ["A1", "A2"]


def test_xml_ids_support_both_gmc_shapes(tmp_path):
    rss = tmp_path / "rss.xml"
    # A real GMC export declares the prefix; without the declaration the
    # document is not well-formed and no parser should be expected to cope.
    rss.write_text(
        '<rss xmlns:g="http://base.google.com/ns/1.0"><channel>'
        "<item><g:id>A1</g:id></item>"
        "<item><g:id>A2</g:id></item></channel></rss>", encoding="utf-8")
    ids, column = feeds.read_feed_ids(rss, "xml")
    assert column == "id"
    assert ids == ["A1", "A2"]

    products = tmp_path / "products.xml"
    products.write_text(
        "<products><product><sku>B1</sku></product>"
        "<product><sku>B2</sku></product></products>", encoding="utf-8")
    ids, column = feeds.read_feed_ids(products, "xml")
    assert column == "sku"
    assert ids == ["B1", "B2"]


def test_format_detection_prefers_declared_then_content_type():
    assert feeds.detect_format("http://x/a.csv", "xml") == "xml"
    assert feeds.detect_format("http://x/a.csv", None, "application/xml") == "xml"
    assert feeds.detect_format("http://x/a.csv", None, "text/csv") == "csv"
    assert feeds.detect_format("http://x/a.xml", None) == "xml"
    assert feeds.detect_format("http://x/a", None) == "csv"


def test_unknown_reader_format_falls_back_to_csv(tmp_path):
    path = tmp_path / "feed.txt"
    path.write_text("id\nA1\n", encoding="utf-8")
    ids, _column = feeds.read_feed_ids(path, "parquet")
    assert ids == ["A1"]


# ---------------------------------------------------------------------------
# Comparison maths
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("raw,expected", [
    ("1,234.50", 1234.5),
    ("1.234,50", 1234.5),
    ("€ 12.00", 12.0),
    (" 7 ", 7.0),
    ("-3", -3.0),
    ("", 0.0),
    (None, 0.0),
    ("n/a", 0.0),
])
def test_parse_number(raw, expected):
    assert compare.parse_number(raw)[0] == expected


def test_online_filter_requires_truthy_online_and_positive_numbers():
    import polars as pl

    frame = pl.DataFrame({
        "id": ["A", "B", "C", "D"],
        "ca_online_uk": ["true", "TRUE", "false", "yes"],
        "bb_stock_uk": ["5", "0", "5", "3"],
        "bb_price_uk": ["9.99", "9.99", "9.99", "0"],
    })
    from core.progress import Reporter

    result = compare.filter_online(frame, "ca_online_uk", "bb_stock_uk",
                                   "bb_price_uk", Reporter(), "uk")
    assert result.get_column("sku").to_list() == ["A"]


def test_missing_rows_are_case_insensitive():
    import polars as pl

    online = pl.DataFrame({
        "sku": ["A1", "B2", "C3"],
        "normalized_sku": ["a1", "b2", "c3"],
    })
    missing = compare.missing_rows(online, {"a1"})
    assert missing.get_column("sku").to_list() == ["B2", "C3"]


def test_sampling_is_deterministic_and_capped():
    import polars as pl

    frame = pl.DataFrame({
        "sku": [f"S{i}" for i in range(100)],
        "normalized_sku": [f"s{i}" for i in range(100)],
    })
    first = compare.sample_skus(frame, 10, 42)
    second = compare.sample_skus(frame, 10, 42)
    assert first == second
    assert len(first) == 10
    assert compare.sample_skus(frame, 500, 42) == sorted(frame.get_column("sku").to_list())


def test_infer_country_prefers_aliases_and_longest_codes():
    aliases = {"uae": "ae", "england": "uk"}
    known = {"ae": {}, "uk": {}, "be": {}, "be-nl": {}}
    assert compare.infer_country("vidaXL.UAE-AR", "GMC", aliases, known) == "ae"
    assert compare.infer_country("vidaXL.BE-NL", "GMC", aliases, known) == "be-nl"
    assert compare.infer_country("vidaXL.UK", "GMC", aliases, known) == "uk"
    assert compare.infer_country("something else", "GMC", aliases, known) == ""


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------

def test_report_has_the_five_sheets_in_order(tmp_path):
    from openpyxl import load_workbook

    path = tmp_path / "report.xlsx"
    write_report(path, {
        "Summary": [{column: 1 for column in SUMMARY_COLUMNS}],
        "Sampled Details": [{"country": "uk", "sku": "A1"}],
        "Skip Reason Distribution": [{"country": "uk", "reason": "x", "count": 1}],
        "API Log": [{"country": "uk", "batch_index": 1}],
        "Failed Batches": [{"country": "uk", "batch_index": 2}],
    })

    workbook = load_workbook(path)
    assert workbook.sheetnames == [
        "Summary", "Sampled Details", "Skip Reason Distribution",
        "API Log", "Failed Batches",
    ]
    header = [cell.value for cell in workbook["Summary"][1]]
    assert header == SUMMARY_COLUMNS


def test_report_keeps_unknown_sheets(tmp_path):
    from openpyxl import load_workbook

    path = tmp_path / "report.xlsx"
    write_report(path, {"Summary": [], "Extra": [{"a": 1, "b": 2}]})
    workbook = load_workbook(path)
    assert "Extra" in workbook.sheetnames
    assert [cell.value for cell in workbook["Extra"][1]] == ["a", "b"]


# ---------------------------------------------------------------------------
# Platform metadata responses
# ---------------------------------------------------------------------------

class _StubResponse:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status

    def json(self):
        return self._payload


class _StubSession:
    def __init__(self, payload, status=200):
        self._payload = payload
        self._status = status

    def get(self, _url, timeout=None):
        return _StubResponse(self._payload, self._status)


def test_metadata_failure_is_not_silently_empty():
    """A 200 with success=false used to come back as an empty list.

    The platform answers {"success": true, "Sites": [...]}; when it answers
    success=false the tree just showed "0 site(s)", which reads as "this
    project has no sites" rather than "the call was rejected".
    """
    from core.productsup import Client

    client = Client("token", "http://example.invalid")
    client._session = _StubSession({"success": False, "message": "no access"})
    with pytest.raises(RuntimeError) as caught:
        client.sites("1")
    assert "no access" in str(caught.value)

    client._session = _StubSession(
        {"success": True, "Sites": [{"id": "123", "title": "site 1"}]})
    assert client.sites("1") == [{"id": "123", "title": "site 1"}]


def test_metadata_missing_success_flag_still_parses():
    """Only an explicit false is an error; older payloads omit the flag."""
    from core.productsup import Client

    client = Client("token", "http://example.invalid")
    client._session = _StubSession({"Sites": [{"id": "1"}]})
    assert client.sites("1") == [{"id": "1"}]


def test_feed_url_takes_the_first_active_destination():
    """Several destinations can be active; the first one wins, every run.

    Confirmed with the owner: a channel holding an EU and a US bucket should
    not flip between them depending on which request answered first.
    """
    from core.productsup import extract_feed_url

    channel = {"feed_destinations": {
        "4711": {"active": False, "urls": ["https://x/off.csv"]},
        "4712": {"active": True, "urls": []},                     # active, no url
        "4713": {"active": True, "urls": ["https://x/first.csv", "https://x/b.csv"]},
        "4714": {"active": True, "urls": ["https://x/later.csv"]},
    }}
    assert extract_feed_url(channel) == "https://x/first.csv"
    assert extract_feed_url({"feed_destinations": {}}) is None
    assert extract_feed_url({}) is None
