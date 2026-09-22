"""Сводка «что в работе» и заранее видимое решение проверки допуска. Без настоящей базы."""

from datetime import date, datetime, timezone

import pytest

import db_runs
import run_control
from run_control import Positions
from schedule_store import TZ


class FakeDb:
    def __init__(self, results, fail_connect=None, fail_execute=None):
        self.results = list(results)
        self.fail_connect = fail_connect
        self.fail_execute = fail_execute
        self.connects = 0
        self.executed = []

    def connect(self):
        self.connects += 1
        if self.fail_connect:
            raise RuntimeError(self.fail_connect)
        return self

    def cursor(self):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=()):
        if self.fail_execute:
            raise RuntimeError(self.fail_execute)
        self.executed.append((sql, params))

    def fetchall(self):
        return self.results.pop(0)

    def commit(self):
        pass

    def close(self):
        pass


def pair(market, our, comp, active=True):
    return (market, our, comp, active)


def test_positions_count_each_asin_once_even_when_it_is_in_several_pairs():
    pairs = [pair("US", "B0OUR00001", "B0COMP0001"), pair("US", "B0OUR00001", "B0COMP0002"), pair("US", "B0OUR00002", "B0COMP0001")]
    assert run_control.positions_summary(pairs) == Positions(total=4, ours=2, competitors=2, by_market={"US": 4})


def test_inactive_pairs_are_not_collected():
    pairs = [pair("US", "B0OUR00001", "B0COMP0001"), pair("CA", "B0OUR00009", "B0COMP0009", active=False)]
    assert run_control.positions_summary(pairs) == Positions(total=2, ours=1, competitors=1, by_market={"US": 2})


def test_an_asin_in_two_marketplaces_is_requested_once_on_the_later_pairs_market():
    pairs = [pair("US", "B0OUR00002", "B0COMP0001"), pair("CA", "B0OUR00001", "B0COMP0001")]
    result = run_control.positions_summary(pairs)
    assert result.total == 3
    assert result.by_market == {"CA": 1, "US": 2}
    assert run_control.positions_summary(reversed(pairs)) == result


def test_an_asin_that_is_ours_in_one_pair_and_a_competitor_in_another_counts_once_as_ours():
    pairs = [pair("US", "B0OUR00001", "B0OUR00002"), pair("US", "B0OUR00002", "B0COMP0001"), pair("CA", "B0OUR00003", "B0COMP0001")]
    result = run_control.positions_summary(pairs)
    assert result.ours == 3 and result.competitors == 1 and result.total == 4
    assert result.ours + result.competitors == result.total == sum(result.by_market.values())


def test_market_breakdown_adds_up_to_the_total():
    pairs = [pair("US", "B0OUR00001", "B0COMP0001"), pair("CA", "B0OUR00002", "B0COMP0002"), pair("CA", "B0OUR00003", "B0COMP0003")]
    result = run_control.positions_summary(pairs)
    assert sum(result.by_market.values()) == result.total == 6
    assert result.by_market == {"US": 2, "CA": 4}


def test_no_pairs_means_nothing_to_collect():
    empty = run_control.positions_summary([])
    assert empty == Positions(0, 0, 0, {})
    assert "нечего" in run_control.format_positions(empty)


def test_format_lists_markets_by_size_then_name():
    text = run_control.format_positions(Positions(total=611, ours=94, competitors=517, by_market={"DE": 68, "US": 273, "CA": 202, "AU": 68}))
    assert text == "В работе 611 ASIN: наших 94 · конкурентов 517 (US 273 · CA 202 · AU 68 · DE 68)"


KYIV_NOON = datetime(2026, 9, 21, 12, 0, tzinfo=TZ)


def preview_db(schedule=((9, 0),), unfinished=False, attempts=0, done=False):
    return FakeDb([list(schedule), [(unfinished,)], [(attempts, done)]])


def test_a_free_moment_is_allowed():
    assert run_control.admission_preview(preview_db().connect, KYIV_NOON) is None


@pytest.mark.parametrize("kwargs, expected", [
    ({"done": True, "attempts": 1}, "успешный сбор"),
    ({"unfinished": True}, "незавершённая"),
    ({"schedule": ()}, "не задано"),
    ({"attempts": 3}, "Лимит"),
])
def test_blocked_moments_explain_why(kwargs, expected):
    reason = run_control.admission_preview(preview_db(**kwargs).connect, KYIV_NOON)
    assert reason is not None and expected in reason


def test_before_the_scheduled_time_is_blocked():
    reason = run_control.admission_preview(preview_db(schedule=((23, 0),)).connect, KYIV_NOON)
    assert "не наступило" in reason


@pytest.mark.parametrize("kwargs", [
    {}, {"done": True, "attempts": 1}, {"unfinished": True}, {"schedule": ()}, {"attempts": 3}, {"schedule": ((23, 0),)},
])
def test_the_preview_is_exactly_the_gates_own_policy(kwargs):
    schedule = kwargs.get("schedule", ((9, 0),))
    expected = db_runs.admission_block_reason(
        now=KYIV_NOON, schedule=schedule[0] if schedule else None, attempts_today=kwargs.get("attempts", 0),
        successful_today=kwargs.get("done", False), unfinished=kwargs.get("unfinished", False), force=False,
    )
    assert run_control.admission_preview(preview_db(**kwargs).connect, KYIV_NOON) == expected


def test_the_preview_reads_over_one_connection_and_writes_nothing():
    db = preview_db()
    run_control.admission_preview(db.connect, KYIV_NOON)
    assert db.connects == 1 and len(db.executed) == 3
    assert all(sql.lstrip().upper().startswith("SELECT") for sql, _ in db.executed)


@pytest.mark.parametrize("now, day", [
    (datetime(2026, 9, 21, 12, 0, tzinfo=TZ), date(2026, 9, 21)),
    (datetime(2026, 9, 20, 22, 30, tzinfo=timezone.utc), date(2026, 9, 21)),
    (datetime(2026, 9, 21, 20, 59, tzinfo=timezone.utc), date(2026, 9, 21)),
    (datetime(2026, 9, 21, 21, 0, tzinfo=timezone.utc), date(2026, 9, 22)),
])
def test_todays_attempts_are_counted_for_the_kyiv_day(now, day):
    db = preview_db()
    run_control.admission_preview(db.connect, now)
    sql, params = db.executed[2]
    assert params == (day, "all") and "Europe/Kyiv" in sql


@pytest.mark.parametrize("scope", ["ours", "competitors"])
def test_a_scope_is_passed_through_to_the_daily_count_query(scope):
    db = preview_db()
    run_control.admission_preview(db.connect, KYIV_NOON, scope=scope)
    sql, params = db.executed[2]
    assert params[1] == scope and "scope = %s" in sql


def test_an_unknown_scope_is_refused_before_touching_the_database():
    db = preview_db()
    with pytest.raises(run_control.RunControlError):
        run_control.admission_preview(db.connect, KYIV_NOON, scope="bogus")
    assert db.connects == 0


def test_the_preview_matches_the_gates_own_policy_for_a_partial_scope():
    expected = db_runs.admission_block_reason(
        now=KYIV_NOON, schedule=(9, 0), attempts_today=2, successful_today=False, unfinished=False, force=False,
    )
    assert run_control.admission_preview(preview_db(attempts=2).connect, KYIV_NOON, scope="ours") == expected


def test_a_time_without_a_zone_is_refused_before_touching_the_database():
    db = preview_db()
    with pytest.raises(run_control.RunControlError):
        run_control.admission_preview(db.connect, datetime(2026, 9, 21, 12, 0))
    assert db.connects == 0


@pytest.mark.parametrize("db", [
    FakeDb([], fail_connect="host=secret password=hunter2"),
    FakeDb([], fail_execute="password=hunter2"),
])
def test_driver_errors_do_not_leak(db):
    with pytest.raises(run_control.RunControlError) as info:
        run_control.admission_preview(db.connect, KYIV_NOON)
    assert "hunter2" not in str(info.value) and "secret" not in str(info.value)


def test_an_invalid_schedule_in_the_database_is_reported_not_guessed():
    with pytest.raises(run_control.RunControlError):
        run_control.admission_preview(preview_db(schedule=((24, 0),)).connect, KYIV_NOON)
