"""Расписание автосбора: правила и SQL без настоящей базы."""

import logging
from datetime import date, datetime, timezone

import pytest

import access
import schedule_store
from schedule_store import Schedule, TZ


class FakeDb:
    def __init__(self, results=None, rowcount=1, fail_connect=None, fail_execute=None, fail_commit=None):
        self.results = list(results or [])
        self.rowcount = rowcount
        self.fail_connect = fail_connect
        self.fail_execute = fail_execute
        self.fail_commit = fail_commit
        self.connects = 0
        self.executed = []
        self.commits = 0
        self.closed = 0

    def connect(self):
        self.connects += 1
        if self.fail_connect:
            raise RuntimeError(self.fail_connect)
        return FakeConn(self)


class FakeConn:
    def __init__(self, db):
        self.db = db

    def cursor(self):
        return FakeCursor(self.db)

    def commit(self):
        if self.db.fail_commit:
            raise RuntimeError(self.db.fail_commit)
        self.db.commits += 1

    def close(self):
        self.db.closed += 1


class FakeCursor:
    def __init__(self, db):
        self.db = db
        self.rowcount = -1

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=()):
        if self.db.fail_execute:
            raise RuntimeError(self.db.fail_execute)
        self.db.executed.append((sql, params))
        self.rowcount = self.db.rowcount

    def fetchall(self):
        return self.db.results.pop(0)


def kyiv(*args):
    return datetime(*args, tzinfo=TZ)


@pytest.mark.parametrize("given, expected", [
    ((9, 0), (9, 0)), ((9, 7), (9, 0)), ((9, 29), (9, 0)), ((9, 30), (9, 30)),
    ((9, 59), (9, 30)), ((23, 45), (23, 30)), ((0, 0), (0, 0)),
])
def test_to_slot_rounds_down_to_half_hours(given, expected):
    assert schedule_store.to_slot(*given) == expected


def test_disabled_schedule_has_no_next_run():
    assert schedule_store.next_run(kyiv(2026, 9, 21, 8, 0), None, False) is None


def test_next_run_is_today_when_time_has_not_come_yet():
    result = schedule_store.next_run(kyiv(2026, 9, 21, 8, 0), Schedule(9, 0), False)
    assert result == schedule_store.NextRun(kyiv(2026, 9, 21, 9, 0), False)


@pytest.mark.parametrize("now", [kyiv(2026, 9, 21, 9, 0), kyiv(2026, 9, 21, 15, 45)])
def test_time_already_passed_without_collection_is_due_now(now):
    result = schedule_store.next_run(now, Schedule(9, 0), False)
    assert result.due_now is True
    assert result.when == kyiv(2026, 9, 21, 9, 0)


@pytest.mark.parametrize("now", [kyiv(2026, 9, 21, 8, 0), kyiv(2026, 9, 21, 20, 0)])
def test_collected_today_moves_next_run_to_tomorrow(now):
    result = schedule_store.next_run(now, Schedule(9, 0), True)
    assert result == schedule_store.NextRun(kyiv(2026, 9, 22, 9, 0), False)


def test_utc_input_is_read_in_kyiv_time():
    utc_evening = datetime(2026, 9, 20, 22, 30, tzinfo=timezone.utc)
    result = schedule_store.next_run(utc_evening, Schedule(9, 0), False)
    assert result.when == kyiv(2026, 9, 21, 9, 0) and result.due_now is False


def test_next_day_keeps_wall_clock_time_across_the_autumn_clock_change():
    result = schedule_store.next_run(kyiv(2026, 10, 24, 12, 0), Schedule(9, 0), True)
    assert (result.when.day, result.when.hour, result.when.minute) == (25, 9, 0)


def test_load_overview_reads_everything_over_one_connection():
    started = kyiv(2026, 9, 20, 9, 5)
    finished = kyiv(2026, 9, 20, 9, 17)
    db = FakeDb(results=[[(9, 0)], [(True,)], [(started, finished, "done", None), (started, None, "running", None)]])
    result = schedule_store.load_overview(db.connect, kyiv(2026, 9, 21, 8, 0))
    assert result.schedule == Schedule(9, 0)
    assert result.collected_today is True
    assert [run["status"] for run in result.runs] == ["done", "running"]
    assert result.runs[0]["finished_at"] == finished
    assert (db.connects, db.commits, db.closed) == (1, 1, 1)
    assert len(db.executed) == 3


@pytest.mark.parametrize("now, expected_day", [
    (kyiv(2026, 9, 21, 8, 0), date(2026, 9, 21)),
    (datetime(2026, 9, 20, 22, 30, tzinfo=timezone.utc), date(2026, 9, 21)),
    (datetime(2026, 9, 21, 20, 59, tzinfo=timezone.utc), date(2026, 9, 21)),
    (datetime(2026, 9, 21, 21, 0, tzinfo=timezone.utc), date(2026, 9, 22)),
])
def test_collected_today_question_uses_the_kyiv_calendar_day(now, expected_day):
    db = FakeDb(results=[[], [(False,)], []])
    schedule_store.load_overview(db.connect, now)
    sql, params = db.executed[1]
    assert params == (expected_day,)
    assert "Europe/Kyiv" in sql


def test_missing_schedule_row_means_autocollection_is_off():
    db = FakeDb(results=[[], [(False,)], []])
    result = schedule_store.load_overview(db.connect, kyiv(2026, 9, 21, 8, 0))
    assert result == schedule_store.Overview(None, False, [])


@pytest.mark.parametrize("db", [
    FakeDb(fail_connect="host=secret-host password=hunter2"),
    FakeDb(fail_execute="password=hunter2"),
    FakeDb(results=[[], [(False,)], []], fail_commit="password=hunter2"),
])
def test_driver_errors_are_wrapped_without_leaking_their_text(db):
    with pytest.raises(schedule_store.ScheduleStoreError) as info:
        schedule_store.load_overview(db.connect, kyiv(2026, 9, 21, 8, 0))
    assert "hunter2" not in str(info.value) and "secret-host" not in str(info.value)


@pytest.mark.parametrize("role", [access.ROLE_EDITOR, access.ROLE_ADMIN])
def test_enabled_save_upserts_the_single_schedule_row(role):
    db = FakeDb()
    schedule_store.save_schedule(db.connect, 10, 30, True, actor_role=role, actor="Аня")
    sql, params = db.executed[0]
    assert "INSERT INTO parser_not_test.schedule" in sql and "ON CONFLICT (id)" in sql
    assert params == (10, 30)
    assert (db.commits, db.closed) == (1, 1)


def test_disabled_save_removes_the_schedule_row():
    db = FakeDb()
    schedule_store.save_schedule(db.connect, 10, 30, False, actor_role=access.ROLE_EDITOR, actor="Аня")
    sql, params = db.executed[0]
    assert sql.strip().startswith("DELETE FROM parser_not_test.schedule WHERE id = 1")
    assert params == ()
    assert db.commits == 1


@pytest.mark.parametrize("role", [None, "viewer", "superuser", ""])
def test_save_without_editor_role_is_denied_and_never_touches_the_database(role):
    db = FakeDb()
    with pytest.raises(access.AccessDenied):
        schedule_store.save_schedule(db.connect, 10, 30, True, actor_role=role, actor="Аня")
    with pytest.raises(access.AccessDenied):
        schedule_store.save_schedule(db.connect, 10, 30, False, actor_role=role, actor="Аня")
    assert db.connects == 0


@pytest.mark.parametrize("hour, minute", [(24, 0), (-1, 0), (9, 60), (9, -1), (None, 0), (9.5, 0), (True, 0), ("9", 0), (9, None)])
def test_invalid_time_is_rejected_without_database_access(hour, minute):
    db = FakeDb()
    with pytest.raises(ValueError):
        schedule_store.save_schedule(db.connect, hour, minute, True, actor_role=access.ROLE_EDITOR, actor="Аня")
    assert db.connects == 0


@pytest.mark.parametrize("db", [
    FakeDb(fail_connect="password=hunter2"),
    FakeDb(fail_execute="password=hunter2"),
    FakeDb(fail_commit="password=hunter2"),
])
def test_save_errors_are_wrapped_without_leaking_their_text(db):
    with pytest.raises(schedule_store.ScheduleStoreError) as info:
        schedule_store.save_schedule(db.connect, 10, 30, True, actor_role=access.ROLE_EDITOR, actor="Аня")
    assert "hunter2" not in str(info.value)


def test_every_change_is_logged_with_who_made_it(caplog):
    with caplog.at_level(logging.INFO, logger="schedule_store"):
        schedule_store.save_schedule(FakeDb().connect, 9, 30, True, actor_role=access.ROLE_EDITOR, actor="Аня")
        schedule_store.save_schedule(FakeDb().connect, 9, 30, False, actor_role=access.ROLE_EDITOR, actor="Борис")
    assert "Аня" in caplog.text and "09:30" in caplog.text
    assert "Борис" in caplog.text and "выключен" in caplog.text
