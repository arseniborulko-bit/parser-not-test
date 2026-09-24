"""Рейтинг и число отзывов: от ответа провайдера до базы и экрана.

Ключевое различие, которое проверяется везде: отсутствие данных и ноль отзывов — разные вещи.
Провайдер уже отдаёт обе метрики в том же ответе, что BSR и цену (scraping.parse_product),
поэтому дополнительных платных запросов не требуется.
"""

from datetime import date

import pandas as pd
import pytest

import dashboard_db as dash_module
import sheets
import sync_sheets_to_db as sync
from test_dashboard_access import SNAPSHOT_ROW, dash, run  # noqa: F401


# --- лист: пусто против нуля -------------------------------------------------

@pytest.mark.parametrize("value, expected", [
    (4.6, 4.6),
    (0, 0),          # ноль отзывов — настоящий ноль
    ("Not Found", ""),
    (None, ""),
    ("", ""),
])
def test_a_missing_metric_becomes_blank_but_zero_stays_zero(value, expected):
    assert sheets._metric_or_blank(value) == expected


def test_the_sheet_preserves_rating_for_a_side_not_checked_this_run():
    """При частичном сборе сторона, которую не проверяли, не должна обнуляться."""
    for field in ("our_rating", "our_reviews_count", "comp_rating", "comp_reviews_count"):
        assert field in sheets._PRESERVABLE_FIELDS, field


# --- синк: разбор значений ---------------------------------------------------

@pytest.mark.parametrize("raw, expected", [
    ("4.6", 4.6),
    ("4,6", 4.6),        # запятая как разделитель
    ("4.65", 4.7),       # одна цифра после запятой
    ("", None),
    ("нет", None),
    ("5.4", None),       # вне диапазона 0–5 — лучше отсутствие, чем неверное значение
    ("-1", None),
])
def test_rating_is_parsed_or_refused(raw, expected):
    assert sync._rating(raw) == expected


@pytest.mark.parametrize("raw, expected", [
    ("1 234", 1234),
    ("1,234", 1234),
    ("0", 0),            # настоящий ноль сохраняется
    ("", None),
    ("нет", None),
    ("-5", None),
])
def test_reviews_are_parsed_or_refused(raw, expected):
    assert sync._reviews(raw) == expected


def test_zero_reviews_is_a_value_not_a_gap():
    assert sync._reviews("0") == 0
    assert sync._reviews("") is None


# --- синк: работа до и после миграции ---------------------------------------

class FakeCursor:
    def __init__(self, counts):
        self.counts = counts
        self.sql = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=()):
        self.sql = sql

    def fetchone(self):
        text = self.sql or ""
        if "pg_constraint" in text:
            return (True,)
        if "our_rating" in text:
            return (self.counts["ratings"],)
        return (self.counts["images"],)


class FakeConn:
    def __init__(self, ratings=4, images=2):
        self.cursor_obj = FakeCursor({"ratings": ratings, "images": images})
        self.committed = False

    def cursor(self):
        return self.cursor_obj

    def commit(self):
        self.committed = True


def test_the_database_is_asked_whether_the_rating_columns_exist():
    assert sync._snapshots_has_rating_columns(FakeConn(ratings=4)) is True
    assert sync._snapshots_has_rating_columns(FakeConn(ratings=3)) is False
    assert sync._snapshots_has_rating_columns(FakeConn(ratings=0)) is False


HEADERS = ["snapshot_date", "marketplace", "our_asin", "comp_asin",
           "our_rating", "our_reviews_count", "comp_rating", "comp_reviews_count"]
ROW = ["2026-09-24", "UK", "B0OURASIN1", "B0COMPAAA1", "4.6", "0", "4.2", "1 234"]


def sync_once(monkeypatch, ratings):
    from test_sync_image_columns import FakeSheet, FakeSpreadsheet

    calls = []
    monkeypatch.setattr(sync, "execute_values", lambda cur, sql, batch, page_size=1000: calls.append((sql, batch)))
    conn = FakeConn(ratings=ratings, images=0)
    sync.sync_snapshots(FakeSpreadsheet(FakeSheet(HEADERS, [ROW])), conn, "Current")
    return calls[-1]


def test_after_the_migration_the_metrics_reach_the_insert(monkeypatch):
    sql, batch = sync_once(monkeypatch, ratings=4)
    assert "our_rating, our_reviews_count, comp_rating, comp_reviews_count" in sql
    assert batch[0][-4:] == (4.6, 0, 4.2, 1234), "ноль отзывов обязан дойти как 0, а не как пусто"


def test_before_the_migration_the_sync_still_writes_everything_else(monkeypatch):
    sql, batch = sync_once(monkeypatch, ratings=0)
    assert "our_rating" not in sql
    assert len(batch[0]) == 15, "без миграции пишем прежний набор столбцов"


# --- экран -------------------------------------------------------------------

def test_the_dashboard_shows_one_decimal_and_keeps_gaps_empty():
    assert dash_module._format_rating_or_blank(4.0) == "4.0"
    assert dash_module._format_rating_or_blank(4.65) == "4.7"
    assert dash_module._format_rating_or_blank(None) == ""
    assert dash_module._format_rating_or_blank(pd.NA) == ""


def test_old_rows_without_the_metrics_open_without_errors_and_without_zeros():
    """Старая история приходит с NULL — на экране это пусто, а не 0."""
    frame = pd.DataFrame([{**SNAPSHOT_ROW, "our_rating": None, "our_reviews_count": None,
                           "comp_rating": 4.2, "comp_reviews_count": 0}])
    presented, _ = dash_module._present_table(frame)
    assert presented["Рейтинг наш"].iloc[0] == ""
    assert presented["Отзывов наш"].iloc[0] == ""
    assert presented["Рейтинг конкурента"].iloc[0] == "4.2"
    assert presented["Отзывов конкурента"].iloc[0] == "0", "настоящий ноль отзывов — это 0"


def test_the_columns_are_named_in_russian_like_the_rest():
    for column in ("our_rating", "our_reviews_count", "comp_rating", "comp_reviews_count"):
        assert column in dash_module._COLUMN_LABELS, column


def test_a_database_without_the_columns_still_gives_the_dashboard_its_columns():
    """Интерфейс не должен знать, применена ли миграция."""
    data = pd.DataFrame([{"our_asin": "B0OURASIN1"}])
    filled = dash_module._with_missing_optional_columns(data, present=[])
    for column in dash_module._OPTIONAL_SNAPSHOT_COLUMNS:
        assert column in filled.columns, column
    assert pd.isna(filled["our_rating"].iloc[0]), "отсутствие данных — NULL, а не ноль"
