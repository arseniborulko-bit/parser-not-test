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

from dotenv import load_dotenv

import db_runs

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


def main() -> int:
    try:
        invocation = db_runs.invocation_from_environment(os.environ)
        external_id = os.environ.get("COLLECTION_RUN_ID")
        if invocation.source == "github_actions":
            if not external_id or not external_id.isascii() or not external_id.isdecimal():
                raise db_runs.RunStoreError("Нет корректного COLLECTION_RUN_ID от gate; сбор запрещён.")
            parser_run_id = int(external_id)
        else:
            if external_id is not None:
                raise db_runs.RunStoreError("Локальный запуск не принимает чужой COLLECTION_RUN_ID.")
            decision = db_runs.admit_parser_run(invocation)
            if not decision.should_run:
                print(decision.reason)
                return 0
            parser_run_id = decision.run_id
        db_runs.claim_parser_run(parser_run_id, invocation)
    except db_runs.RunStoreError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    try:
        return _run_admitted(parser_run_id)
    except Exception as exc:
        # При неизвестном исходе не освобождаем попытку автоматически.
        # Сохранённая running блокирует повтор до проверки оператором.
        error = (str(exc) if isinstance(exc, db_runs.RunStoreError)
                 else f"Неожиданная ошибка выполнения ({type(exc).__name__}); проверьте историю запусков.")
        print(error, file=sys.stderr)
        try:
            _write_status(state="error", finished_at=datetime.now().isoformat(), error=error)
        except OSError:
            pass
        return 1


def _run_admitted(parser_run_id: int) -> int:
    python = sys.executable
    _write_status(
        state="running",
        step="parser",
        started_at=datetime.now().isoformat(),
        finished_at=None,
        pid=os.getpid(),
        error=None,
        run_id=parser_run_id,
    )

    parser_code = _run_step("parser", [python, str(PROJECT_DIR / "parser_not_test.py")])
    if parser_code != 0:
        # Синк после сбоя не запускаем: у пар, обработанных наполовину (только наш ASIN или только конкурент), в Current уже стоит сегодняшняя дата при пустых значениях второй стороны, и он записал бы неполную строку поверх хороших данных.
        error = f"parser_not_test.py завершился с кодом {parser_code} — см. last_run.log. Синхронизация с базой пропущена."
        db_runs.log_finish(parser_run_id, "error", error)
        _write_status(state="error", step="parser", finished_at=datetime.now().isoformat(), error=error)
        return 1

    # Запись sync создаём ДО закрытия parser: между этапами не должно быть окна без running.
    sync_run_id = db_runs.log_start("sync")
    db_runs.log_finish(parser_run_id, "done")
    _write_status(state="running", step="sync")
    sync_code = _run_step("sync", [python, str(PROJECT_DIR / "sync_sheets_to_db.py")])
    if sync_code != 0:
        error = f"sync_sheets_to_db.py завершился с кодом {sync_code} — см. last_run.log"
        db_runs.log_finish(sync_run_id, "error", error)
        _write_status(state="error", step="sync", finished_at=datetime.now().isoformat(), error=error)
        return 1

    db_runs.log_finish(sync_run_id, "done")
    _write_status(state="done", step="sync", finished_at=datetime.now().isoformat(), error=None)
    return 0


if __name__ == "__main__":
    load_dotenv()
    sys.exit(main())
