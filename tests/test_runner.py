"""End-to-end run against a local fake Productsup.

The real token in the repo is not valid, so the only honest way to prove the
pipeline works is to serve the two APIs ourselves: a GOS CSV, two feed CSVs and
a stage endpoint that returns products for one country and nothing for the
other. The assertions are exact counts, not "it ran".
"""
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import runner, settings as settings_module
from core.models import Feed, GosFeed, Selection
from core.progress import Reporter
from core.settings import Settings

GOS_CSV = (
    "id,ca_online_uk,bb_stock_uk,bb_price_uk,ca_online_de,bb_stock_de,bb_price_de\n"
    "A1,true,5,9.99,true,5,9.99\n"
    "A2,true,5,9.99,true,0,9.99\n"
    "A3,false,5,9.99,true,5,9.99\n"
    "B1,true,5,9.99,true,5,9.99\n"
    "B2,true,5,9.99,true,5,9.99\n"
)

UK_FEED = "id,title\nA1,x\nA3,y\n"
DE_FEED = "id,title\nA1,x\nA2,y\n"

# B1 is in the channel, B2 is in the channel but skipped by a rule.
UK_PRODUCTS = [
    {"id": "B1"},
    {"id": "B2", "___skipped_export": "3::price::is 0",
     "___skipped_intermediate": ""},
]


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/gos.csv":
            return self._send(GOS_CSV.encode(), "text/csv")
        if path == "/uk.csv":
            return self._send(UK_FEED.encode(), "text/csv")
        if path == "/de.csv":
            return self._send(DE_FEED.encode(), "text/csv")
        if "/stage/channel/" in path:
            # UK channel 10 answers, DE channel 11 has nothing.
            products = UK_PRODUCTS if "/10" in path else []
            body = json.dumps({"products": products}).encode()
            return self._send(body, "application/json")
        self.send_response(404)
        self.end_headers()

    def _send(self, body: bytes, content_type: str):
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture
def fake_server():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    server.server_close()


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    """Keep every file the run writes inside the test's temp dir."""
    cache = tmp_path / "cache"
    output = tmp_path / "output"
    cache.mkdir()
    output.mkdir()

    monkeypatch.setattr(settings_module, "CACHE_DIR", cache)
    monkeypatch.setattr(settings_module, "LOGS_DIR", tmp_path / "logs")
    monkeypatch.setattr(runner, "CACHE_DIR", cache)

    countries = {"uk": {"online": "ca_online_uk", "stock": "bb_stock_uk",
                        "price": "bb_price_uk"},
                 "de": {"online": "ca_online_de", "stock": "bb_stock_de",
                        "price": "bb_price_de"}}
    monkeypatch.setattr(runner, "load_countries", lambda: ({}, countries))
    return output


def _selection(base_url: str) -> Selection:
    return Selection(
        gos=GosFeed(site_id="1", channel_id="9", url=f"{base_url}/gos.csv",
                    channel_name="GOS"),
        feeds=[
            Feed(country="uk", site_id="2", channel_id="10",
                 url=f"{base_url}/uk.csv", channel_name="UK feed"),
            Feed(country="de", site_id="3", channel_id="11",
                 url=f"{base_url}/de.csv", channel_name="DE feed"),
        ],
        selected=["2/10", "3/11"],
    )


def test_run_produces_exact_counts(fake_server, workspace):
    settings = Settings(output_dir=workspace, max_per_country=1000,
                        random_seed=42, download_concurrency=2)
    credentials = {"token": "test-token", "base_url": fake_server}

    result = runner.run(_selection(fake_server), settings, credentials,
                        Reporter(), threading.Event())

    assert result.success, result.error
    assert result.output_path and result.output_path.exists()

    by_country = {row["country"]: row for row in result.summary}

    # UK: only A3 is offline, so online = A1, A2, B1, B2. The feed has A1 and
    # A3, which leaves A2, B1 and B2 missing.
    uk = by_country["uk"]
    assert uk["gos_count"] == 4
    assert uk["feed_count"] == 2
    assert uk["missing_count"] == 3
    assert uk["sampled_count"] == 3
    assert uk["in_channel_count"] == 1          # B1
    assert uk["skipped_count"] == 1             # B2
    assert uk["not_in_channel_count"] == 1      # A2 was never returned
    assert "price" in uk["top_reason_1"]
    assert uk["top_reason_1_pct"] == 100.0

    # DE: A2 has stock 0 so it is not online; online = A1, A3, B1, B2 and the
    # feed only has A1, so three are missing and the channel knows none of them.
    de = by_country["de"]
    assert de["gos_count"] == 4
    assert de["missing_count"] == 3
    assert de["not_in_channel_count"] == 3
    assert de["in_channel_count"] == 0
    assert de["skipped_count"] == 0


def test_report_contains_the_same_numbers(fake_server, workspace):
    from openpyxl import load_workbook

    settings = Settings(output_dir=workspace, download_concurrency=2)
    credentials = {"token": "test-token", "base_url": fake_server}

    result = runner.run(_selection(fake_server), settings, credentials,
                        Reporter(), threading.Event())
    assert result.success, result.error

    workbook = load_workbook(result.output_path)
    summary = workbook["Summary"]
    header = [cell.value for cell in summary[1]]
    rows = [dict(zip(header, [cell.value for cell in row]))
            for row in summary.iter_rows(min_row=2)]

    uk = next(row for row in rows if row["country"] == "uk")
    assert uk["missing_count"] == 3
    assert uk["skipped_count"] == 1
    assert uk["not_in_channel_count"] == 1

    details = workbook["Sampled Details"]
    detail_header = [cell.value for cell in details[1]]
    detail_rows = [dict(zip(detail_header, [cell.value for cell in row]))
                   for row in details.iter_rows(min_row=2)]
    statuses = {(row["country"], row["sku"]): row["status"] for row in detail_rows}
    assert statuses[("uk", "B1")] == "in_channel"
    assert statuses[("uk", "B2")] == "skipped"
    assert statuses[("de", "B1")] == "not_in_channel"


def test_missing_token_is_a_config_error(workspace):
    selection = Selection(gos=GosFeed(site_id="1", channel_id="9",
                                      url="http://x/gos.csv"),
                          feeds=[Feed(country="uk", site_id="2", channel_id="10",
                                      url="http://x/uk.csv")],
                          selected=["2/10"])
    result = runner.run(selection, Settings(output_dir=workspace), {},
                        Reporter(), threading.Event())
    assert not result.success
    assert result.error_kind == "config"
    assert "token" in result.error.lower()


def test_no_selected_feeds_is_a_config_error(workspace):
    selection = Selection(gos=GosFeed(site_id="1", channel_id="9",
                                      url="http://x/gos.csv"),
                          feeds=[Feed(country="uk", site_id="2", channel_id="10",
                                      url="http://x/uk.csv")],
                          selected=[])
    result = runner.run(selection, Settings(output_dir=workspace),
                        {"token": "t"}, Reporter(), threading.Event())
    assert result.error_kind == "config"


def test_cancellation_returns_cleanly(fake_server, workspace):
    settings = Settings(output_dir=workspace, download_concurrency=1)
    credentials = {"token": "test-token", "base_url": fake_server}
    event = threading.Event()
    event.set()

    result = runner.run(_selection(fake_server), settings, credentials,
                        Reporter(), event)
    assert result.cancelled
    assert not result.success
