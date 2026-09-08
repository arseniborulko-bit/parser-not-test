"""
Разовый скрипт: сбрасывает ВСЮ жёлтую подсветку на листах "Current" и
"Competitors" в белый цвет.

Зачем: раньше .clear() ошибочно считался достаточным для снятия подсветки
(на деле он стирает только значения ячеек, не цвет фона) — из-за этого
жёлтая заливка от старых сбоев копилась годами, даже когда ASIN снова
успешно парсился. Код в sheets.py уже починен (см. _highlight_failed_asins_in_sheet
и mark_failed_asins_in_competitors) — они больше не будут копить "жёлтый
мусор" в будущих прогонах. Но то, что уже накопилось в таблице ДО починки,
код сам за собой не уберёт — этот скрипт делает это один раз.

Запуск (по умолчанию — только предпросмотр, ничего не меняет в таблице):
    python reset_highlighting.py

Реальный сброс подсветки:
    python reset_highlighting.py --apply
"""

import argparse
import sys
from pathlib import Path

from config import KEY_FILE_CANDIDATES, SHEET_NAME
from utils import find_key_file


def main():
    parser = argparse.ArgumentParser(description="Сброс всей жёлтой подсветки в Current и Competitors")
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Реально сбросить подсветку в Google Sheets. Без этого флага — только предпросмотр (dry-run).",
    )
    args = parser.parse_args()

    import gspread

    base_dir = Path(__file__).parent
    key_file = find_key_file(base_dir)
    if not key_file:
        print(f"Не найден файл ключа Google service account (искал: {KEY_FILE_CANDIDATES}). Прерываю.")
        sys.exit(1)

    gc = gspread.service_account(filename=key_file)
    spreadsheet = gc.open(SHEET_NAME)

    white_format = {"backgroundColor": {"red": 1.0, "green": 1.0, "blue": 1.0}}

    for sheet_name in ("Current", "Competitors"):
        try:
            worksheet = spreadsheet.worksheet(sheet_name)
        except gspread.WorksheetNotFound:
            print(f"Лист '{sheet_name}' не найден — пропускаю.")
            continue

        rows = worksheet.get_all_values()
        if not rows:
            print(f"Лист '{sheet_name}' пуст — нечего сбрасывать.")
            continue

        num_rows = len(rows)
        num_cols = max(len(r) for r in rows)
        last_col_letter = ""
        n = num_cols
        while n > 0:
            n, remainder = divmod(n - 1, 26)
            last_col_letter = chr(65 + remainder) + last_col_letter

        cell_range = f"A1:{last_col_letter}{num_rows}"
        print(f"Лист '{sheet_name}': диапазон {cell_range} ({num_rows} строк x {num_cols} колонок)")

        if not args.apply:
            print(f"  --- DRY-RUN: подсветка НЕ сброшена. Запустите с --apply, чтобы применить. ---")
            continue

        worksheet.format(cell_range, white_format)
        print(f"  Готово. Подсветка в '{sheet_name}' сброшена в белый.")

    if not args.apply:
        print("\nЭто был предпросмотр. Чтобы реально сбросить подсветку, запустите:")
        print("    python reset_highlighting.py --apply")


if __name__ == "__main__":
    main()
