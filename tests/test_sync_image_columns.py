"""_snapshots_has_image_columns: проверяет саму базу перед синком, не считает столбцы из листа (без настоящей базы)."""

import sync_sheets_to_db as sync


class FakeCursor:
    def __init__(self, count, key_has_marketplace=True):
        self.count = count
        self.key_has_marketplace = key_has_marketplace
        self.sql = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=()):
        self.sql = sql

    def fetchone(self):
        # Синк спрашивает базу о двух разных вещах: есть ли колонки фото (количество) и есть ли
        # страна в ключе (да/нет). Отвечаем по тексту запроса, а не одним числом на всё.
        if "pg_constraint" in (self.sql or ""):
            return (self.key_has_marketplace,)
        return (self.count,)


class FakeConn:
    def __init__(self, count, key_has_marketplace=True):
        self.cursor_obj = FakeCursor(count, key_has_marketplace)

    def cursor(self):
        return self.cursor_obj


def test_both_columns_present_means_true():
    assert sync._snapshots_has_image_columns(FakeConn(2)) is True


def test_missing_one_or_both_columns_means_false():
    assert sync._snapshots_has_image_columns(FakeConn(1)) is False
    assert sync._snapshots_has_image_columns(FakeConn(0)) is False


def test_checks_the_snapshots_table_in_our_schema():
    conn = FakeConn(2)
    sync._snapshots_has_image_columns(conn)
    sql = conn.cursor_obj.sql
    assert "bsr_radar" in sql and "snapshots" in sql and "information_schema.columns" in sql


class FakeSheet:
    def __init__(self, headers, rows):
        self._headers = headers
        self._rows = rows

    def get_all_values(self):
        return [self._headers] + self._rows


class FakeSpreadsheet:
    def __init__(self, sheet):
        self._sheet = sheet

    def worksheet(self, title):
        return self._sheet


class CommittingFakeConn(FakeConn):
    def __init__(self, count, key_has_marketplace=True):
        super().__init__(count, key_has_marketplace)
        self.committed = False

    def commit(self):
        self.committed = True


HEADERS = ["snapshot_date", "our_asin", "comp_asin", "marketplace", "our_image_url", "comp_image_url"]
ROW = ["2026-09-22", "B0OURASIN1", "B0COMPAAA1", "US", "https://x/our.jpg", "https://x/comp.jpg"]


def test_the_insert_includes_image_columns_only_when_the_database_already_has_them(monkeypatch):
    """Лист Current может обзавестись колонками фото раньше, чем применят миграцию 004 к боевой базе —
    синк должен просто не писать фото, а не падать и не терять остальные данные снимка."""
    calls = []
    monkeypatch.setattr(sync, "execute_values", lambda cur, sql, rows, page_size=1000: calls.append((sql, rows)))
    spreadsheet = FakeSpreadsheet(FakeSheet(HEADERS, [ROW]))

    conn_with = CommittingFakeConn(2)
    sync.sync_snapshots(spreadsheet, conn_with, "Current")
    sql, rows = calls[-1]
    assert "our_image_url" in sql and "comp_image_url" in sql
    assert rows[0][-2:] == ("https://x/our.jpg", "https://x/comp.jpg")
    assert conn_with.committed

    calls.clear()
    conn_without = CommittingFakeConn(0)
    sync.sync_snapshots(spreadsheet, conn_without, "Current")
    sql, rows = calls[-1]
    assert "our_image_url" not in sql and "comp_image_url" not in sql
    assert len(rows[0]) == 15
    assert conn_without.committed
