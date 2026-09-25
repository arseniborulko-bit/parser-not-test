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


def test_confirm_bulk_shows_a_warning_and_checks_the_typed_word(monkeypatch):
    """Раньше это была обычная серая подпись рядом с отключённой кнопкой — владелец принял такой
    блок за отсутствие кнопки вовсе (скриншот, 25.09.2026). Теперь это st.warning (жёлтый блок)."""
    warnings = []
    monkeypatch.setattr(pairs_ui.st, "warning", lambda msg: warnings.append(msg))
    monkeypatch.setattr(pairs_ui.st, "text_input", lambda *a, **k: "убрать")  # регистр не важен
    assert pairs_ui._confirm_bulk("Отключится 100 пар.", key="x") is True
    assert warnings == ["Отключится 100 пар."]

    monkeypatch.setattr(pairs_ui.st, "text_input", lambda *a, **k: "")
    assert pairs_ui._confirm_bulk("Отключится 100 пар.", key="x") is False


def test_a_large_disable_batch_needs_confirmation():
    """Подтверждение вынесено в _confirm_bulk — жёлтым предупреждением, а не серой подписью,
    которую владелец принял за отсутствие кнопки (25.09.2026)."""
    import inspect

    source = inspect.getsource(pairs_ui._render_pairs_grid)
    assert "_CONFIRM_ABOVE" in source
    assert "_confirm_bulk" in source
    assert "УБРАТЬ" in inspect.getsource(pairs_ui._confirm_bulk)
    assert "st.warning" in inspect.getsource(pairs_ui._confirm_bulk)


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
