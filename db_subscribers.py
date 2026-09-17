"""
Чтение подписчиков Telegram напрямую из Postgres (parser_not_test.telegram_subscribers)
как альтернатива чтению листа "Подписчики" в Google Sheets.

Используется парсером только когда задана переменная окружения
SUBSCRIBER_SOURCE=database (по умолчанию — Sheets, поведение не меняется).
Новые подписчики по-прежнему добавляются через sync_subscribers_from_telegram()
в лист Sheets - здесь только чтение готового списка для рассылки.
"""

from __future__ import annotations

import os
from typing import Dict, List

import psycopg2


def _connect():
    return psycopg2.connect(os.environ["DATABASE_URL"])


def load_subscribers_from_db() -> List[Dict[str, str]]:
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT telegram_id, username, first_name, subscribed_at, active
                FROM parser_not_test.telegram_subscribers
                ORDER BY telegram_id
                """
            )
            rows = cur.fetchall()
    finally:
        conn.close()

    return [
        {
            "telegram_id": telegram_id,
            "username": username or "",
            "first_name": first_name or "",
            "subscribed_at": subscribed_at.isoformat() if subscribed_at else "",
            "active": active,
        }
        for telegram_id, username, first_name, subscribed_at, active in rows
    ]


def get_active_subscriber_ids_from_db() -> List[str]:
    return [row["telegram_id"] for row in load_subscribers_from_db() if row["active"]]
