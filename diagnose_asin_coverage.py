"""
Диагностика: почему load_asins_from_config() возвращает меньше ASIN,
чем реально есть в таблице.

Показывает построчный разбор листа "Competitors":
- сколько всего строк с данными,
- сколько отсеялось по Active,
- сколько отсеялось из-за отсутствия/невалидного Our ASIN или Competitor ASIN,
- сколько валидных пар осталось,
- сколько из них — уникальных ASIN (что в итоге вернёт load_asins_from_config).

Ничего не пишет и не меняет в таблице — только читает и печатает отчёт.

Запуск:
    python diagnose_asin_coverage.py
"""

import sys
from pathlib import Path

from config import KEY_FILE_CANDIDATES, SHEET_NAME
from utils import extract_asin_from_text, find_key_file


def main():
    import gspread

    base_dir = Path(__file__).parent
    key_file = find_key_file(base_dir)
    if not key_file:
        print(f"Не найден файл ключа Google service account (искал: {KEY_FILE_CANDIDATES}). Прерываю.")
        sys.exit(1)

    gc = gspread.service_account(filename=key_file)
    spreadsheet = gc.open(SHEET_NAME)

    try:
        worksheet = spreadsheet.worksheet("Competitors")
    except gspread.WorksheetNotFound:
        print("Лист 'Competitors' не найден.")
        sys.exit(1)

    rows = worksheet.get_all_values()
    if not rows:
        print("Лист 'Competitors' пуст.")
        return

    headers = {str(value).strip().casefold(): index for index, value in enumerate(rows[0])}
    required = {"marketplace", "our product", "our asin", "competitor name", "competitor asin"}
    missing = required - set(headers)
    if missing:
        print(f"⚠️ В заголовках не хватает колонок: {missing}")
        print(f"Найденные заголовки: {list(headers)}")
        print("load_active_competitor_pairs() в этом случае вернёт [] и код уйдёт в fallback на вкладку 'Матрица' / 'Config'.")
        return

    print(f"Заголовки в порядке — колонки найдены: {sorted(required)}\n")

    data_rows = rows[1:]
    total_rows = len(data_rows)

    skipped_inactive = []
    skipped_no_our_asin = []
    skipped_no_comp_asin = []
    valid_pairs = []

    def value(row, name):
        idx = headers[name]
        return row[idx].strip() if len(row) > idx else ""

    for row_number, row in enumerate(data_rows, start=2):  # +2: 1-indexed, +заголовок
        if not any(c.strip() for c in row):
            continue  # полностью пустая строка — пропускаем без учёта

        active = row[headers["active"]].strip().upper() if "active" in headers and len(row) > headers["active"] else "Y"
        if active not in {"Y", "YES", "1", "TRUE"}:
            skipped_inactive.append((row_number, value(row, "our asin"), value(row, "competitor asin"), repr(active)))
            continue

        our_asin_raw = value(row, "our asin")
        comp_asin_raw = value(row, "competitor asin")
        our_asin = extract_asin_from_text(our_asin_raw)
        comp_asin = extract_asin_from_text(comp_asin_raw)

        if not our_asin:
            skipped_no_our_asin.append((row_number, our_asin_raw, comp_asin_raw))
            continue
        if not comp_asin:
            skipped_no_comp_asin.append((row_number, our_asin_raw, comp_asin_raw))
            continue

        valid_pairs.append((row_number, our_asin, comp_asin))

    unique_asins = list(dict.fromkeys(
        asin for _, our_asin, comp_asin in valid_pairs for asin in (our_asin, comp_asin)
    ))

    print(f"Всего строк с данными на листе 'Competitors': {total_rows}")
    print(f"  Пустых строк пропущено: {total_rows - len(skipped_inactive) - len(skipped_no_our_asin) - len(skipped_no_comp_asin) - len(valid_pairs)}")
    print(f"  Отсеяно по Active (не Y/YES/1/TRUE): {len(skipped_inactive)}")
    print(f"  Отсеяно из-за отсутствия/невалидного Our ASIN: {len(skipped_no_our_asin)}")
    print(f"  Отсеяно из-за отсутствия/невалидного Competitor ASIN: {len(skipped_no_comp_asin)}")
    print(f"  Валидных пар (строк): {len(valid_pairs)}")
    print(f"  Уникальных ASIN из валидных пар (это и вернёт load_asins_from_config): {len(unique_asins)}")

    def print_examples(title, items, limit=30):
        if not items:
            return
        print(f"\n--- {title} (первые {min(limit, len(items))} из {len(items)}) ---")
        for item in items[:limit]:
            print(f"  строка {item[0]}: {item[1:]}")

    print_examples("Отсеяны по Active", skipped_inactive)
    print_examples("Отсеяны — нет Our ASIN", skipped_no_our_asin)
    print_examples("Отсеяны — нет Competitor ASIN", skipped_no_comp_asin)


if __name__ == "__main__":
    main()
