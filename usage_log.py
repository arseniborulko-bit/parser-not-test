"""Журнал использования инструмента: кто, когда, какое действие и какой объём работы. Без Streamlit.

Таблица public.usage_logs общая для всех инструментов команды (у каждого своё tool_name), поэтому она
лежит вне схемы bsr_radar. Запись никогда не мешает самому действию: нет таблицы, нет прав, база
недоступна или человек не назван — record() возвращает False, а действие считается выполненным.
"""

from __future__ import annotations

import logging
from typing import Dict, Iterable, List, Optional

import dbutil
from dbutil import Connect

log = logging.getLogger(__name__)

TOOL_NAME = "Competitor BSR"
_MAX_TEXT = 120


class UsageLogError(RuntimeError):
    """Журнал использования недоступен (без деталей драйвера)."""


def _run(connect: Connect, sql: str, params: tuple = (), *, fetch: bool = False):
    return dbutil.run_sql(connect, sql, params, fetch=fetch, error=UsageLogError, what="Операция с журналом использования")


def _clean(value: object) -> Optional[str]:
    if not isinstance(value, str):
        return None
    text = " ".join(value.split())[:_MAX_TEXT]
    return text or None


def _clean_volume(value: object) -> Optional[int]:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def record(connect: Connect, user: object, action: object, volume: object = None, unit: object = None) -> bool:
    """Одна строка журнала. True — записано; False — не записано (причина только в логе сервера)."""
    user_name, action_name = _clean(user), _clean(action)
    if user_name is None or action_name is None:
        return False
    amount = _clean_volume(volume)
    try:
        _run(
            connect,
            "INSERT INTO public.usage_logs (user_name, tool_name, action, volume, unit) VALUES (%s, %s, %s, %s, %s);",
            (user_name, TOOL_NAME, action_name, amount, _clean(unit) if amount is not None else None),
        )
    except UsageLogError as exc:
        log.info("Журнал использования: действие «%s» (%s) не записано — %s", action_name, user_name, exc)
        return False
    return True


def log_exists(connect: Connect) -> bool:
    """Журнал необязателен: пока таблицы нет (миграция 012 не применена), действия работают без записи."""
    return bool(_run(connect, "SELECT to_regclass('public.usage_logs') IS NOT NULL;", fetch=True)[0][0])


def recent(connect: Connect, days: int = 30, limit: int = 2000) -> List[dict]:
    """Последние строки этого инструмента за days суток, новые сверху."""
    rows = _run(
        connect,
        "SELECT created_at, user_name, action, volume, unit FROM public.usage_logs "
        "WHERE tool_name = %s AND created_at >= now() - make_interval(days => %s) "
        "ORDER BY created_at DESC, id DESC LIMIT %s;",
        (TOOL_NAME, int(days), int(limit)),
        fetch=True,
    )
    return [
        {"created_at": created_at, "user_name": user_name, "action": action, "volume": volume, "unit": unit}
        for created_at, user_name, action, volume, unit in rows
    ]


def summarize(rows: Iterable[dict]) -> List[dict]:
    """Сводка по людям: сколько действий, в какие дни и когда в последний раз. Самые активные сверху."""
    by_user: Dict[str, dict] = {}
    for row in rows:
        entry = by_user.setdefault(row["user_name"], {
            "user_name": row["user_name"], "actions": 0, "days": set(), "last_at": row["created_at"],
        })
        entry["actions"] += 1
        entry["days"].add(row["created_at"].date())
        entry["last_at"] = max(entry["last_at"], row["created_at"])
    summary = [{**entry, "days": len(entry["days"])} for entry in by_user.values()]
    return sorted(summary, key=lambda entry: (-entry["actions"], entry["user_name"]))
