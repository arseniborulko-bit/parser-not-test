"""Частичный сбор (например, только наши товары или только один рынок) не должен стирать
данные стороны пары, которую в этот раз не проверяли — она сохраняет последнее известное
состояние вместо пустых ячеек. Без настоящих Google Sheets."""

import sheets

HEADERS = [
    "snapshot_date", "marketplace", "currency", "our_product", "our_asin", "our_bsr", "our_price",
    "our_bsr_delta_24h", "our_image_url", "competitor", "comp_asin", "comp_bsr", "comp_price",
    "price_diff_pct", "comp_bsr_delta_24h", "comp_stock", "comp_image_url", "updated_at",
]
OUR, COMP = "B0OUR00001", "B0COMP0001"


class FakeTarget:
    def __init__(self):
        self.updates = []

    def clear(self):
        pass

    def update(self, cell, values, value_input_option=None):
        self.updates.append((cell, values))


class FakeMatrix:
    title = sheets.MATRIX_SHEET_NAME

    def __init__(self):
        self.spreadsheet = object()

    def get_all_values(self):
        return [["Current"], HEADERS, ["History"], HEADERS]


def pair(our=OUR, comp=COMP):
    return {"marketplace": "US", "our_product": "", "our_asin": our, "competitor": "", "comp_asin": comp}


def setup(monkeypatch, previous):
    target = FakeTarget()
    monkeypatch.setattr(sheets, "_get_previous_bsr_snapshot", lambda spreadsheet: previous)
    monkeypatch.setattr(sheets, "get_or_create_worksheet", lambda spreadsheet, name, **kwargs: target)
    monkeypatch.setattr(sheets, "_highlight_failed_asins_in_sheet", lambda *args, **kwargs: None)
    monkeypatch.setattr(sheets, "_append_checked_rows_to_history", lambda spreadsheet, records, failed: 0)
    monkeypatch.setattr(sheets, "mark_failed_asins_in_competitors", lambda *args, **kwargs: None)
    return target


def written_row(target):
    (cell, values), = target.updates
    assert cell == "A1" and values[0] == ["Current"] and values[1] == HEADERS
    row, = values[2:]
    return dict(zip(HEADERS, row))


PREVIOUS = {
    (OUR, COMP): {
        "our_product": "Our old title", "our_bsr": "100", "our_price": "19.99", "our_bsr_delta_24h": "-5",
        "our_image_url": "https://x/our-old.jpg",
        "competitor": "Comp old title", "comp_bsr": "200", "comp_price": "24.99", "comp_bsr_delta_24h": "3",
        "comp_stock": "In Stock", "comp_image_url": "https://x/comp-old.jpg",
    }
}


def test_collecting_only_our_side_preserves_the_untouched_competitor_side(monkeypatch):
    target = setup(monkeypatch, PREVIOUS)
    products = {OUR: {"title": "Our new title", "bsr": "90", "price": 18.0, "image_url": "https://x/our-new.jpg"}}
    sheets._refresh_current_sheet_from_matrix(FakeMatrix(), products, "2026-09-22", [], {OUR}, pairs=[pair()])
    row = written_row(target)
    assert row["our_product"] == "Our new title" and row["our_bsr"] == "90" and row["our_price"] == 18.0
    assert row["our_image_url"] == "https://x/our-new.jpg"
    assert row["competitor"] == "Comp old title" and row["comp_bsr"] == "200" and row["comp_price"] == "24.99"
    assert row["comp_stock"] == "In Stock" and row["comp_image_url"] == "https://x/comp-old.jpg"
    assert row["snapshot_date"] == "2026-09-22"


def test_collecting_only_the_competitor_side_preserves_our_untouched_side(monkeypatch):
    target = setup(monkeypatch, PREVIOUS)
    products = {COMP: {"title": "Comp new title", "bsr": "180", "price": 23.0, "image_url": "https://x/comp-new.jpg"}}
    sheets._refresh_current_sheet_from_matrix(FakeMatrix(), products, "2026-09-22", [], {COMP}, pairs=[pair()])
    row = written_row(target)
    assert row["competitor"] == "Comp new title" and row["comp_bsr"] == "180"
    assert row["our_product"] == "Our old title" and row["our_bsr"] == "100" and row["our_price"] == "19.99"
    assert row["our_image_url"] == "https://x/our-old.jpg"


def test_price_diff_is_recomputed_from_one_fresh_and_one_preserved_price(monkeypatch):
    target = setup(monkeypatch, PREVIOUS)
    products = {OUR: {"title": "Our new title", "bsr": "90", "price": 20.0}}
    sheets._refresh_current_sheet_from_matrix(FakeMatrix(), products, "2026-09-22", [], {OUR}, pairs=[pair()])
    row = written_row(target)
    # our_price свежая (20.0), comp_price сохранена (24.99): (24.99/20 - 1) = 24.9%
    assert row["price_diff_pct"] == "24.9%"


def test_an_explicit_failure_still_marks_at_sign_and_does_not_fall_back_to_old_values(monkeypatch):
    target = setup(monkeypatch, PREVIOUS)
    sheets._refresh_current_sheet_from_matrix(FakeMatrix(), {}, "2026-09-22", [COMP], {OUR, COMP}, pairs=[pair()])
    row = written_row(target)
    assert row["competitor"] == "@" and row["comp_bsr"] == "@" and row["comp_price"] == "@"
    assert row["price_diff_pct"] == ""


def test_a_pair_never_seen_before_has_nothing_to_preserve_and_stays_blank(monkeypatch):
    target = setup(monkeypatch, {})
    sheets._refresh_current_sheet_from_matrix(FakeMatrix(), {}, "2026-09-22", [], set(), pairs=[pair("B0OUR00009", "B0COMP0009")])
    row = written_row(target)
    assert row["our_product"] == "" and row["comp_bsr"] == "" and row["snapshot_date"] == ""


def test_a_fully_untouched_pair_with_prior_data_keeps_both_sides_but_no_new_date(monkeypatch):
    """Например, пара с другого рынка при сборе, ограниченном одним рынком."""
    target = setup(monkeypatch, PREVIOUS)
    sheets._refresh_current_sheet_from_matrix(FakeMatrix(), {}, "2026-09-22", [], set(), pairs=[pair()])
    row = written_row(target)
    assert row["our_bsr"] == "100" and row["comp_bsr"] == "200"
    assert row["snapshot_date"] == "" and row["updated_at"] == ""
