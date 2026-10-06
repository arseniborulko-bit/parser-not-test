"""Журнал использования в дашборде (Streamlit AppTest): что записывается при действиях и что видно во вкладке
«Активность дашборда» (раздел «Кто что меняет»). Настоящей базы нет — usage_log подменён."""

from datetime import datetime, timezone

import pytest

import collect_ui
import pairs_store
import pairs_ui
import schedule_store
import spot_check
import usage_log
from test_dashboard_access import JOURNAL, TEAM_PASSWORD, dash, run, run_on_tab, unlock  # noqa: F401
from test_dashboard_collect import DOG_TOKEN, collect_env, fake_scraping_module, run_button, submit_spot  # noqa: F401


@pytest.fixture
def recorded(monkeypatch):
    rows = []

    def fake_record(connect, user, action, volume=None, unit=None):
        rows.append((user, action, volume, unit))
        return True

    monkeypatch.setattr(usage_log, "record", fake_record)
    return rows


def test_a_dispatched_collection_is_recorded_with_who_and_how_many_asins(collect_env, recorded):  # noqa: F811
    at = run()
    run_button(at).click()
    at.run(timeout=30)
    assert recorded == [("Команда", "collect_all", 2, "ASIN")]


def test_a_partial_collection_records_its_own_scope_and_count(collect_env, recorded):  # noqa: F811
    at = run()
    run_button(at, "collect_run_ours").click()
    at.run(timeout=30)
    assert recorded == [("Команда", "collect_ours", 1, "ASIN")]


def test_the_person_who_unlocked_with_the_password_is_named_in_the_record(monkeypatch, collect_env, recorded):  # noqa: F811
    monkeypatch.setenv("TEAM_PASSWORD", TEAM_PASSWORD)
    at = unlock(run(), name="Аня")
    run_button(at).click()
    at.run(timeout=30)
    assert recorded == [("Аня", "login", None, None), ("Аня", "collect_all", 2, "ASIN")]


def test_a_refused_or_failed_dispatch_is_not_recorded(collect_env, recorded):  # noqa: F811
    import github_dispatch

    collect_env["result"] = github_dispatch.DispatchResult(False, "GitHub недоступен")
    at = run()
    run_button(at).click()
    at.run(timeout=30)
    assert recorded == []


def test_a_spot_check_is_recorded_with_the_number_of_asins(monkeypatch, collect_env, recorded):  # noqa: F811
    monkeypatch.setenv("SCRAPINGDOG_TOKEN", DOG_TOKEN)
    monkeypatch.setattr(spot_check, "_load_scraping", fake_scraping_module())
    at = run()
    at.text_area(key="spot_text").input("B0ABCDEFG1 B0ABCDEFG9:CA")
    submit_spot(at)
    assert recorded == [("Команда", "spot_check", 2, "ASIN")]


def test_a_rejected_spot_check_is_not_recorded(monkeypatch, collect_env, recorded):  # noqa: F811
    monkeypatch.setenv("SCRAPINGDOG_TOKEN", DOG_TOKEN)
    monkeypatch.setattr(spot_check, "_load_scraping", fake_scraping_module())
    monkeypatch.setattr(collect_ui, "_spot_budget", lambda: spot_check.SpotBudget(hour=1, day=1))
    at = run()
    at.text_area(key="spot_text").input("B0ABCDEFG1 B0ABCDEFG2")
    submit_spot(at)
    assert recorded == []


def test_saving_the_schedule_is_recorded(monkeypatch, dash, recorded):  # noqa: F811
    monkeypatch.setattr(schedule_store, "save_schedule", lambda *args, **kwargs: None)
    at = unlock(run(), name="Аня")
    [b for b in at.button if b.label == "Сохранить"][0].click()
    at.run(timeout=30)
    assert recorded == [("Аня", "login", None, None), ("Аня", "schedule_save", None, None)]


def test_pair_changes_are_recorded_only_when_something_really_changed(monkeypatch, recorded):
    import streamlit as st

    monkeypatch.setattr(st, "session_state", {})
    monkeypatch.setattr(pairs_store, "set_pairs_active", lambda connect, keys, active, **kwargs: len(keys))
    pairs_ui._toggle_callback(None, [("US", "B0A", "B0B"), ("US", "B0A", "B0C")], False, "Аня", "editor")
    pairs_ui._toggle_callback(None, [("US", "B0A", "B0B")], True, "Аня", "editor")
    pairs_ui._toggle_callback(None, [], True, "Аня", "editor")
    assert recorded == [("Аня", "pairs_disable", 2, "пар"), ("Аня", "pairs_enable", 1, "пар")]


def test_a_failed_pair_change_is_not_recorded(monkeypatch, recorded):
    import streamlit as st

    def broken(connect, keys, active, **kwargs):
        raise pairs_store.PairsStoreError("база недоступна")

    monkeypatch.setattr(st, "session_state", {})
    monkeypatch.setattr(pairs_store, "set_pairs_active", broken)
    pairs_ui._toggle_callback(None, [("US", "B0A", "B0B")], False, "Аня", "editor")
    assert recorded == []



def journal_tables(at):
    return [d.value for d in at.tabs[-1].dataframe]


def forbid_reading(monkeypatch):
    def must_not_be_called(*args, **kwargs):
        raise AssertionError("журнал правок читается только на вкладке «Активность дашборда»")

    monkeypatch.setattr(usage_log, "log_exists", must_not_be_called)
    monkeypatch.setattr(usage_log, "recent", must_not_be_called)


def test_the_usage_toggle_is_gone_from_the_collect_tab(monkeypatch, dash):  # noqa: F811
    forbid_reading(monkeypatch)
    at = unlock(run())
    assert not at.exception
    assert not [t for t in at.toggle if t.key == "usage_show"]


USAGE_ROWS = [
    {"created_at": datetime(2026, 9, 21, 6, 30, tzinfo=timezone.utc), "user_name": "anna@maximumstores.online",
     "action": "collect_all", "volume": 621, "unit": "ASIN"},
    {"created_at": datetime(2026, 9, 20, 6, 0, tzinfo=timezone.utc), "user_name": "anna@maximumstores.online",
     "action": "schedule_save", "volume": None, "unit": None},
    {"created_at": datetime(2026, 9, 20, 5, 0, tzinfo=timezone.utc), "user_name": "Боря",
     "action": "pairs_add", "volume": 3, "unit": "пар"},
    {"created_at": datetime(2026, 9, 19, 5, 0, tzinfo=timezone.utc), "user_name": "Боря",
     "action": "pairs_edit", "volume": 1, "unit": "пар"},
    {"created_at": datetime(2026, 9, 19, 4, 0, tzinfo=timezone.utc), "user_name": "Вера",
     "action": "export_excel", "volume": 10, "unit": "строк"},
    {"created_at": datetime(2026, 9, 19, 3, 0, tzinfo=timezone.utc), "user_name": "Вера",
     "action": "login", "volume": None, "unit": None},
]


def test_the_journal_shows_who_edits_runs_and_exports(monkeypatch, dash):  # noqa: F811
    monkeypatch.setattr(usage_log, "log_exists", lambda connect: True)
    monkeypatch.setattr(usage_log, "recent", lambda connect, days: USAGE_ROWS)
    at = run_on_tab(JOURNAL)
    assert not at.exception
    changes = next(t for t in journal_tables(at) if "Правок" in t.columns)
    assert changes.to_dict("records") == [
        {"Сотрудник": "Боря", "Правок": 2, "Запусков": 0, "Выгрузок": 0, "Последняя правка": "20.09 08:00"},
        {"Сотрудник": "anna", "Правок": 1, "Запусков": 1, "Выгрузок": 0, "Последняя правка": "20.09 09:00"},
        {"Сотрудник": "Вера", "Правок": 0, "Запусков": 0, "Выгрузок": 1, "Последняя правка": "—"},
    ]
    log = next(t for t in journal_tables(at) if "Что" in t.columns)
    assert log.to_dict("records")[0] == {"Когда (Киев)": "21.09 09:30", "Кто": "anna", "Что": "Сбор: всё",
                                         "Объём": "621 ASIN"}


def test_without_the_table_it_says_the_journal_is_not_connected_yet(monkeypatch, dash):  # noqa: F811
    monkeypatch.setattr(usage_log, "log_exists", lambda connect: False)
    at = run_on_tab(JOURNAL)
    assert not at.exception and not at.error
    assert any("ещё не подключён" in i.value for i in at.tabs[-1].info)


def test_an_unavailable_database_is_reported_without_crashing(monkeypatch, dash):  # noqa: F811
    def broken(connect):
        raise usage_log.UsageLogError("Операция с журналом использования не подтверждена (OperationalError).")

    monkeypatch.setattr(usage_log, "log_exists", broken)
    at = run_on_tab(JOURNAL)
    assert not at.exception
    assert any("журналом использования" in e.value for e in at.tabs[-1].error)
