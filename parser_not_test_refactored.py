import os
from datetime import datetime
from pathlib import Path

from config import DOMAIN, FALLBACK_ASINS, REGION, SCRAPINGDOG_TOKEN, TELEGRAM_ENABLED, resolve_asins_from_environment
from scraping import fetch_product, parse_product
from sheets import (
    append_history_snapshot,
    apply_conditional_formatting,
    connect_sheet,
    find_asin_block_start,
    find_date_column,
    load_asins_from_config,
    write_asin_block,
)
from telegram import send_telegram_message
from utils import average_price, build_parent_formula, find_key_file


def main():
    base_dir = Path(__file__).parent
    key_file = find_key_file(base_dir)

    sheet = None
    date_col_idx = None
    asins = list(FALLBACK_ASINS)

    if key_file:
        sheet = connect_sheet(key_file)
        asins = resolve_asins_from_environment(load_asins_from_config(sheet.spreadsheet, FALLBACK_ASINS))
        if os.environ.get("TEST_ASIN") or os.environ.get("SINGLE_ASIN") or os.environ.get("ASIN"):
            print("Режим теста: используется один ASIN из переменной окружения.")
        date_col_label = "Current"
        date_col_idx = find_date_column(sheet, date_col_label)
        apply_conditional_formatting(sheet, key_file)
    else:
        print("Warning: Google Sheets key file не найден. Запись в таблицу отключена.")
        print("Использую запасной список товаров (FALLBACK_ASINS).")

    if TELEGRAM_ENABLED:
        send_telegram_message(f"🚀 Запуск парсера. Товаров к проверке: {len(asins)}")
    else:
        print("Telegram не настроен (нет TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID) — уведомления отключены.")

    collected_prices = []
    failed_asins = []

    for asin in asins:
        data = fetch_product(asin, DOMAIN, SCRAPINGDOG_TOKEN)
        if data is None:
            failed_asins.append(asin)
            send_telegram_message(f"⚠️ Не удалось получить данные по ASIN <b>{asin}</b>")
            continue

        parsed = parse_product(data)
        collected_prices.append(parsed["price"])

        print("ASIN:", asin)
        print("Title:", parsed["title"])
        print("Rating:", parsed["stars"], "stars,", parsed["reviews"], "reviews")
        print("BSR:", parsed["bsr"])
        print("Price:", parsed["price"])

        sheet_status = ""
        if sheet:
            try:
                parent_formula = build_parent_formula(asin, DOMAIN)
                start_row = find_asin_block_start(sheet, asin)
                write_asin_block(
                    sheet,
                    start_row,
                    date_col_idx,
                    REGION,
                    parsed["category"],
                    parent_formula,
                    parsed["title"],
                    parsed["price"],
                    parsed["stars"],
                    parsed["bsr"],
                    parsed["reviews"],
                )
                append_history_snapshot(
                    sheet,
                    datetime.now().strftime("%d.%m.%Y"),
                    asin,
                    parsed["title"],
                    parsed["price"],
                    parsed["stars"],
                    parsed["bsr"],
                    parsed["reviews"],
                )
                print(f"Блок ASIN {asin} записан/обновлён и добавлен в историю.")
                sheet_status = "✅ записано в таблицу"
            except Exception as e:
                print(f"Ошибка записи в Google Sheets для {asin}: {e}")
                sheet_status = f"⚠️ ошибка записи в таблицу: {e}"
        else:
            print("Google Sheets не настроен — строка не записана.")
            sheet_status = "ℹ️ таблица не настроена"

        print("-" * 40)

        title_short = (parsed["title"][:80] + "…") if len(parsed["title"]) > 80 else parsed["title"]
        send_telegram_message(
            f"📦 <b>{asin}</b>\n"
            f"{title_short}\n"
            f"Цена: {parsed['price']}\n"
            f"Рейтинг: {parsed['stars']} ⭐ ({parsed['reviews']} отзывов)\n"
            f"BSR: {parsed['bsr']}\n"
            f"{sheet_status}"
        )

    avg_price = average_price(collected_prices)
    print("Средняя цена по собранным товарам:", avg_price)

    summary_lines = [
        "✅ <b>Парсер завершил работу</b>",
        f"Собрано товаров: {len(collected_prices)} из {len(asins)}",
        f"Средняя цена: {avg_price}",
    ]
    if failed_asins:
        summary_lines.append(f"Не удалось получить: {', '.join(failed_asins)}")
    send_telegram_message("\n".join(summary_lines))


if __name__ == "__main__":
    # Старый формат записи несовместим с текущей строковой Матрицей.
    from parser_not_test import main as current_main

    current_main()
