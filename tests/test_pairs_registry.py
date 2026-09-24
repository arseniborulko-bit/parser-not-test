"""Список всех ASIN: каждый ASIN один раз, «стереть» = отключить все его пары.

В базе ASIN сам по себе не хранится — он существует только внутри пары, поэтому список
собирается из пар, а удаление переводится в отключение пар.
"""

import pandas as pd
import pytest

import pairs_ui


def pair(market, our, comp, active=True, our_name="Наш", comp_name="Конкурент"):
    return {"marketplace": market, "our_asin": our, "our_product": our_name,
            "comp_asin": comp, "competitor_name": comp_name, "active": active}


PAIRS = pd.DataFrame([
    pair("US", "B0OURASIN1", "B0COMPAAA1"),
    pair("US", "B0OURASIN1", "B0COMPBBB2"),
    pair("DE", "B0OURASIN1", "B0COMPCCC3"),
    pair("US", "B0OURASIN2", "B0COMPAAA1", active=False),
])


def test_each_asin_appears_once_per_market():
    registry = pairs_ui._asin_registry(PAIRS)
    keys = list(zip(registry["marketplace"], registry["asin"]))
    assert len(keys) == len(set(keys))
    assert ("US", "B0OURASIN1") in keys and ("DE", "B0OURASIN1") in keys


def test_it_counts_how_many_pairs_the_asin_takes_part_in():
    registry = pairs_ui._asin_registry(PAIRS).set_index(["marketplace", "asin"])
    assert registry.loc[("US", "B0OURASIN1"), "pairs"] == 2
    assert registry.loc[("US", "B0OURASIN1"), "active_pairs"] == 2
    # B0COMPAAA1 в US участвует в двух парах, но одна из них отключена
    assert registry.loc[("US", "B0COMPAAA1"), "pairs"] == 2
    assert registry.loc[("US", "B0COMPAAA1"), "active_pairs"] == 1


def test_our_products_and_competitors_are_marked():
    registry = pairs_ui._asin_registry(PAIRS).set_index(["marketplace", "asin"])
    assert registry.loc[("US", "B0OURASIN1"), "role"] == "наш"
    assert registry.loc[("US", "B0COMPBBB2"), "role"] == "конкурент"


def test_an_empty_list_does_not_break():
    assert pairs_ui._asin_registry(PAIRS.iloc[0:0]).empty


class Recorder:
    def __init__(self):
        self.keys = None
        self.state = {}

    def __call__(self, connect, keys, active, *, actor_role, actor):
        self.keys = list(keys)
        self.active = active
        return len(self.keys)


@pytest.fixture
def toggler(monkeypatch):
    recorder = Recorder()
    monkeypatch.setattr(pairs_ui.pairs_store, "set_pairs_active", recorder)
    monkeypatch.setattr(pairs_ui.st, "session_state", recorder.state, raising=False)
    return recorder


def test_erasing_an_asin_disables_every_active_pair_it_is_in(toggler):
    pairs_ui._erase_callback(None, PAIRS, [("US", "B0OURASIN1")], "Тест", "editor")
    assert toggler.active is False
    assert sorted(toggler.keys) == [
        ("US", "B0OURASIN1", "B0COMPAAA1"),
        ("US", "B0OURASIN1", "B0COMPBBB2"),
    ], "пара того же ASIN в другой стране не должна задеваться"


def test_erasing_a_competitor_disables_only_its_own_pairs(toggler):
    pairs_ui._erase_callback(None, PAIRS, [("US", "B0COMPBBB2")], "Тест", "editor")
    assert toggler.keys == [("US", "B0OURASIN1", "B0COMPBBB2")]


def test_already_disabled_pairs_are_not_touched_again(toggler):
    pairs_ui._erase_callback(None, PAIRS, [("US", "B0OURASIN2")], "Тест", "editor")
    assert toggler.keys is None
    assert toggler.state["pairs_flash"][0] == "info"


def test_a_store_error_is_shown_and_nothing_is_claimed(monkeypatch):
    state = {}
    monkeypatch.setattr(pairs_ui.st, "session_state", state, raising=False)

    def refuse(*args, **kwargs):
        raise ValueError("За один раз можно изменить не больше 200 пар.")

    monkeypatch.setattr(pairs_ui.pairs_store, "set_pairs_active", refuse)
    pairs_ui._erase_callback(None, PAIRS, [("US", "B0OURASIN1")], "Тест", "editor")
    assert state["pairs_flash"][0] == "error"
    assert "не больше 200" in state["pairs_flash"][1]


def test_a_large_erase_asks_for_confirmation():
    import inspect

    source = inspect.getsource(pairs_ui._render_registry)
    assert "СТЕРЕТЬ" in source
    assert "_CONFIRM_ABOVE" in source
