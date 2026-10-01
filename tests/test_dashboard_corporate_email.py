"""Вход в управление по рабочей почте вместо пароля (секрет CORPORATE_EMAIL_DOMAINS): Streamlit AppTest,
без настоящей базы. Почта становится подписью в журнале использования и в журнале изменений."""

import pytest
from streamlit.testing.v1 import AppTest

import access
import usage_log
from test_dashboard_access import SCRIPT, TEAM_PASSWORD, dash, run, time_inputs, unlock  # noqa: F401

DOMAIN = "company.com"


@pytest.fixture
def email_mode(monkeypatch, dash):  # noqa: F811
    monkeypatch.delenv("TEAM_PASSWORD")
    monkeypatch.setenv("CORPORATE_EMAIL_DOMAINS", DOMAIN)
    recorded = []

    def fake_record(connect, user, action, volume=None, unit=None):
        recorded.append((user, action, volume, unit))
        return True

    monkeypatch.setattr(usage_log, "record", fake_record)
    return recorded


def enter_email(at, email):
    at.text_input(key="unlock_name").input(email)
    [b for b in at.button if b.label == "Открыть управление"][0].click()
    return at.run(timeout=30)


def test_management_is_closed_until_an_email_is_entered_and_no_password_is_asked(email_mode):
    at = run()
    assert not at.exception
    assert at.text_input(key="unlock_name").label == "Рабочая почта"
    assert not [t for t in at.text_input if t.key == "unlock_password"]
    assert not time_inputs(at)
    assert any("Режим просмотра" in info.value for info in at.info)
    assert email_mode == []


def test_a_corporate_email_opens_management_and_is_recorded_as_a_login(email_mode):
    at = enter_email(run(), "  Anna@Company.com ")
    assert not at.exception
    assert any("Управление открыто: anna@company.com" in c.value for c in at.caption)
    assert len(time_inputs(at)) == 1
    assert email_mode == [("anna@company.com", "login", None, None)]


@pytest.mark.parametrize("typed", ["anna@gmail.com", "anna@mail.company.com", "anna@evilcompany.com", "Аня", ""])
def test_a_personal_or_lookalike_address_keeps_management_closed(email_mode, typed):
    at = enter_email(run(), typed)
    assert not at.exception
    assert any("рабочую (корпоративную) почту" in e.value for e in at.error)
    assert not time_inputs(at)
    assert email_mode == []


def test_the_refusal_does_not_reveal_the_corporate_domain(email_mode):
    at = enter_email(run(), "anna@gmail.com")
    shown = " ".join([e.value for e in at.error] + [c.value for c in at.caption] + [m.value for m in at.markdown])
    assert DOMAIN not in shown


def test_the_email_signs_the_actions_that_follow(monkeypatch, email_mode):
    import schedule_store

    saves = []
    monkeypatch.setattr(schedule_store, "save_schedule",
                        lambda connect, hour, minute, enabled, *, slot, actor_role, actor: saves.append((actor, actor_role)))
    at = enter_email(run(), "anna@company.com")
    [b for b in at.button if b.label == "Сохранить"][0].click()
    at.run(timeout=30)
    assert saves == [("anna@company.com", access.ROLE_EDITOR)] * 2
    assert email_mode == [("anna@company.com", "login", None, None), ("anna@company.com", "schedule_save", None, None)]


def test_with_a_team_password_both_the_email_and_the_password_are_required(monkeypatch, email_mode):
    monkeypatch.setenv("TEAM_PASSWORD", TEAM_PASSWORD)
    at = unlock(run(), name="Аня")
    assert any("рабочую (корпоративную) почту" in e.value for e in at.error) and not time_inputs(at)
    at = unlock(at, name="anna@company.com", password="wrong-password")
    assert any("Неверный пароль" in e.value for e in at.error) and not time_inputs(at)
    at = unlock(at, name="anna@company.com")
    assert len(time_inputs(at)) == 1
    assert email_mode == [("anna@company.com", "login", None, None)]


@pytest.mark.parametrize("broken", ["company", "   ", "anna@company.com"])
def test_a_secret_without_a_usable_domain_closes_management_instead_of_opening_it(monkeypatch, email_mode, broken):
    monkeypatch.setenv("CORPORATE_EMAIL_DOMAINS", broken)
    at = run()
    assert not at.exception
    assert any("нет ни одного домена" in c.value for c in at.caption)
    assert not time_inputs(at) and not [t for t in at.text_input if t.key == "unlock_name"]


def test_a_secret_that_ended_up_inside_a_section_closes_management(monkeypatch, email_mode):
    monkeypatch.delenv("CORPORATE_EMAIL_DOMAINS")
    at = AppTest.from_string(SCRIPT)
    at.secrets["gcp_service_account"] = {"CORPORATE_EMAIL_DOMAINS": DOMAIN}
    at = at.run(timeout=30)
    assert not at.exception
    assert any("CORPORATE_EMAIL_DOMAINS стоит внутри секции" in c.value for c in at.caption)
    assert not time_inputs(at)


def test_the_state_reports_email_mode_only_without_a_password(monkeypatch, email_mode, dash):  # noqa: F811
    assert dash._team_password_state() == ("email", "")
    assert dash._management_open() is False
    monkeypatch.setenv("TEAM_PASSWORD", TEAM_PASSWORD)
    assert dash._team_password_state() == ("password", TEAM_PASSWORD)
    monkeypatch.delenv("TEAM_PASSWORD")
    monkeypatch.delenv("CORPORATE_EMAIL_DOMAINS")
    assert dash._team_password_state() == ("open", "")
