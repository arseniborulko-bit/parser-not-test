"""
Разовая/ручная синхронизация подписчиков Telegram — без запуска всего парсера.

Полезно сразу после того, как кто-то написал боту /start: запустите этот
скрипт, чтобы сразу подтянуть его в лист "Подписчики" и получить
приветственное сообщение, не дожидаясь следующего планового прогона парсера.

Запуск:
    python sync_telegram_subscribers.py
"""

import sys
from pathlib import Path

from config import KEY_FILE_CANDIDATES, SHEET_NAME, TELEGRAM_BOT_TOKEN
from subscribers import get_active_subscriber_ids, sync_subscribers_from_telegram
from utils import find_key_file


def main():
    if not TELEGRAM_BOT_TOKEN:
        print("Не найден TELEGRAM_BOT_TOKEN в .env — нечего синхронизировать.")
        sys.exit(1)

    import gspread

    base_dir = Path(__file__).parent
    key_file = find_key_file(base_dir)
    if not key_file:
        print(f"Не найден файл ключа Google service account (искал: {KEY_FILE_CANDIDATES}). Прерываю.")
        sys.exit(1)

    gc = gspread.service_account(filename=key_file)
    spreadsheet = gc.open(SHEET_NAME)

    added = sync_subscribers_from_telegram(spreadsheet, TELEGRAM_BOT_TOKEN)
    print(f"Добавлено новых подписчиков: {added}")

    all_ids = get_active_subscriber_ids(spreadsheet)
    print(f"Всего активных подписчиков сейчас: {len(all_ids)}")
    for chat_id in all_ids:
        print(f"  - {chat_id}")


if __name__ == "__main__":
    main()
