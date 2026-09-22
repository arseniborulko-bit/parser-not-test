"""Ссылка на фото в листе Current: заполняется, если колонка есть в шаблоне; без неё — ничего не ломается."""

import sheets

HEADERS_WITH_IMAGES = [
    "marketplace", "currency", "our_product", "our_asin", "our_bsr", "our_price", "our_image_url",
    "competitor", "comp_asin", "comp_bsr", "comp_price", "comp_image_url", "updated_at",
]
HEADERS_WITHOUT_IMAGES = [h for h in HEADERS_WITH_IMAGES if "image" not in h]


class FakeTarget:
    def __init__(self):
        self.updates = []

    def clear(self):
        pass

    def update(self, cell, values, value_input_option=None):
        self.updates.append((cell, values))


class FakeMatrix:
    title = sheets.MATRIX_SHEET_NAME

    def __init__(self, headers):
        self.spreadsheet = object()
        self._headers = headers

    def get_all_values(self):
        return [["Current"], self._headers, ["History"], self._headers]


def written_rows(target, headers):
    (cell, values), = target.updates
    assert cell == "A1" and values[0] == ["Current"] and values[1] == headers
    return values[2:]


def pair(our, comp):
    return {"marketplace": "US", "our_product": "", "our_asin": our, "competitor": "", "comp_asin": comp}


def setup(monkeypatch):
    target = FakeTarget()
    monkeypatch.setattr(sheets, "_get_previous_bsr_snapshot", lambda spreadsheet: {})
    monkeypatch.setattr(sheets, "get_or_create_worksheet", lambda spreadsheet, name, **kwargs: target)
    monkeypatch.setattr(sheets, "_highlight_failed_asins_in_sheet", lambda *args, **kwargs: None)
    monkeypatch.setattr(sheets, "_append_checked_rows_to_history", lambda spreadsheet, records, failed: 0)
    monkeypatch.setattr(sheets, "mark_failed_asins_in_competitors", lambda *args, **kwargs: None)
    return target


def test_image_urls_are_written_when_the_template_has_the_columns(monkeypatch):
    target = setup(monkeypatch)
    products = {
        "B0OUR00001": {"title": "Our", "bsr": "100", "price": 9.0, "image_url": "https://x/our.jpg"},
        "B0COMP0001": {"title": "Comp", "bsr": "200", "price": 8.0, "image_url": "https://x/comp.jpg"},
    }
    sheets._refresh_current_sheet_from_matrix(
        FakeMatrix(HEADERS_WITH_IMAGES), products, "2026-09-22", [], {"B0OUR00001", "B0COMP0001"},
        pairs=[pair("B0OUR00001", "B0COMP0001")],
    )
    row, = written_rows(target, HEADERS_WITH_IMAGES)
    assert row[HEADERS_WITH_IMAGES.index("our_image_url")] == "https://x/our.jpg"
    assert row[HEADERS_WITH_IMAGES.index("comp_image_url")] == "https://x/comp.jpg"


def test_a_product_without_a_photo_leaves_the_column_blank_not_missing(monkeypatch):
    target = setup(monkeypatch)
    products = {"B0OUR00001": {"title": "Our", "bsr": "100", "price": 9.0, "image_url": ""}}
    sheets._refresh_current_sheet_from_matrix(
        FakeMatrix(HEADERS_WITH_IMAGES), products, "2026-09-22", [], {"B0OUR00001"},
        pairs=[pair("B0OUR00001", "B0COMP0001")],
    )
    row, = written_rows(target, HEADERS_WITH_IMAGES)
    assert row[HEADERS_WITH_IMAGES.index("our_image_url")] == ""


def test_missing_image_columns_in_the_template_do_not_break_the_layout(monkeypatch):
    target = setup(monkeypatch)
    products = {"B0OUR00001": {"title": "Our", "bsr": "100", "price": 9.0, "image_url": "https://x/our.jpg"}}
    sheets._refresh_current_sheet_from_matrix(
        FakeMatrix(HEADERS_WITHOUT_IMAGES), products, "2026-09-22", [], {"B0OUR00001"},
        pairs=[pair("B0OUR00001", "B0COMP0001")],
    )
    row, = written_rows(target, HEADERS_WITHOUT_IMAGES)
    assert len(row) == len(HEADERS_WITHOUT_IMAGES)
