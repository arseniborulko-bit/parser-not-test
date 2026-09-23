"""Допуск и история запусков. Ошибка учёта запрещает продолжение сбора."""

from __future__ import annotations

import os
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Mapping, Optional
from uuid import uuid4
from zoneinfo import ZoneInfo

import psycopg2


TZ = ZoneInfo("Europe/Kyiv")
DAILY_ATTEMPT_LIMIT = 3
# Один ключ для ВСЕХ путей допуска; транзакция заканчивается до парсинга.
ADMISSION_LOCK = (782143, 1)
# Обычный сбор занимает минуты, не часы. Попытка, которая провисела в статусе
# 'running' дольше этого срока, считается зависшей (раннер упал/убит, до
# log_finish дело не дошло) и не должна блокировать сбор до конца дня.
STALE_RUNNING_MINUTES = 180


class RunStoreError(RuntimeError):
    """Безопасное для логов сообщение, без DSN/текста исключения драйвера."""


SCOPES = ("all", "ours", "competitors")


@dataclass(frozen=True)
class Invocation:
    source: str
    owner_key: str
    force: bool = False
    scope: str = "all"


@dataclass(frozen=True)
class Admission:
    should_run: bool
    reason: str
    run_id: Optional[int] = None


def invocation_from_environment(environment: Mapping[str, str]) -> Invocation:
    raw_force = environment.get("FORCE_COLLECTION", "false").lower()
    if raw_force not in ("", "false", "true"):
        raise RunStoreError("Некорректный FORCE_COLLECTION; сбор запрещён.")
    force = raw_force == "true"
    scope = environment.get("COLLECT_SCOPE", "all").strip().lower() or "all"
    if scope not in SCOPES:
        raise RunStoreError("Некорректный COLLECT_SCOPE; сбор запрещён.")
    if environment.get("GITHUB_ACTIONS") == "true":
        event = environment.get("GITHUB_EVENT_NAME")
        repository = environment.get("GITHUB_REPOSITORY", "")
        run_id = environment.get("GITHUB_RUN_ID", "")
        attempt = environment.get("GITHUB_RUN_ATTEMPT", "")
        if (event not in ("schedule", "workflow_dispatch") or not repository
                or not run_id.isascii() or not run_id.isdecimal() or int(run_id) < 1
                or not attempt.isascii() or not attempt.isdecimal() or int(attempt) < 1):
            raise RunStoreError("Не удалось определить запуск GitHub Actions; сбор запрещён.")
        if force and event != "workflow_dispatch":
            raise RunStoreError("force разрешён только для явного ручного запуска GitHub Actions.")
        if scope != "all" and event != "workflow_dispatch":
            raise RunStoreError("Частичный сбор разрешён только для явного ручного запуска GitHub Actions.")
        return Invocation("github_actions", f"github:{repository}:{run_id}:{attempt}", force, scope)
    if force:
        raise RunStoreError("Локальный force не поддерживается; используйте ручной workflow.")
    return Invocation("local", f"local:{uuid4().hex}", scope=scope)


def admission_block_reason(*, now: datetime, schedule: Optional[tuple[int, int]],
                           attempts_today: int, successful_today: bool,
                           unfinished: bool, force: bool = False) -> Optional[str]:
    """Чистая политика допуска. force снимает ТОЛЬКО дневной лимит."""
    if now.tzinfo is None or now.utcoffset() is None or attempts_today < 0:
        raise RunStoreError("Некорректные данные для проверки допуска.")
    if unfinished:
        return "Есть незавершённая попытка (включая прошлые дни) — сбор заблокирован."
    if successful_today:
        return "Сегодня уже был успешный сбор — пропуск."
    if schedule is None:
        return "Расписание в базе не задано — пропуск."
    hour, minute = schedule
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise RunStoreError("Некорректное расписание; сбор запрещён.")
    local_now = now.astimezone(TZ)
    if (local_now.hour, local_now.minute) < (hour, minute):
        return "Время сбора по Europe/Kyiv ещё не наступило — пропуск."
    if attempts_today >= DAILY_ATTEMPT_LIMIT and not force:
        return f"Лимит {DAILY_ATTEMPT_LIMIT} попытки за день Europe/Kyiv исчерпан — пропуск."
    return None


@contextmanager
def _transaction():
    conn = None
    try:
        conn = psycopg2.connect(os.environ["DATABASE_URL"], connect_timeout=10)
        conn.set_session(isolation_level="READ COMMITTED", autocommit=False)
        with conn.cursor() as cur:
            cur.execute("SET LOCAL statement_timeout = '10s';")
            cur.execute("SET LOCAL lock_timeout = '5s';")
            yield cur
        conn.commit()
    except Exception as exc:
        if conn is not None:
            try:
                conn.rollback()
            except Exception:
                pass
        if isinstance(exc, RunStoreError):
            raise
        # Сообщение драйвера может содержать параметры подключения.
        raise RunStoreError(
            f"Операция учёта запусков не подтверждена ({type(exc).__name__}); сбор запрещён."
        ) from None
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def admit_parser_run(invocation: Invocation) -> Admission:
    """Блокировка -> свежие чтения -> регистрация -> COMMIT -> разрешение."""
    with _transaction() as cur:
        cur.execute("SELECT pg_advisory_xact_lock(%s, %s);", ADMISSION_LOCK)
        # Отдельные запросы ПОСЛЕ получения lock: новые снимки на READ COMMITTED.
        # now() был бы временем начала транзакции, ещё до ожидания блокировки.
        cur.execute("SELECT clock_timestamp();")
        now = cur.fetchone()[0]
        cur.execute("SELECT hour, minute FROM bsr_radar.schedule WHERE id = 1;")
        schedule = cur.fetchone()
        # Зависшие попытки (никто не вызвал log_finish, например раннер упал) не
        # должны блокировать сбор на весь день — списываем их как ошибку.
        stale_before = now - timedelta(minutes=STALE_RUNNING_MINUTES)
        cur.execute("""
            UPDATE bsr_radar.collection_runs
            SET status = 'error', error = 'Зависла в статусе running — списано автоматически',
                finished_at = clock_timestamp()
            WHERE status = 'running' AND started_at < %s;
        """, (stale_before,))
        cur.execute("""
            SELECT EXISTS (
                SELECT 1 FROM bsr_radar.collection_runs WHERE status = 'running'
            );
        """)
        unfinished = cur.fetchone()[0]
        cur.execute("""
            SELECT count(*), COALESCE(bool_or(status = 'done'), FALSE)
            FROM bsr_radar.collection_runs
            WHERE step = 'parser'
              AND (started_at AT TIME ZONE 'Europe/Kyiv')::date = %s
              AND scope = %s;
        """, (now.astimezone(TZ).date(), invocation.scope))
        attempts, successful = cur.fetchone()
        reason = admission_block_reason(
            now=now, schedule=schedule, attempts_today=attempts,
            successful_today=successful, unfinished=unfinished, force=invocation.force,
        )
        if reason:
            decision = Admission(False, reason)
        else:
            cur.execute("""
                INSERT INTO bsr_radar.collection_runs
                    (source, step, status, started_at, owner_key, scope)
                VALUES (%s, 'parser', 'running', %s, %s, %s) RETURNING id;
            """, (invocation.source, now, invocation.owner_key, invocation.scope))
            row = cur.fetchone()
            if not row:
                raise RunStoreError("Регистрация попытки не подтверждена; сбор запрещён.")
            _validate_run_id(row[0])
            decision = Admission(True, "Попытка зарегистрирована — сбор разрешён.", row[0])
    # Полученный RETURNING id ещё не является подтверждением: COMMIT уже прошёл.
    return decision


def _validate_run_id(run_id: int) -> None:
    if type(run_id) is not int or not 0 < run_id <= 9223372036854775807:
        raise RunStoreError("Некорректный ID попытки; сбор запрещён.")


def claim_parser_run(run_id: int, invocation: Invocation) -> None:
    """Однократное использование разрешения конкретным workflow/локальным вызовом."""
    _validate_run_id(run_id)
    with _transaction() as cur:
        cur.execute("""
            UPDATE bsr_radar.collection_runs SET claimed_at = clock_timestamp()
            WHERE id = %s AND step = 'parser' AND status = 'running'
              AND claimed_at IS NULL AND finished_at IS NULL
              AND source = %s AND owner_key = %s
            RETURNING id;
        """, (run_id, invocation.source, invocation.owner_key))
        if cur.fetchone() is None:
            raise RunStoreError("Разрешение отсутствует, чужое, завершено или уже использовано.")
    # Не проверяем календарный день: gate мог закончиться прямо перед полуночью.


def log_start(step: str) -> int:
    if step != "sync":
        raise RunStoreError("Для парсера обязательны допуск и однократное использование ID.")
    source = "github_actions" if os.environ.get("GITHUB_ACTIONS") == "true" else "local"
    with _transaction() as cur:
        cur.execute("""
            INSERT INTO bsr_radar.collection_runs (source, step, status)
            VALUES (%s, %s, 'running') RETURNING id;
        """, (source, step))
        row = cur.fetchone()
        if not row:
            raise RunStoreError("Регистрация синхронизации не подтверждена.")
        run_id = row[0]
        _validate_run_id(run_id)
    return run_id


def log_finish(run_id: int, status: str, error: Optional[str] = None) -> None:
    _validate_run_id(run_id)
    if status not in ("done", "error"):
        raise RunStoreError("Некорректный итоговый статус попытки.")
    with _transaction() as cur:
        cur.execute("""
            UPDATE bsr_radar.collection_runs
            SET status = %s, error = %s, finished_at = clock_timestamp()
            WHERE id = %s AND status = 'running'
              AND (step <> 'parser' OR claimed_at IS NOT NULL)
            RETURNING id;
        """, (status, error, run_id))
        if cur.fetchone() is None:
            raise RunStoreError("Завершение попытки не подтверждено; требуется проверка истории.")
