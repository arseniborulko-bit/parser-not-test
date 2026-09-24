"""Исходная ссылка на товар: хранится, видна и правится.

Собранная из ASIN ссылка теряет параметры, вариант товара и метку продавца, а эталонный
справочник состоит именно из ссылок — поэтому исходную нужно хранить отдельно.
"""

import pytest

import access
import asins_store
import pairs_ui


class FakeDb:
    def __init__(self, rows=((True,),), has_url=True):
        self.rows = list(rows)
        self.has_url = has_url
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
        if "information_schema.columns" in self.executed[-1][0]:
            return [(1 if self.has_url else 0,)]
        return self.rows

    def commit(self):
        pass

    def close(self):
        pass


LINK = "https://www.amazon.co.uk/LAPASA-Merino-Thermal/dp/B0725TWS7J?th=1&psc=1"


def test_the_original_link_is_taken_from_the_pasted_text():
    found = asins_store._links_by_asin(f"{LINK} B0OURASIN1")
    assert found == {"B0725TWS7J": LINK}, "голый ASIN ссылки не имеет"


def test_the_link_keeps_its_parameters():
    """Ровно то, чего нет у собранной из ASIN ссылки."""
    assert "?th=1&psc=1" in asins_store._links_by_asin(LINK)["B0725TWS7J"]


def test_a_link_without_an_asin_is_ignored():
    assert asins_store._links_by_asin("https://www.amazon.co.uk/s?k=merino") == {}


def test_adding_stores_the_link(monkeypatch):
    db = FakeDb()
    monkeypatch.setattr(asins_store, "journal_exists", lambda connect: False, raising=False)
    asins_store.add_asins(db.connect, "UK", LINK, "competitor",
                          actor_role=access.ROLE_EDITOR, actor="Тест")
    sql, params = db.executed[-1]
    assert "source_url" in sql
    assert params[-1] == [LINK]


def test_a_bare_asin_does_not_wipe_an_existing_link(monkeypatch):
    db = FakeDb()
    asins_store.add_asins(db.connect, "UK", "B0725TWS7J", "competitor",
                          actor_role=access.ROLE_EDITOR, actor="Тест")
    sql, params = db.executed[-1]
    assert params[-1] == [""]
    assert "ELSE bsr_radar.asins.source_url END" in sql, "пустая ссылка не должна затирать прежнюю"


def test_without_the_migration_adding_still_works():
    db = FakeDb(has_url=False)
    asins_store.add_asins(db.connect, "UK", LINK, "competitor",
                          actor_role=access.ROLE_EDITOR, actor="Тест")
    sql, _ = db.executed[-1]
    assert "source_url" not in sql


def test_editing_saves_name_and_link_together():
    db = FakeDb()
    asins_store.rename(db.connect, ("UK", "B0725TWS7J"), "Lapasa", source_url=LINK,
                       actor_role=access.ROLE_EDITOR, actor="Тест")
    sql, params = db.executed[-1]
    assert "SET name = %s, source_url = %s" in sql
    assert params[0] == "Lapasa" and params[1] == LINK


def test_leaving_the_link_alone_does_not_touch_it():
    db = FakeDb()
    asins_store.rename(db.connect, ("UK", "B0725TWS7J"), "Lapasa",
                       actor_role=access.ROLE_EDITOR, actor="Тест")
    sql, _ = db.executed[-1]
    assert "source_url" not in sql


def test_an_empty_link_clears_it():
    db = FakeDb()
    asins_store.rename(db.connect, ("UK", "B0725TWS7J"), "Lapasa", source_url="",
                       actor_role=access.ROLE_EDITOR, actor="Тест")
    _, params = db.executed[-1]
    assert params[1] == ""


@pytest.mark.parametrize("bad", ["просто текст", "amazon.co.uk/dp/B0725TWS7J", "ftp://x/y"])
def test_a_value_that_is_not_a_link_is_refused(bad):
    db = FakeDb()
    with pytest.raises(ValueError, match="http"):
        asins_store.rename(db.connect, ("UK", "B0725TWS7J"), "Lapasa", source_url=bad,
                           actor_role=access.ROLE_EDITOR, actor="Тест")


def test_a_viewer_cannot_edit_the_link():
    db = FakeDb()
    with pytest.raises(access.AccessDenied):
        asins_store.rename(db.connect, ("UK", "B0725TWS7J"), "Lapasa", source_url=LINK,
                           actor_role=None, actor="Чужой")


def test_saving_reports_partial_progress_on_failure(monkeypatch):
    state = {}
    monkeypatch.setattr(pairs_ui.st, "session_state", state, raising=False)
    done = []

    def save(connect, key, name, *, actor_role, actor, source_url=None):
        if key[1] == "B0BAD00000":
            raise ValueError("Ссылка должна начинаться с http:// или https://")
        done.append(key)
        return 1

    monkeypatch.setattr(pairs_ui.asins_store, "rename", save)
    pairs_ui._save_registry_callback(
        None,
        [(("UK", "B0725TWS7J"), "Lapasa", LINK), (("UK", "B0BAD00000"), "Плохой", "мусор")],
        "Тест", "editor",
    )
    level, text = state["pairs_flash"]
    assert level == "error"
    assert "Сохранено строк: 1" in text
