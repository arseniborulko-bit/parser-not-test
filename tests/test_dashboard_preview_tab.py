"""Вкладка в разработке «🏆 Best Seller подкатегории» видна только админам и адресам из секрета PREVIEW_EMAILS;
остальные сотрудники видят дашборд как раньше. Streamlit AppTest, без Google и настоящей базы."""

import pytest

import access
from test_dashboard_access import ADMIN_TABS, PUBLIC_TABS, dash, employee, run, sign_in  # noqa: F401

PREVIEW = "🏆 Best Seller подкатегории"
# Вторая, сразу за «Текущим состоянием»: последняя вкладка на обычном экране уходит за край ряда.
WITH_PREVIEW = PUBLIC_TABS[:1] + [PREVIEW] + PUBLIC_TABS[1:]
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


def labels(at):
    return [t.label for t in at.tabs]


def test_listed_address_sees_the_new_tab_second(monkeypatch, dash):  # noqa: F811
    monkeypatch.setenv("PREVIEW_EMAILS", f"{OWNER.upper()}, someone@gmail.com")
    sign_in(monkeypatch, dash, employee(OWNER))
    at = run()
    assert not at.exception
    assert labels(at)[: len(WITH_PREVIEW)] == WITH_PREVIEW == ADMIN_TABS
    assert any("PREVIEW_EMAILS" in i.value for i in at.tabs[1].info)


def test_admin_sees_the_new_tab_without_any_setting(monkeypatch, dash):  # noqa: F811
    monkeypatch.delenv("PREVIEW_EMAILS", raising=False)
    monkeypatch.setenv("ADMIN_EMAILS", OWNER)
    sign_in(monkeypatch, dash, employee(OWNER))
    at = run()
    assert not at.exception
    assert labels(at)[: len(WITH_PREVIEW)] == WITH_PREVIEW


def test_other_employees_see_the_dashboard_unchanged(monkeypatch, dash):  # noqa: F811
    # Как на бою: все сотрудники — редакторы, админ — только владелец.
    monkeypatch.setattr(dash, "EMPLOYEE_ROLE", access.ROLE_EDITOR)
    monkeypatch.setenv("ADMIN_EMAILS", OWNER)
    monkeypatch.setenv("PREVIEW_EMAILS", OWNER)
    sign_in(monkeypatch, dash, employee(ANNA))
    at = run()
    assert not at.exception
    assert PREVIEW not in labels(at) and labels(at)[: len(PUBLIC_TABS)] == PUBLIC_TABS
