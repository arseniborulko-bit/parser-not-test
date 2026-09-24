"""Выбор дня в «Истории»: посмотреть срез за дату, не выискивая её в общей таблице."""

from datetime import date

import pandas as pd
import pytest

from test_dashboard_access import SNAPSHOT_ROW, dash, run  # noqa: F401

import dashboard_db as dash_module

THREE_DAYS = pd.DataFrame([
    {**SNAPSHOT_ROW, "snapshot_date": date(2026, 9, 18), "our_bsr": 100},
    {**SNAPSHOT_ROW, "snapshot_date": date(2026, 9, 19), "our_bsr": 90},
    {**SNAPSHOT_ROW, "snapshot_date": date(2026, 9, 20), "our_bsr": 80},
])


@pytest.fixture
def history(dash, monkeypatch):  # noqa: F811
    monkeypatch.setattr(dash, "load_snapshots", lambda: THREE_DAYS.copy())
    return dash


def day_picker(at):
    return next(widget for widget in at.selectbox if widget.key == "history_day")


def history_table(at):
    return [frame.value for frame in at.dataframe if "Дата сбора" in getattr(frame.value, "columns", [])]


def test_the_newest_day_is_offered_first(history):
    options = day_picker(run()).options
    assert options[0] == "Все дни"
    assert options[1:] == ["20.09.2026", "19.09.2026", "18.09.2026"], "свежие даты должны быть сверху"


def test_picking_a_day_leaves_only_that_day(history):
    at = run()
    day_picker(at).set_value("19.09.2026")
    at.run(timeout=30)
    tables = history_table(at)
    assert tables, "таблица истории не нарисована"
    assert set(tables[-1]["Дата сбора"]) == {date(2026, 9, 19)}


def test_all_days_is_the_default_and_keeps_everything(history):
    at = run()
    assert day_picker(at).value == "Все дни"
    assert len(history_table(at)[-1]) == 3


def test_the_matrix_still_shows_every_day(history):
    """Выбор дня относится к таблице ниже: сводная таблица по датам от одного дня бессмысленна."""
    at = run()
    day_picker(at).set_value("19.09.2026")
    at.run(timeout=30)
    matrix = dash_module._history_matrix(THREE_DAYS, "BSR")
    assert list(matrix.columns)[2:] == ["18.09", "19.09", "20.09"]


def test_an_empty_history_does_not_break_the_picker():
    empty = THREE_DAYS.iloc[0:0]
    assert dash_module._pick_day(empty).empty
