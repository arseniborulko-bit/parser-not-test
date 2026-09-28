"""Pivot-таблица «Пары конкурентов»: несколько конкурентов одного товара — одна группа, не дубли строк."""

from datetime import date, timedelta

import pandas as pd

from test_dashboard_access import dash, run  # noqa: F401

OUR = "B000000001"
COMP_A = "B000000002"
COMP_B = "B000000003"

YESTERDAY = date.today() - timedelta(days=1)
TODAY = date.today()

SNAPSHOTS = pd.DataFrame([
    {
        "snapshot_date": YESTERDAY, "marketplace": "US", "currency": "USD", "our_asin": OUR,
        "our_product": "Our product", "our_price": 10.0, "our_bsr": 100, "our_bsr_delta_24h": 0,
        "comp_asin": COMP_A, "competitor_name": "Alpha", "comp_price": 9.0, "comp_bsr": 200,
        "comp_bsr_delta_24h": 0, "comp_stock": "In Stock", "price_diff_pct": 11.1,
        "updated_at": pd.Timestamp(YESTERDAY) + pd.Timedelta(hours=10),
    },
    {
        "snapshot_date": TODAY, "marketplace": "US", "currency": "USD", "our_asin": OUR,
        "our_product": "Our product", "our_price": 10.0, "our_bsr": 90, "our_bsr_delta_24h": -10,
        "comp_asin": COMP_A, "competitor_name": "Alpha", "comp_price": 9.0, "comp_bsr": 150,
        "comp_bsr_delta_24h": -50, "comp_stock": "In Stock", "price_diff_pct": 11.1,
        "updated_at": pd.Timestamp(TODAY) + pd.Timedelta(hours=10),
    },
    {
        "snapshot_date": TODAY, "marketplace": "US", "currency": "USD", "our_asin": OUR,
        "our_product": "Our product", "our_price": 10.0, "our_bsr": 90, "our_bsr_delta_24h": -10,
        "comp_asin": COMP_B, "competitor_name": "Beta", "comp_price": 12.0, "comp_bsr": 300,
        "comp_bsr_delta_24h": 20, "comp_stock": "In Stock", "price_diff_pct": -16.7,
        "updated_at": pd.Timestamp(TODAY) + pd.Timedelta(hours=10),
    },
])
PAIRS = pd.DataFrame([
    {"marketplace": "US", "our_asin": OUR, "our_product": "Our product", "comp_asin": COMP_A,
     "competitor_name": "Alpha", "active": True},
    {"marketplace": "US", "our_asin": OUR, "our_product": "Our product", "comp_asin": COMP_B,
     "competitor_name": "Beta", "active": True},
])


def pivot_html(at):
    return [m.value for m in at.markdown if "cmp-wrap" in m.value][0]


def test_two_competitors_of_one_product_form_one_group_not_two_rows(monkeypatch, dash):  # noqa: F811
    monkeypatch.setattr(dash, "load_current", lambda: SNAPSHOTS)
    monkeypatch.setattr(dash, "load_snapshots", lambda: SNAPSHOTS)
    monkeypatch.setattr(dash, "load_competitor_pairs", lambda: PAIRS)
    at = run()
    assert not at.exception
    html = pivot_html(at)
    # Заголовок группы («наш товар · наш ASIN · N конкурент(ов)») встречается один раз,
    # а не по разу на каждого конкурента — это и есть замена дублирующимся строкам.
    assert html.count("Our product") >= 1
    assert html.count(f"· {OUR} · US · 2 конкурент") == 1
    # Оба конкурента внутри этой же одной группы, не в отдельных повторах шапки.
    assert f"/dp/{COMP_A}" in html and f"/dp/{COMP_B}" in html
    assert f"/dp/{OUR}" in html


def test_country_filter_narrows_the_pivot(monkeypatch, dash):  # noqa: F811
    two_markets = pd.concat([
        SNAPSHOTS,
        SNAPSHOTS.assign(marketplace="DE"),
    ], ignore_index=True)
    two_market_pairs = pd.concat([PAIRS, PAIRS.assign(marketplace="DE")], ignore_index=True)
    monkeypatch.setattr(dash, "load_current", lambda: two_markets)
    monkeypatch.setattr(dash, "load_snapshots", lambda: two_markets)
    monkeypatch.setattr(dash, "load_competitor_pairs", lambda: two_market_pairs)
    at = run()
    assert not at.exception
    market_select = [s for s in at.selectbox if s.key == "cmp_market"][0]
    assert market_select.options == ["DE", "US"]
