"""Раздел в разработке «Best Seller подкатегории» — как в макете, переключатель «Мониторинг BSR | Best Seller
подкатегории» над дашбордом. Его видят только админы и адреса из секрета PREVIEW_EMAILS; остальные видят дашборд
как раньше, без переключателя. Streamlit AppTest, без Google и настоящей базы."""

import pytest

import access
from test_dashboard_access import PUBLIC_TABS, dash, employee, run, sign_in  # noqa: F401

SECTIONS = ["Мониторинг BSR", "Best Seller подкатегории"]
OWNER = "owner@maximumstores.online"
ANNA = "anna@maximumstores.online"


@pytest.mark.parametrize(
    "email, emails, role, allowed",
    [
        (OWNER, {OWNER}, None, True),
        (ANNA, {OWNER}, None, False),
        (OWNER, set(), None, False),  # пустой список и не админ — не видит
        (None, {OWNER}, access.ROLE_ADMIN, False),
        (OWNER, set(), access.ROLE_ADMIN, True),  # админ видит без отдельной настройки
        (ANNA, set(), access.ROLE_EDITOR, False),  # редактор (все сотрудники на бою) — не видит
    ],
)
def test_preview_is_only_for_admins_and_listed_addresses(email, emails, role, allowed):
    assert access.preview_allowed(email, emails, role) is allowed


def section_switch(at):
    return [r for r in at.radio if r.key == "top_section"]


def test_admin_opens_monitoring_by_default_and_can_switch_to_best_seller(monkeypatch, dash):  # noqa: F811
    monkeypatch.delenv("PREVIEW_EMAILS", raising=False)
    monkeypatch.setenv("ADMIN_EMAILS", OWNER)
    sign_in(monkeypatch, dash, employee(OWNER))
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
    assert any("PREVIEW_EMAILS" in i.value for i in at.info)


def test_listed_address_sees_the_switch(monkeypatch, dash):  # noqa: F811
    monkeypatch.setenv("PREVIEW_EMAILS", f"{OWNER.upper()}, someone@gmail.com")
    sign_in(monkeypatch, dash, employee(OWNER))
    at = run()
    assert not at.exception and section_switch(at)


def test_other_employees_see_the_dashboard_unchanged(monkeypatch, dash):  # noqa: F811
    # Как на бою: все сотрудники — редакторы, админ — только владелец.
    monkeypatch.setattr(dash, "EMPLOYEE_ROLE", access.ROLE_EDITOR)
    monkeypatch.setenv("ADMIN_EMAILS", OWNER)
    monkeypatch.setenv("PREVIEW_EMAILS", OWNER)
    sign_in(monkeypatch, dash, employee(ANNA))
    at = run()
    assert not at.exception
    assert not section_switch(at)
    assert [t.label for t in at.tabs] == PUBLIC_TABS
