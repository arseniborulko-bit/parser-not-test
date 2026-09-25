"""Вкладка «Прогноз»: куда идёт BSR каждого ASIN.

BSR — чем меньше, тем лучше, поэтому падающий наклон означает рост позиций. Прогноз — прямая
экстраполяция тренда, поэтому важно, чтобы он не строился на двух точках и не выдумывал данные.
"""

from datetime import date

import pandas as pd
import pytest

from test_dashboard_access import SNAPSHOT_ROW, dash, run  # noqa: F401

import dashboard_db as dash_module


def rows(values, asin="B000000001", comp="B000000002", start=18):
    return pd.DataFrame([
        {**SNAPSHOT_ROW, "snapshot_date": date(2026, 9, start + offset),
         "our_asin": asin, "comp_asin": comp, "our_bsr": value, "comp_bsr": None}
        for offset, value in enumerate(values)
    ])


def test_a_falling_bsr_is_reported_as_growth():
    """BSR 300 → 200 → 100 означает, что товар поднимается."""
    table = dash_module._forecast_table(rows([300, 200, 100]), window_days=30, horizon_days=7)
    line = table[table["ASIN"] == "B000000001"].iloc[0]
    assert line["Тренд"] == "растём"
    assert line["Изменение в день"] == pytest.approx(-100.0)


def test_a_rising_bsr_is_reported_as_falling_positions():
    table = dash_module._forecast_table(rows([100, 200, 300]), window_days=30, horizon_days=7)
    assert table[table["ASIN"] == "B000000001"].iloc[0]["Тренд"] == "падаем"


def test_the_projection_extends_the_trend_by_the_chosen_horizon():
    table = dash_module._forecast_table(rows([300, 200, 100]), window_days=30, horizon_days=7)
    line = table[table["ASIN"] == "B000000001"].iloc[0]
    # последнее значение 100, наклон −100 в день, горизонт 7 дней → проекция упирается в ноль
    assert line["Прогноз через 7 дн."] == 0.0


def test_a_projection_never_goes_below_zero():
    """Отрицательного BSR не бывает: прямая экстраполяция обязана упираться в ноль."""
    table = dash_module._forecast_table(rows([500, 300, 100]), window_days=30, horizon_days=30)
    assert (table["Прогноз через 30 дн."] >= 0).all()


def test_two_measurements_are_not_a_trend():
    assert dash_module._forecast_table(rows([200, 100]), window_days=30, horizon_days=7).empty
    assert dash_module.MIN_FORECAST_POINTS == 3


def test_only_the_chosen_window_is_used():
    """Старые замеры за окном не должны тянуть наклон на себя."""
    data = rows([1000, 900, 800, 100, 110, 120], start=1)
    narrow = dash_module._forecast_table(data, window_days=3, horizon_days=7)
    assert narrow.iloc[0]["Изменение в день"] > 0, "в последние дни BSR рос"


def test_gaps_in_days_do_not_distort_the_slope():
    """Наклон считается по времени, а не по номеру строки: пропущенный день не ускоряет тренд."""
    dense = rows([300, 200, 100])
    sparse = pd.DataFrame([
        {**SNAPSHOT_ROW, "snapshot_date": date(2026, 9, 18), "our_bsr": 300},
        {**SNAPSHOT_ROW, "snapshot_date": date(2026, 9, 20), "our_bsr": 200},
        {**SNAPSHOT_ROW, "snapshot_date": date(2026, 9, 22), "our_bsr": 100},
    ])
    dense_slope = dash_module._forecast_table(dense, 30, 7).iloc[0]["Изменение в день"]
    sparse_slope = dash_module._forecast_table(sparse, 30, 7).iloc[0]["Изменение в день"]
    assert dense_slope == pytest.approx(-100.0)
    assert sparse_slope == pytest.approx(-50.0)


def test_rows_without_numbers_are_skipped_not_counted_as_zero():
    blank = rows([None, None, None])
    assert dash_module._forecast_table(blank, 30, 7).empty


def test_the_same_asin_in_two_countries_is_forecast_separately():
    uk = rows([300, 200, 100])
    uk["marketplace"] = "UK"
    de = rows([100, 200, 300])
    de["marketplace"] = "DE"
    table = dash_module._forecast_table(pd.concat([uk, de], ignore_index=True), 30, 7)
    by_market = table.set_index("Страна")
    assert by_market.loc["UK", "Тренд"] == "растём"
    assert by_market.loc["DE", "Тренд"] == "падаем"


def test_an_empty_history_gives_an_empty_table():
    assert dash_module._forecast_table(rows([]), 30, 7).empty


def test_one_wild_day_does_not_become_the_trend():
    """На боевых данных BSR скачет на сотни тысяч за сутки. Прямая по всем точкам давала из-за
    этого наклоны вроде −180000 в день, и прогноз у большинства упирался в ноль."""
    steady_with_spike = rows([100_000, 99_000, 500_000, 97_000, 96_000])
    slope = dash_module._forecast_table(steady_with_spike, 30, 7).iloc[0]["Изменение в день"]
    assert abs(slope) < 5_000, f"один выброс задал тренд: {slope}"


def test_the_tab_exists_between_history_and_pairs(dash):  # noqa: F811
    labels = [str(tab.label) for tab in run().tabs]
    assert "📈 Прогноз" in labels
    assert labels.index("📈 Прогноз") == labels.index("📅 История") + 1


def test_the_tab_says_when_there_is_not_enough_data(dash, monkeypatch):  # noqa: F811
    monkeypatch.setattr(dash, "load_snapshots", lambda: rows([100, 200]))
    notes = " ".join(info.value for info in run().info)
    assert "Недостаточно замеров" in notes


def test_the_asin_series_bridges_the_last_fact_point_into_the_forecast():
    """Прогнозная (пунктирная) линия должна начинаться с последней фактической точки, а не
    висеть в воздухе отдельной точкой — иначе на графике был бы разрыв."""
    series = dash_module._asin_forecast_series(
        rows([300, 200, 100]), window_days=30, horizon_days=7, asin="B000000001", market="US",
    )
    fact = series[series["Тип"] == "Факт"]
    forecast = series[series["Тип"] == "Прогноз"]
    assert len(fact) == 3
    assert list(fact["BSR"]) == [300, 200, 100]
    assert len(forecast) == 2
    assert forecast.iloc[0]["Дата"] == fact.iloc[-1]["Дата"]
    assert forecast.iloc[0]["BSR"] == fact.iloc[-1]["BSR"]
    # наклон −100/день, горизонт 7 дней от 100 — упирается в ноль (тот же случай, что и в таблице)
    assert forecast.iloc[-1]["BSR"] == 0.0


def test_the_asin_series_is_empty_below_the_minimum_points():
    empty = dash_module._asin_forecast_series(rows([200, 100]), 30, 7, "B000000001", "US")
    assert empty.empty


def test_the_asin_series_is_empty_for_an_asin_not_in_the_data():
    empty = dash_module._asin_forecast_series(rows([300, 200, 100]), 30, 7, "B0NOTHERE1", "US")
    assert empty.empty


def test_the_leaders_chart_plots_the_change_per_day_as_bars():
    table = dash_module._forecast_table(rows([300, 200, 100]), window_days=30, horizon_days=7)
    spec = dash_module._forecast_leaders_chart(table).to_dict()
    mark = spec["mark"]
    assert (mark if isinstance(mark, str) else mark["type"]) == "bar"
    assert spec["encoding"]["x"]["field"] == "Изменение в день"
    assert spec["encoding"]["y"]["field"] == "Метка"


def test_the_line_chart_uses_dashing_to_tell_fact_from_forecast():
    series = dash_module._asin_forecast_series(rows([300, 200, 100]), 30, 7, "B000000001", "US")
    spec = dash_module._asin_forecast_line_chart(series).to_dict()
    mark = spec["mark"]
    assert (mark if isinstance(mark, str) else mark["type"]) == "line"
    assert spec["encoding"]["y"]["field"] == "BSR"
    assert spec["encoding"]["strokeDash"]["field"] == "Тип"


def test_the_forecast_tab_shows_charts_not_a_raw_table(dash, monkeypatch):  # noqa: F811
    """Владелец попросил графики вместо таблицы чисел (25.09.2026)."""
    monkeypatch.setattr(dash, "load_snapshots", lambda: rows([300, 200, 100]))
    at = run()
    assert not at.exception
    assert at.get("vega_lite_chart"), "на вкладке должен быть хотя бы один график"
    assert any(sb.key == "forecast_asin_pick" for sb in at.selectbox)
    assert not any("BSR сейчас" in str(df.value) for df in at.dataframe), (
        "числовая таблица прогноза должна быть заменена графиками"
    )
