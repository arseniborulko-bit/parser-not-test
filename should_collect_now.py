"""
Решает, пора ли запускать сбор в GitHub Actions прямо сейчас.

Используется вместе с cron-триггером "*/30 * * * *" (каждые 30 минут) в
collect.yml: сам воркфлоу срабатывает часто, но реальный сбор (расход
ScrapingDog) должен произойти только один раз в день, в заданное время.

Логика:
1. Читает время автозапуска (parser_not_test.schedule) — то же самое, что
   задаёт панель расписания в dashboard_db.py.
2. Если текущее время (Europe/Kyiv) ещё раньше заданного — пропуск.
3. Если сегодня уже был успешный сбор (parser_not_test.collection_runs,
   step='parser', status='done') — пропуск, чтобы не собирать дважды.
4. Иначе — разрешает запуск.

Результат пишется в $GITHUB_OUTPUT как should_run=true/false, чтобы
следующие шаги workflow могли себя обусловить (if: steps.gate.outputs.should_run == 'true').
"""

from __future__ import annotations

import os
from datetime import datetime
from zoneinfo import ZoneInfo

import psycopg2
from dotenv import load_dotenv

load_dotenv()

TZ = ZoneInfo("Europe/Kyiv")


def _set_output(should_run: bool, reason: str) -> None:
    print(reason)
    output_file = os.environ.get("GITHUB_OUTPUT")
    if output_file:
        with open(output_file, "a", encoding="utf-8") as f:
            f.write(f"should_run={'true' if should_run else 'false'}\n")


def main() -> None:
    conn = psycopg2.connect(os.environ["DATABASE_URL"])
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT hour, minute FROM parser_not_test.schedule WHERE id = 1;")
            row = cur.fetchone()
    finally:
        conn.close()

    if not row:
        _set_output(False, "Расписание в базе не задано — пропуск.")
        return

    hour, minute = row
    now = datetime.now(TZ)
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)

    if now < target:
        _set_output(False, f"Ещё не время (сейчас {now:%H:%M}, цель {hour:02d}:{minute:02d} Europe/Kyiv) — пропуск.")
        return

    conn = psycopg2.connect(os.environ["DATABASE_URL"])
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT started_at FROM parser_not_test.collection_runs
                WHERE step = 'parser' AND status = 'done'
                ORDER BY started_at DESC LIMIT 1;
                """
            )
            last = cur.fetchone()
    finally:
        conn.close()

    if last and last[0].astimezone(TZ).date() == now.date():
        _set_output(False, "Сегодня уже был успешный сбор — пропуск.")
        return

    _set_output(True, f"Пора собирать (цель {hour:02d}:{minute:02d}, сейчас {now:%H:%M} Europe/Kyiv) — запускаю.")


if __name__ == "__main__":
    main()
