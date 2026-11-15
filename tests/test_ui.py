"""UI tests: the country page's bulk actions, the summary table, the window.

Qt runs headless. ``QT_QPA_FONTDIR`` matters on Windows: without it the
offscreen platform has no font database and every glyph renders as a box,
which makes widget-size assertions meaningless.
"""
import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
if sys.platform == "win32":
    os.environ.setdefault("QT_QPA_FONTDIR", "C:/Windows/Fonts")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

from core.models import Feed, GosFeed, Selection

_APP = None


def _app():
    global _APP
    if _APP is None:
        _APP = QApplication.instance() or QApplication([])
    return _APP


def _selection() -> Selection:
    return Selection(
        gos=GosFeed(site_id="1", channel_id="9", url="http://x/gos.csv",
                    channel_name="GOS"),
        feeds=[
            Feed(country="uk", site_id="2", channel_id="10",
                 url="http://x/uk.csv", channel_name="UK feed"),
            Feed(country="uk", site_id="2", channel_id="11",
                 url="http://x/uk2.csv", channel_name="UK promo"),
            Feed(country="de", site_id="3", channel_id="12",
                 url="http://x/de.csv", channel_name="DE feed"),
        ],
        selected=["2/10", "2/11", "3/12"],
    )


def _country(page, code: str):
    """Countries render in alphabetical order, so find by label, not index."""
    for index in range(page.tree.topLevelItemCount()):
        item = page.tree.topLevelItem(index)
        if item.text(0).startswith(code.upper()):
            return item
    raise AssertionError(f"country {code} not found")


COUNTRIES = {
    "uk": {"online": "ca_online_uk", "stock": "bb_stock_uk", "price": "bb_price_uk"},
    "de": {"online": "ca_online_de", "stock": "bb_stock_de", "price": "bb_price_de"},
}


# ---------------------------------------------------------------------------
# Country page
# ---------------------------------------------------------------------------

def test_country_page_groups_feeds_under_countries():
    _app()
    from ui.country_page import CountryPage

    page = CountryPage()
    page.set_selection(_selection(), COUNTRIES)

    assert page.tree.topLevelItemCount() == 2          # uk, de
    uk = _country(page, "uk")
    assert "UK" in uk.text(0)
    assert uk.childCount() == 2
    assert uk.checkState(0) == Qt.Checked
    assert uk.child(0).text(2) == "Ready"
    assert page.selected_keys() == ["2/10", "2/11", "3/12"]
    page.deleteLater()


def test_select_none_clears_every_feed():
    _app()
    from ui.country_page import CountryPage

    page = CountryPage()
    page.set_selection(_selection(), COUNTRIES)
    emitted = []
    page.selection_changed.connect(lambda: emitted.append(True))

    page.select_none_btn.click()

    assert page.selected_keys() == []
    assert _country(page, "uk").checkState(0) == Qt.Unchecked
    assert emitted, "the page must tell the window to persist"
    page.deleteLater()


def test_country_checkbox_is_tristate_when_only_some_feeds_are_ticked():
    _app()
    from ui.country_page import CountryPage

    page = CountryPage()
    page.set_selection(_selection(), COUNTRIES)

    uk = _country(page, "uk")
    uk.child(0).setCheckState(0, Qt.Unchecked)      # one of two UK feeds off

    assert uk.checkState(0) == Qt.PartiallyChecked
    assert page.selected_keys() == ["2/11", "3/12"]
    page.deleteLater()


def test_ticking_a_country_ticks_all_of_its_feeds():
    _app()
    from ui.country_page import CountryPage

    page = CountryPage()
    page.set_selection(_selection(), COUNTRIES)
    uk = _country(page, "uk")

    uk.setCheckState(0, Qt.Unchecked)
    assert page.selected_keys() == ["3/12"]

    uk.setCheckState(0, Qt.Checked)
    assert page.selected_keys() == ["2/10", "2/11", "3/12"]
    page.deleteLater()


def test_bulk_actions_only_touch_visible_rows():
    _app()
    from ui.country_page import CountryPage

    page = CountryPage()
    page.set_selection(_selection(), COUNTRIES)
    page.search_edit.setText("uk")

    page.select_none_btn.click()

    # "de" was filtered out, so its feed keeps its tick.
    assert page.selected_keys() == ["3/12"]
    page.deleteLater()


def test_invert_flips_the_visible_rows():
    _app()
    from ui.country_page import CountryPage

    page = CountryPage()
    page.set_selection(_selection(), COUNTRIES)
    page.select_none_btn.click()
    page.invert_btn.click()

    assert page.selected_keys() == ["2/10", "2/11", "3/12"]
    page.deleteLater()


def test_feed_without_gos_columns_is_flagged():
    _app()
    from ui.country_page import CountryPage

    selection = Selection(
        gos=GosFeed(site_id="1", channel_id="9", url="http://x/g.csv"),
        feeds=[Feed(country="zz", site_id="5", channel_id="50",
                    url="http://x/zz.csv", channel_name="ZZ")],
        selected=["5/50"],
    )
    page = CountryPage()
    page.set_selection(selection, COUNTRIES)
    assert _country(page, "zz").child(0).text(2) == "Needs GOS columns"
    page.deleteLater()


def test_empty_selection_shows_the_hint():
    _app()
    from ui.country_page import CountryPage

    page = CountryPage()
    page.set_selection(Selection(), COUNTRIES)
    assert page.empty_label.isVisible() or not page.empty_label.isHidden()
    assert page.tree.topLevelItemCount() == 0
    page.deleteLater()


# ---------------------------------------------------------------------------
# Results page
# ---------------------------------------------------------------------------

def test_results_page_renders_six_columns_and_totals():
    _app()
    from ui.results_page import COLUMNS, ResultsPage

    page = ResultsPage()
    page.set_rows([
        {"country": "uk", "gos_count": 100, "not_in_channel_count": 7,
         "skipped_count": 2, "top_reason_1": '"price" is 0', "top_reason_1_pct": 50.0},
        {"country": "de", "gos_count": 50, "not_in_channel_count": 0,
         "skipped_count": 0, "top_reason_1": "", "top_reason_1_pct": 0.0},
    ])

    assert page.table.columnCount() == len(COLUMNS) == 6
    assert page.table.rowCount() == 2
    assert page.table.item(0, 0).text() == "uk"
    assert page.table.item(0, 1).text() == "100"
    assert page.table.item(0, 4).text() == '"price" is 0'
    assert page.table.item(0, 5).text() == "50.0%"
    assert page.table.item(1, 4).text() == "—"       # no reason
    assert "2 countries" in page.totals_label.text()
    assert "150 online" in page.totals_label.text()
    page.deleteLater()


# ---------------------------------------------------------------------------
# Main window
# ---------------------------------------------------------------------------

def test_main_window_builds_with_four_tabs(tmp_path, monkeypatch):
    _app()
    from core import settings as settings_module
    from ui.main_window import MainWindow

    for name, filename in (("SETTINGS_FILE", "settings.json"),
                           ("COUNTRIES_FILE", "countries.json"),
                           ("SELECTION_FILE", "selection.json"),
                           ("CREDENTIALS_FILE", "credentials.json"),
                           ("LEGACY_SESSION_FILE", "last_session.json")):
        monkeypatch.setattr(settings_module, name, tmp_path / filename)
    monkeypatch.setattr(settings_module, "BASE_DIR", tmp_path)

    window = MainWindow()
    try:
        assert [window.tabs.tabText(i) for i in range(window.tabs.count())] == [
            "Countries", "Browse", "Results", "Log",
        ]
        assert not window.start_btn.isEnabled()      # nothing selected yet
        assert "0 of 0 feeds" in window.summary_label.text()
    finally:
        window.deleteLater()
