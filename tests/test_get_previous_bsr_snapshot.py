"""_get_previous_bsr_snapshot: читает предыдущее состояние листа Current целиком (не только BSR),
чтобы частичный сбор мог сохранить непроверенную сторону пары. Без настоящих Google Sheets."""

import sheets


class FakeSheet:
    def __init__(self, rows):
        self._rows = rows

    def get_all_values(self):
        return self._rows


def reset_cache(monkeypatch):
    monkeypatch.setattr(sheets, "_PREVIOUS_BSR_SNAPSHOT_CACHE", None)


PREV_HEADERS = [
    "our_asin", "comp_asin", "our_product", "our_bsr", "our_price", "our_bsr_delta_24h", "our_image_url",
    "competitor", "comp_bsr", "comp_price", "comp_bsr_delta_24h", "comp_stock", "comp_image_url",
]


def test_reads_every_preservable_field_for_each_pair(monkeypatch):
    reset_cache(monkeypatch)
    row = ["B0OUR00001", "B0COMP0001", "Our title", "100", "19.99", "-5", "https://x/our.jpg",
           "Comp title", "200", "24.99", "3", "In Stock", "https://x/comp.jpg"]
    monkeypatch.setattr(sheets, "get_or_create_worksheet", lambda spreadsheet, name, **kw: FakeSheet([["Current"], PREV_HEADERS, row]))
    snapshot = sheets._get_previous_bsr_snapshot(object())
    values = snapshot[("B0OUR00001", "B0COMP0001")]
    assert values["our_product"] == "Our title" and values["our_price"] == "19.99"
    assert values["comp_stock"] == "In Stock" and values["comp_image_url"] == "https://x/comp.jpg"


def test_a_pair_missing_from_the_previous_sheet_is_simply_absent(monkeypatch):
    reset_cache(monkeypatch)
    row = ["B0OUR00001", "B0COMP0001"] + [""] * 11
    monkeypatch.setattr(sheets, "get_or_create_worksheet", lambda spreadsheet, name, **kw: FakeSheet([["Current"], PREV_HEADERS, row]))
    snapshot = sheets._get_previous_bsr_snapshot(object())
    assert ("B0OUR00001", "B0COMP9999") not in snapshot


def test_an_unreadable_sheet_yields_an_empty_snapshot_not_an_error(monkeypatch):
    reset_cache(monkeypatch)

    def broken(spreadsheet, name, **kw):
        raise RuntimeError("boom")

    monkeypatch.setattr(sheets, "get_or_create_worksheet", broken)
    assert sheets._get_previous_bsr_snapshot(object()) == {}


def test_the_result_is_cached_for_the_whole_process(monkeypatch):
    reset_cache(monkeypatch)
    calls = []

    def tracked(spreadsheet, name, **kw):
        calls.append(1)
        return FakeSheet([["Current"], PREV_HEADERS, ["B0X", "B0Y"] + [""] * 11])

    monkeypatch.setattr(sheets, "get_or_create_worksheet", tracked)
    sheets._get_previous_bsr_snapshot(object())
    sheets._get_previous_bsr_snapshot(object())
    assert len(calls) == 1
