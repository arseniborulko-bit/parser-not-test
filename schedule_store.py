"""Расписание автосбора. Без Streamlit — чтобы правила проверялись тестами.

Время хранится в bsr_radar.schedule, до двух независимых строк: id=1 и id=2 — два слота
времени в сутки (владелец, 29.09.2026: второй слот запускает настоящий второй сбор, а не
запасное время). Нет строки — соответствующий слот выключен: проверка допуска в GitHub Actions
(db_runs.admission_block_reason) считает не «было/не было», а сколько успехов против скольких
наступивших слотов, так что второй наступивший слот отпирает второй реальный сбор.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import List, Optional, Tuple
from zoneinfo import ZoneInfo

import access
import dbutil
from dbutil import Connect

TZ = ZoneInfo("Europe/Kyiv")
SLOT_MINUTES = 30
_WHAT = "Операция с расписанием"

log = logging.getLogger(__name__)


class ScheduleStoreError(RuntimeError):
    """База недоступна или запись не подтверждена (без деталей драйвера)."""


@dataclass(frozen=True)
class Schedule:
    hour: int
    minute: int


@dataclass(frozen=True)
class NextRun:
    when: datetime
    due_now: bool


@dataclass(frozen=True)
class Overview:
    schedule: Optional[Schedule]
    collected_today: bool
    runs: List[dict]
    schedule2: Optional[Schedule] = None
    # Сколько раз сегодня уже был успешный сбор (scope='all') — нужно отдельно от collected_today,
    # чтобы понять, наступил ли второй слот, а не только первый.
    collected_count: int = 0


def to_slot(hour: int, minute: int) -> Tuple[int, int]:
    """Проверка в GitHub Actions идёт раз в 30 минут — точнее выбирать время бессмысленно."""
    return hour, (minute // SLOT_MINUTES) * SLOT_MINUTES


def _valid_time(hour: object, minute: object) -> Tuple[int, int]:
    for part, upper in ((hour, 23), (minute, 59)):
        if type(part) is not int or not 0 <= part <= upper:
            raise ValueError("Некорректное время.")
    return hour, minute


def next_run(now: datetime, schedule: Optional[Schedule], collected_today: bool) -> Optional[NextRun]:
    if schedule is None:
        return None
    local = now.astimezone(TZ)
    target = local.replace(hour=schedule.hour, minute=schedule.minute, second=0, microsecond=0)
    if collected_today:
        return NextRun(target + timedelta(days=1), False)
    return NextRun(target, local >= target)


def load_overview(connect: Connect, now: datetime) -> Overview:
    today = now.astimezone(TZ).date()
    schedule_rows, schedule2_rows, collected_rows, run_rows = dbutil.run_many(
        connect,
        [
            ("SELECT hour, minute FROM bsr_radar.schedule WHERE id = 1;", (), True),
            ("SELECT hour, minute FROM bsr_radar.schedule WHERE id = 2;", (), True),
            (
                """
                SELECT count(*) FROM bsr_radar.collection_runs
                WHERE step = 'parser' AND status = 'done'
                  AND (started_at AT TIME ZONE 'Europe/Kyiv')::date = %s;
                """,
                (today,),
                True,
            ),
            (
                """
                SELECT started_at, finished_at, status, error
                FROM bsr_radar.collection_runs
                WHERE step = 'parser'
                ORDER BY started_at DESC
                LIMIT 5;
                """,
                (),
                True,
            ),
        ],
        error=ScheduleStoreError,
        what=_WHAT,
    )
    schedule = Schedule(schedule_rows[0][0], schedule_rows[0][1]) if schedule_rows else None
    schedule2 = Schedule(schedule2_rows[0][0], schedule2_rows[0][1]) if schedule2_rows else None
    collected_count = collected_rows[0][0] if collected_rows else 0
    runs = [
        {"started_at": started, "finished_at": finished, "status": status, "error": error}
        for started, finished, status, error in run_rows
    ]
    return Overview(schedule, collected_count > 0, runs, schedule2, collected_count)


def save_schedule(connect: Connect, hour: int, minute: int, enabled: bool, *,
                  slot: int = 1, actor_role: Optional[str], actor: str) -> None:
    access.require_role(actor_role, access.ROLE_EDITOR)
    if slot not in (1, 2):
        raise ValueError("Некорректный слот расписания.")
    hour, minute = _valid_time(hour, minute)
    if enabled:
        dbutil.run_sql(
            connect,
            """
            INSERT INTO bsr_radar.schedule (id, hour, minute, updated_at)
            VALUES (%s, %s, %s, now())
            ON CONFLICT (id) DO UPDATE SET hour = EXCLUDED.hour, minute = EXCLUDED.minute, updated_at = now();
            """,
            (slot, hour, minute),
            error=ScheduleStoreError,
            what=_WHAT,
        )
        log.info("Расписание изменено (%s): автосбор включён (слот %d), %02d:%02d Киев", actor, slot, hour, minute)
    else:
        dbutil.run_sql(
            connect, "DELETE FROM bsr_radar.schedule WHERE id = %s;", (slot,),
            error=ScheduleStoreError, what=_WHAT,
        )
        log.info("Расписание изменено (%s): автосбор выключен (слот %d)", actor, slot)
