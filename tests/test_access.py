"""Изолированные тесты правил доступа (access.py): ни Streamlit, ни настоящей базы."""

import pytest

import access


class FakeDb:
    def __init__(self, rows=None, rowcount=1, fail_connect=None, fail_execute=None, fail_commit=None):
        self.rows = rows or []
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
        return self.db.rows


def logged_in(email, verified=True):
    return {"is_logged_in": True, "email": email, "email_verified": verified}


@pytest.mark.parametrize("raw, expected", [
    ("  Boss@Example.COM ", "boss@example.com"),
    ("a.b+tag@sub.example.org", "a.b+tag@sub.example.org"),
    ("", None),
    ("   ", None),
    ("no-at-sign", None),
    ("two@@example.com", None),
    ("a b@example.com", None),
    ("a@b", None),
    ("a@b.com,c@d.com", None),
    (None, None),
    (123, None),
    ("x" * 250 + "@example.com", None),
])
def test_normalize_email(raw, expected):
    assert access.normalize_email(raw) == expected


def test_parse_email_list_splits_dedupes_and_drops_invalid():
    text = "A@x.com, b@x.com;a@X.com\nbad-entry  c@x.com"
    assert access.parse_email_list(text) == frozenset({"a@x.com", "b@x.com", "c@x.com"})


@pytest.mark.parametrize("raw", [None, "", 5, ["a@x.com"]])
def test_parse_email_list_non_text_or_empty_gives_empty_set(raw):
    assert access.parse_email_list(raw) == frozenset()


@pytest.mark.parametrize("user", [
    None,
    {},
    {"is_logged_in": False, "email": "a@x.com", "email_verified": True},
    {"is_logged_in": True, "email": "a@x.com"},
    {"is_logged_in": True, "email": "a@x.com", "email_verified": False},
    {"is_logged_in": True, "email": "a@x.com", "email_verified": "true"},
    {"is_logged_in": "yes", "email": "a@x.com", "email_verified": True},
    {"is_logged_in": True, "email_verified": True},
    {"is_logged_in": True, "email": "not-an-email", "email_verified": True},
])
def test_verified_email_requires_login_verification_and_valid_address(user):
    assert access.verified_email(user) is None


def test_verified_email_normalizes_address():
    assert access.verified_email(logged_in("  Boss@Example.com ")) == "boss@example.com"


def test_bootstrap_admin_from_secret_is_admin_without_database():
    role = access.resolve_role(logged_in("Boss@Example.com"), access.parse_email_list("boss@example.com"), {})
    assert role == access.ROLE_ADMIN


def test_database_roles_are_honoured():
    roles = {"ed@x.com": "editor", "ad@x.com": "admin"}
    assert access.resolve_role(logged_in("ed@x.com"), frozenset(), roles) == access.ROLE_EDITOR
    assert access.resolve_role(logged_in("ad@x.com"), frozenset(), roles) == access.ROLE_ADMIN


def test_unknown_or_anonymous_user_gets_no_role():
    roles = {"ed@x.com": "editor"}
    assert access.resolve_role(logged_in("stranger@x.com"), frozenset(), roles) is None
    assert access.resolve_role({"is_logged_in": False}, frozenset({"ed@x.com"}), roles) is None
    assert access.resolve_role(None, frozenset({"ed@x.com"}), roles) is None


def test_unverified_email_cannot_claim_admin_or_editor():
    admins = frozenset({"boss@x.com"})
    roles = {"ed@x.com": "editor"}
    assert access.resolve_role(logged_in("boss@x.com", verified=False), admins, roles) is None
    assert access.resolve_role(logged_in("ed@x.com", verified=False), admins, roles) is None


def test_garbage_role_value_in_database_gives_no_role():
    assert access.resolve_role(logged_in("x@x.com"), frozenset(), {"x@x.com": "superuser"}) is None


def test_role_hierarchy():
    assert access.has_role(access.ROLE_ADMIN, access.ROLE_EDITOR)
    assert access.has_role(access.ROLE_ADMIN, access.ROLE_ADMIN)
    assert access.has_role(access.ROLE_EDITOR, access.ROLE_EDITOR)
    assert not access.has_role(access.ROLE_EDITOR, access.ROLE_ADMIN)
    assert not access.has_role(None, access.ROLE_EDITOR)
    assert not access.has_role("superuser", access.ROLE_EDITOR)
    assert not access.has_role(access.ROLE_ADMIN, "superuser")


def test_require_role_raises_for_missing_role():
    access.require_role(access.ROLE_ADMIN, access.ROLE_ADMIN)
    with pytest.raises(access.AccessDenied):
        access.require_role(access.ROLE_EDITOR, access.ROLE_ADMIN)
    with pytest.raises(access.AccessDenied):
        access.require_role(None, access.ROLE_EDITOR)


def test_active_user_roles_reads_only_active_rows_and_skips_bad_roles():
    db = FakeDb(rows=[("a@x.com", "editor"), ("b@x.com", "admin"), ("c@x.com", "weird")])
    assert access.active_user_roles(db.connect) == {"a@x.com": "editor", "b@x.com": "admin"}
    sql = db.executed[0][0]
    assert "WHERE active" in sql
    assert db.closed == 1


def test_list_users_returns_rows_as_dicts():
    db = FakeDb(rows=[("a@x.com", "editor", True, "boss@x.com", "2026-09-20")])
    assert access.list_users(db.connect) == [
        {"email": "a@x.com", "role": "editor", "active": True, "added_by": "boss@x.com", "created_at": "2026-09-20"},
    ]


@pytest.mark.parametrize("db", [
    FakeDb(fail_connect="host=secret-host password=hunter2"),
    FakeDb(fail_execute="password=hunter2"),
    FakeDb(fail_commit="password=hunter2"),
])
def test_store_errors_are_wrapped_without_leaking_driver_text(db):
    with pytest.raises(access.AccessStoreError) as info:
        access.add_user(db.connect, "a@x.com", access.ROLE_EDITOR, actor_role=access.ROLE_ADMIN, actor_email="boss@x.com")
    assert "hunter2" not in str(info.value)
    assert "secret-host" not in str(info.value)
    assert info.value.__cause__ is None and info.value.__suppress_context__


@pytest.mark.parametrize("actor_role", [None, access.ROLE_EDITOR, "superuser"])
def test_add_user_requires_admin_and_never_touches_database_otherwise(actor_role):
    db = FakeDb()
    with pytest.raises(access.AccessDenied):
        access.add_user(db.connect, "a@x.com", access.ROLE_EDITOR, actor_role=actor_role, actor_email="e@x.com")
    assert db.connects == 0


def test_add_user_stores_normalized_email_with_bound_parameters():
    db = FakeDb()
    saved = access.add_user(db.connect, "  New@X.com ", access.ROLE_EDITOR,
                            actor_role=access.ROLE_ADMIN, actor_email="boss@x.com")
    assert saved == "new@x.com"
    sql, params = db.executed[0]
    assert params == ("new@x.com", access.ROLE_EDITOR, "boss@x.com")
    assert "new@x.com" not in sql and "boss@x.com" not in sql
    assert db.commits == 1 and db.closed == 1


@pytest.mark.parametrize("email, role", [
    ("not-an-email", access.ROLE_EDITOR),
    ("", access.ROLE_EDITOR),
    ("a@x.com", "superuser"),
    ("a@x.com", None),
])
def test_add_user_rejects_bad_input_without_database_access(email, role):
    db = FakeDb()
    with pytest.raises(ValueError):
        access.add_user(db.connect, email, role, actor_role=access.ROLE_ADMIN, actor_email="boss@x.com")
    assert db.connects == 0


def test_admin_cannot_demote_themselves_but_can_readd_as_admin():
    db = FakeDb()
    with pytest.raises(ValueError):
        access.add_user(db.connect, "Boss@X.com", access.ROLE_EDITOR, actor_role=access.ROLE_ADMIN, actor_email="boss@x.com")
    assert db.connects == 0
    access.add_user(db.connect, "boss@x.com", access.ROLE_ADMIN, actor_role=access.ROLE_ADMIN, actor_email="boss@x.com")
    assert db.commits == 1


@pytest.mark.parametrize("actor_role", [None, access.ROLE_EDITOR])
def test_set_user_active_requires_admin(actor_role):
    db = FakeDb()
    with pytest.raises(access.AccessDenied):
        access.set_user_active(db.connect, "a@x.com", False, actor_role=actor_role, actor_email="e@x.com")
    assert db.connects == 0


def test_admin_cannot_deactivate_themselves():
    db = FakeDb()
    with pytest.raises(ValueError):
        access.set_user_active(db.connect, "BOSS@x.com", False, actor_role=access.ROLE_ADMIN, actor_email="boss@x.com")
    assert db.connects == 0


def test_set_user_active_updates_exactly_one_row():
    db = FakeDb(rowcount=1)
    access.set_user_active(db.connect, "A@x.com", False, actor_role=access.ROLE_ADMIN, actor_email="boss@x.com")
    sql, params = db.executed[0]
    assert params == (False, "a@x.com")
    assert "a@x.com" not in sql
    assert db.commits == 1


def test_set_user_active_unknown_user_is_an_error():
    db = FakeDb(rowcount=0)
    with pytest.raises(ValueError, match="не найден"):
        access.set_user_active(db.connect, "ghost@x.com", True, actor_role=access.ROLE_ADMIN, actor_email="boss@x.com")


def test_password_matches_only_the_exact_password():
    assert access.password_matches("correct-horse-battery", "correct-horse-battery")
    assert access.password_matches("пароль-команды-2026", "пароль-команды-2026")
    assert not access.password_matches("correct-horse-batter", "correct-horse-battery")
    assert not access.password_matches("Correct-Horse-Battery", "correct-horse-battery")
    assert not access.password_matches("", "correct-horse-battery")


@pytest.mark.parametrize("expected", ["", "short", "1234567", None, 12345678])
def test_missing_or_short_team_password_never_matches_anything(expected):
    assert not access.password_matches(expected, expected)
    assert not access.password_matches("", expected)


@pytest.mark.parametrize("candidate", [None, 123, b"correct-horse-battery", ["correct-horse-battery"]])
def test_password_candidate_must_be_text(candidate):
    assert not access.password_matches(candidate, "correct-horse-battery")


@pytest.mark.parametrize("raw, expected", [
    ("  Аня  ", "Аня"),
    ("Аня   Иванова", "Аня Иванова"),
    ("Аня\nИ", "Аня И"),
    ("Al", "Al"),
    ("А", None),
    ("", None),
    ("   ", None),
    ("x" * 40, "x" * 40),
    ("x" * 41, None),
    ("Аня\x00", None),
    (None, None),
    (7, None),
])
def test_clean_actor_name(raw, expected):
    assert access.clean_actor_name(raw) == expected


class FakeClock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def test_limiter_allows_four_failures_and_locks_on_the_fifth():
    clock = FakeClock()
    limiter = access.AttemptLimiter(max_failures=5, window=600, clock=clock)
    for _ in range(4):
        assert limiter.allowed()
        limiter.record_failure()
    assert limiter.allowed()
    limiter.record_failure()
    assert not limiter.allowed()
    assert limiter.retry_after() > 0


def test_limiter_unlocks_when_failures_leave_the_window():
    clock = FakeClock()
    limiter = access.AttemptLimiter(max_failures=5, window=600, clock=clock)
    for _ in range(5):
        limiter.record_failure()
    clock.now += 599
    assert not limiter.allowed()
    clock.now += 2
    assert limiter.allowed()
    assert limiter.retry_after() == 0


def test_limiter_is_a_sliding_window_not_a_reset():
    clock = FakeClock()
    limiter = access.AttemptLimiter(max_failures=5, window=600, clock=clock)
    for _ in range(5):
        limiter.record_failure()
        clock.now += 100
    assert not limiter.allowed()
    clock.now = 1000 + 601
    assert limiter.allowed()
    limiter.record_failure()
    assert not limiter.allowed()


def test_limiter_retry_after_counts_down():
    clock = FakeClock()
    limiter = access.AttemptLimiter(max_failures=5, window=600, clock=clock)
    for _ in range(5):
        limiter.record_failure()
    assert 599 <= limiter.retry_after() <= 601
    clock.now += 300
    assert 299 <= limiter.retry_after() <= 301


def test_successful_login_clears_earlier_failures():
    limiter = access.AttemptLimiter(max_failures=5, window=600, clock=FakeClock())
    for _ in range(4):
        limiter.record_failure()
    limiter.record_success()
    for _ in range(4):
        limiter.record_failure()
    assert limiter.allowed()
