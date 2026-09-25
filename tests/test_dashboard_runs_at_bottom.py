"""Таблица «Последние запуски» — в самом низу вкладки «Сбор и управление», после «Больше не
собираются» и кнопки обновления, а не в середине блока «Автосбор»."""

import pandas as pd

from test_dashboard_access import TEAM_PASSWORD, dash, run, unlock  # noqa: F401


def test_recent_runs_come_after_the_retired_asins_block(dash, monkeypatch):  # noqa: F811
    import dashboard_db as dash_module

    monkeypatch.setenv("TEAM_PASSWORD", TEAM_PASSWORD)
    monkeypatch.setattr(
        dash_module, "load_retired_asins",
        lambda: pd.DataFrame([
            {"marketplace": "UK", "asin": "B0OLDOLD01", "name": "Старый", "role": "конкурент", "last_seen": None},
        ]),
    )
    at = unlock(run())
    text = " ".join(block.value for block in at.markdown)
    assert "Последние запуски" in text
    assert "Больше не собираются" in text
    assert text.index("Больше не собираются") < text.index("Последние запуски"), (
        "таблица запусков должна идти ПОСЛЕ списка отключённых ASIN"
    )


def test_recent_runs_are_visible_to_a_viewer_without_edit_rights(dash):  # noqa: F811
    """Раньше таблица рисовалась до проверки прав; вынос в конец не должен был это сломать."""
    at = run()
    text = " ".join(block.value for block in at.markdown)
    assert "Последние запуски" in text
