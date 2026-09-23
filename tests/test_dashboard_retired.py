"""«Больше не собираются»: ASIN из отключённых пар видны во вкладке «Сбор и управление».

Пары отключаются, а не удаляются, и снимки не удаляются никогда — поэтому отключённый ASIN
не должен исчезать из виду вместе со своей историей.
"""

from datetime import date

import pandas as pd
import pytest

from test_dashboard_access import dash, run  # noqa: F401

import dashboard_db as dash_module

RETIRED = pd.DataFrame([
    {"marketplace": "UK", "asin": "B0C8NGXZDV", "name": "Старый конкурент",
     "role": "конкурент", "last_seen": date(2026, 9, 1)},
    {"marketplace": "DE", "asin": "B09QMDSVB2", "name": "", "role": "конкурент", "last_seen": None},
])


@pytest.fixture
def with_retired(dash, monkeypatch):  # noqa: F811
    monkeypatch.setattr(dash, "load_retired_asins", lambda: RETIRED.copy())
    return dash


def tables(at):
    return [frame.value for frame in at.dataframe]


def test_retired_asins_are_listed_with_a_count(with_retired):
    at = run()
    text = " ".join(block.value for block in at.markdown)
    assert "Больше не собираются — 2" in text


def test_the_table_uses_russian_headers_and_shows_the_asins(with_retired):
    at = run()
    shown = [frame for frame in tables(at) if "ASIN" in getattr(frame, "columns", [])]
    assert shown, "таблица отключённых ASIN не нарисована"
    table = shown[0]
    assert list(table["ASIN"]) == ["B0C8NGXZDV", "B09QMDSVB2"]
    for label in ("Страна", "Товар", "Роль", "Данные до"):
        assert label in table.columns, label


def test_a_date_is_shown_for_an_asin_that_was_collected_and_blank_for_one_that_never_was(with_retired):
    at = run()
    table = next(frame for frame in tables(at) if "ASIN" in getattr(frame, "columns", []))
    assert list(table["Данные до"]) == ["01.09.2026", ""]


def test_nothing_is_drawn_when_every_asin_is_still_collected(dash, monkeypatch):  # noqa: F811
    monkeypatch.setattr(dash, "load_retired_asins", lambda: RETIRED.iloc[0:0].copy())
    text = " ".join(block.value for block in run().markdown)
    assert "Больше не собираются" not in text


def test_a_database_error_does_not_break_the_tab(dash, monkeypatch):  # noqa: F811
    """Блок необязательный: если запрос не прошёл, вкладка со сбором обязана остаться рабочей."""
    def broken():
        raise RuntimeError("база недоступна")

    monkeypatch.setattr(dash, "load_retired_asins", broken)
    at = run()
    assert not at.exception
    assert "Больше не собираются" not in " ".join(block.value for block in at.markdown)


def test_the_query_looks_for_asins_that_are_in_no_active_pair():
    import inspect

    sql = inspect.getsource(dash_module.load_retired_asins)
    assert "NOT IN (SELECT asin FROM active_asins)" in sql
    assert "WHERE active" in sql
    # История берётся из снимков по обеим сторонам пары, иначе «наш» ASIN остался бы без даты.
    assert "our_asin = s.asin OR comp_asin = s.asin" in sql
