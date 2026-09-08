"""
Лист "Подписчики" в Google Таблице — список Telegram ID всех, кто написал
боту (например, отправил /start), чтобы рассылать отчёты не в один
фиксированный TELEGRAM_CHAT_ID, а всем, кто подписался через бота.

Как это работает:
1. Пользователь находит вашего бота в Telegram (по ссылке t.me/<bot_username>)
   и отправляет ему любое сообщение, например /start.
2. При каждом запуске парсера sync_subscribers_from_telegram() вызывает
   Telegram getUpdates и добавляет НОВЫЕ chat_id в лист "Подписчики" (если их
   там ещё нет), и сразу шлёт этому человеку короткое приветствие — чтобы он
   увидел, что подписка сработала.
3. get_active_subscriber_ids() отдаёт список ID для рассылки отчётов
   (parser_not_test.py использует его вместо одного TELEGRAM_CHAT_ID).

Чтобы не обрабатывать одни и те же сообщения повторно при каждом запуске,
номер последнего обработанного update_id хранится в локальном файле
telegram_update_offset.txt (по тому же принципу, что last_run_date.txt в
run_if_scheduled.py).

Отписаться человек может, поставив "FALSE" в колонку "active" в самой
таблице — тогда рассылка ему больше не идёт, но запись в таблице остаётся
(история, что он был подписан).

Ручная проверка/синхронизация без запуска всего парсера:
    python sync_telegram_subscribers.py
"""

from datetime import datetime
from pathlib import Path
from typing import List

import gspread
import requests

from config import REQUEST_TIMEOUT
from utils import logger

SUBSCRIBERS_SHEET_NAME = "Подписчики"
SUBSCRIBERS_HEADERS = ["telegram_id", "username", "first_name", "subscribed_at", "active"]

OFFSET_FILE = Path(__file__).parent / "telegram_update_offset.txt"

WELCOME_TEXT = (
    "👋 Готово! Вы подписаны на отчёты по мониторингу товаров.\n"
    "Сюда будут приходить ежедневные отчёты и уведомления о битых ASIN.\n\n"
    "Чтобы отписаться — просто скажите об этом в чате с таблицей, "
    "или попросите поставить FALSE напротив вашего ID в листе «Подписчики»."
)


def _read_offset() -> int:
    if OFFSET_FILE.exists():
        try:
            return int(OFFSET_FILE.read_text(encoding="utf-8").strip() or 0)
        except ValueError:
            return 0
    return 0


def _write_offset(update_id: int) -> None:
    OFFSET_FILE.write_text(str(update_id), encoding="utf-8")


def get_bot_username(bot_token: str) -> str:
    """Возвращает username бота (без @) через Telegram getMe, или '' при ошибке."""
    if not bot_token:
        return ""
    try:
        response = requests.get(f"https://api.telegram.org/bot{bot_token}/getMe", timeout=REQUEST_TIMEOUT)
        response.raise_for_status()
        data = response.json()
        if data.get("ok"):
            return data.get("result", {}).get("username", "") or ""
    except requests.RequestException as exc:
        logger.warning(f"Не удалось получить username бота через getMe: {exc}")
    return ""


def ensure_subscribers_sheet(spreadsheet: gspread.Spreadsheet, bot_token: str = "") -> gspread.Worksheet:
    """
    Возвращает лист 'Подписчики', создаёт его с заголовками, если его ещё нет.

    Если передан bot_token и лист создаётся впервые — рядом с заголовками
    (в колонке G) сразу пишется понятная инструкция со ссылкой на бота, чтобы
    любой, у кого есть доступ к таблице, сам понял, как подписаться на
    Telegram-отчёты, не спрашивая вас лично.
    """
    try:
        worksheet = spreadsheet.worksheet(SUBSCRIBERS_SHEET_NAME)
        _repair_misplaced_subscriber_rows(worksheet)
        return worksheet
    except gspread.WorksheetNotFound:
        worksheet = spreadsheet.add_worksheet(
            title=SUBSCRIBERS_SHEET_NAME, rows=200, cols=max(len(SUBSCRIBERS_HEADERS), 8)
        )
        worksheet.append_row(SUBSCRIBERS_HEADERS)
        logger.info(f"Создан лист '{SUBSCRIBERS_SHEET_NAME}' с заголовками {SUBSCRIBERS_HEADERS}.")

        username = get_bot_username(bot_token) if bot_token else ""
        if username:
            instructions = [
                ["Как подписаться на отчёты в Telegram:"],
                [f"1. Откройте t.me/{username}"],
                ["2. Напишите боту любое сообщение, например /start"],
                ["3. Ваш Telegram появится в этом списке при следующей синхронизации"],
                ["Чтобы отписаться — поставьте FALSE в колонке active напротив своей строки."],
            ]
            worksheet.update("G1", instructions, value_input_option="USER_ENTERED")
        return worksheet


def _repair_misplaced_subscriber_rows(worksheet: gspread.Worksheet) -> None:
    """Переносит записи, ошибочно добавленные после инструкции в колонке G.

    Ранние версии использовали ``append_rows`` без ``table_range``. Если на
    листе уже была инструкция в G, API добавлял строки начиная с G, поэтому
    рассылка не могла увидеть ``telegram_id`` в колонке A.
    """
    rows = worksheet.get_all_values()
    repairs = []
    clear_ranges = []
    for row_number, row in enumerate(rows[1:], start=2):
        # Ошибочная запись имеет [telegram_id..active] в колонках G:K,
        # тогда как A:E у этой строки пусты.
        if len(row) < 11 or any(cell.strip() for cell in row[:5]):
            continue
        candidate = row[6:11]
        if candidate[0].strip().lstrip("-").isdigit() and candidate[4].strip().upper() in {"TRUE", "FALSE"}:
            repairs.append({"range": f"A{row_number}:E{row_number}", "values": [candidate]})
            clear_ranges.append(f"G{row_number}:K{row_number}")

    if not repairs:
        return

    worksheet.batch_update(repairs, value_input_option="USER_ENTERED")
    worksheet.batch_clear(clear_ranges)
    logger.warning(
        "Исправлено строк подписчиков, записанных в G:K вместо A:E: %s",
        len(repairs),
    )


def sync_subscribers_from_telegram(
    spreadsheet: gspread.Spreadsheet,
    bot_token: str,
    send_welcome: bool = True,
    poll_timeout: int = 0,
) -> int:
    """
    Опрашивает Telegram (getUpdates) на новые сообщения от пользователей и
    добавляет НОВЫЕ chat_id в лист "Подписчики", если их там ещё нет.

    ``poll_timeout`` включает long polling Telegram. Для обычного запуска
    парсера оставляйте значение 0; постоянный процесс бота передаёт 25 секунд.

    Возвращает количество добавленных новых подписчиков.
    """
    if not bot_token:
        return 0

    worksheet = ensure_subscribers_sheet(spreadsheet, bot_token=bot_token)
    existing_rows = worksheet.get_all_values()[1:]  # без заголовка
    known_ids = {row[0].strip() for row in existing_rows if row and row[0].strip()}

    offset = _read_offset()
    try:
        response = requests.get(
            f"https://api.telegram.org/bot{bot_token}/getUpdates",
            params={"offset": offset + 1, "timeout": max(0, poll_timeout)},
            # Long polling ждёт на стороне Telegram, поэтому сетевой timeout
            # должен быть немного больше запрошенного времени ожидания.
            timeout=max(REQUEST_TIMEOUT, poll_timeout + 10),
        )
        response.raise_for_status()
        data = response.json()
    except requests.RequestException as exc:
        logger.warning(f"Не удалось получить обновления Telegram для синхронизации подписчиков: {exc}")
        return 0

    if not data.get("ok"):
        logger.warning(f"Telegram getUpdates вернул ошибку: {data}")
        return 0

    updates = data.get("result", [])
    if not updates:
        return 0

    new_rows = []
    max_update_id = offset
    for update in updates:
        max_update_id = max(max_update_id, update.get("update_id", 0))
        message = update.get("message") or {}
        chat = message.get("chat") or {}
        chat_id = str(chat.get("id") or "").strip()
        if not chat_id or chat_id in known_ids:
            continue
        known_ids.add(chat_id)
        username = chat.get("username", "") or ""
        first_name = chat.get("first_name", "") or ""
        new_rows.append([chat_id, username, first_name, datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "TRUE"])

    if new_rows:
        # Явно фиксируем таблицу в A:E: на листе есть справка в G, и без
        # table_range Google Sheets начинает добавлять записи после неё.
        worksheet.append_rows(new_rows, value_input_option="USER_ENTERED", table_range="A:E")
        logger.info(f"Добавлено {len(new_rows)} новых подписчиков в '{SUBSCRIBERS_SHEET_NAME}'.")

        if send_welcome:
            from telegram import send_telegram_message  # локальный импорт — избегаем цикличного импорта

            for row in new_rows:
                send_telegram_message(WELCOME_TEXT, token=bot_token, chat_id=row[0])

    _write_offset(max_update_id)
    return len(new_rows)


def get_active_subscriber_ids(spreadsheet: gspread.Spreadsheet) -> List[str]:
    """Возвращает telegram_id всех подписчиков, у кого active != FALSE."""
    worksheet = ensure_subscribers_sheet(spreadsheet)
    rows = worksheet.get_all_values()
    if len(rows) < 2:
        return []

    headers = [h.strip().casefold() for h in rows[0]]
    id_col = headers.index("telegram_id") if "telegram_id" in headers else 0
    active_col = headers.index("active") if "active" in headers else None

    ids = []
    for row in rows[1:]:
        if len(row) <= id_col or not row[id_col].strip():
            continue
        if active_col is not None and len(row) > active_col and row[active_col].strip().upper() == "FALSE":
            continue
        ids.append(row[id_col].strip())
    return ids
