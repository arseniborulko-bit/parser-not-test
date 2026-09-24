"""Дашборд целиком (Streamlit AppTest) с подменёнными данными и правами: без Google, без настоящей базы."""

import importlib
from datetime import date, datetime, time, timedelta

import pandas as pd
import psycopg2
import pytest
from streamlit.testing.v1 import AppTest

import access
import schedule_store

SCRIPT = "import dashboard_db\ndashboard_db.main()"
PUBLIC_TABS = ["📋 Текущее состояние", "📅 История", "📈 Прогноз", "🥊 Пары конкурентов",
               "⚙ Сбор и управление", "ℹ️ Как это работает"]
ADMIN_TABS = PUBLIC_TABS + ["👥 Пользователи"]
TEAM_PASSWORD = "correct-horse-battery"
NOW = datetime(2026, 9, 21, 8, 0, tzinfo=schedule_store.TZ)

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
    monkeypatch.setenv("TEAM_PASSWORD", TEAM_PASSWORD)
    for name in ("GITHUB_DISPATCH_TOKEN", "GITHUB_REPO", "SCRAPINGDOG_TOKEN"):
        monkeypatch.delenv(name, raising=False)

    import spot_check

    def no_real_scraping():
        raise AssertionError("тест обратился к настоящему ScrapingDog")

    monkeypatch.setattr(spot_check, "_load_scraping", no_real_scraping)

    import streamlit as st

    st.cache_resource.clear()
    module = importlib.import_module("dashboard_db")
    monkeypatch.setattr(module, "load_current", lambda: pd.DataFrame([SNAPSHOT_ROW]))
    monkeypatch.setattr(module, "load_snapshots", lambda: pd.DataFrame([SNAPSHOT_ROW]))
    monkeypatch.setattr(module, "load_competitor_pairs", lambda: pd.DataFrame([PAIR_ROW]))
    monkeypatch.setattr(module, "_now", lambda: NOW)
    monkeypatch.setattr(module, "_admission_preview_cached", lambda scope="all": None)
    monkeypatch.setattr(schedule_store, "load_overview", lambda connect, now: overview())
    yield module
    st.cache_resource.clear()


def overview(schedule=schedule_store.Schedule(9, 0), collected_today=False, runs=None):
    if runs is None:
        started = NOW - timedelta(days=1, hours=-1)
        runs = [{"started_at": started, "finished_at": started + timedelta(minutes=12), "status": "done", "error": None}]
    return schedule_store.Overview(schedule, collected_today, runs)


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


def unlock(at, name="Аня", password=TEAM_PASSWORD):
    at.text_input(key="unlock_name").input(name)
    at.text_input(key="unlock_password").input(password)
    [b for b in at.button if b.label == "Открыть управление"][0].click()
    return at.run(timeout=30)


def time_inputs(at):
    return [t for t in at.time_input if t.key == "schedule_time"]


def runs_table(at):
    return [d.value for d in at.dataframe if "Начат (Киев)" in d.value.columns][0]


def test_schedule_tab_is_visible_to_everyone_but_has_no_controls(monkeypatch, dash):
    monkeypatch.setenv("TEAM_PASSWORD", TEAM_PASSWORD)
    at = run()
    assert not at.exception
    assert "⚙ Сбор и управление" in [t.label for t in at.tabs]
    assert any("Автосбор включён: каждый день после 09:00 (Киев)" in s.value for s in at.success)
    assert any("Следующий запуск: 21.09 в 09:00" in c.value for c in at.caption)
    assert any("Чтобы менять время" in c.value for c in at.caption)
    assert not time_inputs(at)
    assert "Ошибка" not in runs_table(at).columns


def record_saves(monkeypatch):
    calls = []
    monkeypatch.setattr(
        schedule_store, "save_schedule",
        lambda connect, hour, minute, enabled, *, actor_role, actor: calls.append((enabled, actor_role, actor)),
    )
    return calls


def save(at):
    [b for b in at.button if b.label == "Сохранить"][0].click()
    return at.run(timeout=30)


def test_without_a_team_password_management_is_open_and_shows_no_notice_or_name_field(monkeypatch, dash):
    monkeypatch.delenv("TEAM_PASSWORD")
    calls = record_saves(monkeypatch)
    at = run()
    assert not at.exception
    assert not any("Управление открыто" in c.value for c in at.caption)
    assert not [t for t in at.text_input if t.key in ("unlock_password", "actor_name")]
    assert not [e for e in at.expander if "Управление" in e.label or "журнала" in e.label]
    assert len(time_inputs(at)) == 1
    assert not any("Дашборд показывает данные" in i.value or "Режим просмотра" in i.value for i in at.info)
    assert not any("Чтобы менять время" in c.value for c in at.caption)
    save(at)
    assert calls == [(True, access.ROLE_EDITOR, "Команда")]


def test_the_page_header_shows_only_the_last_collection_date(monkeypatch, dash):
    monkeypatch.delenv("TEAM_PASSWORD")
    at = run()
    assert not at.exception
    boxes = [m.value for m in at.markdown if '<div class="status-box">' in m.value]
    assert len(boxes) == 1 and "Последний сбор в базе:" in boxes[0]
    assert "Пар в текущем срезе" not in boxes[0] and "Postgres" not in boxes[0]
    shown = " ".join([m.value for m in at.markdown] + [c.value for c in at.caption])
    assert "Количество записей по датам" not in shown
    assert not any("source-badge" in m.value or "Источник: база данных" in m.value for m in at.markdown)
    assert not at.get("arrow_vega_lite_chart") and not at.get("arrow_bar_chart")


def test_a_short_team_password_keeps_management_closed_instead_of_opening_it(monkeypatch, dash):
    monkeypatch.setenv("TEAM_PASSWORD", "short")
    at = run()
    assert any("короче 8 символов" in c.value for c in at.caption)
    assert not time_inputs(at)
    assert not [t for t in at.text_input if t.key in ("unlock_password", "actor_name")]
    assert not any("Управление открыто: любой" in c.value for c in at.caption)


def test_a_password_that_ended_up_inside_a_secrets_section_keeps_management_closed(monkeypatch, dash):
    monkeypatch.delenv("TEAM_PASSWORD")
    at = AppTest.from_string(SCRIPT)
    at.secrets["gcp_service_account"] = {"TEAM_PASSWORD": "long-enough-password"}
    at = at.run(timeout=30)
    assert not at.exception
    assert any("внутри секции" in c.value for c in at.caption)
    assert not time_inputs(at)
    assert not [t for t in at.text_input if t.key == "actor_name"]


def test_the_state_of_the_team_password_secret(monkeypatch, dash):
    import streamlit as st

    monkeypatch.delenv("TEAM_PASSWORD")
    monkeypatch.setattr(st, "secrets", {})
    assert dash._team_password_state() == ("open", "")
    monkeypatch.setattr(st, "secrets", {"TEAM_PASSWORD": "long-enough-password"})
    assert dash._team_password_state() == ("password", "long-enough-password")
    monkeypatch.setattr(st, "secrets", {"TEAM_PASSWORD": "short"})
    assert dash._team_password_state()[0] == "locked"
    monkeypatch.setattr(st, "secrets", {"TEAM_PASSWORD": "   "})
    assert dash._team_password_state()[0] == "locked"
    monkeypatch.setattr(st, "secrets", {"DATABASE_URL": "x", "gcp": {"TEAM_PASSWORD": "long-enough-password"}})
    assert dash._team_password_state()[0] == "locked"
    monkeypatch.setattr(st, "secrets", {"DATABASE_URL": "x", "gcp": {"client_email": "a@b"}})
    assert dash._team_password_state() == ("open", "")
    monkeypatch.setenv("TEAM_PASSWORD", "environment-password")
    assert dash._team_password_state() == ("password", "environment-password")


def test_wrong_password_keeps_management_closed(monkeypatch, dash):
    monkeypatch.setenv("TEAM_PASSWORD", TEAM_PASSWORD)
    at = unlock(run(), password="wrong-password")
    assert not at.exception
    assert any("Неверный пароль" in e.value for e in at.error)
    assert not time_inputs(at)


def test_correct_password_opens_management(monkeypatch, dash):
    monkeypatch.setenv("TEAM_PASSWORD", TEAM_PASSWORD)
    at = unlock(run())
    assert not at.exception
    assert any("Управление открыто: Аня" in c.value for c in at.caption)
    assert len(time_inputs(at)) == 1
    assert "Ошибка" in runs_table(at).columns


def test_name_is_required_and_a_missing_name_is_not_counted_as_a_password_failure(monkeypatch, dash):
    monkeypatch.setenv("TEAM_PASSWORD", TEAM_PASSWORD)
    at = run()
    for _ in range(8):
        at = unlock(at, name=" ", password="whatever")
        assert any("Укажите имя" in e.value for e in at.error)
    at = unlock(at)
    assert any("Управление открыто: Аня" in c.value for c in at.caption)


def test_five_wrong_passwords_lock_out_even_the_correct_one(monkeypatch, dash):
    monkeypatch.setenv("TEAM_PASSWORD", TEAM_PASSWORD)
    at = run()
    for _ in range(5):
        at = unlock(at, password="wrong-password")
    at = unlock(at)
    assert not at.exception
    assert any("Слишком много неудачных попыток" in e.value for e in at.error)
    assert not time_inputs(at)


def test_saving_time_passes_editor_role_and_the_name_to_the_store(monkeypatch, dash):
    monkeypatch.setenv("TEAM_PASSWORD", TEAM_PASSWORD)
    calls = []
    monkeypatch.setattr(
        schedule_store, "save_schedule",
        lambda connect, hour, minute, enabled, *, actor_role, actor: calls.append((hour, minute, enabled, actor_role, actor)),
    )
    at = unlock(run())
    at.time_input(key="schedule_time").set_value(time(10, 30))
    [b for b in at.button if b.label == "Сохранить"][0].click()
    at.run(timeout=30)
    assert not at.exception
    assert calls == [(10, 30, True, access.ROLE_EDITOR, "Аня")]
    assert any("Сохранено: автосбор включён, 10:30 (Киев)" in s.value for s in at.success)


def test_unchecking_the_switch_saves_autocollection_off(monkeypatch, dash):
    monkeypatch.setenv("TEAM_PASSWORD", TEAM_PASSWORD)
    calls = []
    monkeypatch.setattr(
        schedule_store, "save_schedule",
        lambda connect, hour, minute, enabled, *, actor_role, actor: calls.append(enabled),
    )
    at = unlock(run())
    at.checkbox(key="schedule_enabled").uncheck()
    [b for b in at.button if b.label == "Сохранить"][0].click()
    at.run(timeout=30)
    assert calls == [False]
    assert any("автосбор выключен" in s.value for s in at.success)


def test_store_failure_on_save_is_shown_instead_of_crashing(monkeypatch, dash):
    monkeypatch.setenv("TEAM_PASSWORD", TEAM_PASSWORD)

    def broken(*args, **kwargs):
        raise schedule_store.ScheduleStoreError("Операция с расписанием не подтверждена (OperationalError).")

    monkeypatch.setattr(schedule_store, "save_schedule", broken)
    at = unlock(run())
    [b for b in at.button if b.label == "Сохранить"][0].click()
    at.run(timeout=30)
    assert not at.exception
    assert any("не подтверждена" in e.value for e in at.error)


def test_disabled_schedule_is_shown_as_off_with_unchecked_switch(monkeypatch, dash):
    monkeypatch.setenv("TEAM_PASSWORD", TEAM_PASSWORD)
    monkeypatch.setattr(schedule_store, "load_overview", lambda connect, now: overview(schedule=None))
    at = unlock(run())
    assert any("Автосбор выключен" in i.value for i in at.info)
    assert at.checkbox(key="schedule_enabled").value is False
    assert at.time_input(key="schedule_time").value == time(9, 0)


def test_running_collection_is_announced(monkeypatch, dash):
    running = [{"started_at": NOW - timedelta(minutes=3), "finished_at": None, "status": "running", "error": None}]
    monkeypatch.setattr(schedule_store, "load_overview", lambda connect, now: overview(runs=running))
    at = run()
    assert not at.exception
    assert any("Сейчас идёт сбор данных" in w.value for w in at.warning)


def test_todays_time_already_passed_without_collection_shows_no_technical_explanation(monkeypatch, dash):
    monkeypatch.setattr(dash, "_now", lambda: NOW.replace(hour=11))
    at = run()
    assert not at.exception
    assert not any("Следующий запуск" in c.value or "нерегулярно" in c.value for c in at.caption)


def test_collected_today_moves_next_run_to_tomorrow(monkeypatch, dash):
    monkeypatch.setattr(schedule_store, "load_overview", lambda connect, now: overview(collected_today=True))
    at = run()
    assert any("Следующий запуск: 22.09 в 09:00" in c.value for c in at.caption)


def test_schedule_store_outage_is_reported_and_the_rest_of_the_page_works(monkeypatch, dash):
    def down(connect, now):
        raise schedule_store.ScheduleStoreError("Операция с расписанием не подтверждена (OperationalError).")

    monkeypatch.setattr(schedule_store, "load_overview", down)
    at = run()
    assert not at.exception
    assert any("Операция с расписанием не подтверждена" in e.value for e in at.error)
    assert [t.label for t in at.tabs] == PUBLIC_TABS


def test_google_admin_manages_schedule_without_the_team_password(monkeypatch, dash):
    monkeypatch.setenv("ADMIN_EMAILS", "boss@x.com")
    monkeypatch.setattr(access, "list_users", lambda connect: [])
    sign_in(monkeypatch, dash, logged_in("boss@x.com"))
    at = run()
    assert not at.exception
    assert len(time_inputs(at)) == 1
    assert not [t for t in at.text_input if t.key == "unlock_password"]
