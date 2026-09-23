"""Мелочи из внешнего разбора дашборда: знаки у дельт, киевское время, пустые таблицы, заголовок."""

from datetime import date, datetime, timezone

import pandas as pd
import pytest

from test_dashboard_access import SNAPSHOT_ROW, dash, run  # noqa: F401

import dashboard_db as dash_module


@pytest.mark.parametrize("value, expected", [
    (5, "+5"), (5.0, "+5"), (0.5, "+0.5"), (-3, "-3"), (0, "0"), (None, ""),
])
def test_a_delta_shows_its_direction(value, expected):
    assert dash_module._format_signed_or_blank(value) == expected


def test_plain_numbers_keep_no_sign():
    assert dash_module._format_number_or_blank(5) == "5"
    assert dash_module._format_number_or_blank(None) == ""


def test_only_the_columns_where_direction_matters_are_signed():
    assert set(dash_module._SIGNED_LABELS) == {"Δ BSR наш", "Δ BSR конкурента", "Разница цен, %"}
    assert set(dash_module._SIGNED_LABELS) <= set(dash_module._NUMERIC_LABELS)
    assert "Цена наша" not in dash_module._SIGNED_LABELS


def test_the_updated_column_is_shown_in_kyiv_not_utc():
    """В базе время в UTC, а весь остальной дашборд — по Киеву; смешивать их нельзя."""
    utc_moment = datetime(2026, 9, 23, 9, 4, 43, tzinfo=timezone.utc)
    assert dash_module._format_kyiv_time(utc_moment) == "23.09 12:04"


def test_a_naive_timestamp_is_read_as_utc_because_that_is_how_it_was_stored():
    assert dash_module._format_kyiv_time(datetime(2026, 9, 23, 9, 4, 43)) == "23.09 12:04"


def test_a_missing_timestamp_stays_blank():
    assert dash_module._format_kyiv_time(None) == ""


def test_the_presented_table_applies_both_the_sign_and_the_kyiv_time():
    frame = pd.DataFrame([{**SNAPSHOT_ROW, "our_bsr_delta_24h": 7, "comp_bsr_delta_24h": -2,
                           "updated_at": datetime(2026, 9, 23, 9, 4, 43, tzinfo=timezone.utc)}])
    presented, _ = dash_module._present_table(frame)
    assert presented["Δ BSR наш"].iloc[0] == "+7"
    assert presented["Δ BSR конкурента"].iloc[0] == "-2"
    assert presented["Обновлено"].iloc[0] == "23.09 12:04"


def test_an_empty_result_is_explained_in_russian_instead_of_the_default_empty(dash):  # noqa: F811
    at = run()
    at.text_input(key="global_search").set_value("zzzqqq").run()
    notes = " ".join(info.value for info in at.info)
    assert "Ничего не найдено" in notes


def test_the_page_has_a_name_of_its_own(dash):  # noqa: F811
    text = " ".join(block.value for block in run().markdown)
    assert "BSR Radar" in text


def test_the_browser_tab_is_not_called_streamlit():
    source = dash_module.__file__
    with open(source, encoding="utf-8") as handle:
        body = handle.read()
    assert 'page_title="BSR Radar' in body


def test_the_pairs_table_has_russian_headers():
    import pairs_ui

    assert pairs_ui._PAIR_COLUMNS["our_asin"] == "Наш ASIN"
    assert set(pairs_ui._PAIR_COLUMNS) == {
        "marketplace", "our_asin", "our_product", "comp_asin", "competitor_name", "active",
    }


def test_the_product_name_is_taken_from_the_same_market():
    """После миграции 006 у пары на нескольких рынках свои строки — название нельзя брать с чужого."""
    import inspect

    sql = inspect.getsource(dash_module.load_competitor_pairs)
    assert "marketplace = p.marketplace" in sql
