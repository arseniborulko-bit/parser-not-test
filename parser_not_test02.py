"""
Parser (Not Test) — сбор данных о товарах Amazon через ScrapingDog API
и запись/обновление данных в Google Sheets.

Отличия от тестовой версии (Parser02):
- вся логика разбита на функции + есть точка входа main()
- цена, рейтинг, отзывы и BSR парсятся безопасно (не падают на None / строках / мусоре)
- сетевые запросы обёрнуты в retry с паузой при временных сбоях API
- есть функция average_price() для сводки по прогону
"""

import os
import re
import json
import time
from pathlib import Path
from datetime import datetime

import requests
import gspread
from dotenv import load_dotenv

# ---------- Конфигурация ----------

load_dotenv()

SCRAPINGDOG_TOKEN = os.environ.get("SCRAPINGDOG_TOKEN")
if not SCRAPINGDOG_TOKEN:
    raise RuntimeError(
        "Не найден SCRAPINGDOG_TOKEN. Создайте файл .env рядом со скриптом "
        "и добавьте строку SCRAPINGDOG_TOKEN=ваш_ключ"
    )

SHEET_NAME = "Arseniy Sandbox"
KEY_FILE_CANDIDATES = ("arseniy-sheets-key.json", "arseniy-sheets-key.json.json")

CONFIG_SHEET_NAME = "Config"
CONFIG_HEADER = ["ASIN или ссылка на товар"]

# Запасной список — используется только если вкладка Config пустая
# или отсутствует (например, при самом первом запуске).
FALLBACK_ASINS = [
    
]

DOMAIN = "com"
REGION = "Америка" if DOMAIN in ("com", "us", "ca", "mx") else "Европа"

REQUEST_TIMEOUT = 15
MAX_RETRIES = 3
RETRY_DELAY_SEC = 2

# Регулярка для ASIN: ровно 10 символов, буквы (заглавные) и цифры,
# обычно начинается с "B0"
ASIN_PATTERN = re.compile(r"\b(B0[A-Z0-9]{8})\b", re.IGNORECASE)

# Telegram-уведомления — необязательны. Если переменные не заданы в .env,
# скрипт просто не отправляет сообщения и продолжает работать как раньше.
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
TELEGRAM_ENABLED = bool(TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID)


# ---------- Вспомогательные функции ----------

def find_key_file(base_dir: Path):
    """Ищет файл ключа Google service account в рабочей папке."""
    for candidate in KEY_FILE_CANDIDATES:
        p = base_dir / candidate
        if p.exists():
            return str(p)
    return None


def extract_asin_from_text(value):
    """
    Достаёт ASIN как из чистого текста ("B0B917WMSF"),
    так и из полной ссылки на товар
    ("https://www.amazon.com/dp/B0B917WMSF/ref=...", "...gp/product/B0B917WMSF...").
    Возвращает None, если ASIN не найден.
    """
    if not value:
        return None
    value = value.strip()
    if not value:
        return None

    match = ASIN_PATTERN.search(value)
    if match:
        return match.group(1).upper()
    return None


def load_asins_from_config(spreadsheet):
    """
    Читает список товаров со вкладки "Config" той же таблицы.
    Колонка A: ASIN или полная ссылка на товар (первая строка — заголовок, пропускается).
    Если вкладки нет — создаёт её с заголовком и запасным списком FALLBACK_ASINS.
    Если вкладка есть, но пустая — использует FALLBACK_ASINS, ничего не создавая.
    Дубликаты ASIN убираются, порядок сохраняется.
    """
    try:
        config_sheet = spreadsheet.worksheet(CONFIG_SHEET_NAME)
    except gspread.WorksheetNotFound:
        config_sheet = spreadsheet.add_worksheet(title=CONFIG_SHEET_NAME, rows=100, cols=2)
        config_sheet.append_row(CONFIG_HEADER)
        for asin in FALLBACK_ASINS:
            config_sheet.append_row([asin])
        print(f"Создана вкладка '{CONFIG_SHEET_NAME}' с запасным списком товаров.")
        return list(FALLBACK_ASINS)

    try:
        raw_values = config_sheet.col_values(1)
    except Exception as e:
        print(f"Не удалось прочитать вкладку '{CONFIG_SHEET_NAME}': {e}. Использую запасной список.")
        return list(FALLBACK_ASINS)

    if raw_values and raw_values[0].strip().lower() in ("asin", "asin или ссылка на товар", "asin/link"):
        raw_values = raw_values[1:]

    asins = []
    for raw in raw_values:
        asin = extract_asin_from_text(raw)
        if asin and asin not in asins:
            asins.append(asin)

    if not asins:
        print(f"Вкладка '{CONFIG_SHEET_NAME}' пустая или без валидных ASIN — использую запасной список.")
        return list(FALLBACK_ASINS)

    return asins


def connect_sheet(key_file: str):
    """Подключается к Google Sheets и возвращает объект листа."""
    gc = gspread.service_account(filename=key_file)
    return gc.open(SHEET_NAME).sheet1


def normalize_header(values):
    return [v.strip().lower() for v in values]


def clean_number(value):
    """
    Безопасно приводит значение к float.
    Возвращает None, если значение отсутствует или некорректно.
    Понимает числа, строки с запятыми/пробелами/валютой ("$19.99", "1 234,56").
    """
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        cleaned = re.sub(r"[^\d.,]", "", value)
        cleaned = cleaned.replace(",", "")
        if not cleaned:
            return None
        try:
            return float(cleaned)
        except ValueError:
            return None
    return None


def extract_bsr(best_sellers_rank):
    """Извлекает числовой BSR из строки вида '#1,234 in Category' или числа."""
    if isinstance(best_sellers_rank, str):
        match = re.search(r"(\d[\d,]*)", best_sellers_rank)
        if match:
            return match.group(1).replace(",", "")
        return ""
    if isinstance(best_sellers_rank, (int, float)):
        return str(int(best_sellers_rank))
    return ""


def extract_category(data):
    value = (
        data.get("category")
        or data.get("categories")
        or data.get("product_category")
        or data.get("product_categories")
    )
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, list):
        items = []
        for item in value:
            if isinstance(item, str) and item.strip():
                items.append(item.strip())
            elif isinstance(item, dict) and item.get("name"):
                items.append(str(item["name"]).strip())
        return " > ".join(items)
    return ""


def average_price(products):
    """
    Средняя цена по списку товаров.
    products — список чисел ИЛИ список словарей с ключом 'price'.
    Безопасно пропускает None / нечисловые значения.
    """
    prices = []
    for p in products:
        raw = p.get("price") if isinstance(p, dict) else p
        num = clean_number(raw)
        if num is not None:
            prices.append(num)
    return round(sum(prices) / len(prices), 2) if prices else 0.0


# ---------- Работа с Google Sheets ----------

def find_date_column(sheet, date_str):
    try:
        header = sheet.row_values(1)
    except Exception:
        header = []

    normalized = normalize_header(header)
    if not header:
        sheet.append_row(["Region", "Category", "Parent", "Parameter", date_str])
        return 5

    if len(normalized) < 4 or normalized[:4] != ["region", "category", "parent", "parameter"]:
        sheet.update('A1:D1', [["Region", "Category", "Parent", "Parameter"]])

    if date_str in normalized:
        return normalized.index(date_str) + 1

    new_col = len(header) + 1
    sheet.update_cell(1, new_col, date_str)
    return new_col


def find_asin_block_start(sheet, asin):
    try:
        parent_values = sheet.col_values(3)
    except Exception:
        parent_values = []
    if parent_values and parent_values[0].strip().lower() == "parent":
        parent_values = parent_values[1:]
        base_row = 2
    else:
        base_row = 1

    for idx, value in enumerate(parent_values):
        if value.strip() == asin:
            return base_row + idx
    return None


def build_parent_formula(asin, domain):
    domain_name = "www.amazon.com" if domain == "com" else f"www.amazon.{domain}"
    return f'=HYPERLINK("https://{domain_name}/dp/{asin}", "{asin}")'


def write_asin_block(sheet, start_row, date_col, region, category, parent_formula,
                      title, price, stars, bsr, reviews):
    if start_row is None:
        empty_cells = [""] * (date_col - 5)
        rows = [
            [region, category, parent_formula, "Title"] + empty_cells + [title],
            ["", "", "", "Price"] + empty_cells + [price],
            ["", "", "", "Rating"] + empty_cells + [stars],
            ["", "", "", "BSR"] + empty_cells + [bsr],
            ["", "", "", "Number of Reviews"] + empty_cells + [reviews],
        ]
        sheet.append_rows(rows, value_input_option='USER_ENTERED')
        return

    updates = [
        (start_row, date_col, title),
        (start_row + 1, date_col, price),
        (start_row + 2, date_col, stars),
        (start_row + 3, date_col, bsr),
        (start_row + 4, date_col, reviews),
    ]
    for row, col, value in updates:
        try:
            sheet.update_cell(row, col, value)
        except Exception as e:
            print(f"Ошибка обновления строки {row}, столбец {col}: {e}")


def apply_conditional_formatting(sheet, key_file):
    try:
        from googleapiclient.discovery import build
        from google.oauth2.service_account import Credentials
    except Exception:
        print("Условное форматирование требует google-api-python-client и google-auth. Пропускаю.")
        return

    try:
        creds = Credentials.from_service_account_file(
            key_file, scopes=["https://www.googleapis.com/auth/spreadsheets"]
        )
        service = build('sheets', 'v4', credentials=creds)
        spreadsheet_id = sheet.spreadsheet.id
        sheet_id = int(sheet._properties.get('sheetId'))
        row_count = int(sheet._properties.get('gridProperties', {}).get('rowCount', 1000))

        requests_body = {
            "requests": [
                # 1. Очистить старую заливку в колонке "Parent" (C)
                {
                    "repeatCell": {
                        "range": {
                            "sheetId": sheet_id, "startRowIndex": 1, "endRowIndex": row_count,
                            "startColumnIndex": 2, "endColumnIndex": 3
                        },
                        "cell": {"userEnteredFormat": {"backgroundColor": {"red": 1.0, "green": 1.0, "blue": 1.0}}},
                        "fields": "userEnteredFormat.backgroundColor"
                    }
                },
                # 2. Rating: красный / жёлтый / зелёный — на всю строку по датам
                {
                    "addConditionalFormatRule": {
                        "rule": {
                            "ranges": [{"sheetId": sheet_id, "startRowIndex": 1, "startColumnIndex": 4, "endColumnIndex": 30}],
                            "booleanRule": {
                                "condition": {"type": "CUSTOM_FORMULA", "values": [{"userEnteredValue": '=AND($D2="Rating", E2<=4.2)'}]},
                                "format": {"backgroundColor": {"red": 1.0, "green": 0.4, "blue": 0.4}}
                            }
                        },
                        "index": 0
                    }
                },
                {
                    "addConditionalFormatRule": {
                        "rule": {
                            "ranges": [{"sheetId": sheet_id, "startRowIndex": 1, "startColumnIndex": 4, "endColumnIndex": 30}],
                            "booleanRule": {
                                "condition": {"type": "CUSTOM_FORMULA", "values": [{"userEnteredValue": '=AND($D2="Rating", E2=4.3)'}]},
                                "format": {"backgroundColor": {"red": 1.0, "green": 0.95, "blue": 0.3}}
                            }
                        },
                        "index": 0
                    }
                },
                {
                    "addConditionalFormatRule": {
                        "rule": {
                            "ranges": [{"sheetId": sheet_id, "startRowIndex": 1, "startColumnIndex": 4, "endColumnIndex": 30}],
                            "booleanRule": {
                                "condition": {"type": "CUSTOM_FORMULA", "values": [{"userEnteredValue": '=AND($D2="Rating", E2>=4.4)'}]},
                                "format": {"backgroundColor": {"red": 0.3, "green": 0.9, "blue": 0.3}}
                            }
                        },
                        "index": 0
                    }
                },
                # 3. Автоширина колонок под контент
                {
                    "autoResizeDimensions": {
                        "dimensions": {"sheetId": sheet_id, "dimension": "COLUMNS", "startIndex": 0, "endIndex": 30}
                    }
                }
            ]
        }
        service.spreadsheets().batchUpdate(spreadsheetId=spreadsheet_id, body=requests_body).execute()
        print("Условное форматирование и авторасширение столбцов применены.")
    except Exception as e:
        print(f"Не удалось применить условное форматирование: {e}")


# ---------- Telegram-уведомления ----------

def send_telegram_message(text: str):
    """
    Отправляет сообщение в Telegram-чат через Bot API.
    Если TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID не заданы — ничего не делает.
    Не роняет скрипт при сбое отправки — только печатает предупреждение.
    """
    if not TELEGRAM_ENABLED:
        return

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    # Telegram ограничивает длину сообщения 4096 символами — режем на всякий случай
    text = text[:4000]

    try:
        response = requests.post(
            url,
            data={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            },
            timeout=REQUEST_TIMEOUT,
        )
        if response.status_code != 200:
            print(f"Telegram: ошибка отправки ({response.status_code}): {response.text}")
    except requests.RequestException as e:
        print(f"Telegram: не удалось отправить сообщение — {e}")


# ---------- Работа с API ----------

def fetch_product(asin: str, domain: str, token: str):
    """
    Делает запрос к ScrapingDog с повторными попытками при временных сбоях.
    Возвращает распарсенный JSON или None при неустранимой ошибке.
    """
    params = {"api_key": token, "asin": asin, "domain": domain}

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            response = requests.get(
                "https://api.scrapingdog.com/amazon/product",
                params=params,
                timeout=REQUEST_TIMEOUT,
            )
        except requests.RequestException as e:
            print(f"[{asin}] Попытка {attempt}/{MAX_RETRIES}: ошибка сети — {e}")
            time.sleep(RETRY_DELAY_SEC)
            continue

        if response.status_code == 200:
            try:
                return response.json()
            except ValueError:
                print(f"[{asin}] Сервер вернул невалидный JSON.")
                return None

        if response.status_code in (429, 500, 502, 503):
            print(f"[{asin}] Попытка {attempt}/{MAX_RETRIES}: сервер вернул {response.status_code}, повтор через {RETRY_DELAY_SEC}с")
            time.sleep(RETRY_DELAY_SEC)
            continue

        print(f"[{asin}] Ошибка {response.status_code}: {response.text}")
        return None

    print(f"[{asin}] Не удалось получить данные после {MAX_RETRIES} попыток.")
    return None


def parse_product(data: dict):
    """Достаёт нужные поля из ответа API безопасно (без падений на грязных данных)."""
    product_info = data.get("product_information", {}) or {}

    title = data.get("title") or ""
    stars = clean_number(data.get("average_rating"))
    reviews = clean_number(data.get("total_reviews"))
    price = clean_number(data.get("previous_price"))
    bsr = extract_bsr(product_info.get("Best Sellers Rank"))
    category = extract_category(data)

    return {
        "title": title,
        "stars": stars if stars is not None else "",
        "reviews": int(reviews) if reviews is not None else "",
        "price": price if price is not None else "",
        "bsr": bsr,
        "category": category,
    }


# ---------- main ----------

def main():
    base_dir = Path(__file__).parent
    key_file = find_key_file(base_dir)

    sheet = None
    date_col_idx = None
    asins = list(FALLBACK_ASINS)

    if key_file:
        sheet = connect_sheet(key_file)
        asins = load_asins_from_config(sheet.spreadsheet)
        date_col_label = datetime.now().strftime("%d.%m.%Y")
        date_col_idx = find_date_column(sheet, date_col_label)
        apply_conditional_formatting(sheet, key_file)
    else:
        print("Warning: Google Sheets key file не найден. Запись в таблицу отключена.")
        print("Использую запасной список товаров (FALLBACK_ASINS), т.к. вкладка Config недоступна.")

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
                    sheet, start_row, date_col_idx, REGION, parsed["category"],
                    parent_formula, parsed["title"], parsed["price"],
                    parsed["stars"], parsed["bsr"], parsed["reviews"],
                )
                print(f"Блок ASIN {asin} записан/обновлён.")
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
    # Этот файл использовал устаревший вертикальный формат листа
    # (Region/Category/Parent/Parameter). Для Матрицы конкурентов он
    # сдвигает поля по столбцам, поэтому запускаем актуальный сценарий.
    from parser_not_test import main as current_main

    current_main()
