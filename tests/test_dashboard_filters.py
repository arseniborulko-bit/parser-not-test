"""Фильтры общие на весь дашборд, и карточки-итоги считаются ПОСЛЕ фильтра.

Раньше карточки рисовались до вкладок, а фильтры жили внутри каждой вкладки, поэтому карточка
показывала всю базу («592 в выбранном срезе») даже когда таблица была пустой, а выбранный фильтр
терялся при переходе с «Текущего состояния» на «Историю».
"""

from datetime import date, datetime

import pandas as pd
import pytest

from test_dashboard_access import SNAPSHOT_ROW, dash, run  # noqa: F401

import dashboard_db as dash_module


def card_values(at) -> list[str]:
    """Числа из карточек-итогов в том порядке, в каком они нарисованы."""
    values = []
    for block in at.markdown:
        for part in block.value.split('class="metric-value"')[1:]:
            values.append(part.split(">", 1)[1].split("<", 1)[0])
    return values


def test_the_summary_cards_follow_the_filter(dash):  # noqa: F811
    at = run()
    assert card_values(at)[0] == "1", "без фильтра карточка показывает единственную строку"

    at.text_input(key="global_search").set_value("zzzqqq").run()
    assert card_values(at)[0] == "0", "карточка обязана реагировать на фильтр, а не показывать всю базу"


def test_one_set_of_filters_is_shared_by_both_tables(dash):  # noqa: F811
    at = run()
    keys = {widget.key for widget in at.selectbox} | {widget.key for widget in at.text_input}
    assert {"global_asin", "global_period", "global_market", "global_search"} <= keys
    assert not any(str(key).startswith(("current_", "history_")) for key in keys if key), (
        "отдельные фильтры на вкладку означают, что выбор теряется при переходе между ними"
    )


def test_the_note_above_the_tabs_no_longer_promises_filters_on_every_tab(dash):  # noqa: F811
    text = " ".join(block.value for block in run().markdown)
    assert "Фильтры ниже применяются" in text
    assert "Фильтруйте сохранённые данные по ASIN, стране и периоду" not in text


FRAME = pd.DataFrame([
    SNAPSHOT_ROW,
    {**SNAPSHOT_ROW, "marketplace": "UK", "our_asin": "B000000009", "our_product": "Other"},
    {**SNAPSHOT_ROW, "snapshot_date": date(2020, 1, 1), "our_asin": "B000000007", "our_product": "Old"},
])


def choice(**overrides):
    return dash_module.FilterChoice(**overrides)


def test_no_filter_keeps_everything():
    assert len(dash_module._apply_filter(FRAME, choice())) == 3


def test_filtering_by_asin_and_by_market():
    assert list(dash_module._apply_filter(FRAME, choice(asin="B000000009"))["marketplace"]) == ["UK"]
    assert list(dash_module._apply_filter(FRAME, choice(market="UK"))["our_asin"]) == ["B000000009"]


def test_search_looks_across_columns_and_ignores_case():
    assert list(dash_module._apply_filter(FRAME, choice(search="other"))["our_asin"]) == ["B000000009"]
    assert dash_module._apply_filter(FRAME, choice(search="zzzqqq")).empty


def test_the_period_filter_drops_old_rows():
    result = dash_module._apply_filter(FRAME, choice(period="90 дней"))
    assert date(2020, 1, 1) not in list(result["snapshot_date"])


def test_the_options_come_from_both_tables_so_history_only_values_stay_selectable():
    current = pd.DataFrame([SNAPSHOT_ROW])
    history = pd.DataFrame([{**SNAPSHOT_ROW, "marketplace": "DE"}])
    assert dash_module._options([current, history], "marketplace") == ["Все", "DE", "US"]


def test_a_filter_on_a_missing_column_does_not_crash():
    """История и «текущее» — разные таблицы; фильтр не должен падать, если колонки нет."""
    bare = pd.DataFrame([{"snapshot_date": date(2026, 9, 18)}])
    assert len(dash_module._apply_filter(bare, choice(asin="B000000001", market="US"))) == 1
