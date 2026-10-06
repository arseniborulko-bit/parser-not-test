"""Весь дашборд — только для сотрудников (вход через Google, @maximumstores.online), Streamlit AppTest.

Без входа, с чужой почтой или без настроенного входа не рисуется ни одна вкладка и не выполняется ни
один запрос за данными. Вход записывается в bsr_radar.login_log один раз на настоящий вход."""

from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

import access
from test_dashboard_access import (  # noqa: F401
    EMPLOYEE_EMAIL, PUBLIC_TABS, buttons, dash, employee, logged_in, run, sign_in, time_inputs,
)

PROJECT = Path(__file__).resolve().parent.parent


@pytest.fixture
def no_data(monkeypatch, dash):  # noqa: F811
    """Любой запрос за данными дашборда — провал теста."""
    def forbidden():
        raise AssertionError("дашборд загрузил данные до проверки входа")

    for name in ("load_current", "load_snapshots", "load_competitor_pairs"):
        monkeypatch.setattr(dash, name, forbidden)
    return dash


@pytest.fixture
def logins(monkeypatch, dash):  # noqa: F811
    written = []
    monkeypatch.setattr(access, "record_login", lambda connect, email: written.append(email) or True)
    return written


def closed(at):
    return not at.exception and not at.tabs and not time_inputs(at)


def test_without_the_auth_secrets_the_whole_dashboard_is_closed(monkeypatch, no_data):
    monkeypatch.setattr(no_data, "_auth_configured", lambda: False)
    at = run()
    assert closed(at)
    assert any("Вход через Google не настроен" in e.value for e in at.error)
    assert not buttons(at, "login_btn")


def test_a_visitor_who_is_not_signed_in_sees_only_the_google_button(monkeypatch, no_data):
    sign_in(monkeypatch, no_data, {"is_logged_in": False})
    at = run()
    assert closed(at)
    assert [b.label for b in buttons(at, "login_btn")] == ["Войти через Google"]
    assert not buttons(at, "logout_btn")


def test_empty_user_data_after_an_oauth_error_counts_as_not_signed_in(monkeypatch, no_data):
    sign_in(monkeypatch, no_data, {})
    at = run()
    assert closed(at) and buttons(at, "login_btn")


@pytest.mark.parametrize("user", [
    {**logged_in("user@gmail.com"), "hd": None},
    employee("user@maximumstores.online.attacker.com"),
    employee(hd=None),
    employee(hd="attacker.com"),
    employee(email_verified=False),
    {**employee(), "email": None},
])
def test_anyone_but_an_employee_is_refused_and_can_sign_out(monkeypatch, no_data, logins, user):
    sign_in(monkeypatch, no_data, user)
    at = run()
    assert closed(at)
    assert any(e.value == "Доступ только для сотрудников maximumstores.online" for e in at.error)
    assert [b.label for b in buttons(at, "logout_btn")] == ["Выйти"]
    assert logins == []


def test_the_app_py_entrypoint_is_protected_too(monkeypatch, no_data):
    """Streamlit Cloud запускает app.py — он тоже обязан пройти через проверку."""
    sign_in(monkeypatch, no_data, {"is_logged_in": False})
    at = AppTest.from_file(str(PROJECT / "app.py")).run(timeout=30)
    assert closed(at) and buttons(at, "login_btn")


def test_there_are_no_other_pages_that_could_skip_the_check():
    """Страницы в pages/ или st.navigation открывались бы по прямой ссылке мимо main()."""
    assert not (PROJECT / "pages").exists()
    sources = [path.read_text(encoding="utf-8") for path in PROJECT.glob("*.py")]
    assert not any("st.navigation" in text or "st.Page(" in text or "switch_page" in text for text in sources)


def test_an_employee_gets_in_with_full_rights_and_is_named(monkeypatch, dash, logins):  # noqa: F811
    """Решение владельца 01.10.2026: все сотрудники могут всё (EMPLOYEE_ROLE — боевое значение)."""
    monkeypatch.setattr(dash, "EMPLOYEE_ROLE", access.ROLE_EDITOR)
    import schedule_store

    saves = []
    monkeypatch.setattr(schedule_store, "save_schedule",
                        lambda connect, hour, minute, enabled, *, slot, actor_role, actor: saves.append(actor))
    at = run()
    assert not at.exception
    assert [t.label for t in at.tabs] == PUBLIC_TABS
    text = " ".join(m.value for m in at.markdown)
    assert "Анна" in text and f"{EMPLOYEE_EMAIL} · редактор" in text
    assert not [t for t in at.text_input if t.key in ("unlock_name", "unlock_password")]
    assert len(time_inputs(at)) == 1
    [b for b in at.button if b.label == "Сохранить"][0].click()
    at.run(timeout=30)
    assert saves == [EMPLOYEE_EMAIL, EMPLOYEE_EMAIL]


def test_the_production_default_is_that_every_employee_is_an_editor():
    import dashboard_db

    assert dashboard_db.EMPLOYEE_ROLE == access.ROLE_EDITOR


def test_a_login_is_recorded_once_not_on_every_rerun(dash, logins):  # noqa: F811
    at = run()
    for _ in range(3):
        at.run(timeout=30)
    assert logins == [EMPLOYEE_EMAIL]


def test_reloading_the_page_with_the_same_google_login_is_not_a_new_login(dash, logins):  # noqa: F811
    run()
    run()
    assert logins == [EMPLOYEE_EMAIL]


def test_signing_in_again_is_a_new_login(monkeypatch, dash, logins):  # noqa: F811
    run()
    sign_in(monkeypatch, dash, employee(iat=1_800_000_000))
    run()
    assert logins == [EMPLOYEE_EMAIL, EMPLOYEE_EMAIL]


def test_without_the_login_time_each_session_records_once(monkeypatch, dash, logins):  # noqa: F811
    sign_in(monkeypatch, dash, employee(iat=None))
    at = run()
    at.run(timeout=30)
    run()
    assert logins == [EMPLOYEE_EMAIL, EMPLOYEE_EMAIL]


def test_a_failing_login_log_does_not_lock_the_employee_out(monkeypatch, dash):  # noqa: F811
    calls = []

    def broken(connect, email):
        calls.append(email)
        return False

    monkeypatch.setattr(access, "record_login", broken)
    at = run()
    at.run(timeout=30)
    assert not at.exception and [t.label for t in at.tabs] == PUBLIC_TABS
    assert not at.error
    assert calls == [EMPLOYEE_EMAIL], "одна попытка на вход, а не на каждую перерисовку"


def test_no_token_or_secret_is_ever_shown(monkeypatch, dash):  # noqa: F811
    sign_in(monkeypatch, dash, employee(at_hash="SECRET-AT-HASH", nonce="SECRET-NONCE",
                                        tokens={"id": "SECRET-ID-TOKEN"}))
    at = run()
    shown = " ".join([m.value for m in at.markdown] + [c.value for c in at.caption] + [i.value for i in at.info])
    assert "SECRET" not in shown


def test_the_google_login_dependencies_are_installed_and_listed():
    """st.login строит OAuth-клиента через Authlib + httpx. Без httpx кнопка «Войти через Google» вела
    на «Internal server error» (так и случилось на сайте 02.10.2026): httpx — прямая зависимость."""
    from authlib.integrations import starlette_client  # noqa: F401

    requirements = (PROJECT / "requirements.txt").read_text(encoding="utf-8").lower()
    assert "authlib" in requirements and "httpx" in requirements


def test_the_sign_in_screen_looks_like_rating_radar_and_reads_no_data(monkeypatch, no_data):
    sign_in(monkeypatch, no_data, {"is_logged_in": False})
    at = run()
    assert closed(at)
    text = " ".join(m.value for m in at.markdown)
    assert 'class="gate"' in text and "Доступ только для сотрудников maximumstores.online" in text
    assert "bot-link" in text  # бот в шапке, как в Radar
