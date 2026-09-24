"""Справочник ASIN: ASIN сам по себе, без пары."""

import pytest

import access
import asins_store


class FakeDb:
    def __init__(self, rows=((True,),)):
        self.rows = list(rows)
        self.executed = []

    def connect(self):
        return self

    def cursor(self):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=()):
        self.executed.append((" ".join(sql.split()), params))

    def fetchall(self):
        return self.rows

    def commit(self):
        pass

    def close(self):
        pass


@pytest.fixture
def db():
    return FakeDb()


def add(db, text="B0OURASIN1", market="US", kind="competitor", role=access.ROLE_EDITOR):
    return asins_store.add_asins(db.connect, market, text, kind, actor_role=role, actor="Тест")


def test_adding_parses_a_pasted_batch(db):
    db.rows = [(True,), (True,)]
    result = add(db, "B0OURASIN1 B0COMPAAA1")
    assert result["added"] == 2
    _, params = db.executed[-1]
    assert params[2] == ["B0OURASIN1", "B0COMPAAA1"]


def test_a_link_decides_its_own_country(db):
    """У ссылки на amazon.de страна известна из самой ссылки, выбранная в списке не должна её ломать."""
    add(db, "https://www.amazon.de/dp/B0COMPAAA1", market="US")
    _, params = db.executed[-1]
    assert params[1] == ["DE"]


def test_an_already_known_asin_comes_back_instead_of_doubling(db):
    add(db)
    sql, _ = db.executed[-1]
    assert "ON CONFLICT (marketplace, asin) DO UPDATE" in sql
    assert "SET active = TRUE" in sql


def test_text_without_asins_is_refused(db):
    with pytest.raises(ValueError, match="нет ни одного ASIN"):
        add(db, "просто текст")
    assert not db.executed


def test_an_unknown_kind_is_refused(db):
    with pytest.raises(ValueError, match="наш это товар или конкурент"):
        add(db, kind="something")
    assert not db.executed


def test_an_unknown_country_is_refused(db):
    with pytest.raises(ValueError, match="Неизвестная страна"):
        add(db, market="XX")
    assert not db.executed


def test_a_viewer_cannot_add(db):
    with pytest.raises(access.AccessDenied):
        add(db, role=None)
    assert not db.executed


def test_a_huge_batch_is_refused(db):
    many = " ".join(f"B0{index:08d}" for index in range(asins_store.MAX_ADD + 5))
    with pytest.raises(ValueError, match="не больше"):
        add(db, many)
    assert not db.executed


def test_removing_marks_inactive_instead_of_deleting(db):
    asins_store.set_active(db.connect, [("US", "B0OURASIN1")], False,
                           actor_role=access.ROLE_EDITOR, actor="Тест")
    sql, params = db.executed[-1]
    assert sql.startswith("UPDATE bsr_radar.asins")
    assert "DELETE" not in sql
    assert params[0] is False


def test_removing_skips_rows_that_are_already_in_that_state(db):
    asins_store.set_active(db.connect, [("US", "B0OURASIN1")], False,
                           actor_role=access.ROLE_EDITOR, actor="Тест")
    sql, _ = db.executed[-1]
    assert "active IS DISTINCT FROM" in sql


def test_renaming_never_touches_the_key(db):
    asins_store.rename(db.connect, ("US", "B0OURASIN1"), "  Новое   имя ",
                       actor_role=access.ROLE_EDITOR, actor="Тест")
    sql, params = db.executed[-1]
    assert "SET name = %s" in sql
    assert "SET asin" not in sql and "SET marketplace" not in sql
    assert params[0] == "Новое имя"


def test_an_overlong_name_is_refused(db):
    with pytest.raises(ValueError, match="длиннее"):
        asins_store.rename(db.connect, ("US", "B0OURASIN1"), "я" * (asins_store.MAX_NAME + 1),
                           actor_role=access.ROLE_EDITOR, actor="Тест")
    assert not db.executed


def test_a_missing_registry_is_reported_not_crashed(db):
    db.rows = [(False,)]
    assert asins_store.registry_exists(db.connect) is False
    db.rows = [(True,)]
    assert asins_store.registry_exists(db.connect) is True
