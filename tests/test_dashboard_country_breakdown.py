"""Карточка «Стран»: сколько ASIN приходится на каждую страну — по образцу Rating Radar."""

import pandas as pd
import pytest

from test_dashboard_access import SNAPSHOT_ROW, dash, run  # noqa: F401

import dashboard_db as dash_module


def row(market, our, comp):
    return {**SNAPSHOT_ROW, "marketplace": market, "our_asin": our, "comp_asin": comp}


FRAME = pd.DataFrame([
    row("US", "B000000001", "B000000002"),
    row("US", "B000000001", "B000000003"),  # тот же our_asin — не должен считаться дважды
    row("US", "B000000009", "B000000004"),
    row("UK", "B000000005", "B000000006"),
])


def test_counts_unique_asins_per_country_not_rows():
    breakdown = dict(dash_module._asins_per_country(FRAME))
    # US: наши {1,9} + конкуренты {2,3,4} = 5 уникальных ASIN, а не 3 строки
    assert breakdown["US"] == 5
    assert breakdown["UK"] == 2


def test_sorted_by_count_descending():
    breakdown = dash_module._asins_per_country(FRAME)
    assert breakdown[0][0] == "US"


def test_the_card_shows_the_breakdown_as_text():
    text = dash_module._country_breakdown_text(FRAME)
    assert text == "US 5 · UK 2"


def test_an_empty_frame_falls_back_to_the_generic_caption():
    assert dash_module._country_breakdown_text(FRAME.iloc[0:0]) == "маркетплейсов"


def test_a_missing_asin_does_not_count_as_a_product():
    blank = pd.DataFrame([{**SNAPSHOT_ROW, "marketplace": "DE", "our_asin": "", "comp_asin": None}])
    assert dash_module._asins_per_country(blank) == []


def test_the_breakdown_is_shown_on_the_page(dash):  # noqa: F811
    """Фикстура dash даёт одну строку US с нашим и конкурентным ASIN — 2 уникальных."""
    text = " ".join(block.value for block in run().markdown)
    assert "US 2" in text
