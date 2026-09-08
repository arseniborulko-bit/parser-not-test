"""Постоянный обработчик подписок Telegram.

Запускайте этот файл один раз при старте Windows. Он получает /start и другие
сообщения через Telegram long polling, сразу добавляет новый chat_id в лист
«Подписчики» и отправляет приветствие. Парсер продолжает заниматься только
отчётами и не должен ждать следующего запуска для подписки.
"""

import sys
import time
from pathlib import Path

import gspread

from config import KEY_FILE_CANDIDATES, SHEET_NAME, TELEGRAM_BOT_TOKEN
from subscribers import sync_subscribers_from_telegram
from utils import find_key_file, logger

POLL_TIMEOUT_SECONDS = 25
RETRY_DELAY_SECONDS = 5


def open_spreadsheet():
    base_dir = Path(__file__).parent
    key_file = find_key_file(base_dir)
    if not key_file:
        raise RuntimeError(
            "Не найден файл ключа Google service account "
            f"(искал: {KEY_FILE_CANDIDATES})."
        )
    return gspread.service_account(filename=key_file).open(SHEET_NAME)


def main() -> None:
    if not TELEGRAM_BOT_TOKEN:
        raise RuntimeError("В .env не задан TELEGRAM_BOT_TOKEN.")

    spreadsheet = open_spreadsheet()
    logger.info("Обработчик Telegram-подписок запущен: ожидаю новые сообщения.")

    while True:
        try:
            sync_subscribers_from_telegram(
                spreadsheet,
                TELEGRAM_BOT_TOKEN,
                poll_timeout=POLL_TIMEOUT_SECONDS,
            )
        except KeyboardInterrupt:
            logger.info("Обработчик Telegram-подписок остановлен.")
            return
        except Exception as exc:  # сеть/Google могут временно быть недоступны
            logger.exception("Ошибка обработчика Telegram-подписок: %s", exc)
            time.sleep(RETRY_DELAY_SECONDS)
            try:
                spreadsheet = open_spreadsheet()
            except Exception as reconnect_exc:
                logger.warning("Не удалось переподключиться к Google Sheets: %s", reconnect_exc)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        logger.error("Не удалось запустить обработчик Telegram-подписок: %s", exc)
        sys.exit(1)
