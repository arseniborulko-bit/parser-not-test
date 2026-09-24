"""Таблица «ASIN × даты» во вкладке «История» — по образцу сводной таблицы Rating Radar."""

from datetime import date

import pandas as pd
import pytest

from test_dashboard_access import SNAPSHOT_ROW, dash, run  # noqa: F401

import dashboard_db as dash_module


def day(number, **overrides):
    return {**SNAPSHOT_ROW, "snapshot_date": date(2026, 9, number), **overrides}


THREE_DAYS = pd.DataFrame([
    day(18, our_bsr=100, comp_bsr=200, our_price=10.0, comp_price=9.0),
    day(19, our_bsr=90, comp_bsr=210, our_price=11.0, comp_price=9.5),
    day(20, our_bsr=80, comp_bsr=220, our_price=12.0, comp_price=9.9),
])


def test_rows_are_asins_and_columns_are_days():
    matrix = dash_module._history_matrix(THREE_DAYS, "BSR")
    assert list(matrix.columns) == ["ASIN", "Страна", "18.09", "19.09", "20.09"]
    assert sorted(matrix["ASIN"]) == ["B000000001", "B000000002"]


def test_both_sides_of_the_pair_become_their_own_rows():
    matrix = dash_module._history_matrix(THREE_DAYS, "BSR").set_index("ASIN")
    assert list(matrix.loc["B000000001", ["18.09", "19.09", "20.09"]]) == ["100", "90", "80"]
    assert list(matrix.loc["B000000002", ["18.09", "19.09", "20.09"]]) == ["200", "210", "220"]


def test_the_price_parameter_uses_the_price_columns():
    matrix = dash_module._history_matrix(THREE_DAYS, "Цена").set_index("ASIN")
    assert list(matrix.loc["B000000001", ["18.09", "20.09"]]) == ["10", "12"]


def test_the_same_asin_in_several_pairs_is_one_row_not_several():
    """Один ASIN на одном рынке даёт одни и те же цифры во всех своих парах."""
    duplicated = pd.concat([THREE_DAYS, THREE_DAYS.assign(comp_asin="B000000009")], ignore_index=True)
    matrix = dash_module._history_matrix(duplicated, "BSR")
    assert list(matrix["ASIN"]).count("B000000001") == 1


def test_a_day_without_data_is_blank_not_the_word_none():
    """В этой версии Streamlit пустая клетка рисуется видимым текстом «None»."""
    gap = pd.DataFrame([day(18, our_bsr=100), day(20, our_bsr=80)])
    matrix = dash_module._history_matrix(gap, "BSR").set_index("ASIN")
    assert "18.09" in matrix.columns and "20.09" in matrix.columns
    assert "None" not in matrix.loc["B000000001"].to_list()


def test_days_go_in_order():
    shuffled = THREE_DAYS.iloc[[2, 0, 1]].reset_index(drop=True)
    matrix = dash_module._history_matrix(shuffled, "BSR")
    assert list(matrix.columns)[2:] == ["18.09", "19.09", "20.09"]


def test_an_empty_history_gives_an_empty_table_instead_of_an_error():
    assert dash_module._history_matrix(THREE_DAYS.iloc[0:0], "BSR").empty


def test_both_parameters_point_at_columns_that_exist():
    for ours, theirs in dash_module._MATRIX_METRICS.values():
        assert ours in THREE_DAYS.columns and theirs in THREE_DAYS.columns


def test_the_matrix_is_drawn_on_the_history_tab(dash):  # noqa: F811
    at = run()
    assert any(widget.key == "history_matrix_metric" for widget in at.radio)


def test_the_table_is_capped_but_fits_todays_data_whole():
    """На боевых данных 683 строки — предел не должен прятать их за «сузьте фильтры»."""
    assert dash_module.MAX_MATRIX_ROWS >= 700
