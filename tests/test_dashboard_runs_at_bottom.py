"""Порядок блоков во вкладке «Сбор и управление»: «Автосбор» — в самом верху (владелец,
25.09.2026: «это надо в самый верх чтобы стоял над добавить пару»), «Время сбора» — перед
«Запустить сбор», «Последние запуски» — в самом низу, после всего остального."""

from test_dashboard_access import TEAM_PASSWORD, dash, run, unlock  # noqa: F401


def test_recent_runs_come_after_pair_management(dash, monkeypatch):  # noqa: F811
    monkeypatch.setenv("TEAM_PASSWORD", TEAM_PASSWORD)
    at = unlock(run())
    text = " ".join(block.value for block in at.markdown)
    assert "Последние запуски" in text
    assert "Пары — правка и отключение" in text
    assert text.index("Пары — правка и отключение") < text.index("Последние запуски"), (
        "таблица запусков должна идти ПОСЛЕ управления парами"
    )


def test_recent_runs_are_visible_to_a_viewer_without_edit_rights(dash):  # noqa: F811
    """Раньше таблица рисовалась до проверки прав; вынос в конец не должен был это сломать."""
    at = run()
    text = " ".join(block.value for block in at.markdown)
    assert "Последние запуски" in text


def test_the_schedule_time_field_comes_before_start_collection(dash, monkeypatch):  # noqa: F811
    """Владелец хочет видеть «Время сбора (по Киеву)» раньше кнопок «Запустить сбор»."""
    monkeypatch.setenv("TEAM_PASSWORD", TEAM_PASSWORD)
    at = unlock(run())
    text = " ".join(block.value for block in at.markdown)
    assert "Автосбор" in text and "Запустить сбор" in text
    assert text.index("Автосбор") < text.index("Запустить сбор")


def test_autocollect_comes_before_pair_management(dash, monkeypatch):  # noqa: F811
    """Владелец попросил поднять «Автосбор» в самый верх, над «Добавить пару» (25.09.2026)."""
    monkeypatch.setenv("TEAM_PASSWORD", TEAM_PASSWORD)
    at = unlock(run())
    text = " ".join(block.value for block in at.markdown)
    assert "Автосбор" in text and "Добавить пару" in text and "Пары — правка и отключение" in text
    assert text.index("Автосбор") < text.index("Добавить пару")
    assert text.index("Автосбор") < text.index("Пары — правка и отключение")


def test_the_asin_registry_is_hidden_from_viewers_without_edit_rights(monkeypatch, dash):  # noqa: F811
    """Удалённый список ASIN не показывается и в режиме просмотра."""
    monkeypatch.setenv("TEAM_PASSWORD", TEAM_PASSWORD)
    at = run()
    text = " ".join(block.value for block in at.markdown)
    assert "Все ASIN" not in text


def test_pair_management_appears_on_the_collect_tab_without_crashing(dash, monkeypatch):  # noqa: F811
    """Владелец попросил форму управления парами прямо на «Сбор и управление» — тот же компонент,
    что уже есть на «Пары конкурентов», значит виджеты обеих копий рисуются в одном прогоне
    страницы. Раньше это падало с StreamlitDuplicateElementKey — виджеты делили одни и те же ключи."""
    monkeypatch.setenv("TEAM_PASSWORD", TEAM_PASSWORD)
    at = unlock(run())
    assert not at.exception
    text = " ".join(block.value for block in at.markdown)
    assert "Добавить пару" in text
    assert "Пары — правка и отключение" in text
    assert "Все ASIN" not in text
    assert not any("Все ASIN" in expander.label for expander in at.expander)
    assert not any(widget.label == "Вписать ASIN или ссылки" for widget in at.text_input)
    assert not any(widget.label == "Показывать убранные" for widget in at.checkbox)


def test_the_pair_grid_on_the_collect_tab_shows_editable_rows(dash, monkeypatch):  # noqa: F811
    monkeypatch.setenv("TEAM_PASSWORD", TEAM_PASSWORD)
    at = unlock(run())
    grids = [d.value for d in at.dataframe if "Активна" in getattr(d.value, "columns", [])]
    assert grids, "сетка правки пар не нарисована"
    # Фикстура даёт один наш товар — строка сетки схлопывается до одного ASIN конкурента
    # (owner: «чтобы на одной строчке был один асин»), «Наш ASIN» уходит в заголовок над таблицей.
    assert "ASIN конкурента" in grids[0].columns
    assert "Наш ASIN" not in grids[0].columns
