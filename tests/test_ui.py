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


def test_header_checkbox_clears_and_reselects_everything():
    """The header is the only bulk control: one click clears, the next selects."""
    _app()
    from ui.country_page import CountryPage

    page = CountryPage()
    page.set_selection(_selection(), COUNTRIES)
    emitted = []
    page.selection_changed.connect(lambda: emitted.append(True))

    assert page.header.check_state() == Qt.Checked

    page.header.toggled.emit()

    assert page.selected_keys() == []
    assert _country(page, "uk").checkState(0) == Qt.Unchecked
    assert page.header.check_state() == Qt.Unchecked
    assert emitted, "the page must tell the window to persist"

    page.header.toggled.emit()
    assert page.selected_keys() == ["2/10", "2/11", "3/12"]
    assert page.header.check_state() == Qt.Checked
    page.deleteLater()


def test_header_shows_the_indeterminate_state():
    _app()
    from ui.country_page import CountryPage

    page = CountryPage()
    page.set_selection(_selection(), COUNTRIES)
    _country(page, "uk").child(0).setCheckState(0, Qt.Unchecked)

    assert page.header.check_state() == Qt.PartiallyChecked

    # A click on a partial header selects the rest rather than clearing it.
    page.header.toggled.emit()
    assert page.selected_keys() == ["2/10", "2/11", "3/12"]
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


def test_header_only_touches_visible_rows():
    _app()
    from ui.country_page import CountryPage

    page = CountryPage()
    page.set_selection(_selection(), COUNTRIES)
    page.search_edit.setText("uk")

    # The header reflects what is on screen, not the whole selection.
    assert page.header.check_state() == Qt.Checked
    page.header.toggled.emit()

    # "de" was filtered out, so its feed keeps its tick.
    assert page.selected_keys() == ["3/12"]
    assert page.header.check_state() == Qt.Unchecked
    page.deleteLater()


def test_header_checkbox_sits_inside_the_first_section():
    _app()
    from PySide6.QtCore import QRect

    from ui.country_page import CountryPage

    page = CountryPage()
    page.set_selection(_selection(), COUNTRIES)
    page.resize(900, 600)
    page.show()
    _app().processEvents()

    header = page.header
    section = QRect(header.sectionViewportPosition(0), 0,
                    header.sectionSize(0), header.height())
    box = header._box_rect(section)
    assert section.contains(box), (section, box)
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

def test_results_page_renders_the_summary_and_both_reasons():
    _app()
    from ui.results_page import COLUMNS, ResultsPage

    page = ResultsPage()
    page.set_rows([
        {"country": "uk", "gos_count": 100, "not_in_channel_count": 7,
         "skipped_count": 2, "top_reason_1": '"price" is 0', "top_reason_1_pct": 50.0,
         "top_reason_2": '"stock" is 0', "top_reason_2_pct": 25.0},
        {"country": "de", "gos_count": 50, "not_in_channel_count": 0,
         "skipped_count": 0, "top_reason_1": "", "top_reason_1_pct": 0.0,
         "top_reason_2": "", "top_reason_2_pct": 0.0},
    ])

    assert page.table.columnCount() == len(COLUMNS) == 5
    assert page.table.rowCount() == 2
    assert page.table.item(0, 0).text() == "uk"
    assert page.table.item(0, 1).text() == "100"
    assert page.table.item(0, 4).text() == '"price" is 0'

    # The reason cell carries both reasons with their shares, in order.
    pairs = page.table.item(0, 4).data(Qt.UserRole)
    assert pairs == [('"price" is 0', 50.0), ('"stock" is 0', 25.0)]
    assert page.table.item(1, 4).data(Qt.UserRole) == []

    headers = [page.table.horizontalHeaderItem(i).text()
               for i in range(page.table.columnCount())]
    assert headers == ["Country", "SFCC Online", "Not in Google Shopping",
                       "Skipped", "Skip reason"]

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
        assert window.summary_label.text().startswith("GOS:")
    finally:
        window.deleteLater()


def test_header_labels_are_left_aligned_over_their_columns():
    """Header text starts at the section's left edge, like the values below it.

    Fusion centres header labels and ignores TextAlignmentRole, which left
    "Site" floating over the middle of a stretched column. The header draws
    its own labels, so this is the geometry that matters.
    """
    _app()
    from PySide6.QtCore import QRect

    from ui.country_page import CountryPage

    page = CountryPage()
    page.set_selection(_selection(), COUNTRIES)
    page.resize(900, 600)
    page.show()
    _app().processEvents()

    header = page.header
    for column in (1, 2):
        rect = QRect(header.sectionViewportPosition(column), 0,
                     header.sectionSize(column), header.height())
        label = header._label_rect(rect, column)
        assert label.left() == rect.left() + header._PAD, (column, label, rect)
        # and it must not be centred: the label starts near the left edge
        assert label.left() < rect.center().x()
    page.deleteLater()
