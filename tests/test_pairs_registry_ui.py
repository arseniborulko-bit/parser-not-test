"""Справочник ASIN в интерфейсе: вписать и убрать, с запасным вариантом без миграции."""

import pandas as pd
import pytest

import asins_store
import pairs_ui

ROWS = [
    {"marketplace": "US", "asin": "B0OURASIN1", "name": "Наш товар", "kind": "ours", "active": True},
    {"marketplace": "DE", "asin": "B0COMPAAA1", "name": "", "kind": "competitor", "active": False},
]


@pytest.fixture
def state(monkeypatch):
    store = {}
    monkeypatch.setattr(pairs_ui.st, "session_state", store, raising=False)
    return store


def test_writing_in_asins_reports_what_happened(state, monkeypatch):
    monkeypatch.setattr(asins_store, "add_asins",
                        lambda *a, **k: {"added": 2, "restored": 1, "skipped": 3})
    pairs_ui._add_asins_callback(None, "US", "B0AAAAAAAA", "competitor", "Тест", "editor")
    level, text = state["pairs_flash"]
    assert level == "success"
    assert "добавлено 2" in text and "возвращено 1" in text and "уже было 3" in text


def test_a_refusal_is_shown_as_is(state, monkeypatch):
    def refuse(*args, **kwargs):
        raise ValueError("В тексте нет ни одного ASIN.")

    monkeypatch.setattr(asins_store, "add_asins", refuse)
    pairs_ui._add_asins_callback(None, "US", "мусор", "competitor", "Тест", "editor")
    assert state["pairs_flash"] == ("error", "В тексте нет ни одного ASIN.")


def test_a_database_failure_does_not_pretend_to_have_saved(state, monkeypatch):
    def broken(*args, **kwargs):
        raise asins_store.AsinStoreError("Справочник недоступен.")

    monkeypatch.setattr(asins_store, "add_asins", broken)
    pairs_ui._add_asins_callback(None, "US", "B0AAAAAAAA", "competitor", "Тест", "editor")
    assert state["pairs_flash"][0] == "error"


def test_removing_marks_inactive_and_reports_the_count(state, monkeypatch):
    seen = {}

    def drop(connect, keys, active, *, actor_role, actor):
        seen["keys"] = list(keys)
        seen["active"] = active
        return len(keys)

    monkeypatch.setattr(asins_store, "set_active", drop)
    pairs_ui._drop_asins_callback(None, [("US", "B0OURASIN1")], "Тест", "editor")
    assert seen["active"] is False
    assert state["pairs_flash"] == ("success", "Убрано ASIN: 1.")


def test_without_the_migration_the_store_version_steps_aside(monkeypatch):
    """Миграции 008 может не быть на боевой базе — интерфейс обязан обойтись, а не упасть."""
    monkeypatch.setattr(asins_store, "registry_exists", lambda connect: False)
    assert pairs_ui._render_registry_from_store(None, "Тест", "editor") is False


def test_with_the_migration_the_store_version_takes_over(monkeypatch):
    monkeypatch.setattr(asins_store, "registry_exists", lambda connect: True)
    monkeypatch.setattr(asins_store, "load_asins", lambda connect: [])
    calls = []
    monkeypatch.setattr(pairs_ui, "_render_add_asin_form", lambda *a: calls.append(a))
    monkeypatch.setattr(pairs_ui.st, "info", lambda *a, **k: None)
    assert pairs_ui._render_registry_from_store(None, "Тест", "editor") is True
    assert calls, "форма «вписать» должна рисоваться и при пустом справочнике"


def test_the_asin_column_is_its_own_clickable_link():
    """ASIN сам по себе — ссылка на карточку товара (собранный адрес, всегда открывается),
    а «Ссылка» — отдельная колонка с исходным, как её вписали."""
    import inspect

    source = inspect.getsource(pairs_ui._render_registry_from_store)
    assert '"ASIN": st.column_config.LinkColumn("ASIN", display_text=_ASIN_LINK_TEXT' in source
    assert '"Ссылка": st.column_config.LinkColumn("Ссылка", display_text="открыть"' in source
    assert '"ASIN": shown["asin"],' not in source, "ASIN должен быть ссылкой, а не голым текстом"


def test_an_unreachable_registry_falls_back_instead_of_showing_an_error(monkeypatch):
    """Справочник — надстройка: вместо ошибки во вкладке показываем список, собранный из пар.
    Настоящий сбой базы виден выше, на уровне всей страницы."""
    def broken(connect):
        raise asins_store.AsinStoreError("Справочник недоступен.")

    shown = []
    monkeypatch.setattr(asins_store, "registry_exists", broken)
    monkeypatch.setattr(pairs_ui.st, "error", lambda text: shown.append(text))
    assert pairs_ui._render_registry_from_store(None, "Тест", "editor") is False
    assert shown == [], "ошибку во вкладке показывать не нужно"
