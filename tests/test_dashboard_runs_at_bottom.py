"""Порядок блоков во вкладке «Сбор и управление»: «Все ASIN» — первым, «Время сбора» — перед
«Запустить сбор», «Последние запуски» — в самом низу, после всего остального."""

from test_dashboard_access import TEAM_PASSWORD, dash, run, unlock  # noqa: F401


def test_recent_runs_come_after_the_asin_registry(dash, monkeypatch):  # noqa: F811
    monkeypatch.setenv("TEAM_PASSWORD", TEAM_PASSWORD)
    at = unlock(run())
    text = " ".join(block.value for block in at.markdown)
    assert "Последние запуски" in text
    assert "Все ASIN" in text
    assert text.index("Все ASIN") < text.index("Последние запуски"), (
        "таблица запусков должна идти ПОСЛЕ списка ASIN"
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


def test_the_asin_registry_comes_before_autocollect(dash, monkeypatch):  # noqa: F811
    """Владелец хочет видеть список ASIN раньше «Автосбор»."""
    monkeypatch.setenv("TEAM_PASSWORD", TEAM_PASSWORD)
    at = unlock(run())
    text = " ".join(block.value for block in at.markdown)
    assert "Все ASIN" in text and "Автосбор" in text
    assert text.index("Все ASIN") < text.index("Автосбор")


def test_the_asin_registry_is_hidden_from_viewers_without_edit_rights(monkeypatch, dash):  # noqa: F811
    """Список редактируется прямо там же (вписать/убрать/переименовать), поэтому, как и «Пары
    конкурентов», показывается только тем, у кого открыто «Управление» — не всем подряд.

    Ищем именно заголовок раздела, а не любое упоминание фразы «Все ASIN» — она есть и в тексте
    «Как это работает», который виден всем."""
    monkeypatch.setenv("TEAM_PASSWORD", TEAM_PASSWORD)
    at = run()
    text = " ".join(block.value for block in at.markdown)
    assert 'class="section-title">Все ASIN' not in text
