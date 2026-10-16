"""Список ASIN раздела Best Seller в отдельной базе: план добавления, права, запросы и экран управления.
Без настоящей базы."""

import pytest
from streamlit.testing.v1 import AppTest

import access
import best_seller_store as store

DE, IT = ("DE", "B07WVYMY7Y"), ("IT", "B07WVYMY7Y")
EXISTING = [
    {"marketplace": "DE", "asin": "B07WVYMY7Y", "active": True, "added_by": "anna", "updated_at": None},
    {"marketplace": "US", "asin": "B0GNBSDL65", "active": False, "added_by": "anna", "updated_at": None},
]


def test_plan_adds_new_returns_disabled_and_skips_active():
    plan = store.plan_add(EXISTING, [DE, IT, ("US", "B0GNBSDL65"), IT])
    assert plan.to_add == [IT] and plan.to_enable == [("US", "B0GNBSDL65")] and plan.already == [DE]
    assert plan.changes == 2 and not plan.errors


def test_plan_refuses_empty_and_too_big_batches():
    assert store.plan_add([], []).errors == ["Вставьте ссылки на товары Amazon."]
    big = [("DE", f"B0{n:08d}") for n in range(store.MAX_BATCH + 1)]
    assert store.plan_add([], big).errors


class FakeDb:
    def __init__(self, rows):
        self.rows, self.executed = rows, []

    def connect(self):
        return self

    def cursor(self):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=()):
        self.executed.append((sql, params))

    def fetchall(self):
        return self.rows

    rowcount = 0

    def commit(self):
        pass

    def close(self):
        pass


def test_add_writes_country_and_asin_and_the_journal_in_one_query():
    db = FakeDb([("add",), ("enable",)])
    assert store.add(db.connect, [DE, IT], actor_role=access.ROLE_EDITOR, actor="Anna") == {"add": 1, "enable": 1}
    ((sql, params),) = db.executed
    assert "best_seller.asins" in sql and "best_seller.asin_changes" in sql and "bsr_radar" not in sql
    assert params == (["DE", "IT"], ["B07WVYMY7Y", "B07WVYMY7Y"], "Anna", "Anna")


def test_disable_and_enable_are_journaled():
    db = FakeDb([(1,)])
    assert store.set_active(db.connect, [DE], False, actor_role=access.ROLE_EDITOR, actor="Anna") == 1
    ((sql, params),) = db.executed
    assert params == (["DE"], ["B07WVYMY7Y"], False, False, "Anna", "disable")


def test_changes_need_the_editor_role():
    db = FakeDb([])
    with pytest.raises(access.AccessDenied):
        store.add(db.connect, [DE], actor_role=None, actor="x")
    with pytest.raises(access.AccessDenied):
        store.set_active(db.connect, [DE], False, actor_role=None, actor="x")
    assert not db.executed


def test_database_errors_do_not_leak_driver_text():
    def broken():
        raise RuntimeError("postgresql://user:secret@host/db")

    with pytest.raises(store.BestSellerStoreError) as exc:
        store.list_asins(broken)
    assert "secret" not in str(exc.value)


SCRIPT = """
import best_seller_ui
best_seller_ui.render(connect=CONNECT, actor="Anna", can_edit=CAN_EDIT)
"""


def run_list_page(monkeypatch, *, connect, can_edit=True, rows=EXISTING, exists=True):
    calls = {"add": [], "set_active": []}
    monkeypatch.setattr(store, "table_exists", lambda c: exists)
    monkeypatch.setattr(store, "list_asins", lambda c: rows)
    monkeypatch.setattr(store, "add", lambda c, keys, **kw: calls["add"].append((keys, kw)) or {"add": 1, "enable": 0})
    monkeypatch.setattr(store, "set_active",
                        lambda c, keys, active, **kw: calls["set_active"].append((keys, active, kw)) or len(keys))
    at = AppTest.from_string(SCRIPT.replace("CONNECT", connect).replace("CAN_EDIT", str(can_edit)))
    at.run(timeout=30)
    at.radio(key="bs_mode").set_value("Список ASIN").run(timeout=30)
    return at, calls


def infos(at):
    return [i.value for i in at.info]


def test_without_the_secret_the_list_explains_what_is_missing(monkeypatch):
    import best_seller_ui as ui

    at, calls = run_list_page(monkeypatch, connect="None")
    assert not at.exception and ui.NO_DATABASE in infos(at)
    at.text_area(key="bs_batch_input").set_value("https://www.amazon.de/dp/B07WVYMY7Y").run(timeout=30)
    assert not [b for b in at.button if b.key == "bs_watch_add"]  # без базы сохранять некуда
    assert calls == {"add": [], "set_active": []}


def test_missing_tables_are_reported(monkeypatch):
    import best_seller_ui as ui

    at, _ = run_list_page(monkeypatch, connect="(lambda: None)", exists=False)
    assert ui.NO_TABLE in infos(at)


def test_adding_links_saves_country_and_asin(monkeypatch):
    at, calls = run_list_page(monkeypatch, connect="(lambda: None)")
    at.text_area(key="bs_batch_input").set_value(
        "https://www.amazon.de/dp/B07WVYMY7Y\nhttps://www.amazon.it/dp/B07WVYMY7Y").run(timeout=30)
    assert any("Будет добавлено 1 · вернётся из отключённых 0 · уже в списке 1" in c.value for c in at.caption)
    at.button(key="bs_watch_add").click().run(timeout=30)
    assert not at.exception
    assert calls["add"] == [([IT], {"actor_role": access.ROLE_EDITOR, "actor": "Anna"})]


def test_view_only_user_cannot_change_the_list(monkeypatch):
    at, calls = run_list_page(monkeypatch, connect="(lambda: None)", can_edit=False)
    at.text_area(key="bs_batch_input").set_value("https://www.amazon.it/dp/B07WVYMY7Y").run(timeout=30)
    assert at.button(key="bs_watch_add").disabled


def test_manage_by_country_and_disable_selected(monkeypatch):
    at, calls = run_list_page(monkeypatch, connect="(lambda: None)")
    at.radio(key="bs_watch_mode").set_value("Управлять существующими").run(timeout=30)
    assert not at.exception
    market = at.selectbox(key="bs_watch_market")
    assert list(market.options) == ["DE · 1 ASIN", "US · 1 ASIN"]
    # По умолчанию отключённые скрыты: в US активных нет.
    market.set_value("US").run(timeout=30)
    assert "В этой стране нет активных ASIN." in infos(at)
