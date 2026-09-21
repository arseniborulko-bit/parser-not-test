"""Вкладка «Сбор и управление» целиком (Streamlit AppTest): без GitHub, ScrapingDog и настоящей базы."""

from types import SimpleNamespace

import pytest

import access
import collect_ui
import github_dispatch
import run_control
import spot_check
from test_dashboard_access import TEAM_PASSWORD, dash, run, unlock  # noqa: F401

GITHUB_TOKEN = "fake-github-token-value"
DOG_TOKEN = "fake-scrapingdog-token"
BLOCKED = "Сегодня уже был успешный сбор — пропуск."
DEFAULT_REPO = github_dispatch.DEFAULT_REPO


def run_button(at):
    return [b for b in at.button if b.key == "collect_run"][0]


def captions(at):
    return " ".join(c.value for c in at.caption)


@pytest.fixture
def collect_env(monkeypatch, dash):  # noqa: F811
    """Управление открыто (секрета TEAM_PASSWORD нет), токен GitHub есть, отправка и время подменены."""
    monkeypatch.delenv("TEAM_PASSWORD")
    monkeypatch.setenv("GITHUB_DISPATCH_TOKEN", GITHUB_TOKEN)
    state = {"dispatches": [], "now": 1_000_000.0, "gate": None, "result": github_dispatch.DispatchResult(True, "ok")}

    def fake_dispatch(token, repo=DEFAULT_REPO, **kwargs):
        state["dispatches"].append((token, repo))
        return state["result"]

    def fake_gate(connect, now):
        if isinstance(state["gate"], Exception):
            raise state["gate"]
        return state["gate"]

    monkeypatch.setattr(github_dispatch, "dispatch_collection", fake_dispatch)
    monkeypatch.setattr(run_control, "admission_preview", fake_gate)
    monkeypatch.setattr(collect_ui, "_now_ts", lambda: state["now"])
    return state


def test_the_tab_says_what_is_in_work_from_the_active_pairs(collect_env):
    at = run()
    assert not at.exception
    assert "⚙ Сбор и управление" in [t.label for t in at.tabs]
    assert "В работе 2 ASIN: наших 1 · конкурентов 1 (US 2)" in captions(at)
    assert "Каждый ASIN запрашивается один раз за сбор" in captions(at)


def test_without_a_github_token_the_button_is_off_and_explains_how_to_turn_it_on(monkeypatch, collect_env):
    monkeypatch.delenv("GITHUB_DISPATCH_TOKEN")
    at = run()
    assert run_button(at).disabled
    assert "GITHUB_DISPATCH_TOKEN" in captions(at)


def test_a_free_moment_enables_the_button(collect_env):
    at = run()
    assert not run_button(at).disabled
    assert "Второй успешный сбор за день не запустится" in captions(at)


def test_a_closed_gate_disables_the_button_and_shows_its_reason(monkeypatch, dash, collect_env):  # noqa: F811
    monkeypatch.setattr(dash, "_admission_preview_cached", lambda: BLOCKED)
    at = run()
    assert run_button(at).disabled
    assert any(BLOCKED in i.value for i in at.info)


def test_a_failing_preview_disables_the_button_with_a_warning(monkeypatch, dash, collect_env):  # noqa: F811
    def broken():
        raise run_control.RunControlError("Проверка возможности запуска не подтверждена (OperationalError).")

    monkeypatch.setattr(dash, "_admission_preview_cached", broken)
    at = run()
    assert not at.exception and run_button(at).disabled
    assert any("не подтверждена" in w.value for w in at.warning)


def test_clicking_sends_one_dispatch_with_the_token_and_repo_then_cools_down(collect_env):
    at = run()
    run_button(at).click()
    at.run(timeout=30)
    assert not at.exception
    assert collect_env["dispatches"] == [(GITHUB_TOKEN, DEFAULT_REPO)]
    assert any("Запуск отправлен в GitHub" in s.value for s in at.success)
    assert run_button(at).disabled
    assert "Запуск отправлен" in captions(at)
    collect_env["now"] += collect_ui.COOLDOWN_SECONDS + 1
    at.run(timeout=30)
    assert not run_button(at).disabled
    assert len(collect_env["dispatches"]) == 1


def test_the_repository_can_be_changed_in_the_secrets(monkeypatch, collect_env):
    monkeypatch.setenv("GITHUB_REPO", "acme/tracker")
    at = run()
    run_button(at).click()
    at.run(timeout=30)
    assert collect_env["dispatches"] == [(GITHUB_TOKEN, "acme/tracker")]


def test_the_click_asks_the_gate_again_and_does_not_dispatch_if_it_closed_meanwhile(collect_env):
    at = run()
    collect_env["gate"] = BLOCKED
    run_button(at).click()
    at.run(timeout=30)
    assert collect_env["dispatches"] == []
    assert any("Не запускаю" in w.value and BLOCKED in w.value for w in at.warning)


def test_a_dispatch_failure_is_shown_and_does_not_start_the_cooldown(collect_env):
    collect_env["result"] = github_dispatch.DispatchResult(False, "GitHub не принял токен (неверный или истёк).")
    at = run()
    run_button(at).click()
    at.run(timeout=30)
    assert any("не принял токен" in e.value for e in at.error)
    assert GITHUB_TOKEN not in " ".join(e.value for e in at.error)
    assert not run_button(at).disabled


def test_a_database_error_at_the_click_is_shown_and_nothing_is_sent(collect_env):
    collect_env["gate"] = run_control.RunControlError("Проверка возможности запуска не подтверждена (OperationalError).")
    at = run()
    run_button(at).click()
    at.run(timeout=30)
    assert not at.exception and collect_env["dispatches"] == []
    assert any("не подтверждена" in e.value for e in at.error)


def test_while_management_needs_a_password_the_run_button_stays_off_for_visitors(monkeypatch, collect_env):
    monkeypatch.setenv("TEAM_PASSWORD", TEAM_PASSWORD)
    at = run()
    assert run_button(at).disabled
    assert "Чтобы запускать сбор, откройте «🔒 Управление»" in captions(at)
    assert collect_env["dispatches"] == []


def test_after_the_password_is_entered_the_run_button_works(monkeypatch, collect_env):
    monkeypatch.setenv("TEAM_PASSWORD", TEAM_PASSWORD)
    at = unlock(run())
    assert not run_button(at).disabled
    run_button(at).click()
    at.run(timeout=30)
    assert collect_env["dispatches"] == [(GITHUB_TOKEN, DEFAULT_REPO)]


def test_the_click_handler_itself_refuses_without_rights(monkeypatch, collect_env):
    import streamlit as st

    monkeypatch.setattr(st, "session_state", {})
    collect_ui._run_callback(object(), GITHUB_TOKEN, DEFAULT_REPO, False)
    assert collect_env["dispatches"] == []
    assert "только с открытым управлением" in st.session_state["collect_flash"][1]


def fake_scraping_module():
    def fetch(asin, domain=None, token=None):
        return {"asin": asin, "domain": domain} if asin != "B0ABCDEFG9" else None

    def parse(data, asin=""):
        return SimpleNamespace(title=f"Title {asin}", price=10.0, bsr="123", stock_status="In Stock", stars=4.0,
                               reviews=5, category="Cat", brand="Brand")

    return lambda: SimpleNamespace(fetch_product=fetch, parse_product=parse)


def submit_spot(at):
    [b for b in at.button if b.label == "Выполнить проверку"][0].click()
    return at.run(timeout=30)


def test_without_a_scrapingdog_token_the_spot_check_is_not_shown_at_all(collect_env):
    at = run()
    assert not at.exception
    assert not any("Точечная проверка" in m.value for m in at.markdown)
    assert not any("Точечная проверка" in i.value or "SCRAPINGDOG_TOKEN" in i.value for i in at.info)
    assert not [t for t in at.text_area if t.key == "spot_text"]


def test_the_spot_check_shows_results_and_the_remaining_budget(monkeypatch, collect_env):
    monkeypatch.setenv("SCRAPINGDOG_TOKEN", DOG_TOKEN)
    monkeypatch.setattr(spot_check, "_load_scraping", fake_scraping_module())
    at = run()
    at.text_area(key="spot_text").input("B0ABCDEFG1 B0ABCDEFG9:CA")
    at = submit_spot(at)
    assert not at.exception
    table = [d.value for d in at.dataframe if {"ASIN", "Статус"} <= set(d.value.columns)][0]
    assert list(table["ASIN"]) == ["B0ABCDEFG1", "B0ABCDEFG9"]
    assert list(table["Статус"]) == ["ок", "не получено"]
    assert list(table["Страна"]) == ["US", "CA"]
    assert f"Осталось проверок: {spot_check.HOUR_LIMIT - 2}" in captions(at)
    assert DOG_TOKEN not in str(table.to_dict()) and DOG_TOKEN not in captions(at)


def test_spot_check_input_problems_are_reported_without_spending_anything(monkeypatch, collect_env):
    monkeypatch.setenv("SCRAPINGDOG_TOKEN", DOG_TOKEN)
    monkeypatch.setattr(spot_check, "_load_scraping", fake_scraping_module())
    at = submit_spot(run())
    assert any("Вставьте ASIN" in e.value for e in at.error)
    at.text_area(key="spot_text").input(" ".join(f"B0{i:08d}" for i in range(spot_check.MAX_PER_CHECK + 1)))
    at = submit_spot(at)
    assert any("не больше" in e.value for e in at.error)
    assert f"Осталось проверок: {spot_check.HOUR_LIMIT}" in captions(at)


def test_an_exhausted_spot_budget_is_reported(monkeypatch, collect_env):
    monkeypatch.setenv("SCRAPINGDOG_TOKEN", DOG_TOKEN)
    monkeypatch.setattr(spot_check, "_load_scraping", fake_scraping_module())
    monkeypatch.setattr(collect_ui, "_spot_budget", lambda: spot_check.SpotBudget(hour=1, day=1))
    at = run()
    at.text_area(key="spot_text").input("B0ABCDEFG1 B0ABCDEFG2")
    at = submit_spot(at)
    assert any("Лимит точечных проверок исчерпан" in e.value for e in at.error)


def test_the_spot_check_needs_open_management_too(monkeypatch, collect_env):
    monkeypatch.setenv("TEAM_PASSWORD", TEAM_PASSWORD)
    monkeypatch.setenv("SCRAPINGDOG_TOKEN", DOG_TOKEN)
    at = run()
    assert not [t for t in at.text_area if t.key == "spot_text"]
    assert "Чтобы проверять ASIN, откройте «🔒 Управление»" in captions(at)


def test_the_refresh_button_clears_the_cache_for_everyone(monkeypatch, collect_env):
    import streamlit as st

    cleared = []
    monkeypatch.setattr(st.cache_data, "clear", lambda: cleared.append(1))
    monkeypatch.setenv("TEAM_PASSWORD", TEAM_PASSWORD)
    at = run()
    [b for b in at.button if b.key == "collect_refresh"][0].click()
    at.run(timeout=30)
    assert not at.exception and cleared
    assert any("Данные обновлены" in s.value for s in at.success)


def test_the_schedule_is_still_on_the_same_tab_under_its_own_heading(collect_env):
    at = run()
    assert any("Автосбор включён: каждый день после 09:00" in s.value for s in at.success)
    assert any(m.value.count("Автосбор") and "section-title" in m.value for m in at.markdown)
