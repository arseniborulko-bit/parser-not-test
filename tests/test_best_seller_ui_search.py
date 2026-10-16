"""Раздел Best Seller, режим «Один ASIN» с настоящим поиском: запуск, показ сохранённого результата, причины,
по которым кнопка неактивна. Поиск и база подменены — без сети."""

from datetime import datetime
from html import unescape

from streamlit.testing.v1 import AppTest

import best_seller_store as store
import best_seller_ui as ui

SCRIPT = """
import best_seller_ui
best_seller_ui.render(connect=(lambda: None), actor="Anna", can_edit=CAN_EDIT, token=TOKEN)
"""
FOUND = {
    "id": 7, "market": "US", "asin": "B0H9LK9T39", "title": "Classic Straw Hat", "our_bsr": 859857,
    "target_bsr": 60000, "status": "done", "error": "", "requests": 13, "actor": "Anna",
    "finished_at": datetime(2026, 10, 16, 18, 30),
    "own": [{"node": "9057034011", "name": "Men's Novelty Sun Hats", "rank": 8},
            {"node": "9056981011", "name": "Women's Novelty Sun Hats", "rank": 12}],
    "results": [
        {"node": "9057034011", "name": "Men's Novelty Sun Hats", "leader_bsr": 428399, "k1": True, "k2": True,
         "k3": True, "k4": True, "relevance": "relevant", "status": "suitable", "priority": "высокий", "reason": "подходит"},
        {"node": "9057030011", "name": "Men's Novelty Cowboy Hats", "leader_bsr": 420000, "k1": True, "k2": True,
         "k3": None, "k4": None, "relevance": None, "status": "pending", "priority": None,
         "reason": "нужна оценка релевантности"},
        {"node": "9056958011", "name": "Women's Novelty Accessories", "leader_bsr": 222, "k1": False, "k2": None,
         "k3": None, "k4": None, "relevance": None, "status": "rejected", "priority": None, "reason": "отклонена по К1"},
    ],
}


def page_text(at):
    return unescape(" ".join([m.value for m in at.markdown] + [c.value for c in at.caption] + [i.value for i in at.info]))


def start(monkeypatch, *, can_edit=True, token="T", found=FOUND, tables=True):
    runs = []
    monkeypatch.setattr(store, "search_tables_exist", lambda c: tables)
    monkeypatch.setattr(store, "latest_search", lambda c, m, a: found)
    monkeypatch.setattr(ui, "_run_search", lambda c, m, a, target, actor, tok: runs.append((m, a, target, actor, tok)))
    at = AppTest.from_string(SCRIPT.replace("CAN_EDIT", str(can_edit)).replace("TOKEN", repr(token)))
    return at.run(timeout=30), runs


def test_without_search_tables_the_mockup_example_stays():
    import pytest

    mp = pytest.MonkeyPatch()
    try:
        at, _ = start(mp, tables=False)
        assert "примените best_seller_db/001_watchlist.sql и 002_search.sql" in page_text(at)
        assert "×7,7" in page_text(at)  # пример из макета
    finally:
        mp.undo()


def test_search_runs_with_the_link_country_and_target(monkeypatch):
    at, runs = start(monkeypatch)
    at.text_input(key="bs_link").set_value("https://www.amazon.com/dp/B0H9LK9T39").run(timeout=30)
    at.number_input(key="bs_target").set_value(60000).run(timeout=30)
    at.button(key="bs_find").click().run(timeout=30)
    assert not at.exception
    assert runs == [("US", "B0H9LK9T39", 60000, "Anna", "T")]


def test_target_zero_means_the_current_bsr(monkeypatch):
    at, runs = start(monkeypatch)
    at.text_input(key="bs_link").set_value("https://www.amazon.de/dp/B07WVYMY7Y").run(timeout=30)
    at.button(key="bs_find").click().run(timeout=30)
    assert runs == [("DE", "B07WVYMY7Y", None, "Anna", "T")]


def test_saved_result_is_shown_instead_of_the_mockup(monkeypatch):
    at, _ = start(monkeypatch)
    at.text_input(key="bs_link").set_value("https://www.amazon.com/dp/B0H9LK9T39").run(timeout=30)
    text = page_text(at)
    for expected in ("Classic Straw Hat", "859 857", "60 000", "#8 · #12", "запросов ScrapingDog: 13",
                     "Подходят: 1", "Ждут проверки: 1", "Отклонены: 1", "×7,1", "отклонена по К1",
                     "нужна оценка релевантности"):
        assert expected in text, expected
    assert "×7,7" not in text  # не пример из макета
    assert "· 9057034011" not in text and "9057034011" in text  # без пути — без лишней точки


def test_button_is_disabled_without_link_token_or_rights(monkeypatch):
    at, _ = start(monkeypatch)
    assert at.button(key="bs_find").disabled  # нет ссылки
    at, _ = start(monkeypatch, token="")
    at.text_input(key="bs_link").set_value("https://www.amazon.com/dp/B0H9LK9T39").run(timeout=30)
    assert at.button(key="bs_find").disabled and "Нет токена ScrapingDog" in page_text(at)
    at, _ = start(monkeypatch, can_edit=False)
    at.text_input(key="bs_link").set_value("https://www.amazon.com/dp/B0H9LK9T39").run(timeout=30)
    assert at.button(key="bs_find").disabled and "правом редактирования" in page_text(at)


def test_not_needed_and_errors_and_first_time(monkeypatch):
    at, _ = start(monkeypatch, found={**FOUND, "status": "not_needed", "results": []})
    at.text_input(key="bs_link").set_value("https://www.amazon.com/dp/B0H9LK9T39").run(timeout=30)
    assert any("уже в топ-3" in s.value for s in at.success)
    at, _ = start(monkeypatch, found={**FOUND, "status": "error", "error": "ScrapingDog не вернул карточку товара"})
    at.text_input(key="bs_link").set_value("https://www.amazon.com/dp/B0H9LK9T39").run(timeout=30)
    assert any("ScrapingDog не вернул карточку" in e.value for e in at.error)
    at, _ = start(monkeypatch, found=None)
    at.text_input(key="bs_link").set_value("https://www.amazon.com/dp/B0H9LK9T39").run(timeout=30)
    assert "поиска ещё не было" in page_text(at)
