"""
Разовый скрипт очистки дублей в листе "History".

Что делает:
1. Подключается к таблице (по SHEET_NAME/KEY_FILE_CANDIDATES из config.py).
2. Скачивает ВСЕ строки листа History и на всякий случай сохраняет их
   локально в файл history_backup_<timestamp>.csv — до какого-либо удаления.
3. Находит дубли по ключу (snapshot_date, our_asin, comp_asin) — ASIN
   извлекается даже если ячейка хранит формулу =HYPERLINK(...).
4. Оставляет по одной (первой встреченной) строке на каждый ключ, остальные
   отбрасывает.
5. Полностью перезаписывает лист History дедуплицированными данными.

Запуск (по умолчанию — только предпросмотр, ничего не меняет в таблице):
    python dedupe_history.py

Реальная запись изменений в Google Sheets:
    python dedupe_history.py --apply
"""

import argparse
import csv
import sys
from datetime import datetime
from pathlib import Path

from config import KEY_FILE_CANDIDATES, SHEET_NAME
from utils import extract_asin_from_text, find_key_file


def dedup_key(headers, row):
    row_dict = {h: (row[i] if i < len(row) else "") for i, h in enumerate(headers)}
    date_value = str(row_dict.get("snapshot_date", "")).strip()
    comp_asin = extract_asin_from_text(str(row_dict.get("comp_asin", "")))
    if not date_value or not comp_asin:
        return None
    our_asin = extract_asin_from_text(str(row_dict.get("our_asin", ""))) or ""
    return (date_value, our_asin, comp_asin)


def main():
    parser = argparse.ArgumentParser(description="Очистка дублей в листе History")
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
    history_sheet = spreadsheet.worksheet("History")

    print("Скачиваю все строки листа History...")
    rows = history_sheet.get_all_values()
    print(f"Всего строк (включая заголовок): {len(rows)}")

    if not rows:
        print("Лист History пуст — нечего чистить.")
        return

    # Находим строку заголовков (может быть не первой, если раньше уже что-то дописывалось руками)
    header_row_index = None
    headers = []
    for idx, row in enumerate(rows):
        normalized = [str(v).strip() for v in row]
        if "snapshot_date" in normalized and "comp_asin" in normalized:
            headers = normalized
            header_row_index = idx
            break

    if header_row_index is None:
        print("Не нашёл строку заголовков с 'snapshot_date' и 'comp_asin' — прерываю, чтобы не повредить данные.")
        sys.exit(1)

    data_rows = rows[header_row_index + 1:]
    print(f"Строк данных (без заголовка): {len(data_rows)}")

    # --- Бэкап перед любыми изменениями ---
    backup_path = base_dir / f"history_backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
    with open(backup_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(headers)
        writer.writerows(data_rows)
    print(f"Бэкап всех текущих строк сохранён в: {backup_path}")

    # --- Дедупликация: оставляем первую встреченную строку на каждый ключ ---
    seen_keys = set()
    deduped_rows = []
    skipped_no_key = 0
    duplicates_removed = 0

    for row in data_rows:
        key = dedup_key(headers, row)
        if key is None:
            # Строку без даты/ASIN не трогаем (например, случайно оставшаяся пустая/служебная строка)
            deduped_rows.append(row)
            skipped_no_key += 1
            continue
        if key in seen_keys:
            duplicates_removed += 1
            continue
        seen_keys.add(key)
        deduped_rows.append(row)

    print(f"Строк без ключа (дата/ASIN не распознаны, оставлены как есть): {skipped_no_key}")
    print(f"Найдено и будет удалено дублей: {duplicates_removed}")
    print(f"Останется строк данных после очистки: {len(deduped_rows)}")

    if not args.apply:
        print("\n--- DRY-RUN: изменения НЕ записаны в Google Sheets. ---")
        print("Проверьте цифры выше. Если всё верно — запустите с флагом --apply, чтобы применить.")
        return

    print("\nЗаписываю дедуплицированные данные обратно в лист History...")
    history_sheet.clear()
    history_sheet.update("A1", [headers] + deduped_rows, value_input_option="USER_ENTERED")
    print(f"Готово. В History теперь {len(deduped_rows)} строк данных (было {len(data_rows)}).")
    print(f"Резервная копия исходных данных: {backup_path}")


if __name__ == "__main__":
    main()
