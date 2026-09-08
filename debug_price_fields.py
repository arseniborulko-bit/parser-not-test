"""
Диагностика: почему price (our_price / comp_price) почти всегда пустой.

Берёт несколько ASIN (свои + конкурентов, желательно с разных маркетплейсов —
US и хотя бы один EU) и делает по ним РЕАЛЬНЫЙ запрос к ScrapingDog, показывая
сырой JSON целиком по каждому. Не пишет ничего в Google Sheets, не трогает
Current/History — только читает и печатает/сохраняет.

Зачем: в scraping.py сейчас цена берётся так:

    price = clean_number(
        data.get("price")
        or data.get("product_price")
        or data.get("discounted_price")
        or data.get("previous_price")
    )

Судя по официальной документации ScrapingDog, реально существует только поле
"price" (и "list_price" отдельно) — остальные три ключа ("product_price",
"discounted_price", "previous_price") в примерах API не встречаются вообще.
Если у вашего реального товара цена всё равно есть на странице Amazon, но
"price" в JSON пуст/None — значит, у этого ASIN просто нет единого Buy Box
(например, "Смотреть все варianты покупки"), и Amazon для таких товаров сам
не отдаёт единую цену на странице — тогда нужно доставать её по-другому
(например, из списка предложений). Этот скрипт покажет, какой именно случай
у вас на практике.

Запуск:
    python debug_price_fields.py B0XXXXXXXX B0YYYYYYYY --domain com
    python debug_price_fields.py B0XXXXXXXX --domain de

Если не передать ASIN явно — возьмёт первые несколько ASIN из Competitors
(через load_active_competitor_pairs), по одному с разных маркетплейсов.
"""

import argparse
import json
import sys
from pathlib import Path

from config import KEY_FILE_CANDIDATES, get_scrapingdog_token
from scraping import fetch_product
from sheets import _amazon_domain, connect_sheet, load_active_competitor_pairs
from utils import find_key_file


def pick_sample_asins(spreadsheet, limit_per_marketplace=1):
    """Берёт по 1 паре (наш+конкурент) с каждого встретившегося маркетплейса из Competitors."""
    pairs = load_active_competitor_pairs(spreadsheet)
    seen_marketplaces = set()
    samples = []
    for pair in pairs:
        mp = pair["marketplace"] or "US"
        if mp in seen_marketplaces:
            continue
        seen_marketplaces.add(mp)
        samples.append((pair["our_asin"], mp, "our_asin"))
        samples.append((pair["comp_asin"], mp, "comp_asin"))
    return samples


def dump_asin(asin, domain, token, label=""):
    print(f"\n{'=' * 70}")
    print(f"ASIN: {asin}  domain: {domain}  {label}")
    print("=" * 70)
    data = fetch_product(asin, domain=domain, token=token)
    if data is None:
        print("  -> Запрос не удался (None). Смотрите лог выше на код ошибки.")
        return None

    price_related_keys = [k for k in data.keys() if "price" in k.lower()]
    print(f"Ключи верхнего уровня, содержащие 'price': {price_related_keys or '(нет ни одного!)'}")
    for k in price_related_keys:
        print(f"  data[{k!r}] = {data[k]!r}")

    print(f"\ndata['price'] напрямую = {data.get('price')!r}")
    print(f"data.get('buybox') = {data.get('buybox')!r}" if "buybox" in data else "(поля 'buybox' нет)")
    print(f"data.get('other_sellers') есть: {'other_sellers' in data}, "
          f"length={len(data.get('other_sellers') or [])}")

    out_path = Path(__file__).parent / f"raw_{asin}_{domain}.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print(f"\nПолный сырой JSON сохранён в: {out_path}")
    return data


def main():
    parser = argparse.ArgumentParser(description="Дамп сырого JSON ScrapingDog для диагностики отсутствующей цены")
    parser.add_argument("asins", nargs="*", help="ASIN(ы) для проверки")
    parser.add_argument("--domain", default=None, help="Домен amazon (com, de, fr, es, it, co.uk...). "
                                                          "Если не задан и ASIN берутся из Competitors — определится сам по маркетплейсу.")
    args = parser.parse_args()

    token = get_scrapingdog_token()

    if args.asins:
        for asin in args.asins:
            domain = args.domain or "com"
            dump_asin(asin, domain, token)
        return

    base_dir = Path(__file__).parent
    key_file = find_key_file(base_dir)
    if not key_file:
        print(f"Не найден файл ключа Google (искал: {KEY_FILE_CANDIDATES}), и ASIN не переданы явно.")
        print("Запустите так: python debug_price_fields.py B0XXXXXXXX --domain com")
        sys.exit(1)

    sheet = connect_sheet(key_file)
    samples = pick_sample_asins(sheet.spreadsheet)
    if not samples:
        print("Не нашёл ни одной валидной пары в Competitors для автоматической выборки.")
        print("Запустите с явными ASIN: python debug_price_fields.py B0XXXXXXXX --domain com")
        sys.exit(1)

    print(f"Автоматически выбрано {len(samples)} ASIN (по 1 паре на маркетплейс) для проверки.")
    for asin, marketplace, role in samples:
        domain = _amazon_domain(marketplace)
        dump_asin(asin, domain, token, label=f"marketplace={marketplace} role={role}")


if __name__ == "__main__":
    main()
