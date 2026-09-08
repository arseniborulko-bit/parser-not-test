"""
Аудит "Not Found": находит все ячейки с "Not Found" в листе Current, заново
запрашивает эти ASIN у ScrapingDog и парсит СВЕЖИМ (исправленным) кодом —
чтобы честно показать, что из старых "Not Found" теперь реально подтягивается,
а что действительно отсутствует у Amazon (это нормально, не баг).

ВАЖНО: каждый повторный запрос — это реальный запрос к ScrapingDog и списание
1 токена за ASIN. Используйте --limit, чтобы сначала проверить на небольшой
выборке, прежде чем гонять весь список.

Скрипт ничего не пишет в Google Sheets — только показывает отчёт. Чтобы
реально обновить таблицу новыми значениями, запустите обычный парсер
(python parser_not_test.py) — он использует тот же исправленный код и
перезапишет Current/History как обычно.

Запуск:
    python audit_not_found.py                 # проверить все ASIN с Not Found
    python audit_not_found.py --limit 10       # проверить только первые 10
"""

import argparse
import sys
from pathlib import Path

from config import KEY_FILE_CANDIDATES, SHEET_NAME, get_scrapingdog_token
from scraping import fetch_product, parse_product
from sheets import _amazon_domain
from utils import extract_asin_from_text, find_key_file, logger

# Какие колонки Current считаем "проверяемыми на Not Found" и какой ASIN/домен
# им соответствует (our_* -> our_asin, comp_* -> comp_asin).
FIELD_TO_ASIN_ROLE = {
    "our_bsr": "our_asin",
    "our_price": "our_asin",
    "comp_bsr": "comp_asin",
    "comp_price": "comp_asin",
}


def main():
    parser = argparse.ArgumentParser(description="Аудит Not Found в Current против свежего парсинга")
    parser.add_argument("--limit", type=int, default=None, help="Проверить только первые N ASIN (экономит токены)")
    args = parser.parse_args()

    import gspread

    base_dir = Path(__file__).parent
    key_file = find_key_file(base_dir)
    if not key_file:
        print(f"Не найден файл ключа Google service account (искал: {KEY_FILE_CANDIDATES}). Прерываю.")
        sys.exit(1)

    gc = gspread.service_account(filename=key_file)
    spreadsheet = gc.open(SHEET_NAME)

    try:
        current_sheet = spreadsheet.worksheet("Current")
    except gspread.WorksheetNotFound:
        print("Лист 'Current' не найден.")
        sys.exit(1)

    rows = current_sheet.get_all_values()
    if len(rows) < 2:
        print("Лист 'Current' пуст или без данных.")
        return

    # Заголовок Current может быть не в первой строке (см. dedupe_history.py) —
    # ищем строку, где есть snapshot_date и comp_asin.
    header_row_index = None
    headers = []
    for idx, row in enumerate(rows):
        normalized = [str(v).strip() for v in row]
        if "comp_asin" in normalized and ("marketplace" in normalized or "our_asin" in normalized):
            headers = normalized
            header_row_index = idx
            break

    if header_row_index is None:
        print("Не нашёл строку заголовков в Current (искал 'comp_asin' и 'marketplace'/'our_asin'). Прерываю.")
        sys.exit(1)

    columns = {h: i for i, h in enumerate(headers)}
    data_rows = rows[header_row_index + 1:]

    # Собираем уникальные (asin, domain), у которых хоть одно из полей = Not Found.
    needs_check = {}  # asin -> domain
    for row in data_rows:
        if not any(c.strip() for c in row):
            continue
        marketplace = row[columns["marketplace"]].strip().upper() if "marketplace" in columns and len(row) > columns["marketplace"] else ""
        domain = _amazon_domain(marketplace)

        for field, asin_role in FIELD_TO_ASIN_ROLE.items():
            if field not in columns or asin_role not in columns:
                continue
            field_idx = columns[field]
            asin_idx = columns[asin_role]
            if len(row) <= field_idx or len(row) <= asin_idx:
                continue
            if row[field_idx].strip() != "Not Found":
                continue
            asin = extract_asin_from_text(row[asin_idx])
            if asin:
                needs_check[asin] = domain

    asin_list = list(needs_check.items())
    print(f"Найдено уникальных ASIN с хотя бы одним 'Not Found': {len(asin_list)}")

    if not asin_list:
        print("Проверять нечего.")
        return

    if args.limit:
        asin_list = asin_list[: args.limit]
        print(f"Ограничиваю проверку до {len(asin_list)} ASIN (--limit).")

    print(f"\nБудет сделано {len(asin_list)} реальных запросов к ScrapingDog (1 токен за каждый).")
    confirm = input("Продолжить? [y/N]: ").strip().lower()
    if confirm != "y":
        print("Отменено.")
        return

    token = get_scrapingdog_token()

    fixed_count = 0
    still_missing_count = 0

    print(f"\n{'ASIN':<12} {'Домен':<6} {'BSR':<10} {'Цена':<10} {'Рейтинг':<8} {'Отзывы':<8} {'Наличие'}")
    print("-" * 70)

    for asin, domain in asin_list:
        data = fetch_product(asin, domain=domain, token=token)
        if data is None:
            print(f"{asin:<12} {domain:<6} — запрос не удался (сеть/ошибка API), пропуск")
            continue

        product = parse_product(data, asin=asin)

        fields = [product.bsr, product.price, product.stars, product.reviews]
        was_all_not_found_before = True  # мы уже знаем, что в Current было Not Found хотя бы по одному полю
        any_now_found = any(str(f) != "Not Found" for f in fields)

        if any_now_found:
            fixed_count += 1
        else:
            still_missing_count += 1

        print(
            f"{asin:<12} {domain:<6} {str(product.bsr):<10} {str(product.price):<10} "
            f"{str(product.stars):<8} {str(product.reviews):<8} {product.stock_status or '—'}"
        )

    print("\n--- Итог ---")
    print(f"У {fixed_count} из {len(asin_list)} ASIN сейчас нашлось хотя бы одно поле, которого не было раньше.")
    print(f"У {still_missing_count} ASIN всё так же ничего не нашлось — вероятно, данных реально нет у Amazon по этому товару.")
    print("\nЧтобы записать эти новые значения в Current/History — запустите обычный парсер:")
    print("    python parser_not_test.py")


if __name__ == "__main__":
    main()
