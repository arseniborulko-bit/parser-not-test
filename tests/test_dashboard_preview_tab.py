"""Вкладка в разработке «🏆 Best Seller подкатегории» видна только адресам из секрета PREVIEW_EMAILS;
остальные сотрудники видят дашборд как раньше. Streamlit AppTest, без Google и настоящей базы."""

import pytest

import access
from test_dashboard_access import PUBLIC_TABS, dash, employee, run, sign_in  # noqa: F401

PREVIEW = "🏆 Best Seller подкатегории"
OWNER = "owner@maximumstores.online"


@pytest.mark.parametrize(
    "email, emails, allowed",
    [
        (OWNER, {OWNER}, True),
        ("anna@maximumstores.online", {OWNER}, False),
        (OWNER, set(), False),  # пустой список — не видит никто
        (None, {OWNER}, False),
    ],
)
def test_preview_is_only_for_listed_addresses(email, emails, allowed):
    assert access.preview_allowed(email, emails) is allowed


def test_listed_address_sees_the_new_tab_last(monkeypatch, dash):  # noqa: F811
    monkeypatch.setenv("PREVIEW_EMAILS", f"{OWNER.upper()}, someone@gmail.com")
    sign_in(monkeypatch, dash, employee(OWNER))
    at = run()
    assert not at.exception
    assert [t.label for t in at.tabs[: len(PUBLIC_TABS) + 1]] == PUBLIC_TABS + [PREVIEW]
    assert any("PREVIEW_EMAILS" in i.value for i in at.tabs[len(PUBLIC_TABS)].info)


def test_other_employees_see_the_dashboard_unchanged(monkeypatch, dash):  # noqa: F811
    monkeypatch.setenv("PREVIEW_EMAILS", OWNER)
    sign_in(monkeypatch, dash, employee("anna@maximumstores.online"))
    at = run()
    assert not at.exception
    labels = [t.label for t in at.tabs]
    assert PREVIEW not in labels and labels[: len(PUBLIC_TABS)] == PUBLIC_TABS
