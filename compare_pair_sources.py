"""
Сверяет данные между Google Sheets и Postgres до того, как включать
PAIR_SOURCE=database / SUBSCRIBER_SOURCE=database где-либо всерьёз -
без единого запроса к ScrapingDog и без сообщений в Telegram.

Запуск: python compare_pair_sources.py
"""

from __future__ import annotations

from pathlib import Path

from config import KEY_FILE_CANDIDATES
from db_pairs import load_active_competitor_pairs_from_db
from db_subscribers import load_subscribers_from_db
from sheets import connect_sheet, load_active_competitor_pairs
from subscribers import get_active_subscriber_ids
from utils import find_key_file


def _pair_key(pair):
    return (pair["marketplace"], pair["our_asin"], pair["comp_asin"])


def compare_subscribers(sheet) -> None:
    sheets_ids = set(get_active_subscriber_ids(sheet.spreadsheet))
    db_rows = load_subscribers_from_db()
    db_ids = {row["telegram_id"] for row in db_rows if row["active"]}

    print(f"\nАктивных подписчиков -- Sheets: {len(sheets_ids)}, база: {len(db_ids)}")
    only_in_sheets = sheets_ids - db_ids
    only_in_db = db_ids - sheets_ids
    if not only_in_sheets and not only_in_db:
        print("Совпадает полностью -- можно безопасно пробовать SUBSCRIBER_SOURCE=database.")
    else:
        print(f"Есть расхождения: только в Sheets -- {len(only_in_sheets)}, только в базе -- {len(only_in_db)}.")
        print("Прогоните sync_sheets_to_db.py заново.")


def main() -> None:
    base_dir = Path(__file__).parent
    key_file = find_key_file(base_dir)
    if not key_file:
        print(f"Не найден файл ключа Google service account (искал: {KEY_FILE_CANDIDATES}).")
        return

    sheet = connect_sheet(key_file)
    sheets_pairs = load_active_competitor_pairs(sheet.spreadsheet)
    db_pairs = load_active_competitor_pairs_from_db()

    sheets_keys = {_pair_key(p) for p in sheets_pairs}
    db_keys = {_pair_key(p) for p in db_pairs}

    sheets_asins = {a for p in sheets_pairs for a in (p["our_asin"], p["comp_asin"]) if a}
    db_asins = {a for p in db_pairs for a in (p["our_asin"], p["comp_asin"]) if a}

    sheets_markets = {p["marketplace"] for p in sheets_pairs}
    db_markets = {p["marketplace"] for p in db_pairs}

    print(f"Активных пар      -- Sheets: {len(sheets_pairs)}, база: {len(db_pairs)}")
    print(f"Уникальных ASIN   -- Sheets: {len(sheets_asins)}, база: {len(db_asins)}")
    print(f"Маркетплейсов     -- Sheets: {sorted(sheets_markets)}, база: {sorted(db_markets)}")

    only_in_sheets = sheets_keys - db_keys
    only_in_db = db_keys - sheets_keys

    if not only_in_sheets and not only_in_db:
        print("\nСовпадает полностью -- можно безопасно пробовать PAIR_SOURCE=database.")
    else:
        print(f"\nЕсть расхождения: только в Sheets -- {len(only_in_sheets)}, только в базе -- {len(only_in_db)}.")
        print("Скорее всего база не синхронизирована -- прогоните sync_sheets_to_db.py заново.")
        for key in sorted(only_in_sheets)[:10]:
            print("  только в Sheets:", key)
        for key in sorted(only_in_db)[:10]:
            print("  только в базе:", key)

    compare_subscribers(sheet)


if __name__ == "__main__":
    main()
