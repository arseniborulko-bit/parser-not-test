"""
Работа с ежедневной задачей "AmazonParserDaily" в Планировщике заданий Windows.

Логика перенесена из sync_schedule_trigger.py (которая брала время из листа
Config в Google Sheets) — здесь она переиспользуется дашбордом, который берёт
время из базы (bsr_radar.schedule) и сразу перепрограммирует задачу,
без отдельного опроса.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

from utils import logger

TASK_NAME = "AmazonParserDaily"
_TIME_RE = re.compile(r"(\d{1,2}):(\d{2})")


def _normalize_hhmm(value: str) -> str:
    """'9:00:00 AM' / '14:00:00' / '9:00' -> '09:00'; '' если не распозналось."""
    match = _TIME_RE.search(value)
    if not match:
        return ""
    return f"{int(match.group(1)):02d}:{match.group(2)}"


def task_exists(task_name: str = TASK_NAME) -> bool:
    result = subprocess.run(
        ["schtasks", "/query", "/tn", task_name],
        capture_output=True, text=True,
    )
    return result.returncode == 0


def get_task_current_time(task_name: str = TASK_NAME) -> str:
    """Возвращает текущее время старта задачи как 'HH:MM' (с ведущим нулём), или '' если не удалось узнать."""
    result = subprocess.run(
        ["schtasks", "/query", "/tn", task_name, "/fo", "LIST", "/v"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        return ""
    for line in result.stdout.splitlines():
        if line.strip().lower().startswith("start time:"):
            return _normalize_hhmm(line.split(":", 1)[1].strip())
    return ""


def sync_task(hour: int, minute: int, python_exe: str, script_path: str) -> tuple[bool, str]:
    """Создаёт или обновляет задачу так, чтобы она стартовала в hour:minute. Возвращает (успех, сообщение)."""
    time_str = f"{hour:02d}:{minute:02d}"
    task_command = f'"{python_exe}" "{script_path}"'

    if not task_exists(TASK_NAME):
        result = subprocess.run(
            [
                "schtasks", "/create", "/tn", TASK_NAME,
                "/tr", task_command,
                "/sc", "daily",
                "/st", time_str,
                "/f",
            ],
            capture_output=True, text=True,
        )
        if result.returncode == 0:
            msg = f"Задача '{TASK_NAME}' создана: ежедневный запуск в {time_str}."
            logger.info(msg)
            return True, msg
        msg = f"Не удалось создать задачу '{TASK_NAME}': {result.stderr.strip()}"
        logger.error(msg)
        return False, msg

    # Всегда переприменяем и время, и команду запуска (schtasks /change идемпотентен) —
    # так дрейф команды (например, если раньше задача указывала на другой скрипт)
    # чинится сам, а не только несовпадение времени.
    result = subprocess.run(
        ["schtasks", "/change", "/tn", TASK_NAME, "/st", time_str, "/tr", task_command],
        capture_output=True, text=True,
    )
    if result.returncode == 0:
        msg = f"Задача '{TASK_NAME}' обновлена: запуск в {time_str}, команда '{task_command}'."
        logger.info(msg)
        return True, msg
    msg = f"Не удалось обновить задачу '{TASK_NAME}': {result.stderr.strip()}"
    logger.error(msg)
    return False, msg


def default_python_and_script() -> tuple[str, str]:
    """Парсер + синк в базу одним запуском — чтобы дашборд (читает из Postgres)
    всегда видел свежие данные и после автоматического, и после ручного запуска."""
    base_dir = Path(__file__).resolve().parent
    python_exe = base_dir / ".venv" / "Scripts" / "python.exe"
    script_path = base_dir / "run_parser_and_sync.py"
    return str(python_exe), str(script_path)
