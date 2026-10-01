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
    assert any(ms.key == "forecast_asins_Все страны" for ms in at.multiselect)
    assert not any("BSR сейчас" in str(df.value) for df in at.dataframe), (
        "числовая таблица прогноза должна быть заменена графиками"
    )


def two_markets_with_names():
    us = rows([300, 200, 100])
    us["our_product"] = "Коврик для йоги"
    ca = rows([500, 400, 300], asin="B0CAASIN01", comp="B0CACOMP01")
    ca["marketplace"] = "CA"
    ca["competitor_name"] = "Чужой коврик"
    ca["comp_bsr"] = [900, 800, 700]
    return pd.concat([us, ca], ignore_index=True)


def asin_picker(at, market="Все страны"):
    return [ms for ms in at.multiselect if ms.key == f"forecast_asins_{market}"][0]


@pytest.fixture
def drawn(dash, monkeypatch):  # noqa: F811
    """Ряды, которые ушли в линейный график: сколько разных ASIN на нём нарисовано."""
    seen = []
    real = dash._asin_forecast_line_chart

    def spy(series):
        seen.append(series)
        return real(series)

    monkeypatch.setattr(dash, "_asin_forecast_line_chart", spy)
    monkeypatch.setattr(dash, "load_snapshots", two_markets_with_names)
    return lambda: set(seen[-1]["Метка"]) if seen else set()


def test_the_country_picker_narrows_the_asin_list_to_that_country(dash, monkeypatch):  # noqa: F811
    """Владелец, 01.10.2026: «нужно чтобы можно было находить по стране»."""
    monkeypatch.setattr(dash, "load_snapshots", two_markets_with_names)
    at = run()
    assert not at.exception
    country = [sb for sb in at.selectbox if sb.key == "forecast_market"][0]
    assert country.options == ["Все страны", "CA", "US"]
    assert len(asin_picker(at).options) == 3
    country.set_value("CA")
    at.run(timeout=30)
    assert not at.exception
    assert sorted(asin_picker(at, "CA").options) == [
        "CA · B0CAASIN01 · Our", "CA · B0CACOMP01 · Чужой коврик",
    ]


def test_asins_can_be_found_by_product_name(dash, monkeypatch):  # noqa: F811
    monkeypatch.setattr(dash, "load_snapshots", two_markets_with_names)
    at = run()
    assert "US · B000000001 · Коврик для йоги" in asin_picker(at).options


def test_several_asins_can_be_picked_and_each_gets_its_own_line(drawn):
    at = run()
    picker = asin_picker(at)
    picker.set_value(picker.options[:2])
    at.run(timeout=30)
    assert not at.exception
    assert drawn() == set(picker.options[:2])


def test_all_asins_of_a_country_can_be_picked_at_once(drawn):
    """Владелец, 01.10.2026: «выбирать все асины»."""
    at = run()
    [sb for sb in at.selectbox if sb.key == "forecast_market"][0].set_value("CA")
    at.run(timeout=30)
    box = [cb for cb in at.checkbox if cb.key == "forecast_all_CA"][0]
    assert box.label == "Выбрать все (2)"
    box.check()
    at.run(timeout=30)
    assert not at.exception
    assert not [ms for ms in at.multiselect if ms.key == "forecast_asins_CA"]
    assert drawn() == {"CA · B0CAASIN01 · Our", "CA · B0CACOMP01 · Чужой коврик"}


def test_an_empty_pick_asks_to_choose_instead_of_drawing_nothing(dash, monkeypatch):  # noqa: F811
    monkeypatch.setattr(dash, "load_snapshots", two_markets_with_names)
    at = run()
    asin_picker(at).set_value([])
    at.run(timeout=30)
    assert any("Выберите хотя бы один ASIN" in info.value for info in at.info)


def test_the_leaders_chart_follows_the_country():
    table = dash_module._forecast_table(two_markets_with_names(), 30, 7)
    assert set(table["Страна"]) == {"US", "CA"}


def test_many_lines_use_a_log_scale_and_never_plot_zero():
    """BSR разных ASIN отличается в сотни раз; прогноз, упёршийся в ноль, на лог-шкале — единица."""
    data = two_markets_with_names()
    series = dash_module._forecast_series_many(
        data, 30, 30, {("US", "B000000001"): "a", ("CA", "B0CAASIN01"): "b"},
    )
    assert set(series["Метка"]) == {"a", "b"}
    spec = dash_module._asin_forecast_line_chart(series).to_dict()
    assert spec["encoding"]["y"]["scale"]["type"] == "log"
    assert spec["encoding"]["color"]["field"] == "Метка"
    plotted = [row["BSR"] for values in spec["datasets"].values() for row in values]
    assert min(plotted) >= 1


def test_the_many_series_matches_the_single_series_for_one_asin():
    data = two_markets_with_names()
    one = dash_module._asin_forecast_series(data, 30, 7, "B0CAASIN01", "CA")
    many = dash_module._forecast_series_many(data, 30, 7, {("CA", "B0CAASIN01"): "x"})
    assert list(many["BSR"]) == list(one["BSR"])
    assert list(many["Тип"]) == list(one["Тип"])


def test_a_long_product_name_is_shortened_in_the_label():
    label = dash_module._forecast_label("US", "B000000001", "я" * 100)
    assert label.startswith("US · B000000001 · ") and label.endswith("…")
    assert len(label.split(" · ", 2)[2]) == dash_module._FORECAST_NAME_LENGTH
    assert dash_module._forecast_label("US", "B000000001", None) == "US · B000000001"
