"""График «наш против конкурента» по дням во вкладке «История».

Отдельно проверяется, что график строится по самим снимкам и не сверяется со справочником
активных пар: снимки не удаляются, поэтому история по ASIN, который уже перестали собирать,
обязана остаться доступной.
"""

from datetime import date

import pandas as pd
import pytest

from test_dashboard_access import SNAPSHOT_ROW, dash, run  # noqa: F401

import dashboard_db as dash_module


def day(number: int, **overrides) -> dict:
    return {**SNAPSHOT_ROW, "snapshot_date": date(2026, 9, number), **overrides}


THREE_DAYS = pd.DataFrame([
    day(18, our_bsr=100, comp_bsr=200, our_price=10.0, comp_price=9.0),
    day(19, our_bsr=90, comp_bsr=210, our_price=11.0, comp_price=9.5),
    day(20, our_bsr=80, comp_bsr=220, our_price=12.0, comp_price=9.9),
])

LABEL = "US · B000000001 ↔ B000000002"


def test_the_pair_label_names_the_market_and_both_asins():
    assert dash_module._pair_label(THREE_DAYS.iloc[0]) == LABEL


def test_the_chart_has_one_line_per_side_ordered_by_day():
    series = dash_module._history_series(THREE_DAYS, LABEL, "our_bsr", "comp_bsr")
    assert list(series.columns) == ["Наш товар", "Конкурент"]
    assert list(series["Наш товар"]) == [100, 90, 80]
    assert list(series["Конкурент"]) == [200, 210, 220]
    assert list(series.index) == sorted(series.index), "дни обязаны идти по возрастанию"


def test_the_price_metric_uses_the_price_columns():
    series = dash_module._history_series(THREE_DAYS, LABEL, "our_price", "comp_price")
    assert list(series["Наш товар"]) == [10.0, 11.0, 12.0]


def test_both_metrics_point_at_columns_that_exist():
    for ours, theirs in dash_module._CHART_METRICS.values():
        assert ours in THREE_DAYS.columns and theirs in THREE_DAYS.columns


def test_history_of_an_asin_that_is_no_longer_collected_still_charts():
    """Ключевое: ряд строится из снимков, без обращения к таблице активных пар."""
    retired = pd.DataFrame([
        day(1, our_asin="B0OLDOLD01", comp_asin="B0OLDOLD02", our_bsr=500, comp_bsr=600),
        day(2, our_asin="B0OLDOLD01", comp_asin="B0OLDOLD02", our_bsr=450, comp_bsr=610),
    ])
    label = "US · B0OLDOLD01 ↔ B0OLDOLD02"
    assert label in sorted(retired.apply(dash_module._pair_label, axis=1).unique())
    series = dash_module._history_series(retired, label, "our_bsr", "comp_bsr")
    assert list(series["Наш товар"]) == [500, 450]


def test_a_pair_without_numbers_gives_an_empty_frame_instead_of_a_broken_chart():
    blank = pd.DataFrame([day(18, our_bsr=None, comp_bsr=None)])
    assert dash_module._history_series(blank, LABEL, "our_bsr", "comp_bsr").empty


def test_an_unknown_pair_gives_an_empty_frame():
    assert dash_module._history_series(THREE_DAYS, "XX · nope ↔ nope", "our_bsr", "comp_bsr").empty


def test_the_chart_block_is_drawn_on_the_history_tab(dash):  # noqa: F811
    at = run()
    assert any(widget.key == "history_chart_pair" for widget in at.selectbox)
    assert any(widget.key == "history_chart_metric" for widget in at.selectbox)
    assert "Динамика по дням" in " ".join(block.value for block in at.markdown)


def test_no_chart_controls_when_the_filter_leaves_nothing(dash):  # noqa: F811
    at = run()
    at.text_input(key="global_search").set_value("zzzqqq").run()
    assert not any(widget.key == "history_chart_pair" for widget in at.selectbox)


def test_the_current_snapshot_query_keeps_markets_apart():
    """После миграции 006 у пары на нескольких рынках свои строки — запрос обязан их различать,
    иначе дашборд по-прежнему показывал бы одну строку вместо трёх."""
    import inspect

    sql = inspect.getsource(dash_module.load_current)
    assert "DISTINCT ON (s.marketplace, s.our_asin, s.comp_asin)" in sql
    assert "p.marketplace = s.marketplace" in sql
    assert "DISTINCT ON (s.our_asin, s.comp_asin)" not in sql
