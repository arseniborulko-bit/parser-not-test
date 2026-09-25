"""Объединённая сетка пар: название и активность правятся вместе, сохранение одной кнопкой.

Владелец попросил форму управления парами прямо на вкладке «Сбор и управление» — тот же
компонент, что и в «Пары конкурентов», но доступный из обеих вкладок без конфликта виджетов.
"""

import pandas as pd
import pytest

import access
import pairs_ui


def pair(market="US", our="B0OURASIN1", comp="B0COMPAAA1", our_name="Наш", comp_name="Конкурент", active=True):
    return {"marketplace": market, "our_asin": our, "our_product": our_name,
            "comp_asin": comp, "competitor_name": comp_name, "active": active}


PAIRS = pd.DataFrame([
    pair(),
    pair(market="UK", our="B0OURASIN2", comp="B0COMPBBB2", active=False),
])


class Recorder:
    def __init__(self):
        self.state = {}
        self.name_calls = []
        self.active_calls = []

    def edit_pair_names(self, connect, key, our, comp, *, actor_role, actor):
        self.name_calls.append((key, our, comp))
        return 1

    def set_pairs_active(self, connect, keys, active, *, actor_role, actor):
        self.active_calls.append((list(keys), active))
        return len(keys)


@pytest.fixture
def recorder(monkeypatch):
    rec = Recorder()
    monkeypatch.setattr(pairs_ui.st, "session_state", rec.state, raising=False)
    monkeypatch.setattr(pairs_ui.pairs_store, "edit_pair_names", rec.edit_pair_names)
    monkeypatch.setattr(pairs_ui.pairs_store, "set_pairs_active", rec.set_pairs_active)
    monkeypatch.setattr(pairs_ui.st, "cache_data", type("C", (), {"clear": staticmethod(lambda: None)}))
    return rec


def test_a_name_change_and_an_active_change_are_saved_together(recorder):
    pairs_ui._save_pairs_grid_callback(
        None,
        [(("US", "B0OURASIN1", "B0COMPAAA1"), "Новое имя", "Конкурент")],
        {True: [], False: [("UK", "B0OURASIN2", "B0COMPBBB2")]},
        "Тест", "editor",
    )
    assert recorder.name_calls == [(("US", "B0OURASIN1", "B0COMPAAA1"), "Новое имя", "Конкурент")]
    assert recorder.active_calls == [([("UK", "B0OURASIN2", "B0COMPBBB2")], False)]
    level, text = recorder.state["pairs_flash"]
    assert level == "success"
    assert "названий 1" in text and "статусов 1" in text


def test_an_empty_active_group_is_not_sent(recorder):
    pairs_ui._save_pairs_grid_callback(None, [], {True: [], False: []}, "Тест", "editor")
    assert recorder.active_calls == []


def test_a_failure_partway_reports_what_was_already_saved(monkeypatch, recorder):
    def fail_on_active(connect, keys, active, *, actor_role, actor):
        raise ValueError("За один раз можно изменить не больше 500 ASIN.")

    monkeypatch.setattr(pairs_ui.pairs_store, "set_pairs_active", fail_on_active)
    pairs_ui._save_pairs_grid_callback(
        None,
        [(("US", "B0OURASIN1", "B0COMPAAA1"), "Ок", "Ок")],
        {True: [], False: [("UK", "B0OURASIN2", "B0COMPBBB2")]},
        "Тест", "editor",
    )
    level, text = recorder.state["pairs_flash"]
    assert level == "error"
    assert "Сохранено строк: 1" in text


def test_the_asin_columns_in_the_grid_are_links_and_are_not_editable():
    """Ключ пары не редактируется: сменить ASIN здесь означало бы другую пару без истории."""
    for collapse in (False, True):
        _, disabled, config = pairs_ui._pairs_grid_table(PAIRS, collapse)
        assert "ASIN конкурента" in disabled
        assert isinstance(config["ASIN конкурента"], type(pairs_ui.st.column_config.LinkColumn("x")))
        assert isinstance(config["Активна"], type(pairs_ui.st.column_config.CheckboxColumn("x")))


def test_a_single_product_shows_one_row_per_competitor_asin():
    """Владелец: «чтобы на одной строчке был один асин» — раньше строка показывала пару ASIN
    (наш + конкурента), при одном нашем товаре это дублирование убираем."""
    one_product = PAIRS[PAIRS["our_asin"] == "B0OURASIN1"]
    assert pairs_ui._pairs_grid_is_single_product(one_product) is True
    table, disabled, config = pairs_ui._pairs_grid_table(one_product, True)
    assert list(table.columns) == ["Страна", "ASIN конкурента", "Конкурент", "Активна"]
    assert disabled == ["Страна", "ASIN конкурента"]
    assert "Наш ASIN" not in config and "Наш товар" not in config


def test_several_products_keep_both_asin_columns():
    assert pairs_ui._pairs_grid_is_single_product(PAIRS) is False
    table, disabled, config = pairs_ui._pairs_grid_table(PAIRS, False)
    assert list(table.columns) == ["Страна", "Наш ASIN", "Наш товар", "ASIN конкурента", "Конкурент", "Активна"]
    assert disabled == ["Страна", "Наш ASIN", "ASIN конкурента"]
    assert "Наш ASIN" in config and "Наш товар" in config


def test_pair_toggle_options_label_shows_market_competitor_and_status():
    options = pairs_ui._pair_toggle_options(PAIRS)
    assert options["US · B0COMPAAA1 — Конкурент (включена)"] == ("US", "B0OURASIN1", "B0COMPAAA1", True)
    assert options["UK · B0COMPBBB2 — Конкурент (выключена)"] == ("UK", "B0OURASIN2", "B0COMPBBB2", False)


def test_single_pair_pick_returns_the_selected_pair_and_its_state(monkeypatch):
    """Выбор — отдельно от кнопки: саму кнопку теперь рисует _render_pairs_grid, в одном ряду
    с массовыми кнопками (владелец, 25.09.2026: «сделай так чтобы они были возле друг друга»)."""
    monkeypatch.setattr(pairs_ui.st, "selectbox", lambda label, options, key=None: options[0])
    monkeypatch.setattr(pairs_ui.st, "markdown", lambda *a, **k: None)

    active_only = PAIRS[PAIRS["active"]]
    picked = pairs_ui._render_single_pair_pick(active_only, "collect")
    assert picked == ("US", "B0OURASIN1", "B0COMPAAA1", True)

    inactive_only = PAIRS[~PAIRS["active"]]
    picked = pairs_ui._render_single_pair_pick(inactive_only, "collect")
    assert picked == ("UK", "B0OURASIN2", "B0COMPBBB2", False)


def test_no_pairs_means_no_toggle_pick(monkeypatch):
    calls = []
    monkeypatch.setattr(pairs_ui.st, "selectbox", lambda *a, **k: calls.append(1))
    picked = pairs_ui._render_single_pair_pick(PAIRS.iloc[0:0], "collect")
    assert picked is None
    assert calls == []  # пустой список — селектор не рисуется вовсе


def test_the_single_toggle_button_sits_in_the_same_row_as_the_bulk_buttons():
    """Три кнопки — одна выбранная пара и обе массовые — в одном st.columns(3), не в двух рядах."""
    import inspect

    source = inspect.getsource(pairs_ui._render_pairs_grid)
    assert "st.columns(3)" in source
    assert "_render_single_pair_pick" in source


def test_a_large_disable_batch_needs_no_typed_confirmation():
    """Владелец сперва попросил сделать заметнее подтверждение текстом «УБРАТЬ», а затем прямо
    попросил его убрать целиком (25.09.2026, скриншот с полем ввода + «убере это»). Кнопки массового
    отключения теперь работают сразу, без набора слова и без _confirm_bulk (функция удалена)."""
    import inspect

    source = inspect.getsource(pairs_ui._render_pairs_grid)
    assert "_confirm_bulk" not in source
    assert "Введите «УБРАТЬ»" not in source
    assert not hasattr(pairs_ui, "_confirm_bulk")


def test_render_pairs_management_covers_both_add_and_grid():
    import inspect

    source = inspect.getsource(pairs_ui.render_pairs_management)
    assert "_render_add" in source
    assert "_render_pairs_grid" in source


def test_flash_messages_use_a_prefixed_key_so_they_show_on_the_right_tab(recorder):
    """Реальный баг: сообщения об успехе/ошибке использовали общий ключ pairs_flash, который
    показывается только на «Пары конкурентов» — действия на «Сбор и управление» проходили молча.
    Действие с key_prefix="collect" не должно попадать под "pairs_flash"."""
    pairs_ui._save_pairs_grid_callback(
        None,
        [(("US", "B0OURASIN1", "B0COMPAAA1"), "Новое имя", "Конкурент")],
        {True: [], False: []},
        "Тест", "editor", key_prefix="collect",
    )
    assert "collect_flash" in recorder.state
    assert "pairs_flash" not in recorder.state


def test_the_collect_tab_actually_displays_its_own_flash(recorder, monkeypatch):
    """render_pairs_management обязан показать сообщение своей вкладки, а не оставить его висеть
    в session_state до следующего прогона «Пары конкурентов»."""
    recorder.state["collect_flash"] = ("success", "Готово: добавлено 2.")
    shown = []
    monkeypatch.setattr(pairs_ui.st, "success", lambda msg: shown.append(msg))
    monkeypatch.setattr(pairs_ui.st, "markdown", lambda *a, **k: None)
    monkeypatch.setattr(pairs_ui, "_render_add", lambda *a: None)
    monkeypatch.setattr(pairs_ui, "_render_pairs_grid", lambda *a: None)
    pairs_ui.render_pairs_management(None, PAIRS, "Тест", "editor", 1000, key_prefix="collect")
    assert shown == ["Готово: добавлено 2."]
    assert "collect_flash" not in recorder.state


def test_widget_keys_differ_by_prefix_so_two_tabs_do_not_clash():
    """Список показывается и на «Пары конкурентов», и на «Сбор и управление» одновременно —
    без разных префиксов Streamlit падает с StreamlitDuplicateElementKey (было найдено на живом сайте)."""
    import inspect

    add_source = inspect.getsource(pairs_ui._render_add)
    grid_source = inspect.getsource(pairs_ui._render_pairs_grid)
    assert "key_prefix" in add_source and "key_prefix" in grid_source
    assert 'key="pairs_our"' not in add_source
    assert 'key="pairs_registry_search"' not in grid_source
