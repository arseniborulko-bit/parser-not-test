"""Дашборд целиком (Streamlit AppTest) с подменёнными данными и правами: без Google, без настоящей базы."""

import importlib
from datetime import date, datetime, time, timedelta, timezone

import pandas as pd
import psycopg2
import pytest
from streamlit.testing.v1 import AppTest

import access
import schedule_store

SCRIPT = "import dashboard_db\ndashboard_db.main()"
PUBLIC_TABS = ["📋 Текущее состояние", "📅 История", "📈 Прогноз", "🥊 Пары конкурентов",
               "⚙ Сбор и управление", "ℹ️ Как это работает", "📒 Журнал"]
# Журнал открыт всем сотрудникам; админа отличает раздел «Пользователи и роли» внутри него.
ADMIN_TABS = PUBLIC_TABS
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


EMPLOYEE_EMAIL = "anna@maximumstores.online"


def employee(email=EMPLOYEE_EMAIL, **claims):
    """Вошедший сотрудник: так Google отдаёт рабочий аккаунт Google Workspace (hd — домен компании)."""
    return {**logged_in(email), "hd": "maximumstores.online", "sub": "100", "iat": 1_700_000_000,
            "name": "Анна", **claims}


# Настоящие _auth_configured/_current_user: фикстура подменяет их «вошедшим сотрудником», а тесты самих
# этих функций возвращают оригиналы.
REAL: dict = {}


@pytest.fixture
def dash(monkeypatch):
    import dotenv

    monkeypatch.setattr(dotenv, "load_dotenv", lambda *args, **kwargs: False)

    def no_real_database(*args, **kwargs):
        raise AssertionError("тест обратился к настоящей базе")

    monkeypatch.setattr(psycopg2, "connect", no_real_database)
    for name in ("DATABASE_URL", "ADMIN_EMAILS", "CORPORATE_EMAIL_DOMAINS"):
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
    # Дашборд открыт только вошедшему сотруднику: по умолчанию тесты работают от его имени. Роль
    # сотрудника по умолчанию снята (EMPLOYEE_ROLE = None), чтобы проверять и «🔒 Управление»
    # (пароль команды, открытое управление); боевую настройку — все сотрудники редакторы — проверяют
    # тесты в test_dashboard_google_login.py.
    REAL.setdefault("_auth_configured", module._auth_configured)
    REAL.setdefault("_current_user", module._current_user)
    monkeypatch.setattr(module, "_auth_configured", lambda: True)
    monkeypatch.setattr(module, "_current_user", lambda: employee())
    monkeypatch.setattr(module, "EMPLOYEE_ROLE", None)
    monkeypatch.setattr(access, "record_login", lambda connect, email: True)
    monkeypatch.setattr(access, "recent_logins", lambda connect, days=30, limit=5000: [])
    import activity

    monkeypatch.setattr(activity, "table_exists", lambda connect: False)
    import usage_log

    monkeypatch.setattr(usage_log, "log_exists", lambda connect: False)
    monkeypatch.setattr(access, "allowed_table_exists", lambda connect: False)
    monkeypatch.setattr(schedule_store, "load_overview", lambda connect, now: overview())
    yield module
    st.cache_resource.clear()


def overview(schedule=schedule_store.Schedule(9, 0), collected_today=False, runs=None,
             schedule2=None, collected_count=None):
    if runs is None:
        started = NOW - timedelta(days=1, hours=-1)
        runs = [{"started_at": started, "finished_at": started + timedelta(minutes=12), "status": "done", "error": None}]
    if collected_count is None:
        collected_count = 1 if collected_today else 0
    return schedule_store.Overview(schedule, collected_today, runs, schedule2, collected_count)


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

    monkeypatch.setattr(dash, "_auth_configured", REAL["_auth_configured"])
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
            return employee()

    monkeypatch.setattr(dash, "_auth_configured", REAL["_auth_configured"])
    monkeypatch.setattr(dash, "_current_user", REAL["_current_user"])
    monkeypatch.setattr(st, "secrets", {})
    monkeypatch.setattr(st, "user", FakeUser())
    assert dash._current_user() == {}
    monkeypatch.setattr(st, "secrets", {"auth": dict(FULL_AUTH)})
    assert dash._current_user() == employee()


def test_broken_user_data_counts_as_not_signed_in(monkeypatch, dash):
    import streamlit as st

    class BrokenUser:
        def to_dict(self):
            raise RuntimeError("oauth state lost")

    monkeypatch.setattr(dash, "_current_user", REAL["_current_user"])
    monkeypatch.setattr(st, "secrets", {"auth": dict(FULL_AUTH)})
    monkeypatch.setattr(st, "user", BrokenUser())
    assert dash._current_user() == {}


def test_secret_prefers_environment_then_streamlit_secrets_then_empty(monkeypatch, dash):
    import streamlit as st

    monkeypatch.setattr(st, "secrets", {"ADMIN_EMAILS": "from-secrets@x.com"})
    assert dash._secret("ADMIN_EMAILS") == "from-secrets@x.com"
    monkeypatch.setenv("ADMIN_EMAILS", "from-env@x.com")
    assert dash._secret("ADMIN_EMAILS") == "from-env@x.com"
    monkeypatch.delenv("ADMIN_EMAILS")
    monkeypatch.setattr(st, "secrets", {})
    assert dash._secret("ADMIN_EMAILS") == ""


def test_signed_in_employee_without_a_role_sees_data_and_their_name(dash):
    at = run()
    assert not at.exception
    assert [t.label for t in at.tabs] == PUBLIC_TABS
    assert "Анна" in page_text(at) and f"{EMPLOYEE_EMAIL} · только просмотр" in page_text(at)
    assert len(buttons(at, "logout_btn")) == 1 and not buttons(at, "login_btn")


def test_editor_from_database_sees_role_but_no_admin_tab(monkeypatch, dash):
    sign_in(monkeypatch, dash, employee("Ed@MaximumStores.online"))
    monkeypatch.setattr(access, "active_user_roles", lambda connect: {"ed@maximumstores.online": "editor"})
    at = run()
    assert not at.exception
    assert "ed@maximumstores.online · редактор" in page_text(at)
    assert len(buttons(at, "logout_btn")) == 1
    assert [t.label for t in at.tabs] == PUBLIC_TABS


def test_admin_from_secret_gets_users_tab_without_database_lookup(monkeypatch, dash):
    monkeypatch.setenv("ADMIN_EMAILS", "Boss@MaximumStores.online")
    sign_in(monkeypatch, dash, employee("boss@maximumstores.online"))

    def must_not_be_called(connect):
        raise AssertionError("админу из секрета база для роли не нужна")

    monkeypatch.setattr(access, "active_user_roles", must_not_be_called)
    monkeypatch.setattr(access, "list_users", lambda connect: [])
    at = run_on_tab(JOURNAL)
    assert not at.exception
    assert [t.label for t in at.tabs] == ADMIN_TABS
    assert "boss@maximumstores.online · админ" in page_text(at)
    assert any("В базе пока никого нет" in info.value for info in at.info)


def test_an_outside_admin_from_the_secret_is_still_not_let_in(monkeypatch, dash):
    """ADMIN_EMAILS не обходит проверку домена: дашборд — только для сотрудников."""
    monkeypatch.setenv("ADMIN_EMAILS", "boss@x.com")
    sign_in(monkeypatch, dash, {**logged_in("boss@x.com"), "hd": "x.com"})

    def must_not_be_called(connect):
        raise AssertionError("не сотрудник не должен доходить до базы")

    monkeypatch.setattr(access, "active_user_roles", must_not_be_called)
    at = run()
    assert not at.exception and not at.tabs
    assert any("Доступ только для сотрудников maximumstores.online" in e.value for e in at.error)


def test_unverified_email_never_gets_access_even_if_listed_as_admin(monkeypatch, dash):
    monkeypatch.setenv("ADMIN_EMAILS", EMPLOYEE_EMAIL)
    sign_in(monkeypatch, dash, employee(email_verified=False))

    def must_not_be_called(connect):
        raise AssertionError("неподтверждённый email не должен доходить до базы")

    monkeypatch.setattr(access, "active_user_roles", must_not_be_called)
    at = run()
    assert not at.exception and not at.tabs
    assert any("Доступ только для сотрудников" in e.value for e in at.error)


def test_unavailable_user_list_fails_closed_and_page_still_renders(monkeypatch, dash):
    """Сотрудник без общей роли (EMPLOYEE_ROLE = None) и без доступа к списку — только просмотр."""
    def broken(connect):
        raise access.AccessStoreError("Операция со списком пользователей не подтверждена (OperationalError).")

    monkeypatch.setattr(access, "active_user_roles", broken)
    at = run()
    assert not at.exception
    assert any("Доступ к управлению временно закрыт" in w.value for w in at.warning)
    assert [t.label for t in at.tabs] == PUBLIC_TABS
    assert "только просмотр" in page_text(at)


def test_admin_adds_user_through_the_form_with_admin_rights(monkeypatch, dash):
    monkeypatch.setenv("ADMIN_EMAILS", "boss@maximumstores.online")
    sign_in(monkeypatch, dash, employee("boss@maximumstores.online"))
    monkeypatch.setattr(access, "list_users", lambda connect: [])
    calls = []

    def fake_add_user(connect, email, role, *, actor_role, actor_email):
        calls.append((email, role, actor_role, actor_email))
        return access.normalize_email(email)

    monkeypatch.setattr(access, "add_user", fake_add_user)
    at = run_on_tab(JOURNAL)
    at.text_input(key="add_user_email").input("New@X.com")
    at.selectbox(key="add_user_role").select(access.ROLE_ADMIN)
    [b for b in at.button if b.label == "Добавить или обновить"][0].click()
    at.run(timeout=30)
    assert not at.exception
    assert calls == [("New@X.com", access.ROLE_ADMIN, access.ROLE_ADMIN, "boss@maximumstores.online")]
    assert any("Сохранено: new@x.com" in s.value for s in at.success)


def test_form_shows_validation_error_instead_of_crashing(monkeypatch, dash):
    monkeypatch.setenv("ADMIN_EMAILS", "boss@maximumstores.online")
    sign_in(monkeypatch, dash, employee("boss@maximumstores.online"))
    monkeypatch.setattr(access, "list_users", lambda connect: [])
    at = run_on_tab(JOURNAL)
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
        lambda connect, hour, minute, enabled, *, slot=1, actor_role, actor: calls.append((enabled, actor_role, actor, slot)),
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
    assert calls == [(True, access.ROLE_EDITOR, "Команда", 1), (False, access.ROLE_EDITOR, "Команда", 2)]


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
        lambda connect, hour, minute, enabled, *, slot=1, actor_role, actor: calls.append((hour, minute, enabled, slot, actor_role, actor)),
    )
    at = unlock(run())
    at.time_input(key="schedule_time").set_value(time(10, 30))
    [b for b in at.button if b.label == "Сохранить"][0].click()
    at.run(timeout=30)
    assert not at.exception
    assert calls[0] == (10, 30, True, 1, access.ROLE_EDITOR, "Аня")
    assert any("Сохранено: автосбор включён, слот 1 — 10:30 (Киев)" in s.value for s in at.success)


def test_unchecking_the_switch_saves_autocollection_off(monkeypatch, dash):
    monkeypatch.setenv("TEAM_PASSWORD", TEAM_PASSWORD)
    calls = []
    monkeypatch.setattr(
        schedule_store, "save_schedule",
        lambda connect, hour, minute, enabled, *, slot=1, actor_role, actor: calls.append((enabled, slot)),
    )
    at = unlock(run())
    at.checkbox(key="schedule_enabled").uncheck()
    [b for b in at.button if b.label == "Сохранить"][0].click()
    at.run(timeout=30)
    assert calls == [(False, 1), (False, 2)]
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
    monkeypatch.setenv("ADMIN_EMAILS", "boss@maximumstores.online")
    monkeypatch.setattr(access, "list_users", lambda connect: [])
    sign_in(monkeypatch, dash, employee("boss@maximumstores.online"))
    at = run()
    assert not at.exception
    assert len(time_inputs(at)) == 1
    assert not [t for t in at.text_input if t.key == "unlock_password"]


def test_the_current_state_query_only_counts_active_pairs():
    import inspect

    import dashboard_db

    source = inspect.getsource(dashboard_db.load_current.__wrapped__ if hasattr(dashboard_db.load_current, "__wrapped__") else dashboard_db.load_current)
    assert "JOIN bsr_radar.competitor_pairs" in source and "p.active" in source




def at_kyiv(day, hour, minute=0):
    return datetime(2026, 9, day, hour, minute, tzinfo=schedule_store.TZ).astimezone(timezone.utc)


# Журнал ведётся с 12.09 (первый вход), «сегодня» — 21.09 (NOW): период 10 дней.
LOGINS = [
    {"email": "anna@maximumstores.online", "logged_in_at": at_kyiv(21, 7)},
    {"email": "anna@maximumstores.online", "logged_in_at": at_kyiv(20, 9)},
    {"email": "anna@maximumstores.online", "logged_in_at": at_kyiv(20, 18)},
    {"email": "boss@maximumstores.online", "logged_in_at": at_kyiv(12, 10)},
]
JOURNAL = "📒 Журнал"


def run_on_tab(tab, timeout=30):
    at = AppTest.from_string(SCRIPT)
    at.session_state["main_tab"] = tab
    return at.run(timeout=timeout)


def journal_text(at):
    tab = at.tabs[-1]
    return " ".join([m.value for m in tab.markdown] + [i.value for i in tab.info] + [e.value for e in tab.error])


def test_journal_counts_logins_share_and_activity_per_employee(dash):
    journal = dash._login_journal(LOGINS, NOW.date())
    assert journal.period_days == 10
    rows = journal.summary.to_dict("records")
    assert [(r["email"], r["logins"], r["share"], r["active_days"], r["activity"], r["last"]) for r in rows] == [
        ("anna", 3, 75, 2, 20, "21.09 07:00"),
        ("boss", 1, 25, 1, 10, "12.09 10:00"),
    ]
    assert list(journal.log["logged_in_at"]) == [
        "21.09.2026 07:00", "20.09.2026 18:00", "20.09.2026 09:00", "12.09.2026 10:00"]


def test_the_period_is_capped_at_thirty_days(dash):
    old = [{"email": "a@maximumstores.online", "logged_in_at": at_kyiv(1, 10) - timedelta(days=60)}]
    assert dash._login_journal(old, NOW.date()).period_days == 30


def test_an_empty_journal_is_not_an_error(dash):
    journal = dash._login_journal([], NOW.date())
    assert journal.summary.empty and journal.period_days == 0


def test_there_is_no_sidebar_any_more(dash):
    at = run()
    assert not at.exception
    assert len(at.sidebar.markdown) == 0 and len(at.sidebar.dataframe) == 0


def test_every_employee_sees_the_journal_but_not_role_management(monkeypatch, dash):
    monkeypatch.setattr(access, "recent_logins", lambda connect, days=30, limit=5000: LOGINS)

    def must_not_be_called(connect):
        raise AssertionError("список ролей — только админу")

    monkeypatch.setattr(access, "list_users", must_not_be_called)
    at = run_on_tab(JOURNAL)
    assert not at.exception
    assert [t.label for t in at.tabs] == PUBLIC_TABS
    summary = at.tabs[-1].dataframe[0].value
    assert list(summary["Доля входов, %"]) == [75, 25]
    assert list(summary["Активность, %"]) == [20, 10]
    assert "Пользователи и роли" not in journal_text(at)


def test_the_journal_reads_the_database_only_when_its_tab_is_open(monkeypatch, dash):
    def must_not_be_called(connect, days=30, limit=5000):
        raise AssertionError("журнал читается только на своей вкладке")

    monkeypatch.setattr(access, "recent_logins", must_not_be_called)
    assert not run().exception


def test_admin_also_manages_roles_in_the_journal_tab(monkeypatch, dash):
    monkeypatch.setenv("ADMIN_EMAILS", "boss@maximumstores.online")
    sign_in(monkeypatch, dash, employee("boss@maximumstores.online"))
    monkeypatch.setattr(access, "list_users", lambda connect: [])
    at = run_on_tab(JOURNAL)
    assert not at.exception
    assert "Пользователи и роли" in journal_text(at)


def test_a_broken_login_log_does_not_break_the_page(monkeypatch, dash):
    def broken(connect, days=30, limit=5000):
        raise access.AccessStoreError("Журнал недоступен.")

    monkeypatch.setattr(access, "recent_logins", broken)
    at = run_on_tab(JOURNAL)
    assert not at.exception
    assert [e.value for e in at.tabs[-1].error] == ["Журнал недоступен."]
    assert [t.label for t in at.tabs] == PUBLIC_TABS


@pytest.fixture
def actions(monkeypatch, dash):
    """Журнал действий подключён; записи складываются в список вместо базы."""
    import activity

    written = []
    monkeypatch.setattr(activity, "table_exists", lambda connect: True)
    monkeypatch.setattr(activity, "record",
                        lambda connect, session_id, email, section=None: written.append((session_id, email, section)))
    return written


def test_opening_the_page_records_the_first_section(actions):
    at = run()
    assert not at.exception
    assert [(email, section) for _, email, section in actions] == [(EMPLOYEE_EMAIL, PUBLIC_TABS[0])]


def test_switching_tabs_records_the_new_section_in_the_same_session(actions):
    at = run()
    at.session_state["main_tab"] = "📈 Прогноз"
    at.run()
    assert not at.exception
    assert [section for _, _, section in actions] == [PUBLIC_TABS[0], "📈 Прогноз"]
    assert len({session for session, _, _ in actions}) == 1


def test_quick_clicks_on_the_same_tab_are_not_written_every_time(actions):
    at = run()
    at.run()
    at.run()
    assert len(actions) == 1


def test_without_the_activity_table_nothing_is_written_and_its_sections_are_hidden(monkeypatch, dash):
    import activity

    def must_not_be_called(*args, **kwargs):
        raise AssertionError("без таблицы писать нельзя")

    monkeypatch.setattr(activity, "record", must_not_be_called)
    at = run_on_tab(JOURNAL)
    assert not at.exception
    text = journal_text(at)
    assert "Сколько времени проводят" not in text and "Какие разделы открывают" not in text and "014" not in text


def test_the_journal_shows_time_spent_and_sections(monkeypatch, dash, actions):
    import activity

    events = [
        {"session_id": "s1", "email": "anna@maximumstores.online", "at": at_kyiv(20, 9, 0), "section": PUBLIC_TABS[0]},
        {"session_id": "s1", "email": "anna@maximumstores.online", "at": at_kyiv(20, 9, 10), "section": "📈 Прогноз"},
        {"session_id": "s2", "email": "boss@maximumstores.online", "at": at_kyiv(20, 10, 0), "section": PUBLIC_TABS[0]},
    ]
    monkeypatch.setattr(activity, "recent", lambda connect, days=30, limit=50000: events)
    at = run_on_tab(JOURNAL)
    assert not at.exception
    text = journal_text(at)
    assert "Сколько времени проводят" in text and "Какие разделы открывают" in text
    frames = [frame.value for frame in at.tabs[-1].dataframe]
    time_table = next(f for f in frames if "В среднем" in f)
    assert list(time_table["Сотрудник"]) == ["anna", "boss"]
    assert list(time_table["Всего"]) == ["10 мин", "0 сек"]
    sections = next(f for f in frames if "Раздел" in f)
    assert dict(zip(sections["Раздел"], sections["Открытий"]))["📈 Прогноз"] == 1
    assert dict(zip(sections["Раздел"], sections["Открытий"]))["📅 История"] == 0


def test_weekly_users_count_distinct_people_from_the_first_week_of_the_journal(dash):
    rows = [
        {"email": "anna@maximumstores.online", "logged_in_at": at_kyiv(8, 10)},   # пн 07.09 — неделя 07.09
        {"email": "anna@maximumstores.online", "logged_in_at": at_kyiv(9, 10)},
        {"email": "boss@maximumstores.online", "logged_in_at": at_kyiv(10, 10)},
        {"email": "anna@maximumstores.online", "logged_in_at": at_kyiv(21, 7)},   # пн 21.09 — текущая
    ]
    weekly = dash._login_journal(rows, NOW.date()).weekly
    assert weekly.to_dict("records") == [
        {"week": "07.09", "users": 2}, {"week": "14.09", "users": 0}, {"week": "21.09 (идёт)", "users": 1},
    ]


def test_weekly_users_go_back_at_most_twelve_weeks(dash):
    old = [{"email": "a@maximumstores.online", "logged_in_at": at_kyiv(21, 7) - timedelta(weeks=30)}]
    assert len(dash._login_journal(old, NOW.date()).weekly) == 12


def test_the_summary_covers_thirty_days_although_weeks_need_more_history(dash):
    rows = LOGINS + [{"email": "old@maximumstores.online", "logged_in_at": at_kyiv(21, 7) - timedelta(days=40)}]
    journal = dash._login_journal(rows, NOW.date())
    assert "old" not in list(journal.summary["email"]) and "old" not in list(journal.log["email"])
    assert journal.period_days == 30


def test_the_journal_draws_the_weekly_chart(monkeypatch, dash):
    monkeypatch.setattr(access, "recent_logins", lambda connect, days=30, limit=5000: LOGINS)
    at = run_on_tab(JOURNAL)
    assert not at.exception
    assert "Сотрудников со входом по неделям" in journal_text(at)


# Scorecard: «сейчас» — NOW (21.09 08:00 Киев). Допущены трое; за 7 дней зашли anna и boss,
# неделей раньше — только anna; чужой (не из списка) в процент не попадает.
ALLOWED = ["anna@maximumstores.online", "boss@maximumstores.online", "carl@maximumstores.online"]
SCORE_LOGINS = [
    {"email": "anna@maximumstores.online", "logged_in_at": at_kyiv(20, 9)},
    {"email": "Boss@maximumstores.online", "logged_in_at": at_kyiv(15, 9)},
    {"email": "stranger@maximumstores.online", "logged_in_at": at_kyiv(19, 9)},
    {"email": "anna@maximumstores.online", "logged_in_at": at_kyiv(10, 9)},
]


def test_scorecard_counts_only_allowed_people_over_the_last_seven_days(dash):
    card = dash._scorecard(SCORE_LOGINS, ALLOWED, NOW)
    assert (card.seen, card.total, card.pct, card.last_pct, card.delta) == (2, 3, 67, 33, 34)
    assert (card.start, card.end) == ("15.09", "21.09")
    assert card.by_dates.to_dict("records") == [
        {"date": "14.09", "users": 1, "pct": 33},
        {"date": "21.09 (сейчас)", "users": 2, "pct": 67},
    ]


def test_without_allowed_people_there_is_no_scorecard(dash):
    assert dash._scorecard(SCORE_LOGINS, [], NOW) is None


def test_the_journal_shows_the_scorecard_card_with_a_green_badge(monkeypatch, dash):
    monkeypatch.setattr(access, "recent_logins", lambda connect, days=30, limit=5000: SCORE_LOGINS)
    monkeypatch.setattr(access, "allowed_table_exists", lambda connect: True)
    monkeypatch.setattr(access, "list_allowed", lambda connect: ALLOWED)
    at = run_on_tab(JOURNAL)
    assert not at.exception
    text = journal_text(at)
    assert "Для Scorecard — эта неделя" in text and "67%" in text and "+34%" in text and "#166534" in text
    assert "2 из 3 допущенных зашли" in text and "прошлая неделя — 33%" in text
    assert "% для Scorecard по датам" in text
    assert "Допущенные для Scorecard" not in text  # список ведёт только админ


def test_a_drop_gets_a_red_badge(monkeypatch, dash):
    monkeypatch.setattr(access, "recent_logins", lambda connect, days=30, limit=5000: [SCORE_LOGINS[-1]])
    monkeypatch.setattr(access, "allowed_table_exists", lambda connect: True)
    monkeypatch.setattr(access, "list_allowed", lambda connect: ALLOWED)
    text = journal_text(run_on_tab(JOURNAL))
    assert "-33%" in text and "#991b1b" in text


def test_without_the_allowed_table_there_is_no_card_for_employees(dash):
    text = journal_text(run_on_tab(JOURNAL))
    assert "Scorecard" not in text


def test_admin_manages_the_allowed_list(monkeypatch, dash):
    monkeypatch.setenv("ADMIN_EMAILS", "boss@maximumstores.online")
    sign_in(monkeypatch, dash, employee("boss@maximumstores.online"))
    monkeypatch.setattr(access, "list_users", lambda connect: [])
    monkeypatch.setattr(access, "allowed_table_exists", lambda connect: True)
    stored = ["anna@maximumstores.online"]
    monkeypatch.setattr(access, "list_allowed", lambda connect: list(stored))
    added = []
    monkeypatch.setattr(access, "add_allowed",
                        lambda connect, email, actor_role, actor_email: added.append((email, actor_role, actor_email)) or email)
    at = run_on_tab(JOURNAL)
    assert "Допущенные для Scorecard" in journal_text(at)
    at.text_input(key="allowed_add_email").input("v.tereshyn@maximumstores.online")
    next(b for b in at.button if b.label == "Добавить").click()
    at.run()
    assert not at.exception
    assert added == [("v.tereshyn@maximumstores.online", "admin", "boss@maximumstores.online")]
