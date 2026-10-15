"""Вкладка «🏆 Best Seller подкатегории» по макету: обе страницы рисуются, таблица подкатегорий считается
правилами К1–К4, фильтры списка работают, неподключённые действия ничего не делают, а только объясняют это.
Streamlit AppTest, без сети и базы."""

from html import unescape

import pytest
from streamlit.testing.v1 import AppTest

import best_seller
import best_seller_ui as ui

SCRIPT = "import best_seller_ui\nbest_seller_ui.render()"


def page_text(at):
    # Текст как его видит человек: HTML-сущности (апостроф в «Women's») раскрыты.
    return unescape(" ".join([m.value for m in at.markdown] + [c.value for c in at.caption]))


@pytest.mark.parametrize("ratio, text", [(460_779 / 60_000, "×7,7"), (77_590 / 60_000, "×1,3"),
                                          (35_598 / 60_000, "×0,6"), (358 / 60_000, "×0,006"), (None, "–")])
def test_ratio_is_formatted_like_the_mockup(ratio, text):
    assert ui.fmt_ratio(ratio) == text


def test_demo_subcategories_go_through_the_real_rules_in_the_mockup_order():
    evaluated = ui.evaluate_demo(ui.DEMO_LISTING, ui.DEMO_SUBCATEGORIES)
    assert [sub.name for sub, _ in evaluated] == [
        "Men's Novelty Sun Hats", "Women's Ice Skating Clothing", "Women's Novelty Sun Hats", "Men's Hats & Caps",
        "Women's Sport Headbands"]
    assert [v.priority or v.reason for _, v in evaluated] == [
        best_seller.HIGH, best_seller.LOW, "отклонена по К1", "отклонена по К1", "отклонена по К1"]


@pytest.mark.parametrize(
    "query, group, status, asins",
    [
        ("", "Все группы", "Все", [r.asin for r in ui.DEMO_BATCH]),
        ("", "Все группы", "С подходящими", ["B0GNBSDL65", "B0XXXXXX03", "B0XXXXXX04", "B0XXXXXX05"]),
        ("", "Все группы", "Без подходящих", ["B0XXXXXX06"]),
        ("", "Все группы", "Ошибки", ["B0XXXXXX07"]),
        ("", "beanie", "Все", ["B0GNBSDL65", "B0XXXXXX06"]),
        (" b0dg2 ", "Все группы", "Все", ["B0DG2B7KJN"]),
    ],
)
def test_batch_filters(query, group, status, asins):
    assert [r.asin for r in ui.filter_batch(ui.DEMO_BATCH, query, group, status)] == asins


def test_single_asin_page_matches_the_mockup():
    at = AppTest.from_string(SCRIPT).run(timeout=30)
    assert not at.exception
    text = page_text(at)
    for expected in ("Поиск подкатегорий Best Seller", "91 233", "61 496 – 101 435", "55 000 – 60 000", "#166 · #191",
                     "Подходят: 2", "Отклонены: 3", "×7,7", "×0,006", "отклонена по К1", "Выбрано: 1"):
        assert expected in text, expected
    # Выбрать можно только подходящие подкатегории; по умолчанию отмечена та, что с высоким приоритетом.
    picks = [c for c in at.checkbox if c.key.startswith("bs_pick_")]
    assert [c.value for c in picks] == [True, False, False, False, False]
    assert [c.disabled for c in picks] == [False, False, True, True, True]


def test_unconnected_actions_only_explain_themselves():
    at = AppTest.from_string(SCRIPT).run(timeout=30)
    at.button(key="bs_find").click().run(timeout=30)
    assert not at.exception
    assert [t.value for t in at.toast] == [ui.NOT_CONNECTED]


def test_nothing_selected_disables_writing_to_the_sheet():
    at = AppTest.from_string(SCRIPT).run(timeout=30)
    first = next(c for c in at.checkbox if c.key.startswith("bs_pick_"))
    first.uncheck().run(timeout=30)
    assert at.button(key="bs_write_one").disabled
    assert "Выбрано: 0" in page_text(at)


def test_list_page_matches_the_mockup_and_filters():
    at = AppTest.from_string(SCRIPT).run(timeout=30)
    at.radio(key="bs_mode").set_value("Список ASIN").run(timeout=30)
    assert not at.exception
    text = page_text(at)
    for expected in ("Поиск подкатегорий: список ASIN", "Обработано 19 из 28", "Women's Cold Weather Neck Gaiters",
                     "ошибка запроса", "повторить", "Показано 8 из 28"):
        assert expected in text, expected
    at.radio(key="bs_batch_status").set_value("Ошибки").run(timeout=30)
    text = page_text(at)
    assert "Показано 1 из 28" in text and "B0XXXXXX07" in text and "B0GNBSDL65" not in text
