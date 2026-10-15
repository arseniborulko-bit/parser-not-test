"""Раздел «Best Seller подкатегории» — как в макете, переключатель «Мониторинг BSR | Best Seller подкатегории» над
дашбордом. Его видят все вошедшие сотрудники; по умолчанию открыт мониторинг с прежними вкладками.
Streamlit AppTest, без Google и настоящей базы."""

import access
from test_dashboard_access import PUBLIC_TABS, dash, employee, run, sign_in  # noqa: F401

SECTIONS = ["Мониторинг BSR", "Best Seller подкатегории"]
ANNA = "anna@maximumstores.online"


def section_switch(at):
    return [r for r in at.radio if r.key == "top_section"]


def test_every_employee_opens_monitoring_by_default_and_can_switch(monkeypatch, dash):  # noqa: F811
    # Как на бою: обычный сотрудник — редактор, не админ.
    monkeypatch.setattr(dash, "EMPLOYEE_ROLE", access.ROLE_EDITOR)
    sign_in(monkeypatch, dash, employee(ANNA))
    at = run()
    assert not at.exception
    (switch,) = section_switch(at)
    assert list(switch.options) == SECTIONS and switch.value == SECTIONS[0]
    assert [t.label for t in at.tabs] == PUBLIC_TABS

    def no_data():
        raise AssertionError("раздел Best Seller не должен грузить данные мониторинга")

    for name in ("load_current", "load_snapshots", "load_competitor_pairs"):
        monkeypatch.setattr(dash, name, no_data)
    switch.set_value(SECTIONS[1]).run(timeout=30)
    assert not at.exception
    assert not at.tabs  # вкладок мониторинга в этом разделе нет
    assert not at.error  # и мониторинг не пытался загрузить данные (дашборд показал бы ошибку загрузки)
    assert any("Поиск подкатегорий Best Seller" in m.value for m in at.markdown)


def test_view_only_employee_also_sees_the_switch(monkeypatch, dash):  # noqa: F811
    sign_in(monkeypatch, dash, employee(ANNA))  # роль в фикстуре снята: только просмотр
    at = run()
    assert not at.exception and section_switch(at)
