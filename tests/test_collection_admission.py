"""Изолированные тесты допуска. Моки НЕ доказывают конкуренцию PostgreSQL."""

from datetime import datetime, timezone

import pytest

import db_runs


NOW = datetime(2026, 9, 18, 12, 0, tzinfo=timezone.utc)
INVOCATION = db_runs.Invocation("github_actions", "github:example/project:100:1")


@pytest.fixture(autouse=True)
def forbid_real_database(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "unused-test-dsn")

    def forbidden(*args, **kwargs):
        raise AssertionError("Real database connections are forbidden in unit tests")

    monkeypatch.setattr(db_runs.psycopg2, "connect", forbidden)


def policy(**overrides):
    arguments = dict(now=NOW, schedule=(9, 0), attempts_today=0,
                     successful_today=False, unfinished=False, force=False)
    arguments.update(overrides)
    return db_runs.admission_block_reason(**arguments)


@pytest.mark.parametrize("count", [0, 1, 2])
def test_below_limit_allows(count):
    assert policy(attempts_today=count) is None


@pytest.mark.parametrize("count", [3, 4, 100])
def test_limit_blocks(count):
    assert policy(attempts_today=count) is not None


def test_force_overrides_only_limit():
    assert policy(attempts_today=3, force=True) is None


@pytest.mark.parametrize("override", [
    {"unfinished": True}, {"successful_today": True}, {"schedule": None},
    {"schedule": (23, 0)},
])
@pytest.mark.parametrize("force", [False, True])
def test_other_blocks_cannot_be_forced(override, force):
    assert policy(**override, force=force) is not None


def test_schedule_uses_kyiv_not_utc():
    assert policy(now=datetime(2026, 9, 18, 6, 0, tzinfo=timezone.utc)) is None
    assert policy(now=datetime(2026, 9, 18, 5, 59, tzinfo=timezone.utc)) is not None


@pytest.mark.parametrize("override", [
    {"now": datetime(2026, 9, 18)}, {"attempts_today": -1}, {"schedule": (24, 0)},
])
def test_invalid_policy_input_fails(override):
    with pytest.raises(db_runs.RunStoreError):
        policy(**override)


def github_environment(**overrides):
    env = dict(GITHUB_ACTIONS="true", GITHUB_EVENT_NAME="workflow_dispatch",
               GITHUB_REPOSITORY="example/project", GITHUB_RUN_ID="100",
               GITHUB_RUN_ATTEMPT="1")
    env.update(overrides)
    return env


def test_github_owner_is_bound_to_repository_run_and_attempt():
    first = db_runs.invocation_from_environment(github_environment())
    assert first == INVOCATION
    second = db_runs.invocation_from_environment(github_environment(GITHUB_RUN_ATTEMPT="2"))
    assert second.owner_key != first.owner_key


def test_explicit_manual_force():
    assert db_runs.invocation_from_environment(github_environment(FORCE_COLLECTION="true")).force


@pytest.mark.parametrize("override", [
    {"GITHUB_RUN_ID": ""}, {"GITHUB_RUN_ATTEMPT": "0"}, {"GITHUB_REPOSITORY": ""},
    {"GITHUB_EVENT_NAME": "push"}, {"FORCE_COLLECTION": "1"},
    {"GITHUB_EVENT_NAME": "schedule", "FORCE_COLLECTION": "true"},
    {"COLLECT_SCOPE": "everything"},
    {"GITHUB_EVENT_NAME": "schedule", "COLLECT_SCOPE": "ours"},
])
def test_invalid_github_context_fails(override):
    with pytest.raises(db_runs.RunStoreError):
        db_runs.invocation_from_environment(github_environment(**override))


@pytest.mark.parametrize("scope", ["ours", "competitors"])
def test_a_partial_scope_is_read_from_the_environment_for_a_manual_run(scope):
    invocation = db_runs.invocation_from_environment(github_environment(COLLECT_SCOPE=scope))
    assert invocation.scope == scope


def test_missing_scope_defaults_to_all():
    assert db_runs.invocation_from_environment(github_environment()).scope == "all"


def test_scope_all_is_explicitly_allowed_on_a_schedule_run():
    invocation = db_runs.invocation_from_environment(github_environment(GITHUB_EVENT_NAME="schedule", COLLECT_SCOPE="all"))
    assert invocation.scope == "all"


def test_local_owner_is_unique_and_cannot_force():
    one = db_runs.invocation_from_environment({})
    two = db_runs.invocation_from_environment({})
    assert one.source == two.source == "local"
    assert one.owner_key != two.owner_key
    with pytest.raises(db_runs.RunStoreError):
        db_runs.invocation_from_environment({"FORCE_COLLECTION": "true"})


class FakeConnection:
    def __init__(self, rows, *, commit_error=False):
        self.rows = iter(rows)
        self.events = []
        self.commit_error = commit_error

    def set_session(self, **kwargs):
        self.events.append(("session", kwargs))

    def cursor(self):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def execute(self, query, params=None):
        self.events.append(("sql", " ".join(query.split()), params))

    def fetchone(self):
        return next(self.rows)

    def commit(self):
        self.events.append(("commit",))
        if self.commit_error:
            raise RuntimeError("PRIVATE_PASSWORD_DO_NOT_PRINT")

    def rollback(self):
        self.events.append(("rollback",))

    def close(self):
        self.events.append(("close",))


def connect_fake(monkeypatch, rows, **kwargs):
    conn = FakeConnection(rows, **kwargs)
    monkeypatch.setattr(db_runs.psycopg2, "connect", lambda *a, **k: conn)
    return conn


def admission_rows(*, now=NOW, unfinished=False, count=0, success=False, result=(42,)):
    return [(now,), (9, 0), (unfinished,), (count, success), result]


def test_admission_lock_before_fresh_reads_and_commit_before_permission(monkeypatch):
    conn = connect_fake(monkeypatch, admission_rows())
    result = db_runs.admit_parser_run(INVOCATION)
    assert result.should_run and result.run_id == 42
    assert conn.events[0] == ("session", dict(isolation_level="READ COMMITTED", autocommit=False))
    queries = [e for e in conn.events if e[0] == "sql"]
    assert "pg_advisory_xact_lock" in queries[2][1]
    assert queries[2][2] == db_runs.ADMISSION_LOCK
    assert "clock_timestamp" in queries[3][1]
    assert all("collection_runs" not in q[1] for q in queries[:4])
    assert queries[-1][2] == (INVOCATION.source, NOW, INVOCATION.owner_key, INVOCATION.scope)
    assert conn.events[-2:] == [("commit",), ("close",)]


@pytest.mark.parametrize("override", [
    {"unfinished": True}, {"count": 3}, {"success": True},
])
def test_denial_does_not_register_attempt(monkeypatch, override):
    conn = connect_fake(monkeypatch, admission_rows(**override))
    decision = db_runs.admit_parser_run(INVOCATION)
    assert not decision.should_run and decision.run_id is None
    assert not any("INSERT" in e[1] for e in conn.events if e[0] == "sql")


def test_a_stale_running_attempt_is_written_off_before_the_unfinished_check(monkeypatch):
    conn = connect_fake(monkeypatch, admission_rows())
    db_runs.admit_parser_run(INVOCATION)
    queries = [e for e in conn.events if e[0] == "sql"]
    stale_index = next(i for i, q in enumerate(queries) if "collection_runs" in q[1] and "SET status = 'error'" in q[1])
    exists_index = next(i for i, q in enumerate(queries) if "EXISTS" in q[1])
    assert stale_index < exists_index
    stale_query = queries[stale_index]
    assert "WHERE status = 'running'" in stale_query[1]
    assert stale_query[2] == (NOW - db_runs.timedelta(minutes=db_runs.STALE_RUNNING_MINUTES),)


def test_unfinished_check_has_no_day_or_step_filter(monkeypatch):
    conn = connect_fake(monkeypatch, admission_rows(unfinished=True))
    assert not db_runs.admit_parser_run(INVOCATION).should_run
    query = next(e[1] for e in conn.events if e[0] == "sql" and "EXISTS" in e[1])
    assert "WHERE status = 'running'" in query
    assert "started_at" not in query and "step" not in query


def test_daily_count_uses_kyiv_day_and_counts_all_parser_statuses(monkeypatch):
    near_midnight = datetime(2026, 9, 18, 21, 1, tzinfo=timezone.utc)
    conn = connect_fake(monkeypatch, admission_rows(now=near_midnight))
    db_runs.admit_parser_run(INVOCATION)
    count_query = next(e for e in conn.events if e[0] == "sql" and "count(*)" in e[1])
    assert count_query[2][0].isoformat() == "2026-09-19"
    assert count_query[2][1] == INVOCATION.scope
    assert "AT TIME ZONE 'Europe/Kyiv'" in count_query[1]
    where = count_query[1].split("WHERE", 1)[1]
    assert "step = 'parser'" in where and "status" not in where


def test_daily_count_and_success_are_scoped_independently(monkeypatch):
    """Дневные лимиты «наших» и «конкурентов» не делят один и тот же счётчик с полным сбором."""
    ours = db_runs.Invocation("github_actions", "github:example/project:101:1", scope="ours")
    conn = connect_fake(monkeypatch, admission_rows())
    db_runs.admit_parser_run(ours)
    count_query = next(e for e in conn.events if e[0] == "sql" and "count(*)" in e[1])
    assert count_query[2][1] == "ours"
    insert_query = next(e for e in conn.events if e[0] == "sql" and "INSERT" in e[1])
    assert insert_query[2] == (ours.source, NOW, ours.owner_key, "ours")
    assert "scope" in insert_query[1]


def test_the_unfinished_check_still_ignores_scope_so_one_running_collection_blocks_every_scope(monkeypatch):
    ours = db_runs.Invocation("github_actions", "github:example/project:101:1", scope="ours")
    conn = connect_fake(monkeypatch, admission_rows(unfinished=True))
    assert not db_runs.admit_parser_run(ours).should_run
    query = next(e[1] for e in conn.events if e[0] == "sql" and "EXISTS" in e[1])
    assert "scope" not in query


@pytest.mark.parametrize("result", [None, (None,), (0,)])
def test_missing_or_invalid_insert_id_is_not_permission(monkeypatch, result):
    conn = connect_fake(monkeypatch, admission_rows(result=result))
    with pytest.raises(db_runs.RunStoreError):
        db_runs.admit_parser_run(INVOCATION)
    assert ("rollback",) in conn.events and ("commit",) not in conn.events


def test_commit_failure_after_returning_id_is_not_permission(monkeypatch):
    conn = connect_fake(monkeypatch, admission_rows(), commit_error=True)
    with pytest.raises(db_runs.RunStoreError) as exc:
        db_runs.admit_parser_run(INVOCATION)
    assert "PRIVATE_PASSWORD" not in str(exc.value)
    assert conn.events[-2:] == [("rollback",), ("close",)]


def test_connection_error_is_sanitized_even_with_force(monkeypatch):
    def broken(*args, **kwargs):
        raise RuntimeError("PRIVATE_PASSWORD_DO_NOT_PRINT")
    monkeypatch.setattr(db_runs.psycopg2, "connect", broken)
    with pytest.raises(db_runs.RunStoreError) as exc:
        db_runs.admit_parser_run(db_runs.Invocation(INVOCATION.source, INVOCATION.owner_key, True))
    assert "PRIVATE_PASSWORD" not in str(exc.value)


def test_claim_is_conditional_owner_bound_and_not_limited_to_today(monkeypatch):
    conn = connect_fake(monkeypatch, [(42,)])
    db_runs.claim_parser_run(42, INVOCATION)
    query = next(e for e in conn.events if e[0] == "sql" and "UPDATE" in e[1])
    for condition in ("step = 'parser'", "status = 'running'", "claimed_at IS NULL",
                      "finished_at IS NULL", "source = %s", "owner_key = %s"):
        assert condition in query[1]
    assert "started_at" not in query[1] and "::date" not in query[1]
    assert query[2] == (42, INVOCATION.source, INVOCATION.owner_key)
    assert conn.events[-2:] == [("commit",), ("close",)]


def test_claim_no_matching_row_fails_closed(monkeypatch):
    conn = connect_fake(monkeypatch, [None])
    with pytest.raises(db_runs.RunStoreError):
        db_runs.claim_parser_run(42, INVOCATION)
    assert ("commit",) not in conn.events


def test_claim_commit_failure_fails_closed(monkeypatch):
    connect_fake(monkeypatch, [(42,)], commit_error=True)
    with pytest.raises(db_runs.RunStoreError):
        db_runs.claim_parser_run(42, INVOCATION)


@pytest.mark.parametrize("run_id", [None, 0, -1, "42", True, 2**63])
def test_invalid_claim_id_rejected_before_connection(run_id):
    with pytest.raises(db_runs.RunStoreError, match="Некорректный ID"):
        db_runs.claim_parser_run(run_id, INVOCATION)


def test_parser_cannot_use_unprotected_log_start():
    with pytest.raises(db_runs.RunStoreError, match="обязательны"):
        db_runs.log_start("parser")


@pytest.mark.parametrize("operation,rows", [
    (lambda: db_runs.log_start("sync"), [(50,)]),
    (lambda: db_runs.log_finish(42, "done"), [(42,)]),
])
def test_history_commit_failure_is_not_swallowed(monkeypatch, operation, rows):
    connect_fake(monkeypatch, rows, commit_error=True)
    with pytest.raises(db_runs.RunStoreError):
        operation()


@pytest.mark.parametrize("operation", [
    lambda: db_runs.log_start("sync"), lambda: db_runs.log_finish(42, "done"),
])
def test_history_missing_row_is_failure(monkeypatch, operation):
    connect_fake(monkeypatch, [None])
    with pytest.raises(db_runs.RunStoreError):
        operation()


def test_finish_requires_running_and_claimed_parser(monkeypatch):
    conn = connect_fake(monkeypatch, [(42,)])
    db_runs.log_finish(42, "done")
    query = next(e for e in conn.events if e[0] == "sql" and "UPDATE" in e[1])
    assert "status = 'running'" in query[1]
    assert "step <> 'parser' OR claimed_at IS NOT NULL" in query[1]


def test_invalid_finish_status_is_rejected():
    with pytest.raises(db_runs.RunStoreError, match="Некорректный итоговый"):
        db_runs.log_finish(42, "running")
