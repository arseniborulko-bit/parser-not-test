"""
Синхронизация Google Sheets -> Postgres (schema bsr_radar).

Читает листы Competitors / Current / History из таблицы BSR_Competitors_Tracker
(только на чтение, ничего в Google Sheets не меняет) и заливает данные в
таблицы bsr_radar.competitor_pairs и bsr_radar.snapshots через
upsert (ON CONFLICT DO UPDATE), так что повторный запуск безопасен и не
создаёт дублей.

Google Таблица остаётся источником истины и продолжает наполняться парсером
как раньше — этот скрипт её не заменяет, а копирует данные в базу для
будущего SaaS-дашборда.

Исключение — пары: при PAIR_SOURCE=database их ведёт дашборд, база — источник
истины, и лист Competitors в базу больше не копируется (иначе он затирал бы
правки из дашборда).
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
        # Та же семантика, что sheets.load_active_competitor_pairs() (её реально
        # использует парсер через load_asins_from_config): пустая ячейка Active
        # считается НЕактивной, а не активной по умолчанию.
        active_raw = _cell(raw, headers.get("active")).upper()
        active = active_raw in {"Y", "YES", "1", "TRUE"}
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
            INSERT INTO bsr_radar.competitor_pairs
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

    # Строку, которую целиком удалили из Sheets (не Active=N, а просто убрали),
    # upsert выше не видит и не трогает — база тихо расходится с таблицей.
    # Помечаем такие пары неактивными явно.
    seen_keys = set(deduped.keys())
    with conn.cursor() as cur:
        cur.execute("SELECT marketplace, our_asin, comp_asin FROM bsr_radar.competitor_pairs WHERE active = TRUE;")
        stale_keys = {tuple(row) for row in cur.fetchall()} - seen_keys

    if stale_keys:
        with conn.cursor() as cur:
            execute_values(
                cur,
                """
                UPDATE bsr_radar.competitor_pairs AS t
                SET active = FALSE
                FROM (VALUES %s) AS s(marketplace, our_asin, comp_asin)
                WHERE t.marketplace = s.marketplace
                  AND t.our_asin = s.our_asin
                  AND t.comp_asin = s.comp_asin
                """,
                list(stale_keys),
                page_size=1000,
            )
        conn.commit()
        logger.info(f"Деактивировано пар, пропавших из Sheets: {len(stale_keys)}")

    return len(rows)


def _snapshots_has_image_columns(conn) -> bool:
    """Миграция 004 (our_image_url/comp_image_url) могла ещё не применяться к боевой базе — тогда
    синк пишет снимки без фото, а не падает целиком (иначе одна новая колонка сломала бы весь сбор)."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM information_schema.columns WHERE table_schema = 'bsr_radar' "
            "AND table_name = 'snapshots' AND column_name IN ('our_image_url', 'comp_image_url');"
        )
        return cur.fetchone()[0] == 2


def _snapshots_key_has_marketplace(conn) -> bool:
    """Миграция 006 (страна в ключе снимков) могла ещё не применяться к боевой базе. Тогда ключ
    старый — (дата, наш ASIN, ASIN конкурента) — и ON CONFLICT обязан называть именно его, иначе
    Postgres откажет ("no unique or exclusion constraint matching"). Так синк работает в любом
    порядке выкатки: и когда код опубликован раньше миграции, и когда позже."""
    with conn.cursor() as cur:
        cur.execute("""
            SELECT EXISTS (
                SELECT 1 FROM pg_constraint
                WHERE conrelid = 'bsr_radar.snapshots'::regclass AND contype = 'u'
                  AND (SELECT count(*) FROM unnest(conkey) AS k
                       JOIN pg_attribute a ON a.attrelid = conrelid AND a.attnum = k
                       WHERE a.attname = 'marketplace') = 1
            );
        """)
        return cur.fetchone()[0]


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

    has_images = _snapshots_has_image_columns(conn)
    # Ключ дедупликации обязан совпадать с ключом в базе. Если развести строки по стране, пока в
    # базе ключ без страны, то в одном execute_values окажутся две строки с одинаковым ключом и
    # Postgres откажет: "ON CONFLICT DO UPDATE command cannot affect row a second time".
    key_has_marketplace = _snapshots_key_has_marketplace(conn)
    deduped: Dict[Tuple[str, ...], Tuple] = {}
    for raw in values[header_row_idx + 1:]:
        snapshot_date = _cell(raw, headers.get("snapshot_date"))
        our_asin = extract_asin_from_text(_cell(raw, headers.get("our_asin")))
        comp_asin = extract_asin_from_text(_cell(raw, headers.get("comp_asin")))
        if not snapshot_date or not our_asin or not comp_asin:
            continue

        competitor_col = headers.get("competitor")
        if competitor_col is None:
            competitor_col = headers.get("competitor name")

        marketplace = _cell(raw, headers.get("marketplace")).upper() or "US"
        key = ((snapshot_date, marketplace, our_asin, comp_asin) if key_has_marketplace
               else (snapshot_date, our_asin, comp_asin))
        deduped[key] = (
            snapshot_date,
            marketplace,
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
        ) + ((
            _cell(raw, headers.get("our_image_url")) or None,
            _cell(raw, headers.get("comp_image_url")) or None,
        ) if has_images else ())

    rows = list(deduped.values())
    if not rows:
        return 0

    image_columns = ", our_image_url, comp_image_url" if has_images else ""
    image_updates = "our_image_url = EXCLUDED.our_image_url, comp_image_url = EXCLUDED.comp_image_url," if has_images else ""
    conflict_target = ("(snapshot_date, marketplace, our_asin, comp_asin)" if key_has_marketplace
                       else "(snapshot_date, our_asin, comp_asin)")
    with conn.cursor() as cur:
        execute_values(
            cur,
            f"""
            INSERT INTO bsr_radar.snapshots
                (snapshot_date, marketplace, currency, our_asin, our_product, our_price,
                 our_bsr, our_bsr_delta_24h, comp_asin, competitor_name, comp_price,
                 comp_bsr, comp_bsr_delta_24h, comp_stock, price_diff_pct{image_columns})
            VALUES %s
            ON CONFLICT {conflict_target} DO UPDATE SET
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
                {image_updates}
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


def sync_subscribers(spreadsheet: gspread.Spreadsheet, conn) -> int:
    """Зеркалит лист 'Подписчики' в bsr_radar.telegram_subscribers.
    Новые подписчики по-прежнему пишутся ботом только в Sheets - здесь только чтение."""
    try:
        ws = spreadsheet.worksheet("Подписчики")
    except gspread.WorksheetNotFound:
        logger.warning("Лист 'Подписчики' не найден — пропускаю.")
        return 0

    values = ws.get_all_values()
    if len(values) < 2:
        return 0

    headers = _header_index(values[0])
    id_col = headers.get("telegram_id", 0)

    deduped: Dict[str, Tuple] = {}
    for raw in values[1:]:
        telegram_id = _cell(raw, id_col)
        if not telegram_id:
            continue
        active_cell = _cell(raw, headers.get("active")).upper()
        active = active_cell != "FALSE"
        deduped[telegram_id] = (
            telegram_id,
            _cell(raw, headers.get("username")) or None,
            _cell(raw, headers.get("first_name")) or None,
            _cell(raw, headers.get("subscribed_at")) or None,
            active,
        )

    rows = list(deduped.values())
    if not rows:
        return 0

    with conn.cursor() as cur:
        execute_values(
            cur,
            """
            INSERT INTO bsr_radar.telegram_subscribers
                (telegram_id, username, first_name, subscribed_at, active)
            VALUES %s
            ON CONFLICT (telegram_id) DO UPDATE SET
                username = EXCLUDED.username,
                first_name = EXCLUDED.first_name,
                subscribed_at = EXCLUDED.subscribed_at,
                active = EXCLUDED.active
            """,
            rows,
            page_size=1000,
        )
    conn.commit()
    return len(rows)


def main() -> None:
    spreadsheet = connect_spreadsheet()
    conn = connect_db()
    try:
        if os.environ.get("PAIR_SOURCE", "sheets").strip().lower() == "database":
            print("competitor_pairs: пропущено — PAIR_SOURCE=database, пары ведутся в базе (дашборд), лист Competitors не читается")
        else:
            pairs_count = sync_competitor_pairs(spreadsheet, conn)
            print(f"competitor_pairs: обработано строк {pairs_count}")

        history_count = sync_snapshots(spreadsheet, conn, "History")
        print(f"snapshots (History): обработано строк {history_count}")

        current_count = sync_snapshots(spreadsheet, conn, "Current")
        print(f"snapshots (Current): обработано строк {current_count}")

        subscribers_count = sync_subscribers(spreadsheet, conn)
        print(f"telegram_subscribers: обработано строк {subscribers_count}")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
