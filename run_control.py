"""Запуск сбора из дашборда: сводка «что в работе» и заранее видимое решение проверки допуска. Без Streamlit.

Решение о запуске по-прежнему принимает проверка допуска в GitHub Actions (db_runs.admit_parser_run): здесь
она только заранее показывается на экране, чтобы кнопка не нажималась впустую. Правила берутся из db_runs.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from typing import Dict, Iterable, Optional, Tuple

import db_runs
import dbutil
from dbutil import Connect

_WHAT = "Проверка возможности запуска"


class RunControlError(RuntimeError):
    """База недоступна или данные для проверки некорректны (без деталей драйвера)."""


@dataclass(frozen=True)
class Positions:
    total: int
    ours: int
    competitors: int
    by_market: Dict[str, int]


def positions_summary(pairs: Iterable[Tuple[str, str, str, bool]]) -> Positions:
    """Сколько ASIN уйдёт в каждый сбор. Как у парсера: ASIN запрашивается один раз, на рынке своей пары
    (при повторе в разных рынках побеждает более поздняя пара в порядке «рынок, наш ASIN»). «Наш» — ASIN,
    который хотя бы в одной активной паре стоит как наш; остальные — конкуренты, поэтому сумма всегда равна total."""
    active = sorted(((market, our, comp) for market, our, comp, is_active in pairs if is_active), key=lambda p: (p[0], p[1]))
    market_of: Dict[str, str] = {}
    for market, our, comp in active:
        market_of[our] = market
        market_of[comp] = market
    ours = {our for _, our, _ in active}
    return Positions(
        total=len(market_of),
        ours=len(ours),
        competitors=len(market_of.keys() - ours),
        by_market=dict(Counter(market_of.values())),
    )


def format_positions(positions: Positions) -> str:
    if not positions.total:
        return "Активных пар нет: собирать нечего."
    markets = " · ".join(f"{market} {count}" for market, count in sorted(positions.by_market.items(), key=lambda kv: (-kv[1], kv[0])))
    return f"В работе {positions.total} ASIN: наших {positions.ours} · конкурентов {positions.competitors} ({markets})"


def admission_preview(connect: Connect, now: datetime) -> Optional[str]:
    """Причина, по которой проверка допуска не пустит сбор прямо сейчас; None — пустит. Ничего не записывает."""
    if now.tzinfo is None:
        raise RunControlError("Некорректное время для проверки.")
    today = now.astimezone(db_runs.TZ).date()
    schedule_rows, unfinished_rows, count_rows = dbutil.run_many(
        connect,
        [
            ("SELECT hour, minute FROM bsr_radar.schedule WHERE id = 1;", (), True),
            ("SELECT EXISTS (SELECT 1 FROM bsr_radar.collection_runs WHERE status = 'running');", (), True),
            (
                "SELECT count(*), COALESCE(bool_or(status = 'done'), FALSE) FROM bsr_radar.collection_runs "
                "WHERE step = 'parser' AND (started_at AT TIME ZONE 'Europe/Kyiv')::date = %s;",
                (today,),
                True,
            ),
        ],
        error=RunControlError,
        what=_WHAT,
    )
    schedule = (schedule_rows[0][0], schedule_rows[0][1]) if schedule_rows else None
    attempts, successful = count_rows[0]
    try:
        return db_runs.admission_block_reason(
            now=now, schedule=schedule, attempts_today=attempts, successful_today=bool(successful),
            unfinished=bool(unfinished_rows[0][0]), force=False,
        )
    except db_runs.RunStoreError as exc:
        raise RunControlError(str(exc)) from None
