"""Журнал действий (activity.py): сессии, время, разделы — без Streamlit и без настоящей базы."""

from datetime import datetime, timedelta, timezone

import activity
from test_access import FakeDb

T0 = datetime(2026, 10, 5, 9, 0, tzinfo=timezone.utc)


def event(session, email, minutes, section=None):
    return {"session_id": session, "email": email, "at": T0 + timedelta(minutes=minutes), "section": section}


def test_a_session_lasts_from_the_first_to_the_last_action():
    table = activity.sessions([event("s", "a", 0), event("s", "a", 5), event("s", "a", 12)])
    assert list(table["seconds"]) == [12 * 60]
    assert list(table["actions"]) == [3]


def test_a_long_pause_starts_a_new_session():
    rows = [event("s", "a", 0), event("s", "a", 10), event("s", "a", 10 + 31), event("s", "a", 45)]
    assert sorted(activity.sessions(rows)["seconds"]) == [4 * 60, 10 * 60]


def test_a_page_opened_and_left_is_a_zero_second_session():
    assert list(activity.sessions([event("s", "a", 0)])["seconds"]) == [0]


def test_time_by_employee_sums_averages_and_counts_short_sessions():
    rows = [event("s1", "a", 0), event("s1", "a", 20), event("s2", "a", 100), event("s3", "b", 0)]
    table = activity.time_by_employee(activity.sessions(rows)).to_dict("records")
    assert table == [
        {"email": "a", "sessions": 2, "avg": 600, "longest": 1200, "total": 1200, "short": 1},
        {"email": "b", "sessions": 1, "avg": 0, "longest": 0, "total": 0, "short": 1},
    ]


def test_sections_count_opens_people_and_share_and_keep_unopened_ones():
    rows = [event("s1", "a", 0, "Текущее"), event("s1", "a", 1, "Прогноз"), event("s1", "a", 2, "Прогноз"),
            event("s2", "b", 0, "Текущее"), event("s2", "b", 1)]
    table = activity.sections(rows, ["Текущее", "История", "Прогноз"]).to_dict("records")
    assert table == [
        {"section": "Текущее", "opens": 2, "employees": 2, "share": 100},
        {"section": "Прогноз", "opens": 2, "employees": 1, "share": 50},
        {"section": "История", "opens": 0, "employees": 0, "share": 0},
    ]


def test_empty_journal_gives_empty_tables():
    assert activity.sessions([]).empty
    assert activity.time_by_employee(activity.sessions([])).empty
    assert list(activity.sections([], ["A"])["opens"]) == [0]


def test_format_duration():
    assert [activity.format_duration(s) for s in (0, 45, 60, 12 * 60 + 5, 3600 + 5 * 60)] == [
        "0 сек", "45 сек", "1 мин", "12 мин", "1 ч 05 мин"]


def test_record_writes_one_row_and_skips_unnamed_actions():
    db = FakeDb()
    assert activity.record(db.connect, "sess", " a@x.com ", "📈 Прогноз") is True
    sql, params = db.executed[0]
    assert "INSERT INTO bsr_radar.session_events" in sql and params == ("sess", "a@x.com", "📈 Прогноз")
    assert activity.record(db.connect, "sess", None) is False
    assert len(db.executed) == 1


def test_a_broken_database_never_breaks_the_action():
    db = FakeDb(fail_execute='relation "bsr_radar.session_events" does not exist')
    assert activity.record(db.connect, "sess", "a@x.com") is False


def test_recent_reads_a_period_newest_first():
    db = FakeDb(rows=[("s", "a@x.com", T0, "Прогноз")])
    assert activity.recent(db.connect, 30, 10) == [{"session_id": "s", "email": "a@x.com", "at": T0, "section": "Прогноз"}]
    sql, params = db.executed[0]
    assert "make_interval(days => %s)" in sql and "ORDER BY at DESC" in sql and params == (30, 10)
