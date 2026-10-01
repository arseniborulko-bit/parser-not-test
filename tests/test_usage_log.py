"""Изолированные тесты журнала использования (usage_log.py): ни Streamlit, ни настоящей базы."""

from datetime import datetime, timedelta, timezone

import pytest

import usage_log
from test_access import FakeDb

T0 = datetime(2026, 9, 30, 9, 0, tzinfo=timezone.utc)


def test_record_writes_one_row_with_the_tool_name_and_commits():
    db = FakeDb()
    assert usage_log.record(db.connect, "  Аня   Иванова ", "collect_all", 621, "ASIN") is True
    sql, params = db.executed[0]
    assert "INSERT INTO public.usage_logs" in sql
    assert params == ("Аня Иванова", usage_log.TOOL_NAME, "collect_all", 621, "ASIN")
    assert (db.commits, db.closed) == (1, 1)


def test_an_action_without_volume_is_recorded_without_a_unit():
    db = FakeDb()
    assert usage_log.record(db.connect, "ed@x.com", "schedule_save", None, "ASIN") is True
    assert db.executed[0][1] == ("ed@x.com", usage_log.TOOL_NAME, "schedule_save", None, None)


@pytest.mark.parametrize("volume", [-1, True, "12", 3.5])
def test_a_nonsense_volume_is_dropped_but_the_action_is_still_recorded(volume):
    db = FakeDb()
    assert usage_log.record(db.connect, "Аня", "pairs_add", volume, "пар") is True
    assert db.executed[0][1][3:] == (None, None)


@pytest.mark.parametrize("user", [None, "", "   ", 42])
def test_an_unnamed_person_is_not_recorded_and_the_database_is_not_touched(user):
    db = FakeDb()
    assert usage_log.record(db.connect, user, "collect_all", 5, "ASIN") is False
    assert db.connects == 0


@pytest.mark.parametrize("failure", [{"fail_connect": "no route"}, {"fail_execute": "relation does not exist"},
                                     {"fail_commit": "lost"}])
def test_a_broken_journal_never_breaks_the_action(failure):
    """Таблицы может ещё не быть (миграция 012 не применена), база может быть недоступна — кнопка всё равно сработала."""
    db = FakeDb(**failure)
    assert usage_log.record(db.connect, "Аня", "collect_all", 5, "ASIN") is False


def test_record_survives_a_connect_that_is_not_even_callable():
    assert usage_log.record(None, "Аня", "collect_all", 5, "ASIN") is False


def test_long_names_are_cut_so_a_pasted_essay_does_not_land_in_the_table():
    db = FakeDb()
    usage_log.record(db.connect, "я" * 500, "collect_all")
    assert len(db.executed[0][1][0]) == 120


def test_recent_reads_only_this_tool_and_maps_the_columns():
    db = FakeDb(rows=[(T0, "Аня", "collect_all", 621, "ASIN")])
    rows = usage_log.recent(db.connect, days=7, limit=50)
    sql, params = db.executed[0]
    assert "tool_name = %s" in sql and params == (usage_log.TOOL_NAME, 7, 50)
    assert rows == [{"created_at": T0, "user_name": "Аня", "action": "collect_all", "volume": 621, "unit": "ASIN"}]


def test_recent_and_log_exists_report_a_failure_without_driver_details():
    db = FakeDb(fail_execute="password=secret host=db.internal")
    for call in (usage_log.recent, usage_log.log_exists):
        with pytest.raises(usage_log.UsageLogError) as caught:
            call(db.connect)
        assert "secret" not in str(caught.value)


def test_log_exists_reads_the_flag():
    assert usage_log.log_exists(FakeDb(rows=[(True,)]).connect) is True
    assert usage_log.log_exists(FakeDb(rows=[(False,)]).connect) is False


def test_summary_counts_actions_distinct_days_and_the_last_action_per_person():
    rows = [
        {"created_at": T0 + timedelta(days=1), "user_name": "Аня", "action": "collect_all", "volume": 5, "unit": "ASIN"},
        {"created_at": T0, "user_name": "Аня", "action": "spot_check", "volume": 1, "unit": "ASIN"},
        {"created_at": T0 + timedelta(hours=1), "user_name": "Аня", "action": "pairs_add", "volume": 2, "unit": "пар"},
        {"created_at": T0, "user_name": "Боря", "action": "collect_all", "volume": 5, "unit": "ASIN"},
    ]
    assert usage_log.summarize(rows) == [
        {"user_name": "Аня", "actions": 3, "days": 2, "last_at": T0 + timedelta(days=1)},
        {"user_name": "Боря", "actions": 1, "days": 1, "last_at": T0},
    ]


def test_summary_of_nothing_is_empty():
    assert usage_log.summarize([]) == []
