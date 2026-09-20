"""PAIR_SOURCE=database: пары ведёт база. Раскладка листа Current и синк не читают лист Competitors."""

import ast
import importlib
from pathlib import Path

import pytest

import sheets

PROJECT = Path(__file__).resolve().parents[1]
HEADERS = [
    "snapshot_date", "marketplace", "currency", "our_product", "our_asin", "our_bsr", "our_price",
    "our_bsr_delta_24h", "competitor", "comp_asin", "comp_bsr", "comp_price", "price_diff_pct",
    "comp_bsr_delta_24h", "comp_stock", "updated_at",
]
LEGACY_ROW = ["", "US", "USD", "Legacy", "B0LEGACY01", "", "", "", "Legacy comp", "B0LEGACY02"] + [""] * 6


class FakeTarget:
    def __init__(self):
        self.cleared = False
        self.updates = []

    def clear(self):
        self.cleared = True

    def update(self, cell, values, value_input_option=None):
        self.updates.append((cell, values))


class FakeMatrix:
    title = sheets.MATRIX_SHEET_NAME

    def __init__(self):
        self.spreadsheet = object()

    def get_all_values(self):
        return [["Current"], HEADERS, LEGACY_ROW, ["History"], HEADERS]


def pair(market, our, comp, product="", name=""):
    return {"marketplace": market, "our_asin": our, "our_product": product, "competitor": name, "comp_asin": comp}


@pytest.fixture
def layout(monkeypatch):
    target = FakeTarget()
    monkeypatch.setattr(sheets, "_get_previous_bsr_snapshot", lambda spreadsheet: {})
    monkeypatch.setattr(sheets, "get_or_create_worksheet", lambda spreadsheet, name, **kwargs: target)
    monkeypatch.setattr(sheets, "_highlight_failed_asins_in_sheet", lambda *args, **kwargs: None)
    monkeypatch.setattr(sheets, "_append_checked_rows_to_history", lambda spreadsheet, records, failed: 0)
    monkeypatch.setattr(sheets, "mark_failed_asins_in_competitors", lambda *args, **kwargs: None)

    def forbidden(spreadsheet):
        raise AssertionError("лист Competitors не должен читаться, когда пары переданы из базы")

    monkeypatch.setattr(sheets, "load_active_competitor_pairs", forbidden)
    return target


def written_rows(target):
    (cell, values), = target.updates
    assert cell == "A1" and values[0] == ["Current"] and values[1] == HEADERS
    return values[2:]


def test_layout_is_built_from_the_given_pairs_in_order_without_reading_the_competitors_sheet(layout):
    pairs = [pair("CA", "B0OUR00001", "B0COMP0001", "Our", "Comp"), pair("US", "B0OUR00002", "B0COMP0002")]
    result = sheets._refresh_current_sheet_from_matrix(FakeMatrix(), {}, "2026-09-21", [], set(), pairs=pairs)
    rows = written_rows(layout)
    assert [r["asin"] for r in result["records"]] == ["B0COMP0001", "B0COMP0002"]
    assert len(rows) == 2 and layout.cleared
    assert "amazon.ca/dp/B0OUR00001" in rows[0][HEADERS.index("our_asin")]
    assert "amazon.com/dp/B0COMP0002" in rows[1][HEADERS.index("comp_asin")]
    assert rows[0][HEADERS.index("marketplace")] == "CA" and rows[0][HEADERS.index("currency")] == "CAD"


def test_the_old_layout_rows_never_leak_into_a_database_layout(layout):
    sheets._refresh_current_sheet_from_matrix(FakeMatrix(), {}, "2026-09-21", [], set(), pairs=[pair("US", "B0OUR00001", "B0COMP0001")])
    assert "Legacy" not in str(written_rows(layout))


def test_a_disabled_or_removed_pair_is_absent_from_the_layout(layout):
    kept = pair("US", "B0OUR00001", "B0COMP0001")
    sheets._refresh_current_sheet_from_matrix(FakeMatrix(), {}, "2026-09-21", [], set(), pairs=[kept])
    assert len(written_rows(layout)) == 1


def test_no_active_pairs_means_an_empty_layout_not_the_legacy_fallback(layout):
    result = sheets._refresh_current_sheet_from_matrix(FakeMatrix(), {}, "2026-09-21", [], set(), pairs=[])
    assert result["records"] == []
    assert written_rows(layout) == []


def test_scraped_titles_still_fill_names_for_new_pairs_with_blank_names(layout):
    products = {
        "B0OUR00001": {"title": "Our title", "bsr": "100", "price": "10"},
        "B0COMP0001": {"title": "Competitor title", "bsr": "200", "price": "9", "stock_status": "In Stock"},
    }
    sheets._refresh_current_sheet_from_matrix(
        FakeMatrix(), products, "2026-09-21", [], set(), pairs=[pair("US", "B0OUR00001", "B0COMP0001")],
    )
    row = written_rows(layout)[0]
    assert row[HEADERS.index("our_product")] == "Our title"
    assert row[HEADERS.index("competitor")] == "Competitor title"
    assert row[HEADERS.index("snapshot_date")] == "2026-09-21" and row[HEADERS.index("comp_bsr")] == "200"


def test_without_given_pairs_the_competitors_sheet_is_still_used(monkeypatch, layout):
    monkeypatch.setattr(sheets, "load_active_competitor_pairs", lambda spreadsheet: [pair("DE", "B0OUR00009", "B0COMP0009")])
    result = sheets._refresh_current_sheet_from_matrix(FakeMatrix(), {}, "2026-09-21", [], set())
    assert [r["asin"] for r in result["records"]] == ["B0COMP0009"]


def test_refresh_current_matrix_forwards_the_pairs(monkeypatch):
    seen = {}
    monkeypatch.setattr(sheets, "_refresh_current_sheet_from_matrix", lambda *args: seen.setdefault("args", args))
    given = [pair("US", "B0OUR00001", "B0COMP0001")]
    sheets.refresh_current_matrix(FakeMatrix(), {}, "2026-09-21", [], set(), pairs=given)
    assert seen["args"][5] is given
    seen.clear()
    sheets.refresh_current_matrix(FakeMatrix(), {}, "2026-09-21", [], set())
    assert seen["args"][5] is None


def test_the_parser_hands_its_database_pairs_to_every_layout_refresh():
    tree = ast.parse((PROJECT / "parser_not_test.py").read_text(encoding="utf-8"))
    calls = [node for node in ast.walk(tree)
             if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "refresh_current_matrix"]
    assert len(calls) == 2
    for call in calls:
        keywords = {kw.arg: ast.unparse(kw.value) for kw in call.keywords}
        assert keywords.get("pairs") == "layout_pairs"


@pytest.fixture
def sync_module(monkeypatch):
    import dotenv

    monkeypatch.setattr(dotenv, "load_dotenv", lambda *args, **kwargs: False)
    return importlib.import_module("sync_sheets_to_db")


class Conn:
    closed = False

    def close(self):
        self.closed = True


def run_sync(module, monkeypatch, pair_source):
    calls = []
    conn = Conn()
    monkeypatch.setattr(module, "connect_spreadsheet", lambda: "spreadsheet")
    monkeypatch.setattr(module, "connect_db", lambda: conn)
    monkeypatch.setattr(module, "sync_competitor_pairs", lambda spreadsheet, c: calls.append("pairs") or 1)
    monkeypatch.setattr(module, "sync_snapshots", lambda spreadsheet, c, title: calls.append(f"snapshots:{title}") or 1)
    monkeypatch.setattr(module, "sync_subscribers", lambda spreadsheet, c: calls.append("subscribers") or 1)
    if pair_source is None:
        monkeypatch.delenv("PAIR_SOURCE", raising=False)
    else:
        monkeypatch.setenv("PAIR_SOURCE", pair_source)
    module.main()
    assert conn.closed
    return calls


@pytest.mark.parametrize("value", ["database", " Database ", "DATABASE"])
def test_sync_does_not_copy_the_competitors_sheet_when_the_database_owns_the_pairs(sync_module, monkeypatch, value):
    calls = run_sync(sync_module, monkeypatch, value)
    assert calls == ["snapshots:History", "snapshots:Current", "subscribers"]


@pytest.mark.parametrize("value", [None, "sheets", "", "unknown"])
def test_sync_still_copies_pairs_by_default(sync_module, monkeypatch, value):
    calls = run_sync(sync_module, monkeypatch, value)
    assert calls == ["pairs", "snapshots:History", "snapshots:Current", "subscribers"]
