import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger("amazon_parser")

SCRAPINGDOG_TOKEN = os.environ.get("SCRAPINGDOG_TOKEN", "")

SHEET_NAME = "BSR_Competitors_Tracker"
# Ссылка на саму Google Таблицу (без привязки к конкретной вкладке/gid) —
# используется в Telegram-отчёте о битых ASIN, чтобы сразу дать ссылку на
# таблицу для замены.
BROKEN_ASINS_SHEET_URL = "https://docs.google.com/spreadsheets/d/117dnxsBT7H-XOv-y3SvzXYzGS2ZlNXZSQ42JgAjzBNQ/edit"
KEY_FILE_CANDIDATES = ("arseniy-sheets-key.json", "arseniy-sheets-key.json.json")
CONFIG_SHEET_NAME = "Матрица"
CONFIG_HEADER = ["ASIN конкурента"]
MATRIX_SHEET_NAME = "Матрица"
COMPETITOR_ASIN_HEADERS = ("asin конкурента", "competitor asin", "asin competitor", "comp_asin")

FALLBACK_ASINS: List[str] = []

DOMAIN = "com"
REGION = "Америка" if DOMAIN in ("com", "us", "ca", "mx") else "Европа"

REQUEST_TIMEOUT = 15
MAX_RETRIES = 3
RETRY_DELAY_SEC = 2
MAX_CONCURRENT_WORKERS = 5
BATCH_SIZE = 10

ASIN_PATTERN = re.compile(r"\b(B0[A-Z0-9]{8})\b", re.IGNORECASE)



def get_scrapingdog_token() -> str:
    """Возвращает токен ScrapingDog или поднимает понятную ошибку во время исполнения."""
    token = os.environ.get("SCRAPINGDOG_TOKEN") or SCRAPINGDOG_TOKEN
    if not token:
        raise RuntimeError(
            "Не найден SCRAPINGDOG_TOKEN. Добавьте SCRAPINGDOG_TOKEN в .env файл."
        )
    return token


def resolve_asins_from_environment(fallback_asins: List[str]) -> List[str]:
    """Возвращает один или несколько ASIN из переменной окружения, если она задана."""
    override = os.environ.get("TEST_ASIN") or os.environ.get("SINGLE_ASIN") or os.environ.get("ASIN")
    if not override:
        return list(fallback_asins)

    asins = []
    for raw_value in str(override).split(","):
        value = raw_value.strip()
        if not value:
            continue
        match = ASIN_PATTERN.search(value)
        if match:
            asins.append(match.group(1).upper())
    return asins or list(fallback_asins)


TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
TELEGRAM_ENABLED = bool(TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID)


@dataclass
class AppConfig:
    scrapingdog_token: str = field(default_factory=lambda: os.environ.get("SCRAPINGDOG_TOKEN", ""))
    sheet_name: str = SHEET_NAME
    config_sheet_name: str = CONFIG_SHEET_NAME
    matrix_sheet_name: str = MATRIX_SHEET_NAME
    domain: str = DOMAIN
    max_workers: int = MAX_CONCURRENT_WORKERS
    telegram_enabled: bool = TELEGRAM_ENABLED