"""
Обёртка для ручного запуска с дашборда: сначала прогоняет парсер
(parser_not_test.py, пишет в Google Таблицу как обычно), затем — синк в базу
(sync_sheets_to_db.py), чтобы дашборд на Postgres сразу увидел свежие данные.

Статус пишется в run_status.json, полный лог — в last_run.log. Дашборд
опрашивает run_status.json, чтобы показать "идёт сбор" / "готово" / "ошибка".

Запускается как отдельный процесс (см. кнопку в dashboard_db.py), а не
импортируется — так падение парсера не может уронить сам дашборд.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent
STATUS_FILE = PROJECT_DIR / "run_status.json"
LOG_FILE = PROJECT_DIR / "last_run.log"


def _write_status(**fields) -> None:
    data = {}
    if STATUS_FILE.exists():
        try:
            data = json.loads(STATUS_FILE.read_text(encoding="utf-8"))
        except Exception:
            data = {}
    data.update(fields)
    STATUS_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _run_step(name: str, args: list[str]) -> int:
    with open(LOG_FILE, "a", encoding="utf-8") as log:
        log.write(f"\n=== {name}: старт {datetime.now().isoformat()} ===\n")
        log.flush()
        result = subprocess.run(args, cwd=PROJECT_DIR, stdout=log, stderr=subprocess.STDOUT)
        log.write(f"=== {name}: код завершения {result.returncode} ===\n")
    return result.returncode


def main() -> None:
    python = sys.executable
    _write_status(
        state="running",
        step="parser",
        started_at=datetime.now().isoformat(),
        finished_at=None,
        pid=os.getpid(),
        error=None,
    )

    parser_code = _run_step("parser", [python, str(PROJECT_DIR / "parser_not_test.py")])
    if parser_code != 0:
        _write_status(
            state="error", step="parser",
            finished_at=datetime.now().isoformat(),
            error=f"parser_not_test.py завершился с кодом {parser_code} — см. last_run.log",
        )
        return

    _write_status(state="running", step="sync")
    sync_code = _run_step("sync", [python, str(PROJECT_DIR / "sync_sheets_to_db.py")])
    if sync_code != 0:
        _write_status(
            state="error", step="sync",
            finished_at=datetime.now().isoformat(),
            error=f"sync_sheets_to_db.py завершился с кодом {sync_code} — см. last_run.log",
        )
        return

    _write_status(state="done", step="sync", finished_at=datetime.now().isoformat(), error=None)


if __name__ == "__main__":
    main()
