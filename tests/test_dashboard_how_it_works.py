"""Раздел «Как это работает»: свёрнутый и с верным описанием системы.

Владелец не хочет пояснений, разбросанных по интерфейсу, но этот раздел попросил сам. Поэтому он
один, свёрнут по умолчанию и лежит во вкладке управления, а не над данными.
"""

import pytest

from test_dashboard_access import dash, run  # noqa: F401

import dashboard_db as dash_module


def test_the_section_exists_and_is_collapsed_by_default(dash):  # noqa: F811
    import inspect

    at = run()
    titles = [str(block.label) for block in at.expander]
    assert any("Как это работает" in title for title in titles), titles
    # AppTest этой версии не отдаёт признак свёрнутости, поэтому проверяем вызов:
    # без expanded=True раздел свёрнут, иначе это снова текст поверх данных.
    source = inspect.getsource(dash_module._render_how_it_works)
    assert "expanded=True" not in source


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
