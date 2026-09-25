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


def run_button(at, key="collect_run"):
    return [b for b in at.button if b.key == key][0]


def captions(at):
    return " ".join(c.value for c in at.caption)


@pytest.fixture
def collect_env(monkeypatch, dash):  # noqa: F811
    """Управление открыто (секрета TEAM_PASSWORD нет), токен GitHub есть, отправка и время подменены."""
    monkeypatch.delenv("TEAM_PASSWORD")
    monkeypatch.setenv("GITHUB_DISPATCH_TOKEN", GITHUB_TOKEN)
    state = {"dispatches": [], "now": 1_000_000.0, "gate": None, "gate_by_scope": {},
             "result": github_dispatch.DispatchResult(True, "ok")}

    def fake_dispatch(token, repo=DEFAULT_REPO, *, scope="all", force=False, **kwargs):
        state["dispatches"].append((token, repo, scope, force))
        return state["result"]

    def fake_gate(connect, now, scope="all", force=False):
        state.setdefault("gate_calls", []).append((scope, force))
        key = (scope, True) if force and (scope, True) in state["gate_by_scope"] else scope
        value = state["gate_by_scope"].get(key, state["gate"])
        if isinstance(value, Exception):
            raise value
        return value

    monkeypatch.setattr(github_dispatch, "dispatch_collection", fake_dispatch)
    monkeypatch.setattr(run_control, "admission_preview", fake_gate)
    monkeypatch.setattr(collect_ui, "_now_ts", lambda: state["now"])
    return state


def test_the_tab_renders_without_an_explanatory_caption(collect_env):
    """Владелец не хочет пояснительных подписей в интерфейсе: числа видны на самих кнопках."""
    at = run()
    assert not at.exception
    assert "⚙ Сбор и управление" in [t.label for t in at.tabs]
    assert "В работе 2 ASIN" not in " ".join(captions(at))
    assert "Собрать всё (2)" in " ".join(b.label for b in at.button)


def test_without_a_github_token_the_button_is_off_and_nothing_is_explained(monkeypatch, collect_env):
    monkeypatch.delenv("GITHUB_DISPATCH_TOKEN")
    at = run()
    assert run_button(at).disabled
    assert "GITHUB_DISPATCH_TOKEN" not in captions(at)


def test_a_free_moment_enables_the_button(collect_env):
    at = run()
    assert not run_button(at).disabled


def test_a_closed_gate_disables_the_button_without_explaining_why(monkeypatch, dash, collect_env):  # noqa: F811
    """Владелец не хочет пояснительных подписей: кнопка просто неактивна, без текста причины."""
    monkeypatch.setattr(dash, "_admission_preview_cached", lambda scope="all": BLOCKED)
    at = run()
    assert run_button(at).disabled
    assert BLOCKED not in captions(at)


def test_a_failing_preview_disables_the_button_with_a_warning(monkeypatch, dash, collect_env):  # noqa: F811
    def broken(scope="all"):
        raise run_control.RunControlError("Проверка возможности запуска не подтверждена (OperationalError).")

    monkeypatch.setattr(dash, "_admission_preview_cached", broken)
    at = run()
    assert not at.exception and run_button(at).disabled
    assert any("не подтверждена" in c.value for c in at.caption)


def test_clicking_sends_one_dispatch_with_the_token_and_repo_then_cools_down(collect_env):
    at = run()
    run_button(at).click()
    at.run(timeout=30)
    assert not at.exception
    assert collect_env["dispatches"] == [(GITHUB_TOKEN, DEFAULT_REPO, "all", False)]
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
    assert collect_env["dispatches"] == [(GITHUB_TOKEN, "acme/tracker", "all", False)]


def test_the_partial_scope_buttons_show_their_own_counts_and_dispatch_their_own_scope(collect_env):
    at = run()
    assert run_button(at, "collect_run_ours").label == "Собрать наши (1)"
    assert run_button(at, "collect_run_competitors").label == "Собрать конкурентов (1)"
    run_button(at, "collect_run_ours").click()
    at.run(timeout=30)
    assert collect_env["dispatches"] == [(GITHUB_TOKEN, DEFAULT_REPO, "ours", False)]


def test_a_scope_blocked_today_does_not_disable_the_other_scopes(monkeypatch, dash, collect_env):  # noqa: F811
    monkeypatch.setattr(dash, "_admission_preview_cached", lambda scope="all": BLOCKED if scope == "all" else None)
    at = run()
    assert run_button(at).disabled
    assert not run_button(at, "collect_run_ours").disabled
    assert not run_button(at, "collect_run_competitors").disabled


def test_no_force_override_appears_when_the_button_is_not_blocked(collect_env):
    at = run()
    assert not [b for b in at.button if b.key == "collect_run_force"]


def test_the_force_override_appears_when_blocked_and_works_in_one_click(monkeypatch, dash, collect_env):  # noqa: F811
    """Решение владельца 25.09.2026: «уже был сбор сегодня» можно обойти вручную, одним кликом —
    без отдельного подтверждения, это платный повторный сбор и так понятно без объяснений."""
    monkeypatch.setattr(dash, "_admission_preview_cached", lambda scope="all": BLOCKED if scope == "all" else None)
    collect_env["gate_by_scope"]["all"] = BLOCKED
    collect_env["gate_by_scope"][("all", True)] = None  # force снимает именно этот блок
    at = run()
    assert run_button(at).disabled
    assert BLOCKED not in captions(at), "владелец не хочет пояснительных подписей"
    force_button = [b for b in at.button if b.key == "collect_run_force"][0]
    assert not force_button.disabled
    force_button.click()
    at = at.run(timeout=30)
    assert collect_env["dispatches"] == [(GITHUB_TOKEN, DEFAULT_REPO, "all", True)]
    assert any("Повторный запуск отправлен в GitHub" in s.value for s in at.success)


def test_the_force_button_does_the_real_check_live_not_the_stale_caption(monkeypatch, dash, collect_env):  # noqa: F811
    """Подпись под кнопкой могла устареть за 20 секунд кэша; решение принимает живой вызов."""
    monkeypatch.setattr(dash, "_admission_preview_cached", lambda scope="all": BLOCKED if scope == "all" else None)
    collect_env["gate_by_scope"]["all"] = BLOCKED
    collect_env["gate_by_scope"][("all", True)] = "Есть незавершённая попытка — сбор заблокирован."
    at = run()
    [b for b in at.button if b.key == "collect_run_force"][0].click()
    at = at.run(timeout=30)
    assert collect_env["dispatches"] == []
    assert any("Не запускаю" in w.value and "незавершённая" in w.value for w in at.warning)


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
    assert collect_env["dispatches"] == [(GITHUB_TOKEN, DEFAULT_REPO, "all", False)]


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
