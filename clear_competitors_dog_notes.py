"""
Разовый скрипт очистки старых пометок "Собачка" в колонке Notes/Комментарии
на листе "Competitors".

Раньше код автоматически писал туда текст "Собачка" для битых ASIN. Сейчас
эта запись убрана из кода (подсветка ячейки жёлтым осталась, текст — нет),
но в самой таблице могли остаться старые записи с прошлых прогонов — этот
скрипт их находит и очищает.

Запуск (по умолчанию — только предпросмотр, ничего не меняет в таблице):
    python clear_competitors_dog_notes.py

Реальная запись изменений в Google Sheets:
    python clear_competitors_dog_notes.py --apply
"""

import argparse
import sys
from pathlib import Path

from config import KEY_FILE_CANDIDATES, SHEET_NAME
from utils import find_key_file

NOTES_HEADER_NAMES = ("комментарии", "comment", "notes")
DOG_MARKERS = ("собачка",)


def main():
    parser = argparse.ArgumentParser(description="Очистка старых пометок 'Собачка' в Notes листа Competitors")
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Реально записать изменения в Google Sheets. Без этого флага — только предпросмотр (dry-run).",
    )
    args = parser.parse_args()

    import gspread  # локальный импорт, чтобы --help работал даже без установленного gspread

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
        print("Лист 'Competitors' не найден — нечего чистить.")
        return

    rows = worksheet.get_all_values()
    if not rows:
        print("Лист 'Competitors' пуст — нечего чистить.")
        return

    headers = [str(v).strip().casefold() for v in rows[0]]
    notes_col = next((i for i, h in enumerate(headers) if h in NOTES_HEADER_NAMES), None)
    if notes_col is None:
        print(f"Не нашёл колонку Notes/Комментарии (искал: {NOTES_HEADER_NAMES}) — нечего чистить.")
        return

    to_clear = []
    for row_idx, row in enumerate(rows[1:], start=2):
        if len(row) <= notes_col:
            continue
        value = str(row[notes_col]).strip()
        if value and value.casefold() in DOG_MARKERS:
            to_clear.append(row_idx)

    print(f"Найдено ячеек с пометкой 'Собачка' в Notes: {len(to_clear)}")
    if not to_clear:
        return

    if not args.apply:
        print("\n--- DRY-RUN: изменения НЕ записаны в Google Sheets. ---")
        print(f"Строки, которые будут очищены: {to_clear}")
        print("Если всё верно — запустите с флагом --apply, чтобы применить.")
        return

    from gspread.utils import rowcol_to_a1

    requests = [
        {"range": rowcol_to_a1(row_idx, notes_col + 1), "values": [[""]]}
        for row_idx in to_clear
    ]
    worksheet.batch_update(requests, value_input_option="USER_ENTERED")
    print(f"Готово. Очищено {len(to_clear)} ячеек в колонке Notes/Комментарии.")


if __name__ == "__main__":
    main()
