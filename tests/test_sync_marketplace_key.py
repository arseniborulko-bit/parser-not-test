"""Страна в ключе снимков: пара ASIN на нескольких рынках больше не схлопывается в одну строку.

Ключ дедупликации в Python и ON CONFLICT в SQL обязаны совпадать с тем ограничением, которое
реально есть в базе, — иначе либо теряются строки (старый ключ), либо Postgres отказывает
("ON CONFLICT DO UPDATE command cannot affect row a second time" / "no unique constraint matching").
"""

import sync_sheets_to_db as sync
from test_sync_image_columns import CommittingFakeConn, FakeConn, FakeSheet, FakeSpreadsheet

HEADERS = ["snapshot_date", "marketplace", "our_asin", "comp_asin", "our_price"]
# Одна и та же пара ASIN, которую отслеживают на трёх рынках: на боевой базе таких пар 25.
SAME_PAIR_THREE_MARKETS = [
    ["2026-09-23", "ES", "B0OURASIN1", "B0COMPAAA1", "19.99"],
    ["2026-09-23", "FR", "B0OURASIN1", "B0COMPAAA1", "21.50"],
    ["2026-09-23", "IT", "B0OURASIN1", "B0COMPAAA1", "23.00"],
]


def sync_rows(rows, *, key_has_marketplace, monkeypatch):
    calls = []
    monkeypatch.setattr(sync, "execute_values", lambda cur, sql, batch, page_size=1000: calls.append((sql, batch)))
    spreadsheet = FakeSpreadsheet(FakeSheet(HEADERS, rows))
    conn = CommittingFakeConn(0, key_has_marketplace=key_has_marketplace)
    written = sync.sync_snapshots(spreadsheet, conn, "Current")
    return written, calls[-1]


def test_a_pair_tracked_on_three_markets_keeps_three_rows(monkeypatch):
    written, (sql, batch) = sync_rows(SAME_PAIR_THREE_MARKETS, key_has_marketplace=True, monkeypatch=monkeypatch)
    assert written == 3 and len(batch) == 3
    assert {row[1] for row in batch} == {"ES", "FR", "IT"}
    assert sorted(str(row[5]) for row in batch) == ["19.99", "21.5", "23.0"]


def test_the_conflict_target_names_the_key_that_the_database_actually_has(monkeypatch):
    _, (new_sql, _) = sync_rows(SAME_PAIR_THREE_MARKETS, key_has_marketplace=True, monkeypatch=monkeypatch)
    assert "ON CONFLICT (snapshot_date, marketplace, our_asin, comp_asin)" in new_sql

    _, (old_sql, _) = sync_rows(SAME_PAIR_THREE_MARKETS, key_has_marketplace=False, monkeypatch=monkeypatch)
    assert "ON CONFLICT (snapshot_date, our_asin, comp_asin)" in old_sql


def test_before_the_migration_rows_are_still_collapsed_so_the_upsert_cannot_fail(monkeypatch):
    """Пока миграции 006 нет, развести строки по странам НЕЛЬЗЯ: в одном запросе оказались бы
    две строки с одинаковым ключом, и Postgres отказал бы целиком, потеряв весь сбор."""
    written, (_, batch) = sync_rows(SAME_PAIR_THREE_MARKETS, key_has_marketplace=False, monkeypatch=monkeypatch)
    assert written == 1 and len(batch) == 1


def test_different_pairs_on_the_same_market_are_never_merged(monkeypatch):
    rows = [
        ["2026-09-23", "US", "B0OURASIN1", "B0COMPAAA1", "10.00"],
        ["2026-09-23", "US", "B0OURASIN1", "B0COMPBBB2", "11.00"],
    ]
    written, (_, batch) = sync_rows(rows, key_has_marketplace=True, monkeypatch=monkeypatch)
    assert written == 2 and len(batch) == 2


def test_the_probe_asks_postgres_about_the_real_constraint_not_the_sheet():
    conn = FakeConn(0, key_has_marketplace=True)
    assert sync._snapshots_key_has_marketplace(conn) is True
    sql = conn.cursor_obj.sql
    assert "pg_constraint" in sql and "marketplace" in sql
    assert "bsr_radar.snapshots" in sql


def test_a_database_without_the_marketplace_key_is_detected():
    assert sync._snapshots_key_has_marketplace(FakeConn(0, key_has_marketplace=False)) is False
