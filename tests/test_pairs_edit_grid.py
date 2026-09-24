"""Правка названий сеткой: много строк за одно нажатие «Сохранить»."""

import pandas as pd
import pytest

import pairs_ui


class Recorder:
    def __init__(self):
        self.calls = []
        self.state = {}

    def __call__(self, connect, key, our_product, competitor_name, *, actor_role, actor):
        self.calls.append((key, our_product, competitor_name, actor_role, actor))
        return 1


@pytest.fixture
def saver(monkeypatch):
    recorder = Recorder()
    monkeypatch.setattr(pairs_ui.pairs_store, "edit_pair_names", recorder)
    monkeypatch.setattr(pairs_ui.st, "session_state", recorder.state, raising=False)
    return recorder


KEY_A = ("US", "B0OURASIN1", "B0COMPAAA1")
KEY_B = ("DE", "B0OURASIN2", "B0COMPBBB2")


def test_every_changed_row_is_saved_in_one_click(saver):
    changes = [(KEY_A, "Новое наше", "Новый конкурент"), (KEY_B, "Второе", "Тоже")]
    pairs_ui._edit_callback(None, changes, "Тест", "editor")
    assert [call[0] for call in saver.calls] == [KEY_A, KEY_B]
    assert saver.state["pairs_flash"] == ("success", "Сохранено строк: 2.")


def test_nothing_changed_means_nothing_is_written(saver):
    pairs_ui._edit_callback(None, [], "Тест", "editor")
    assert saver.calls == []
    assert saver.state["pairs_flash"][0] == "info"


def test_an_error_partway_reports_what_was_already_saved(saver, monkeypatch):
    """Иначе после ошибки непонятно, сохранилось что-то или нет."""
    def fail_on_second(connect, key, our, comp, *, actor_role, actor):
        if key == KEY_B:
            raise ValueError("Название длиннее 200 символов.")
        saver.calls.append((key, our, comp, actor_role, actor))
        return 1

    monkeypatch.setattr(pairs_ui.pairs_store, "edit_pair_names", fail_on_second)
    pairs_ui._edit_callback(None, [(KEY_A, "Ок", "Ок"), (KEY_B, "я" * 300, "")], "Тест", "editor")
    level, text = saver.state["pairs_flash"]
    assert level == "error"
    assert "Сохранено строк: 1" in text and "длиннее" in text


def test_the_asin_columns_are_links_and_cannot_be_edited():
    """Ключ пары не редактируется: сменить ASIN здесь означало бы другую пару без истории."""
    import inspect

    source = inspect.getsource(pairs_ui._render_edit)
    assert 'disabled=["Страна", "Наш ASIN", "ASIN конкурента"]' in source
    assert "LinkColumn" in source
    assert '"Наш товар": st.column_config.TextColumn' in source


def test_the_grid_is_capped_so_a_huge_list_stays_usable():
    assert pairs_ui.MAX_EDIT_ROWS == 200
    import inspect

    source = inspect.getsource(pairs_ui._render_edit)
    assert "head(MAX_EDIT_ROWS)" in source
    assert "уточните поиск" in source
