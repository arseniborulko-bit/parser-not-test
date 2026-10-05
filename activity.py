"""Действия в дашборде по сессиям: сколько времени проводят и какие разделы открывают. Без Streamlit.

Таблица bsr_radar.session_events (миграция 014). Одна строка — одно действие на странице; section
заполнен, когда открыт раздел. Время сессии — от первого до последнего действия: открыл и ушёл —
0 секунд, работал полчаса — полчаса. Если человек просто читает, ничего не нажимая, это время
не видно — так задумано, считаются действия, а не открытая вкладка.
Запись никогда не мешает работе: нет таблицы или база недоступна — record() возвращает False.
"""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Iterable, List, Optional

import pandas as pd

import dbutil
from dbutil import Connect

log = logging.getLogger(__name__)

# Страница, оставленная открытой на ночь: действие утром — уже новая сессия, а не «работал 14 часов».
IDLE_GAP = timedelta(minutes=30)
SHORT_SESSION = timedelta(minutes=1)
_MAX_TEXT = 120


class ActivityError(RuntimeError):
    """Журнал действий недоступен (без деталей драйвера)."""


def _run(connect: Connect, sql: str, params: tuple = (), *, fetch: bool = False):
    return dbutil.run_sql(connect, sql, params, fetch=fetch, error=ActivityError, what="Операция с журналом действий")


def _clean(value: object) -> Optional[str]:
    if not isinstance(value, str):
        return None
    text = " ".join(value.split())[:_MAX_TEXT]
    return text or None


def table_exists(connect: Connect) -> bool:
    return bool(_run(connect, "SELECT to_regclass('bsr_radar.session_events') IS NOT NULL;", fetch=True)[0][0])


def record(connect: Connect, session_id: object, email: object, section: object = None) -> bool:
    """Одно действие. True — записано; False — не записано (причина только в логе сервера)."""
    session, who = _clean(session_id), _clean(email)
    if session is None or who is None:
        return False
    try:
        _run(
            connect,
            "INSERT INTO bsr_radar.session_events (session_id, email, section) VALUES (%s, %s, %s);",
            (session, who, _clean(section)),
        )
    except ActivityError as exc:
        log.info("Журнал действий: действие %s не записано — %s", who, exc)
        return False
    return True


def recent(connect: Connect, days: int = 30, limit: int = 50000) -> List[dict]:
    rows = _run(
        connect,
        "SELECT session_id, email, at, section FROM bsr_radar.session_events "
        "WHERE at >= now() - make_interval(days => %s) ORDER BY at DESC, id DESC LIMIT %s;",
        (int(days), int(limit)),
        fetch=True,
    )
    return [{"session_id": s, "email": e, "at": at, "section": sec} for s, e, at, sec in rows]


def sessions(rows: Iterable[dict]) -> pd.DataFrame:
    """Сессии: email, start, end, seconds, actions. Одна открытая страница с перерывом дольше
    IDLE_GAP делится на несколько сессий."""
    events = pd.DataFrame(list(rows), columns=["session_id", "email", "at", "section"])
    if events.empty:
        return pd.DataFrame(columns=["email", "start", "end", "seconds", "actions"])
    events["at"] = pd.to_datetime(events["at"], utc=True)
    events = events.sort_values(["session_id", "at"], ignore_index=True)
    new_part = events.groupby("session_id")["at"].diff().gt(IDLE_GAP)
    events["part"] = new_part.groupby(events["session_id"]).cumsum()
    result = events.groupby(["session_id", "part"]).agg(
        email=("email", "first"), start=("at", "min"), end=("at", "max"), actions=("at", "count"),
    ).reset_index(drop=True)
    result["seconds"] = (result["end"] - result["start"]).dt.total_seconds().astype(int)
    return result[["email", "start", "end", "seconds", "actions"]]


def time_by_employee(session_table: pd.DataFrame) -> pd.DataFrame:
    """По сотруднику: sessions, avg/longest/total (секунды), short — сессий короче минуты."""
    if session_table.empty:
        return pd.DataFrame(columns=["email", "sessions", "avg", "longest", "total", "short"])
    grouped = session_table.groupby("email")["seconds"]
    result = pd.DataFrame({
        "sessions": grouped.count(),
        "avg": grouped.mean().round().astype(int),
        "longest": grouped.max(),
        "total": grouped.sum(),
        "short": grouped.apply(lambda s: int((s < SHORT_SESSION.total_seconds()).sum())),
    }).reset_index()
    return result.sort_values(["total", "email"], ascending=[False, True], ignore_index=True)


def sections(rows: Iterable[dict], all_sections: Iterable[str]) -> pd.DataFrame:
    """По разделу: opens — сколько раз открывали, employees — сколько разных людей, share — в скольких
    процентах страниц (сессий) его открывали хоть раз. Неоткрытые разделы тоже в списке, с нулями:
    по ним и видно, что можно убрать."""
    events = pd.DataFrame(list(rows), columns=["session_id", "email", "at", "section"])
    total_sessions = events["session_id"].nunique()
    opened = events.dropna(subset=["section"])
    stats = opened.groupby("section").agg(
        opens=("section", "count"), employees=("email", "nunique"), pages=("session_id", "nunique"),
    )
    order = list(dict.fromkeys([*all_sections, *stats.index]))
    result = stats.reindex(order, fill_value=0).rename_axis("section").reset_index()
    result["share"] = (result["pages"] / total_sessions * 100).round().astype(int) if total_sessions else 0
    result = result.drop(columns="pages")
    return result.sort_values("opens", ascending=False, kind="stable", ignore_index=True)


def format_duration(seconds: object) -> str:
    """45 сек · 12 мин · 1 ч 05 мин."""
    total = int(seconds) if pd.notna(seconds) else 0
    if total < 60:
        return f"{total} сек"
    minutes = total // 60
    if minutes < 60:
        return f"{minutes} мин"
    return f"{minutes // 60} ч {minutes % 60:02d} мин"
