"""Правка уже заведённой пары: меняются подписи, ключ пары остаётся прежним.

Ключ (рынок, наш ASIN, ASIN конкурента) — это же ключ снимков, поэтому менять его здесь нельзя:
получилась бы другая пара, у которой нет прежней истории.
"""

import pytest

import access
import pairs_store

KEY = ("US", "B0OURASIN1", "B0COMPAAA1")


class FakeDb:
    def __init__(self, rows=((1,),), journal=True):
        self.rows = list(rows)
        self.journal = journal
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
        if "pair_changes" in self.executed[-1][0] and "to_regclass" in self.executed[-1][0]:
            return [(self.journal,)]
        return self.rows

    def fetchone(self):
        return (self.journal,)

    def commit(self):
        pass

    def close(self):
        pass


@pytest.fixture
def db(monkeypatch):
    fake = FakeDb()
    monkeypatch.setattr(pairs_store, "journal_exists", lambda connect: fake.journal)
    return fake


def edit(db, our="Наш товар", comp="Конкурент", role=access.ROLE_EDITOR, key=KEY):
    return pairs_store.edit_pair_names(db.connect, key, our, comp, actor_role=role, actor="Тест")


def test_editing_updates_the_names(db):
    assert edit(db) == 1
    sql, params = db.executed[-1]
    assert "UPDATE bsr_radar.competitor_pairs" in sql
    assert params[:5] == ("Наш товар", "Конкурент", "US", "B0OURASIN1", "B0COMPAAA1")


def test_the_pair_key_itself_is_never_changed(db):
    edit(db)
    sql, _ = db.executed[-1]
    assert "SET our_product = %s, competitor_name = %s" in sql
    for column in ("SET marketplace", "SET our_asin", "SET comp_asin"):
        assert column not in sql, column


def test_an_unchanged_name_is_not_written_again(db):
    """Условие IS DISTINCT FROM: иначе журнал заполнялся бы правками, которых не было."""
    edit(db)
    sql, _ = db.executed[-1]
    assert "IS DISTINCT FROM" in sql


def test_the_change_goes_into_the_journal(db):
    edit(db)
    sql, params = db.executed[-1]
    assert "INSERT INTO bsr_radar.pair_changes" in sql
    assert "'edit'" in sql
    assert params[-1] == "Тест"


def test_without_the_journal_the_edit_still_applies(monkeypatch):
    fake = FakeDb(journal=False)
    monkeypatch.setattr(pairs_store, "journal_exists", lambda connect: False)
    assert edit(fake) == 1
    sql, _ = fake.executed[-1]
    assert "UPDATE bsr_radar.competitor_pairs" in sql
    assert "pair_changes" not in sql


def test_a_viewer_cannot_edit(db):
    with pytest.raises(access.AccessDenied):
        edit(db, role=None)
    assert not db.executed


@pytest.mark.parametrize("key", [
    ("XX", "B0OURASIN1", "B0COMPAAA1"),   # неизвестный рынок
    ("US", "junk", "B0COMPAAA1"),          # не ASIN
    ("US", "B0OURASIN1", ""),              # пусто
])
def test_a_broken_key_is_refused_before_touching_the_database(db, key):
    with pytest.raises(ValueError):
        edit(db, key=key)
    assert not db.executed


def test_an_overlong_name_is_refused(db):
    with pytest.raises(ValueError, match="длиннее"):
        edit(db, our="я" * (pairs_store.MAX_NAME + 1))
    assert not db.executed


def test_whitespace_in_names_is_tidied(db):
    edit(db, our="  Наш   товар  ", comp="\nКонкурент\t")
    _, params = db.executed[-1]
    assert params[0] == "Наш товар"
    assert params[1] == "Конкурент"
