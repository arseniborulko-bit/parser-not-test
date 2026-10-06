"""Вкладка «Прогноз» (forecast_ui.py): группа товаров — история, сравнение, прогноз BSR.

BSR — чем меньше, тем лучше: «хуже порога» — BSR больше порога. Прогноз — продолжение тренда (медиана
дневных изменений) до общей целевой даты; на малой истории он не строится и не выдумывает данные.
"""

from datetime import date, timedelta

import numpy as np
import pandas as pd
import pytest
from streamlit.testing.v1 import AppTest

import forecast_ui as fc
from test_dashboard_access import SCRIPT, SNAPSHOT_ROW, dash, run  # noqa: F401

FIRST = date(2026, 9, 1)


def snap(day, market, our, our_bsr, comp, comp_bsr, our_name="Наш", comp_name="Конкурент", reviews=None):
    return {**SNAPSHOT_ROW, "snapshot_date": FIRST + timedelta(days=day), "marketplace": market,
            "our_asin": our, "our_product": our_name, "our_bsr": our_bsr,
            "comp_asin": comp, "competitor_name": comp_name, "comp_bsr": comp_bsr,
            "our_reviews_count": reviews, "comp_reviews_count": None}


def history(days=20):
    """US: наш A (BSR растёт = хуже) против C1 (стабилен) и C2 (улучшается); DE: наш B против C3 (мало замеров)."""
    rows = []
    for d in range(days):
        rows.append(snap(d, "US", "B0OURA0001", 50_000 + 4_000 * d, "B0COMP0001", 20_000, "Наш А", "Конкурент 1",
                         reviews=100 + d))
        rows.append(snap(d, "US", "B0OURA0001", 50_000 + 4_000 * d, "B0COMP0002", 300_000 - 5_000 * d,
                         "Наш А", "Конкурент 2"))
        rows.append(snap(d, "DE", "B0OURB0001", 9_000, "B0COMP0003", 40_000 if d >= days - 2 else None,
                         "Наш Б", "Конкурент 3"))
    return pd.DataFrame(rows)


def settings(end=FIRST + timedelta(days=19), days=30, horizon=14, threshold=100_000):
    return fc.Settings(start=end - timedelta(days=days - 1), end=end, horizon=horizon, threshold=threshold)


@pytest.fixture
def prepared():
    return fc.prepare(history())


# --- Тренд ---

def test_a_falling_bsr_gives_a_negative_trend():
    days = pd.Series(pd.to_datetime(["2026-09-18", "2026-09-19", "2026-09-20"]))
    assert fc.trend(days, pd.Series([300, 200, 100])) == pytest.approx(-100.0)


def test_one_wild_day_does_not_become_the_trend():
    """На боевых данных BSR скачет на сотни тысяч за сутки — медиана не даёт выбросу задать тренд."""
    days = pd.Series(pd.date_range("2026-09-16", periods=5))
    assert abs(fc.trend(days, pd.Series([100_000, 99_000, 500_000, 97_000, 96_000]))) < 5_000


def test_gaps_in_days_do_not_distort_the_slope():
    days = pd.Series(pd.to_datetime(["2026-09-10", "2026-09-12", "2026-09-14"]))
    assert fc.trend(days, pd.Series([1000, 800, 600])) == pytest.approx(-100.0)


# --- Каталог и замеры ---

def test_prepare_counts_each_product_once_and_keeps_its_role(prepared):
    catalog, daily = prepared
    assert sorted(catalog["key"]) == ["DE|B0COMP0003", "DE|B0OURB0001", "US|B0COMP0001", "US|B0COMP0002", "US|B0OURA0001"]
    kinds = dict(zip(catalog["key"], catalog["kind"]))
    assert kinds["US|B0OURA0001"] == "наш" and kinds["US|B0COMP0001"] == "конкурент"
    rivals = dict(zip(catalog["key"], catalog["rivals"]))
    assert rivals["US|B0OURA0001"] == ["US|B0COMP0001", "US|B0COMP0002"]
    # Наш А в двух парах — но один замер на день, а не два.
    assert len(daily[daily["key"] == "US|B0OURA0001"]) == 20


def test_missing_bsr_stays_missing_not_zero(prepared):
    _, daily = prepared
    c3 = daily[daily["key"] == "DE|B0COMP0003"]
    assert c3["bsr"].isna().sum() == 18 and not (c3["bsr"] == 0).any()


def test_the_same_asin_in_two_countries_is_two_products():
    data = pd.DataFrame([snap(0, "US", "B0X", 100, "B0Y", 200), snap(0, "UK", "B0X", 900, "B0Y", 800)])
    catalog, _ = fc.prepare(data)
    assert sorted(catalog["key"]) == ["UK|B0X", "UK|B0Y", "US|B0X", "US|B0Y"]


# --- Анализ группы ---

def test_analyze_reports_change_forecast_and_risks(prepared):
    catalog, daily = prepared
    table = fc.analyze(catalog, daily, list(catalog["key"]), settings()).set_index("key")
    ours = table.loc["US|B0OURA0001"]
    assert ours["current"] == 50_000 + 4_000 * 19
    assert ours["change_pct"] == pytest.approx((126_000 - 50_000) / 50_000 * 100)
    assert ours["below"] and ours["falling"] and ours["risk"] == fc.RISK_NOW
    # Прогноз — до общей даты: расчёт 20.09 + 14 дней.
    assert ours["forecast"] == pytest.approx(126_000 + 4_000 * 14)
    assert ours["reviews"] == 119
    improving = table.loc["US|B0COMP0002"]  # 300 000 → 205 000: улучшается, но всё ещё хуже порога
    assert improving["change_pct"] < 0 and not improving["falling"] and improving["risk"] == fc.RISK_NOW
    assert table.loc["US|B0COMP0001", "risk"] == fc.RISK_NONE


def test_a_product_that_will_cross_the_threshold_is_a_new_risk(prepared):
    catalog, daily = prepared
    table = fc.analyze(catalog, daily, ["US|B0OURA0001"], settings(threshold=150_000)).iloc[0]
    assert not table["below"] and table["forecast_risk"] and table["risk"] == fc.RISK_FORECAST


def test_a_short_history_gets_no_forecast_and_says_why(prepared):
    catalog, daily = prepared
    row = fc.analyze(catalog, daily, ["DE|B0COMP0003"], settings()).iloc[0]
    assert np.isnan(row["forecast"]) and row["forecast_note"] == "мало замеров за 14 дн.: 2 из 3"
    assert row["current"] == 40_000


def test_the_forecast_targets_the_same_date_even_if_the_last_measurement_is_older():
    data = pd.DataFrame([snap(d, "US", "B0X", 1000 + 100 * d, "B0Y", 500) for d in range(5)]
                        + [snap(d, "US", "B0Z", 300, "B0Y", 500) for d in range(5, 10)])
    catalog, daily = fc.prepare(data)
    s = settings(end=FIRST + timedelta(days=9), horizon=7)
    row = fc.analyze(catalog, daily, ["US|B0X"], s).iloc[0]
    # Последний замер B0X — 05.09 (BSR 1400), до цели 17.09 — 12 дней по +100.
    assert row["last_date"] == pd.Timestamp(FIRST + timedelta(days=4))
    assert row["forecast"] == pytest.approx(1400 + 100 * 12)


def test_a_product_without_any_bsr_is_no_data_not_zero():
    data = pd.DataFrame([snap(d, "US", "B0X", None, "B0Y", 500) for d in range(5)])
    catalog, daily = fc.prepare(data)
    row = fc.analyze(catalog, daily, ["US|B0X"], settings(end=FIRST + timedelta(days=4))).iloc[0]
    assert np.isnan(row["current"]) and row["risk"] == fc.RISK_NO_DATA and row["forecast_note"] == "нет замеров BSR"


def test_a_trend_that_would_push_bsr_below_one_gives_no_forecast():
    data = pd.DataFrame([snap(d, "US", "B0X", 500 - 200 * d if d < 3 else 100, "B0Y", 5) for d in range(3)])
    catalog, daily = fc.prepare(data)
    row = fc.analyze(catalog, daily, ["US|B0X"], settings(end=FIRST + timedelta(days=2), horizon=30)).iloc[0]
    assert np.isnan(row["forecast"]) and "ниже 1" in row["forecast_note"]


def test_problem_keys_metrics_and_distribution(prepared):
    catalog, daily = prepared
    table = fc.analyze(catalog, daily, list(catalog["key"]), settings())
    problems = set(fc.problem_keys(table))
    assert "US|B0OURA0001" in problems and "US|B0COMP0001" not in problems
    metrics = fc.group_metrics(table)
    assert metrics["total"] == 5 and metrics["with_data"] == 5
    assert metrics["below"] == int(table["below"].sum())
    dist = fc.distribution(table, 100_000)
    assert list(dist["bucket"]) == ["Лучше 50 000", "50 000 – 100 000", "Хуже порога 100 000", "Нет данных"]
    assert dist["count"].sum() == 5


def test_sorting_puts_risks_first_and_filters_work(prepared):
    catalog, daily = prepared
    table = fc.analyze(catalog, daily, list(catalog["key"]), settings())
    assert list(fc.sort_table(table, "Сначала риски")["risk"]) == sorted(table["risk"])
    assert set(fc.TABLE_FILTERS["Хуже порога"](table)["key"]) == set(table.loc[table["below"], "key"])
    assert list(fc.search(catalog, "конкурент 2")["key"]) == ["US|B0COMP0002"]
    assert list(fc.search(catalog, "b0ourb")["key"]) == ["DE|B0OURB0001"]


# --- Точки графика ---

def test_a_missed_day_breaks_the_line_instead_of_bridging_it():
    data = pd.DataFrame([snap(d, "US", "B0X", 1000, "B0Y", 5) for d in (0, 1, 2, 5, 6, 7)])
    catalog, daily = fc.prepare(data)
    s = settings(end=FIRST + timedelta(days=7))
    points = fc.series_frame(daily, fc.analyze(catalog, daily, ["US|B0X"], s), s)
    fact = points[points["kind"] == "Факт"]
    assert fact["segment"].nunique() == 2
    forecast = points[points["kind"] == "Прогноз"]
    assert list(forecast["date"]) == [pd.Timestamp(FIRST + timedelta(days=7)), pd.Timestamp(s.target)]


def test_relative_scale_starts_at_zero(prepared):
    catalog, daily = prepared
    s = settings()
    points = fc.series_frame(daily, fc.analyze(catalog, daily, ["US|B0OURA0001"], s), s, relative=True)
    assert points[points["kind"] == "Факт"]["value"].iloc[0] == 0


# --- Вкладка ---

FORECAST = "📈 Прогноз"


@pytest.fixture
def tab(dash, monkeypatch):  # noqa: F811
    monkeypatch.setattr(dash, "load_snapshots", history)
    monkeypatch.setattr(dash, "load_current", lambda: history().tail(3))

    def open_tab(**state):
        at = AppTest.from_string(SCRIPT)
        for key, value in state.items():
            at.session_state[key] = value
        return at.run(timeout=30)
    return open_tab


def forecast_tab(at):
    return next(t for t in at.tabs if t.label == FORECAST)


def texts(at):
    tab = forecast_tab(at)
    return " ".join([m.value for m in tab.markdown] + [i.value for i in tab.info] + [c.value for c in tab.caption])


def test_the_tab_exists_between_history_and_pairs(dash):  # noqa: F811
    labels = [str(t.label) for t in run().tabs]
    assert labels.index(FORECAST) == labels.index("📅 История") + 1


def test_nothing_selected_asks_to_choose_and_shows_no_zero_ratings(tab):
    at = tab()
    assert not at.exception
    assert "Выберите товары" in texts(at)
    assert not forecast_tab(at).metric


def test_select_all_then_one_chart_for_up_to_five(tab):
    at = tab()
    next(b for b in forecast_tab(at).button if b.key == "fc_all").click()
    at.run(timeout=30)
    assert not at.exception
    labels = {m.label: m.value for m in forecast_tab(at).metric}
    assert labels["Уже хуже порога"] == "2 · 40%"  # Наш А (126 000) и Конкурент 2 (205 000)
    assert "Выбрано: 5 из 5" in texts(at)
    assert "больше 5" not in texts(at)


def test_more_than_five_products_switch_to_small_multiples(tab, dash, monkeypatch):  # noqa: F811
    many = pd.concat([history()] + [pd.DataFrame([snap(d, "UK", f"B0UK{i:06d}", 1000 * (i + 1), "B0UKCOMP01", 7)
                                                  for d in range(20)]) for i in range(4)])
    monkeypatch.setattr(dash, "load_snapshots", lambda: many)
    at = AppTest.from_string(SCRIPT)
    at.session_state["main_tab"] = FORECAST
    at.run(timeout=30)
    next(b for b in forecast_tab(at).button if b.key == "fc_all").click()
    at.run(timeout=30)
    assert not at.exception
    assert "больше 5, поэтому отдельные графики" in texts(at)


def test_select_problems_takes_only_problem_products(tab):
    at = tab()
    next(b for b in forecast_tab(at).button if b.key == "fc_problems").click()
    at.run(timeout=30)
    chosen = at.session_state["fc_selected"]
    assert "US|B0OURA0001" in chosen and "US|B0COMP0001" not in chosen


def test_search_and_period_changes_keep_the_selection(tab):
    at = tab(fc_selected={"US|B0OURA0001", "US|B0COMP0001"})
    at.text_input(key="fc_search").input("Наш Б")
    at.run(timeout=30)
    at.radio(key="fc_period").set_value("7 дней")
    at.selectbox(key="fc_horizon").set_value(30)
    at.run(timeout=30)
    assert not at.exception
    assert at.session_state["fc_selected"] == {"US|B0OURA0001", "US|B0COMP0001"}


def test_switching_country_hides_products_from_others_and_says_so(tab):
    at = tab(fc_selected={"US|B0OURA0001", "DE|B0OURB0001"})
    at.selectbox(key="fc_market").set_value("DE")
    at.run(timeout=30)
    assert "Выбрано: 1 из 2" in texts(at) and "ещё 1 выбранных не подходят" in texts(at)


def test_a_product_opens_and_back_returns_to_the_same_group(tab):
    group = {"US|B0OURA0001", "US|B0COMP0001", "US|B0COMP0002"}
    at = tab(fc_selected=group, fc_product="US|B0OURA0001", fc_horizon=30)
    assert not at.exception
    text = texts(at)
    assert "Конкуренты этого товара" in text
    back = next(b for b in forecast_tab(at).button if b.key == "fc_back")
    back.click()
    at.run(timeout=30)
    assert "fc_product" not in at.session_state
    assert at.session_state["fc_selected"] == group and at.session_state["fc_horizon"] == 30


def test_product_mode_shows_one_product(tab):
    at = tab(fc_selected={"US|B0OURA0001", "US|B0COMP0001"}, fc_mode="Товар")
    assert not at.exception
    labels = [m.label for m in forecast_tab(at).metric]
    assert "BSR сейчас" in labels and "Статус" in labels


def test_portfolio_mode_shows_aggregation_and_distribution(tab):
    at = tab(fc_selected={"US|B0OURA0001", "US|B0COMP0001", "US|B0COMP0002"}, fc_mode="Портфель")
    assert not at.exception
    text = texts(at)
    assert "Медиана BSR по товарам группы" in text and "Распределение BSR" in text
    assert "Прогноз для группы не строится" in text and "Проблемные в группе" in text


def test_the_table_filter_changes_only_the_table(tab):
    at = tab(fc_selected={"US|B0OURA0001", "US|B0COMP0001", "US|B0COMP0002"})
    at.radio(key="fc_table_filter").set_value("Хуже порога")
    at.run(timeout=30)
    assert not at.exception
    assert "Показано 2 из 3 в группе" in texts(at)
    table = forecast_tab(at).dataframe[-1].value
    assert sorted(table["Товар"]) == ["Конкурент 2 (B0COMP0002)", "Наш А (B0OURA0001)"]
    # Показатели — по всей группе, а не по отфильтрованной таблице.
    assert {m.label: m.value for m in forecast_tab(at).metric}["Уже хуже порога"] == "2 · 67%"
    assert any(d.label.startswith("⬇ CSV") for d in at.get("download_button"))


def test_a_missing_forecast_is_explained(tab):
    at = tab(fc_selected={"DE|B0COMP0003"}, fc_market="DE")
    assert "Без прогноза: Конкурент 3 — мало замеров за 14 дн.: 2 из 3" in texts(at)
