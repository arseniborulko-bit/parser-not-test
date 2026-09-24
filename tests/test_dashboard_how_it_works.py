"""Раздел «Как это работает»: свёрнутый и с верным описанием системы.

Владелец не хочет пояснений, разбросанных по интерфейсу, но этот раздел попросил сам. Поэтому он
один, свёрнут по умолчанию и лежит во вкладке управления, а не над данными.
"""

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
    assert "Больше не собираются" in text
    assert "не удаляются никогда" in text


@pytest.mark.parametrize("fact", [
    "ASIN × даты",             # сводная таблица в «Истории»
    "выбор дня",               # срез за одну дату
    "Все ASIN",                # общий список со ссылками
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
