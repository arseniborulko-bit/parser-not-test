"""Порядок блоков во вкладке «Сбор и управление»: «Автосбор» — в самом верху (владелец,
25.09.2026: «это надо в самый верх чтобы стоял над добавить пару»), «Время сбора» — перед
«Запустить сбор», «Запустить сбор» — сразу над «Последние запуски» (владелец, тот же день:
«перенисти это над Последние запуски»), «Последние запуски» — в самом низу, после всего остального.

Блок «Добавить пару»/«Пары — правка и отключение» стал переключателем «Добавить новые» /
«Управлять существующими» (владелец, 25.09.2026, макет) — своих заголовков-ориентиров у него
больше нет, поэтому порядок ниже проверяется по key кнопок, а не по тексту markdown."""

from test_dashboard_access import TEAM_PASSWORD, dash, run, unlock  # noqa: F401


def _button_index(at, key):
    keys = [b.key for b in at.button]
    assert key in keys, f"кнопка с key={key!r} не найдена среди {keys}"
    return keys.index(key)


def _select_manage_mode(at, key="collect_pairs_mode"):
    """Переключает «Добавить новые» → «Управлять существующими»: это настоящий Python if/else,
    а не st.tabs (тот путал бы at.tabs верхнего уровня) — вторая ветка рисуется только после
    переключения и повторного прогона."""
    control = at.segmented_control(key=key)
    manage_option = next(o for o in control.options if o.startswith("☰ Управлять"))
    control.set_value(manage_option)
    at.run(timeout=30)
    return at


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
    labels = [b.label for b in at.button]
    assert "Сохранить" in labels, "кнопка сохранения времени сбора не найдена"
    assert labels.index("Сохранить") < _button_index(at, "collect_add")


def test_start_collection_moved_right_above_recent_runs(dash, monkeypatch):  # noqa: F811
    """Владелец попросил перенести «Запустить сбор» вниз, над «Последние запуски» (25.09.2026) —
    раньше блок стоял вверху рядом с «Автосбор»."""
    monkeypatch.setenv("TEAM_PASSWORD", TEAM_PASSWORD)
    at = unlock(run())
    text = " ".join(block.value for block in at.markdown)
    assert "Запустить сбор" in text and "Последние запуски" in text
    assert text.index("Запустить сбор") < text.index("Последние запуски")
    # «Добавить новые» (вид по умолчанию) стоит перед «Запустить сбор» — тот же порядок,
    # что и раньше, только заголовки сменились переключателем.
    assert _button_index(at, "collect_add") < _button_index(at, "collect_run")


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
    assert "collect_add" in [b.key for b in at.button], "вид «Добавить новые» не нарисован по умолчанию"

    _select_manage_mode(at)
    assert not at.exception
    text = " ".join(block.value for block in at.markdown)
    assert "Все ASIN" not in text
    assert not any("Все ASIN" in expander.label for expander in at.expander)
    assert not any(widget.label == "Вписать ASIN или ссылки" for widget in at.text_input)
    assert not any(widget.label == "Показывать убранные" for widget in at.checkbox)


def test_the_pair_grid_on_the_collect_tab_shows_editable_rows(dash, monkeypatch):  # noqa: F811
    monkeypatch.setenv("TEAM_PASSWORD", TEAM_PASSWORD)
    at = unlock(run())
    _select_manage_mode(at)
    grids = [d.value for d in at.dataframe if "Активна" in getattr(d.value, "columns", [])]
    assert grids, "сетка правки пар не нарисована"
    # Фикстура даёт один наш товар — строка сетки схлопывается до одного ASIN конкурента
    # (owner: «чтобы на одной строчке был один асин»), «Наш ASIN» уходит в заголовок над таблицей.
    assert "ASIN конкурента" in grids[0].columns
    assert "Наш ASIN" not in grids[0].columns
