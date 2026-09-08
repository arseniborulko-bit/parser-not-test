import importlib


class FakeSheet:
    def __init__(self, values):
        self._values = values

    def col_values(self, index):
        return [row[index - 1] if index - 1 < len(row) else "" for row in self._values]

    def append_row(self, row):
        self._values.append(row)

    def get_all_values(self):
        return self._values


class FakeHistorySheet(FakeSheet):
    def append_rows(self, rows, **_kwargs):
        self._values.extend(rows)

    def get_all_values(self):
        return self._values

    def insert_rows(self, rows, row, **_kwargs):
        self._values[row - 1:row - 1] = rows

    def format(self, *_args, **_kwargs):
        pass

    def freeze(self, **_kwargs):
        pass


def test_find_asin_block_start_matches_hyperlink_value():
    sheets = importlib.import_module("sheets")

    sheet = FakeSheet([
        ["Region", "Category", "Parent", "Parameter"],
        ["", "", "=HYPERLINK(\"https://www.amazon.com/dp/B0CZ767JDG\", \"B0CZ767JDG\")", ""],
    ])

    assert sheets.find_asin_block_start(sheet, "B0CZ767JDG") == 2


def test_append_history_snapshot_adds_daily_row():
    sheets = importlib.import_module("sheets")

    sheet = FakeSheet([["Date", "ASIN", "Title", "Price", "Rating", "BSR", "Reviews"]])

    sheets.append_history_snapshot(
        sheet,
        "30.07.2026",
        "B0CZ767JDG",
        "Title",
        "19.99",
        "4.8",
        "123",
        "456",
    )

    assert sheet._values[-1] == ["30.07.2026", "B0CZ767JDG", "Title", "19.99", "4.8", "123", "456"]


def test_find_current_history_blocks_in_matrix_sheet():
    sheets = importlib.import_module("sheets")
    rows = [
        ["Current"],
        ["API", "Competitors"],
        ["snapshot_date", "comp_asin"],
        ["2026-07-31", "B0CZ767JDG"],
        ["History"],
        ["API", "Competitors"],
        ["snapshot_date", "comp_asin"],
        ["2026-07-30", "B0CZ767JDG"],
    ]

    assert sheets._find_current_history_blocks(rows) == (2, 4, 6)


def test_convert_to_asin_link_creates_hyperlink_for_asin():
    sheets = importlib.import_module("sheets")

    result = sheets._convert_to_asin_link("B0CZ767JDG", "US")

    assert result == '=HYPERLINK("https://www.amazon.com/dp/B0CZ767JDG", "B0CZ767JDG")'


def test_append_matrix_history_snapshot_inserts_linked_asin_values():
    sheets = importlib.import_module("sheets")
    sheet = FakeHistorySheet([[]])

    sheets.append_matrix_history_snapshot(
        sheet,
        "2026-07-31",
        {
            "Маркетплейс": "US",
            "Наш товар": "Test Product",
            "Наш ASIN": "B0CZ767JDG",
            "BSR Наш.": "100",
            "Наша цена": "29.99",
            "Конкурент": "Other Brand",
            "ASIN конкурента": "B0CZ767JDG",
            "BSR конкурента": "200",
            "Цена конкурента": "24.99",
            "Разница, %": "-17%",
            "Динамика BSR (24ч).": "5",
            "Статус наличия (Stock)": "In Stock",
        },
        ["snapshot_date", "comp_asin"],
        ["Date", "ASIN", "Title", "Price", "Rating", "BSR", "Reviews"],
    )

    assert sheet._values[-1][3] == '=HYPERLINK("https://www.amazon.com/dp/B0CZ767JDG", "B0CZ767JDG")'
    assert sheet._values[-1][7] == '=HYPERLINK("https://www.amazon.com/dp/B0CZ767JDG", "B0CZ767JDG")'
