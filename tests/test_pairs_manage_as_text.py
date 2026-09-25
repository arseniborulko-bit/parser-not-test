"""«Управлять существующими» — та же форма, что и «Добавить новые», только заполненная текущими
парами: одна строка на товар в «Наш товар», его конкуренты через запятую той же строкой в
«Конкуренты» (владелец, 25.09.2026, с примером от начальника). Стереть строку/ссылку и сохранить —
отключает; дописать и сохранить — добавляет. Без отдельной таблицы/сетки."""

import pandas as pd
import pytest

import pairs_ui


def pair(market="US", our="B0OURASIN1", comp="B0COMPAAA1", active=True):
    return {"marketplace": market, "our_asin": our, "our_product": "Наш",
            "comp_asin": comp, "competitor_name": "Конкурент", "active": active}


def test_manage_text_groups_competitors_onto_the_matching_products_line():
    pairs = pd.DataFrame([
        pair(our="B0OURASIN1", comp="B0COMPAAA1"),
        pair(our="B0OURASIN1", comp="B0COMPAAA2"),
        pair(our="B0OURASIN2", comp="B0COMPBBB1"),
    ])
    our_text, comp_text, original = pairs_ui._manage_text_from_pairs(pairs)
    our_lines = our_text.splitlines()
    comp_lines = comp_text.splitlines()
    assert our_lines == ["https://www.amazon.com/dp/B0OURASIN1", "https://www.amazon.com/dp/B0OURASIN2"]
    # конкуренты первого товара — оба, через запятую, на его же строке (индекс 0)
    assert "B0COMPAAA1" in comp_lines[0] and "B0COMPAAA2" in comp_lines[0]
    assert "B0COMPBBB1" in comp_lines[1]
    assert original == {
        ("US", "B0OURASIN1"): {"B0COMPAAA1", "B0COMPAAA2"},
        ("US", "B0OURASIN2"): {"B0COMPBBB1"},
    }


def test_manage_text_excludes_disabled_pairs():
    pairs = pd.DataFrame([pair(active=False)])
    our_text, comp_text, original = pairs_ui._manage_text_from_pairs(pairs)
    assert our_text == "" and comp_text == "" and original == {}


def test_manage_text_on_an_empty_frame():
    assert pairs_ui._manage_text_from_pairs(pd.DataFrame(columns=["marketplace", "our_asin", "comp_asin", "active"])) \
        == ("", "", {})


def test_parse_manage_text_pairs_lines_by_position():
    parsed = pairs_ui._parse_manage_text(
        "https://www.amazon.com/dp/B0OURASIN1\nhttps://www.amazon.com/dp/B0OURASIN2",
        "https://www.amazon.com/dp/B0COMPAAA1, https://www.amazon.com/dp/B0COMPAAA2\n"
        "https://www.amazon.com/dp/B0COMPBBB1",
    )
    assert parsed == {
        ("US", "B0OURASIN1"): {"B0COMPAAA1", "B0COMPAAA2"},
        ("US", "B0OURASIN2"): {"B0COMPBBB1"},
    }


def test_parse_manage_text_skips_an_unrecognized_our_line():
    parsed = pairs_ui._parse_manage_text("not a link", "https://www.amazon.com/dp/B0COMPAAA1")
    assert parsed == {}


def test_parse_manage_text_a_product_with_no_matching_competitor_line_gets_an_empty_set():
    parsed = pairs_ui._parse_manage_text("https://www.amazon.com/dp/B0OURASIN1", "")
    assert parsed == {("US", "B0OURASIN1"): set()}


class Recorder:
    def __init__(self):
        self.state = {}
        self.disable_calls = []
        self.apply_calls = []

    def set_pairs_active(self, connect, keys, active, *, actor_role, actor):
        self.disable_calls.append((sorted(keys), active))
        return len(keys)

    def apply_plan(self, connect, plan, *, actor_role, actor):
        self.apply_calls.append(plan)
        return {"add": len(plan.to_add), "enable": len(plan.to_enable)}


@pytest.fixture
def recorder(monkeypatch):
    rec = Recorder()
    monkeypatch.setattr(pairs_ui.st, "session_state", rec.state, raising=False)
    monkeypatch.setattr(pairs_ui.pairs_store, "set_pairs_active", rec.set_pairs_active)
    monkeypatch.setattr(pairs_ui.pairs_store, "apply_plan", rec.apply_plan)
    monkeypatch.setattr(pairs_ui.st, "cache_data", type("C", (), {"clear": staticmethod(lambda: None)}))
    return rec


def test_erasing_a_competitor_line_disables_just_that_pair(recorder, monkeypatch):
    """Стереть один ASIN конкурента из строки и сохранить — отключает только эту пару, остальные
    конкуренты того же товара не трогаются."""
    recorder.state["collect_manage_original"] = {("US", "B0OURASIN1"): {"B0COMPAAA1", "B0COMPAAA2"}}
    recorder.state["collect_manage_our"] = "https://www.amazon.com/dp/B0OURASIN1"
    recorder.state["collect_manage_comps"] = "https://www.amazon.com/dp/B0COMPAAA1"  # AAA2 стёрли

    def fake_make_plans(connect, our_text, comp_text, *, max_active):
        return [pairs_ui.pairs_store.Plan(market="US", our_asin="B0OURASIN1", already=["B0COMPAAA1"])]

    monkeypatch.setattr(pairs_ui.pairs_store, "make_plans", fake_make_plans)
    pairs_ui._manage_callback(None, "Тест", "editor", 1500, "collect")

    assert recorder.disable_calls == [([("US", "B0OURASIN1", "B0COMPAAA2")], False)]
    assert "collect_manage_original" not in recorder.state
    level, text = recorder.state["collect_flash"]
    assert level == "success" and "отключено 1" in text


def test_removing_a_whole_product_line_disables_all_its_competitors(recorder, monkeypatch):
    recorder.state["collect_manage_original"] = {
        ("US", "B0OURASIN1"): {"B0COMPAAA1"},
        ("US", "B0OURASIN2"): {"B0COMPBBB1"},
    }
    recorder.state["collect_manage_our"] = "https://www.amazon.com/dp/B0OURASIN1"  # ASIN2 стёрли целиком
    recorder.state["collect_manage_comps"] = "https://www.amazon.com/dp/B0COMPAAA1"

    def fake_make_plans(connect, our_text, comp_text, *, max_active):
        return [pairs_ui.pairs_store.Plan(market="US", our_asin="B0OURASIN1", already=["B0COMPAAA1"])]

    monkeypatch.setattr(pairs_ui.pairs_store, "make_plans", fake_make_plans)
    pairs_ui._manage_callback(None, "Тест", "editor", 1500, "collect")

    assert recorder.disable_calls == [([("US", "B0OURASIN2", "B0COMPBBB1")], False)]


def test_adding_a_new_competitor_line_applies_the_new_plan(recorder, monkeypatch):
    recorder.state["collect_manage_original"] = {("US", "B0OURASIN1"): {"B0COMPAAA1"}}
    recorder.state["collect_manage_our"] = "https://www.amazon.com/dp/B0OURASIN1"
    recorder.state["collect_manage_comps"] = "https://www.amazon.com/dp/B0COMPAAA1, https://www.amazon.com/dp/B0NEW0001"

    new_plan = pairs_ui.pairs_store.Plan(market="US", our_asin="B0OURASIN1", to_add=["B0NEW0001"], already=["B0COMPAAA1"])

    def fake_make_plans(connect, our_text, comp_text, *, max_active):
        return [new_plan]

    monkeypatch.setattr(pairs_ui.pairs_store, "make_plans", fake_make_plans)
    pairs_ui._manage_callback(None, "Тест", "editor", 1500, "collect")

    assert recorder.disable_calls == []  # ничего не убирали
    assert recorder.apply_calls == [new_plan]
    level, text = recorder.state["collect_flash"]
    assert level == "success" and "добавлено 1" in text


def test_a_plan_that_cannot_apply_is_not_sent_to_apply_plan(recorder, monkeypatch):
    broken_plan = pairs_ui.pairs_store.Plan(market="US", our_asin="B0OURASIN1", errors=["что-то не так"])
    monkeypatch.setattr(pairs_ui.pairs_store, "make_plans", lambda *a, **k: [broken_plan])
    recorder.state["collect_manage_original"] = {}
    recorder.state["collect_manage_our"] = "https://www.amazon.com/dp/B0OURASIN1"
    recorder.state["collect_manage_comps"] = ""

    pairs_ui._manage_callback(None, "Тест", "editor", 1500, "collect")

    assert recorder.apply_calls == []
    level, text = recorder.state["collect_flash"]
    assert level == "success" and text == "Ничего не изменилось."


def test_manage_flash_uses_the_shared_key_prefix_flash(recorder, monkeypatch):
    """«Добавить новые» и «Управлять существующими» — две ветки одного переключателя, рисуются
    по очереди, не разом, поэтому им ничего не мешает делить один и тот же flash-ключ (в отличие
    от случая, когда два разных блока показываются на странице одновременно)."""
    monkeypatch.setattr(pairs_ui.pairs_store, "make_plans", lambda *a, **k: [])
    recorder.state["collect_manage_original"] = {}
    recorder.state["collect_manage_our"] = ""
    recorder.state["collect_manage_comps"] = ""
    pairs_ui._manage_callback(None, "Тест", "editor", 1500, "collect")
    assert "collect_flash" in recorder.state
