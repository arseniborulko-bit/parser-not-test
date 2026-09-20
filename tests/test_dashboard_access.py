"""Дашборд целиком (Streamlit AppTest) с подменёнными данными и правами: без Google, без настоящей базы."""

import importlib
from datetime import date, datetime

import pandas as pd
import psycopg2
import pytest
from streamlit.testing.v1 import AppTest

import access

SCRIPT = "import dashboard_db\ndashboard_db.main()"
PUBLIC_TABS = ["📋 Текущее состояние", "📅 История", "🥊 Пары конкурентов"]
ADMIN_TABS = PUBLIC_TABS + ["👥 Пользователи"]

SNAPSHOT_ROW = {
    "snapshot_date": date(2026, 9, 18), "marketplace": "US", "currency": "USD", "our_asin": "B000000001",
    "our_product": "Our", "our_price": 10.0, "our_bsr": 100, "our_bsr_delta_24h": 1, "comp_asin": "B000000002",
    "competitor_name": "Comp", "comp_price": 9.0, "comp_bsr": 200, "comp_bsr_delta_24h": -1,
    "comp_stock": "In Stock", "price_diff_pct": 11.1, "updated_at": datetime(2026, 9, 18, 10, 0),
}
PAIR_ROW = {
    "marketplace": "US", "our_asin": "B000000001", "our_product": "Our", "comp_asin": "B000000002",
    "competitor_name": "Comp", "active": True,
}


def logged_in(email, verified=True):
    return {"is_logged_in": True, "email": email, "email_verified": verified}


@pytest.fixture
def dash(monkeypatch):
    import dotenv

    monkeypatch.setattr(dotenv, "load_dotenv", lambda *args, **kwargs: False)

    def no_real_database(*args, **kwargs):
        raise AssertionError("тест обратился к настоящей базе")

    monkeypatch.setattr(psycopg2, "connect", no_real_database)
    for name in ("DATABASE_URL", "ADMIN_EMAILS"):
        monkeypatch.delenv(name, raising=False)

    module = importlib.import_module("dashboard_db")
    monkeypatch.setattr(module, "load_current", lambda: pd.DataFrame([SNAPSHOT_ROW]))
    monkeypatch.setattr(module, "load_snapshots", lambda: pd.DataFrame([SNAPSHOT_ROW]))
    monkeypatch.setattr(module, "load_competitor_pairs", lambda: pd.DataFrame([PAIR_ROW]))
    return module


def sign_in(monkeypatch, dash, user):
    monkeypatch.setattr(dash, "_auth_configured", lambda: True)
    monkeypatch.setattr(dash, "_current_user", lambda: user)


def run(timeout=30):
    return AppTest.from_string(SCRIPT).run(timeout=timeout)


def buttons(at, key):
    return [b for b in at.button if b.key == key]


def page_text(at):
    parts = [m.value for m in at.markdown] + [i.value for i in at.info] + [w.value for w in at.warning]
    return " ".join(parts)


FULL_AUTH = {
    "client_id": "id", "client_secret": "secret", "cookie_secret": "cookie",
    "redirect_uri": "https://app.example/oauth2callback",
    "server_metadata_url": "https://accounts.example/.well-known/openid-configuration",
}


def test_auth_is_configured_only_with_every_required_key(monkeypatch, dash):
    import streamlit as st

    monkeypatch.setattr(st, "secrets", {"auth": dict(FULL_AUTH)})
    assert dash._auth_configured() is True
    for missing in FULL_AUTH:
        partial = {key: value for key, value in FULL_AUTH.items() if key != missing}
        monkeypatch.setattr(st, "secrets", {"auth": partial})
        assert dash._auth_configured() is False, missing
    monkeypatch.setattr(st, "secrets", {"auth": {**FULL_AUTH, "client_secret": ""}})
    assert dash._auth_configured() is False
    monkeypatch.setattr(st, "secrets", {})
    assert dash._auth_configured() is False


def test_signed_in_user_data_is_ignored_while_auth_is_not_configured(monkeypatch, dash):
    import streamlit as st

    class FakeUser:
        def to_dict(self):
            return logged_in("boss@x.com")

    monkeypatch.setattr(st, "secrets", {})
    monkeypatch.setattr(st, "user", FakeUser())
    assert dash._current_user() == {}
    monkeypatch.setattr(st, "secrets", {"auth": dict(FULL_AUTH)})
    assert dash._current_user() == logged_in("boss@x.com")


def test_secret_prefers_environment_then_streamlit_secrets_then_empty(monkeypatch, dash):
    import streamlit as st

    monkeypatch.setattr(st, "secrets", {"ADMIN_EMAILS": "from-secrets@x.com"})
    assert dash._secret("ADMIN_EMAILS") == "from-secrets@x.com"
    monkeypatch.setenv("ADMIN_EMAILS", "from-env@x.com")
    assert dash._secret("ADMIN_EMAILS") == "from-env@x.com"
    monkeypatch.delenv("ADMIN_EMAILS")
    monkeypatch.setattr(st, "secrets", {})
    assert dash._secret("ADMIN_EMAILS") == ""


def test_without_auth_configuration_dashboard_stays_public_and_read_only(dash):
    at = run()
    assert not at.exception
    assert [t.label for t in at.tabs] == PUBLIC_TABS
    assert not buttons(at, "login_btn") and not buttons(at, "logout_btn")
    assert any("Режим просмотра" in info.value for info in at.info)


def test_anonymous_visitor_sees_login_button_and_public_tabs(monkeypatch, dash):
    sign_in(monkeypatch, dash, {"is_logged_in": False})
    at = run()
    assert not at.exception
    assert len(buttons(at, "login_btn")) == 1
    assert [t.label for t in at.tabs] == PUBLIC_TABS


def test_editor_from_database_sees_role_but_no_admin_tab(monkeypatch, dash):
    sign_in(monkeypatch, dash, logged_in("Ed@X.com"))
    monkeypatch.setattr(access, "active_user_roles", lambda connect: {"ed@x.com": "editor"})
    at = run()
    assert not at.exception
    assert "ed@x.com · редактор" in page_text(at)
    assert len(buttons(at, "logout_btn")) == 1
    assert [t.label for t in at.tabs] == PUBLIC_TABS


def test_admin_from_secret_gets_users_tab_without_database_lookup(monkeypatch, dash):
    monkeypatch.setenv("ADMIN_EMAILS", "Boss@X.com")
    sign_in(monkeypatch, dash, logged_in("boss@x.com"))

    def must_not_be_called(connect):
        raise AssertionError("админу из секрета база для роли не нужна")

    monkeypatch.setattr(access, "active_user_roles", must_not_be_called)
    monkeypatch.setattr(access, "list_users", lambda connect: [])
    at = run()
    assert not at.exception
    assert [t.label for t in at.tabs] == ADMIN_TABS
    assert "boss@x.com · админ" in page_text(at)
    assert any("В базе пока никого нет" in info.value for info in at.info)


def test_signed_in_stranger_has_no_management_access(monkeypatch, dash):
    sign_in(monkeypatch, dash, logged_in("stranger@x.com"))
    monkeypatch.setattr(access, "active_user_roles", lambda connect: {"ed@x.com": "editor"})
    at = run()
    assert not at.exception
    assert "нет доступа к управлению" in page_text(at)
    assert [t.label for t in at.tabs] == PUBLIC_TABS


def test_unverified_email_never_gets_access_even_if_listed_as_admin(monkeypatch, dash):
    monkeypatch.setenv("ADMIN_EMAILS", "boss@x.com")
    sign_in(monkeypatch, dash, logged_in("boss@x.com", verified=False))

    def must_not_be_called(connect):
        raise AssertionError("неподтверждённый email не должен доходить до базы")

    monkeypatch.setattr(access, "active_user_roles", must_not_be_called)
    at = run()
    assert not at.exception
    assert "не подтвердил email" in page_text(at)
    assert [t.label for t in at.tabs] == PUBLIC_TABS


def test_unavailable_user_list_fails_closed_and_page_still_renders(monkeypatch, dash):
    sign_in(monkeypatch, dash, logged_in("ed@x.com"))

    def broken(connect):
        raise access.AccessStoreError("Операция со списком пользователей не подтверждена (OperationalError).")

    monkeypatch.setattr(access, "active_user_roles", broken)
    at = run()
    assert not at.exception
    assert any("Доступ к управлению временно закрыт" in w.value for w in at.warning)
    assert [t.label for t in at.tabs] == PUBLIC_TABS
    assert "нет доступа к управлению" in page_text(at)


def test_admin_adds_user_through_the_form_with_admin_rights(monkeypatch, dash):
    monkeypatch.setenv("ADMIN_EMAILS", "boss@x.com")
    sign_in(monkeypatch, dash, logged_in("boss@x.com"))
    monkeypatch.setattr(access, "list_users", lambda connect: [])
    calls = []

    def fake_add_user(connect, email, role, *, actor_role, actor_email):
        calls.append((email, role, actor_role, actor_email))
        return access.normalize_email(email)

    monkeypatch.setattr(access, "add_user", fake_add_user)
    at = run()
    at.text_input(key="add_user_email").input("New@X.com")
    at.selectbox(key="add_user_role").select(access.ROLE_ADMIN)
    [b for b in at.button if b.label == "Добавить или обновить"][0].click()
    at.run(timeout=30)
    assert not at.exception
    assert calls == [("New@X.com", access.ROLE_ADMIN, access.ROLE_ADMIN, "boss@x.com")]
    assert any("Сохранено: new@x.com" in s.value for s in at.success)


def test_form_shows_validation_error_instead_of_crashing(monkeypatch, dash):
    monkeypatch.setenv("ADMIN_EMAILS", "boss@x.com")
    sign_in(monkeypatch, dash, logged_in("boss@x.com"))
    monkeypatch.setattr(access, "list_users", lambda connect: [])
    at = run()
    at.text_input(key="add_user_email").input("not-an-email")
    [b for b in at.button if b.label == "Добавить или обновить"][0].click()
    at.run(timeout=30)
    assert not at.exception
    assert any("Некорректный email" in e.value for e in at.error)
