"""Раздел «Как это работает»: свёрнутый и с верным описанием системы.

Владелец не хочет пояснений, разбросанных по интерфейсу, но этот раздел попросил сам. Поэтому он
один, свёрнут по умолчанию и лежит во вкладке управления, а не над данными.
"""

import pandas as pd
import pytest

from test_dashboard_access import dash, run  # noqa: F401

import dashboard_db as dash_module


def test_it_is_a_tab_of_its_own_right_after_the_management_tab(dash):  # noqa: F811
    """Владелец хочет видеть, как пользоваться, рядом со «Сбор и управление», а не внутри неё."""
    at = run()
    labels = [str(tab.label) for tab in at.tabs]
    assert "ℹ️ Как это работает" in labels, labels
    assert labels.index("ℹ️ Как это работает") == labels.index("⚙ Сбор и управление") + 1


def test_it_is_the_only_explanatory_text_on_screen(dash):  # noqa: F811
    """Пояснения живут внутри этого раздела, а не подписями под элементами."""
    text = " ".join(block.value for block in run().markdown)
    for note in ("Фильтры ниже применяются", "Фильтруйте сохранённые данные"):
        assert note not in text, note


@pytest.mark.parametrize("fact", [
    "чем **меньше**, тем лучше",       # BSR
    "один раз в сутки",                 # защита от повторных трат
    "не больше\nтрёх",                  # лимит попыток
    "каждые 5 минут",                   # внешний будильник
    "«не раньше»",                      # время старта
])
def test_the_text_states_the_rules_the_system_actually_follows(fact):
    assert fact in dash_module._HOW_IT_WORKS, fact


def test_the_difference_between_the_at_sign_and_an_empty_cell_is_explained():
    """Владелец намеренно оставил «@» как сигнал: раздел обязан объяснять, чем он отличается от пустоты."""
    text = dash_module._HOW_IT_WORKS
    assert "«@»" in text
    assert "пустая ячейка" in text and "не проверяли" in text


def test_it_explains_that_history_outlives_a_disabled_pair():
    text = dash_module._HOW_IT_WORKS
    assert "Вернуть отключённые" in text
    assert "Все ASIN" not in text
    assert "не удаляются никогда" in text


@pytest.mark.parametrize("fact", [
    "ASIN × даты",             # сводная таблица в «Истории»
    "выбор дня",               # срез за одну дату
    "Исправить названия",      # правка сеткой
    "отметить несколько",      # множественный выбор стран
])
def test_the_text_covers_what_the_dashboard_actually_has_now(fact):
    """Раздел устаревает молча: тут перечислено то, что появилось после его первой версии."""
    assert fact in dash_module._HOW_IT_WORKS, fact


def test_it_does_not_still_say_below_now_that_it_is_a_separate_tab(dash):  # noqa: F811
    """Пока раздел лежал внутри «Сбор и управление», «ниже» было правдой. Теперь это другая вкладка."""
    text = dash_module._HOW_IT_WORKS
    assert "задаётся ниже" not in text
    assert "видны ниже" not in text


def test_bold_markdown_becomes_html_and_everything_else_is_escaped():
    html = dash_module._markdown_bold_to_html("BSR — чем **меньше**, тем лучше <script>")
    assert "<b>меньше</b>" in html
    assert "<script>" not in html  # экранирован, а не вставлен как есть
    assert "&lt;script&gt;" in html


def test_sections_are_parsed_out_of_how_it_works_one_per_paragraph():
    """Карточки строятся из того же текста, что проверяют тесты выше — не из отдельной копии."""
    sections = dash_module._how_it_works_sections()
    titles = [title for title, _ in sections]
    assert "Откуда берутся данные" in titles
    assert "Что не пропадает" in titles
    # у каждого раздела есть непустой текст, а заголовок не утащил в себя весь абзац
    assert all(title and body for title, body in sections)
    assert all(len(title) < 60 for title, _ in sections)


def test_the_flow_diagram_shows_live_numbers_not_placeholders():
    pairs = pd.DataFrame([
        {"marketplace": "US", "active": True}, {"marketplace": "US", "active": False},
    ])
    data = pd.DataFrame([{"snapshot_date": "2026-09-25", "marketplace": "US"}])
    html = dash_module._how_it_works_flow(pairs, data)
    assert "1 активных из 2" in html
    assert "25.09.2026" in html
    assert "1 строк" in html and "1 стран" in html


def test_the_tab_renders_the_new_design_without_crashing(dash):  # noqa: F811
    at = run()
    assert not at.exception
    text = " ".join(block.value for block in at.markdown)
    assert "how-title" in text or "Как это работает" in text
    assert "Порядок работы" in text
