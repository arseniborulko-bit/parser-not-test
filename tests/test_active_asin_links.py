"""Список ссылок на все ASIN, которые сейчас в работе, по маркетплейсам — без таблицы и без
правки, только чтобы быстро открыть карточку товара (владелец, 25.09.2026: «мне нужн чисто
список сылок»)."""

import pandas as pd

from test_dashboard_access import TEAM_PASSWORD, dash, run, unlock  # noqa: F401

import pairs_ui


def pair(market="US", our="B0OURASIN1", comp="B0COMPAAA1", active=True):
    return {"marketplace": market, "our_asin": our, "our_product": "Наш",
            "comp_asin": comp, "competitor_name": "Конкурент", "active": active}


PAIRS = pd.DataFrame([
    pair(),
    pair(market="US", our="B0OURASIN1", comp="B0COMPBBB2"),  # тот же наш ASIN, другой конкурент
    pair(market="UK", our="B0OURASIN3", comp="B0COMPCCC3"),
    pair(market="UK", our="B0OURASIN3", comp="B0COMPDDD4", active=False),  # отключена — не попадает
])


def test_links_are_grouped_by_market_and_deduplicated():
    by_market = pairs_ui._active_asin_links_by_market(PAIRS)
    assert set(by_market) == {"US", "UK"}
    us_asins = [asin for asin, _ in by_market["US"]]
    assert us_asins == ["B0COMPAAA1", "B0COMPBBB2", "B0OURASIN1"]  # наш ASIN один раз, не дважды


def test_disabled_pairs_do_not_contribute_asins():
    by_market = pairs_ui._active_asin_links_by_market(PAIRS)
    uk_asins = [asin for asin, _ in by_market["UK"]]
    assert uk_asins == ["B0COMPCCC3", "B0OURASIN3"]
    assert "B0COMPDDD4" not in uk_asins


def test_links_point_to_the_right_marketplace_domain():
    by_market = pairs_ui._active_asin_links_by_market(PAIRS)
    url = dict(by_market["UK"])["B0OURASIN3"]
    assert url == "https://www.amazon.co.uk/dp/B0OURASIN3"


def test_no_active_pairs_means_no_groups():
    empty = pd.DataFrame([pair(active=False)])
    assert pairs_ui._active_asin_links_by_market(empty) == {}


def test_render_shows_an_info_message_when_nothing_is_active(monkeypatch):
    shown = []
    monkeypatch.setattr(pairs_ui.st, "info", lambda msg: shown.append(msg))
    pairs_ui.render_active_asin_links(pd.DataFrame([pair(active=False)]))
    assert shown


def test_render_builds_one_link_per_asin_html_escaped(monkeypatch):
    class FakeExpander:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    monkeypatch.setattr(pairs_ui.st, "expander", lambda label: FakeExpander())
    written = []
    monkeypatch.setattr(pairs_ui.st, "markdown", lambda html, **kwargs: written.append(html))
    pairs_ui.render_active_asin_links(PAIRS)
    text = " ".join(written)
    assert 'href="https://www.amazon.com/dp/B0OURASIN1"' in text
    assert "B0OURASIN1" in text and "B0COMPCCC3" in text


def test_the_link_list_appears_on_the_collect_tab(dash, monkeypatch):  # noqa: F811
    monkeypatch.setenv("TEAM_PASSWORD", TEAM_PASSWORD)
    at = unlock(run())
    assert not at.exception
    text = " ".join(block.value for block in at.markdown) + " ".join(
        exp.label for exp in at.expander
    )
    assert "Ссылки на все ASIN в работе" in text
