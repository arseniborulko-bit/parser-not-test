"""
Синхронизация Google Sheets -> Postgres (schema parser_not_test).

Читает листы Competitors / Current / History из таблицы BSR_Competitors_Tracker
(только на чтение, ничего в Google Sheets не меняет) и заливает данные в
таблицы parser_not_test.competitor_pairs и parser_not_test.snapshots через
upsert (ON CONFLICT DO UPDATE), так что повторный запуск безопасен и не
создаёт дублей.

Google Таблица остаётся источником истины и продолжает наполняться парсером
как раньше — этот скрипт её не заменяет, а копирует данные в базу для
будущего SaaS-дашборда.
"""

from __future__ import annotations

import os
from typing import Dict, List, Optional, Tuple

import gspread
import psycopg2
from psycopg2.extras import execute_values
from google.oauth2.service_account import Credentials
from dotenv import load_dotenv

from config import KEY_FILE_CANDIDATES, SHEET_NAME
from utils import clean_number, extract_asin_from_text, logger

load_dotenv()

READONLY_SCOPES = (
    "https://www.googleapis.com/auth/spreadsheets.readonly",
    "https://www.googleapis.com/auth/drive.readonly",
)


def resolve_key_file() -> str:
    for candidate in KEY_FILE_CANDIDATES:
        if os.path.exists(candidate):
            return candidate
    raise RuntimeError(
        f"Не найден файл ключа Google service account (искал: {KEY_FILE_CANDIDATES})."
    )


def connect_spreadsheet() -> gspread.Spreadsheet:
    creds = Credentials.from_service_account_file(resolve_key_file(), scopes=READONLY_SCOPES)
    client = gspread.authorize(creds)
    return client.open(SHEET_NAME)


def connect_db():
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        raise RuntimeError("Не найден DATABASE_URL в .env.")
    return psycopg2.connect(database_url)


def _header_index(headers: List[str]) -> Dict[str, int]:
    return {h.strip().casefold(): i for i, h in enumerate(headers) if h.strip()}


def _cell(row: List[str], idx: Optional[int]) -> str:
    if idx is None or idx >= len(row):
        return ""
    return row[idx].strip()


def sync_competitor_pairs(spreadsheet: gspread.Spreadsheet, conn) -> int:
    try:
        ws = spreadsheet.worksheet("Competitors")
    except gspread.WorksheetNotFound:
        logger.warning("Лист 'Competitors' не найден — пропускаю.")
        return 0

    values = ws.get_all_values()
    if not values:
        return 0

    headers = _header_index(values[0])
    required = {"marketplace", "our asin", "competitor name", "competitor asin"}
    if not required.issubset(headers):
        logger.warning("В листе 'Competitors' нет ожидаемых колонок — пропускаю.")
        return 0

    deduped: Dict[Tuple[str, str, str], Tuple] = {}
    for raw in values[1:]:
        our_asin = extract_asin_from_text(_cell(raw, headers.get("our asin")))
        comp_asin = extract_asin_from_text(_cell(raw, headers.get("competitor asin")))
        if not our_asin or not comp_asin:
            continue
        marketplace = _cell(raw, headers.get("marketplace")).upper() or "US"
        active_raw = _cell(raw, headers.get("active")).upper()
        active = active_raw in {"Y", "YES", "1", "TRUE", ""}
        # Более поздняя строка с тем же ключом (marketplace, our_asin, comp_asin)
        # перезаписывает более раннюю — так же ведёт себя ON CONFLICT DO UPDATE.
        deduped[(marketplace, our_asin, comp_asin)] = (
            marketplace,
            our_asin,
            _cell(raw, headers.get("our product")),
            comp_asin,
            _cell(raw, headers.get("competitor name")),
            active,
        )

    rows = list(deduped.values())
    if not rows:
        return 0

    with conn.cursor() as cur:
        execute_values(
            cur,
            """
            INSERT INTO parser_not_test.competitor_pairs
                (marketplace, our_asin, our_product, comp_asin, competitor_name, active)
            VALUES %s
            ON CONFLICT (marketplace, our_asin, comp_asin) DO UPDATE SET
                our_product = EXCLUDED.our_product,
                competitor_name = EXCLUDED.competitor_name,
                active = EXCLUDED.active
            """,
            rows,
            page_size=1000,
        )
    conn.commit()
    return len(rows)


def sync_snapshots(spreadsheet: gspread.Spreadsheet, conn, sheet_title: str) -> int:
    try:
        ws = spreadsheet.worksheet(sheet_title)
    except gspread.WorksheetNotFound:
        logger.warning(f"Лист '{sheet_title}' не найден — пропускаю.")
        return 0

    values = ws.get_all_values()
    if not values:
        return 0

    header_row_idx = 1 if values[0] and values[0][0].strip().casefold() == "current" else 0
    headers = _header_index(values[header_row_idx])
    required = {"snapshot_date", "our_asin", "comp_asin"}
    if not required.issubset(headers):
        logger.warning(f"В листе '{sheet_title}' нет ожидаемых колонок — пропускаю.")
        return 0

    deduped: Dict[Tuple[str, str, str], Tuple] = {}
    for raw in values[header_row_idx + 1:]:
        snapshot_date = _cell(raw, headers.get("snapshot_date"))
        our_asin = extract_asin_from_text(_cell(raw, headers.get("our_asin")))
        comp_asin = extract_asin_from_text(_cell(raw, headers.get("comp_asin")))
        if not snapshot_date or not our_asin or not comp_asin:
            continue

        competitor_col = headers.get("competitor")
        if competitor_col is None:
            competitor_col = headers.get("competitor name")

        deduped[(snapshot_date, our_asin, comp_asin)] = (
            snapshot_date,
            _cell(raw, headers.get("marketplace")).upper() or "US",
            _cell(raw, headers.get("currency")) or None,
            our_asin,
            _cell(raw, headers.get("our_product")) or None,
            clean_number(_cell(raw, headers.get("our_price"))),
            _to_int(_cell(raw, headers.get("our_bsr"))),
            clean_number(_cell(raw, headers.get("our_bsr_delta_24h"))),
            comp_asin,
            _cell(raw, competitor_col) or None,
            clean_number(_cell(raw, headers.get("comp_price"))),
            _to_int(_cell(raw, headers.get("comp_bsr"))),
            clean_number(_cell(raw, headers.get("comp_bsr_delta_24h"))),
            _cell(raw, headers.get("comp_stock")) or None,
            clean_number(_cell(raw, headers.get("price_diff_pct"))),
        )

    rows = list(deduped.values())
    if not rows:
        return 0

    with conn.cursor() as cur:
        execute_values(
            cur,
            """
            INSERT INTO parser_not_test.snapshots
                (snapshot_date, marketplace, currency, our_asin, our_product, our_price,
                 our_bsr, our_bsr_delta_24h, comp_asin, competitor_name, comp_price,
                 comp_bsr, comp_bsr_delta_24h, comp_stock, price_diff_pct)
            VALUES %s
            ON CONFLICT (snapshot_date, our_asin, comp_asin) DO UPDATE SET
                marketplace = EXCLUDED.marketplace,
                currency = EXCLUDED.currency,
                our_product = EXCLUDED.our_product,
                our_price = EXCLUDED.our_price,
                our_bsr = EXCLUDED.our_bsr,
                our_bsr_delta_24h = EXCLUDED.our_bsr_delta_24h,
                competitor_name = EXCLUDED.competitor_name,
                comp_price = EXCLUDED.comp_price,
                comp_bsr = EXCLUDED.comp_bsr,
                comp_bsr_delta_24h = EXCLUDED.comp_bsr_delta_24h,
                comp_stock = EXCLUDED.comp_stock,
                price_diff_pct = EXCLUDED.price_diff_pct,
                updated_at = now()
            """,
            rows,
            page_size=1000,
        )
    conn.commit()
    return len(rows)


def _to_int(value: str) -> Optional[int]:
    number = clean_number(value)
    return int(number) if number is not None else None


def main() -> None:
    spreadsheet = connect_spreadsheet()
    conn = connect_db()
    try:
        pairs_count = sync_competitor_pairs(spreadsheet, conn)
        print(f"competitor_pairs: обработано строк {pairs_count}")

        history_count = sync_snapshots(spreadsheet, conn, "History")
        print(f"snapshots (History): обработано строк {history_count}")

        current_count = sync_snapshots(spreadsheet, conn, "Current")
        print(f"snapshots (Current): обработано строк {current_count}")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
