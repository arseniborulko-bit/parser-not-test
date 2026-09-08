"""
Локальный тест логики дедупликации при записи в History — без реального
подключения к Google Sheets. Имитирует лист History через простой мок-объект.

Проверяет:
1. Если в History уже есть строка с (дата, our_asin, comp_asin) — она не дублируется.
2. Если history_asins ограничивает батч, "чужие" пары в этот вызов не попадают.
"""

import sys
from unittest.mock import MagicMock

sys.path.insert(0, ".")

from sheets import _append_checked_rows_to_history, _history_dedup_key


class FakeHistorySheet:
    """Простая имитация gspread.Worksheet для листа History."""

    def __init__(self, initial_rows):
        self.rows = [list(row) for row in initial_rows]
        self.append_rows_calls = []

    def get_all_values(self):
        return [list(row) for row in self.rows]

    def append_row(self, row):
        self.rows.append(list(row))

    def append_rows(self, rows, value_input_option="USER_ENTERED"):
        self.append_rows_calls.append([list(r) for r in rows])
        self.rows.extend([list(r) for r in rows])


def make_spreadsheet(history_sheet):
    spreadsheet = MagicMock()
    spreadsheet.worksheet.return_value = history_sheet
    return spreadsheet


HEADERS = ["snapshot_date", "marketplace", "our_asin", "comp_asin", "comp_price"]


def make_record(date, our_asin, comp_asin, price, checked=True):
    return {
        "asin": comp_asin,
        "checked": checked,
        "values": {
            "snapshot_date": date,
            "marketplace": "US",
            "our_asin": our_asin,
            "comp_asin": comp_asin,
            "comp_price": price,
        },
    }


def test_no_duplicates_on_fresh_history():
    history_sheet = FakeHistorySheet([HEADERS])
    spreadsheet = make_spreadsheet(history_sheet)

    records = [
        make_record("2026-08-05", "B0AAAAAAAA", "B0BBBBBBBB", "19.99"),
        make_record("2026-08-05", "B0AAAAAAAA", "B0CCCCCCCC", "29.99"),
    ]
    added = _append_checked_rows_to_history(spreadsheet, records)
    assert added == 2, f"Ожидали 2 добавленные строки, получили {added}"
    assert len(history_sheet.rows) == 1 + 2  # header + 2 data rows
    print("test_no_duplicates_on_fresh_history: OK")


def test_duplicate_same_batch_key_is_skipped():
    """Строка с тем же (дата, our_asin, comp_asin) уже есть в History — не должна дублироваться."""
    history_sheet = FakeHistorySheet([
        HEADERS,
        ["2026-08-05", "US", "B0AAAAAAAA", "B0BBBBBBBB", "19.99"],
    ])
    spreadsheet = make_spreadsheet(history_sheet)

    records = [
        make_record("2026-08-05", "B0AAAAAAAA", "B0BBBBBBBB", "19.99"),  # дубль — уже есть
        make_record("2026-08-05", "B0AAAAAAAA", "B0DDDDDDDD", "9.99"),   # новая пара
    ]
    added = _append_checked_rows_to_history(spreadsheet, records)
    assert added == 1, f"Ожидали, что добавится только 1 новая строка, получили {added}"
    assert len(history_sheet.rows) == 1 + 1 + 1  # header + существовавшая + новая
    print("test_duplicate_same_batch_key_is_skipped: OK")


def test_simulates_old_bug_now_fixed():
    """
    Имитирует старый баг: 3 последовательных прогона за один день с одним и тем же
    набором ASIN (как в реальном логе пользователя). Раньше каждый прогон добавлял
    все N строк заново. Теперь второй и третий прогон не должны добавить ничего нового.
    """
    history_sheet = FakeHistorySheet([HEADERS])
    spreadsheet = make_spreadsheet(history_sheet)

    batch_records = [
        make_record("2026-08-05", "B0AAAAAAAA", "B0BBBBBBBB", "19.99"),
        make_record("2026-08-05", "B0AAAAAAAA", "B0CCCCCCCC", "29.99"),
        make_record("2026-08-05", "B0AAAAAAAA", "B0DDDDDDDD", "39.99"),
    ]

    first_run = _append_checked_rows_to_history(spreadsheet, batch_records)
    second_run = _append_checked_rows_to_history(spreadsheet, batch_records)
    third_run = _append_checked_rows_to_history(spreadsheet, batch_records)

    assert first_run == 3, f"Первый прогон должен добавить 3 строки, получили {first_run}"
    assert second_run == 0, f"Второй прогон не должен добавлять дубли, получили {second_run}"
    assert third_run == 0, f"Третий прогон не должен добавлять дубли, получили {third_run}"
    assert len(history_sheet.rows) == 1 + 3, "В итоге в History должно остаться header + 3 строки, без дублей"
    print("test_simulates_old_bug_now_fixed: OK")


def test_history_dedup_key_extracts_asin_from_hyperlink_formula():
    """our_asin/comp_asin в Current могут быть записаны как формулы HYPERLINK — ключ должен извлекать ASIN из них."""
    row_values = {
        "snapshot_date": "2026-08-05",
        "our_asin": '=HYPERLINK("https://www.amazon.com/dp/B0AAAAAAAA", "B0AAAAAAAA")',
        "comp_asin": '=HYPERLINK("https://www.amazon.com/dp/B0BBBBBBBB", "B0BBBBBBBB")',
    }
    key = _history_dedup_key(HEADERS, row_values)
    assert key == ("2026-08-05", "B0AAAAAAAA", "B0BBBBBBBB"), f"Неверный ключ: {key}"
    print("test_history_dedup_key_extracts_asin_from_hyperlink_formula: OK")


if __name__ == "__main__":
    test_no_duplicates_on_fresh_history()
    test_duplicate_same_batch_key_is_skipped()
    test_simulates_old_bug_now_fixed()
    test_history_dedup_key_extracts_asin_from_hyperlink_formula()
    print("\nВсе тесты прошли успешно.")
