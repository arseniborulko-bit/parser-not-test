"""
Синхронизирует ТОЧНОЕ время ежедневного запуска парсера (задача Планировщика
заданий Windows) с временем, заданным в Google Sheets (вкладка "Config",
KEY=SCHEDULE_TIME, Vol=время в формате "14-00"/"14:00"/"14.00").

В отличие от прошлого подхода (scheduler_run.py, который запускался каждые
N минут и САМ решал, не пора ли запускать парсер) — здесь парсер запускается
Планировщиком Windows НАПРЯМУЮ, ровно в заданное время, без единой лишней
проверки между запусками. Этот скрипт не запускает парсер сам — он только
один раз в день (или когда вы захотите) ПЕРЕПРОГРАММИРУЕТ время триггера
задачи "AmazonParserDaily" в самом Планировщике, если время в Config
изменилось. Сам парсер сработает точно по этому триггеру, а не по опросу.

ЧТО ДЕЛАЕТ:
1. Читает время из Config (как и раньше в scheduler_run.py).
2. Проверяет, существует ли уже задача "AmazonParserDaily" в Планировщике.
   - Если НЕТ — создаёт её (ежедневный запуск parser_not_test.py в это время).
   - Если ДА — меняет её время начала на актуальное из Config (если отличается).

КАК ЭТО РАЗВЕРНУТЬ ПОЛНОСТЬЮ (делается один раз):
1. Запустить этот скрипт вручную — он создаст задачу "AmazonParserDaily"
   с временем из Config прямо сейчас:
       python sync_schedule_trigger.py
2. Отдельно зарегистрировать ВТОРУЮ, лёгкую задачу в Планировщике — она
   не запускает парсер, а просто раз в день сверяет время с Config и
   при необходимости подправляет триггер первой задачи:
   - Открыть "Планировщик заданий" -> "Создать задачу"
   - Триггер: Ежедневно, один раз, например в 00:05
   - Действие: powershell.exe -NoProfile -ExecutionPolicy Bypass -File
     "<путь к run_scheduler_check.ps1 или напрямую python sync_schedule_trigger.py>"
   Назвать её, например, "SyncParserSchedule".

После этого: поменяли время в Config -> в течение суток (на следующей
проверке SyncParserSchedule) триггер "AmazonParserDaily" подстроится
автоматически, и парсер сам запустится Планировщиком точно в новое время —
без единого polling-запроса между этим.
"""

import re
import subprocess
import sys
from pathlib import Path

from config import KEY_FILE_CANDIDATES, SHEET_NAME
from utils import find_key_file, logger

DEFAULT_SCHEDULE_TIME = "09:00"
CONFIG_SHEET_NAME = "Config"
TIME_COLUMN_HEADER = "vol"  # тот же формат, что уже используется в run_if_scheduled.py
TASK_NAME = "AmazonParserDaily"


def _parse_time_value(raw: str):
    """
    Парсит '14-00', '14:00', '14.00' -> (hour, minute) или None.
    Та же регулярка, что уже используется в run_if_scheduled.py — специально
    держим оба скрипта в одинаковом понимании формата времени из Config.
    """
    match = re.search(r"(\d{1,2})\D+(\d{2})", str(raw))
    if not match:
        return None
    hour, minute = int(match.group(1)), int(match.group(2))
    if 0 <= hour <= 23 and 0 <= minute <= 59:
        return hour, minute
    return None


def get_schedule_time_from_config(spreadsheet) -> tuple:
    """
    Читает время из листа 'Config', колонка 'Vol' — берёт ПЕРВОЕ непустое
    значение под этим заголовком (та же логика, что в run_if_scheduled.py:
    KEY-колонка может быть пустой, это нормально для вашего реального Config).
    """
    default = (9, 0)
    try:
        worksheet = spreadsheet.worksheet(CONFIG_SHEET_NAME)
        rows = worksheet.get_all_values()
    except Exception as exc:
        logger.warning(f"Не удалось прочитать лист '{CONFIG_SHEET_NAME}': {exc}. Время по умолчанию 09:00.")
        return default

    if len(rows) < 2:
        logger.warning(f"Лист '{CONFIG_SHEET_NAME}' пуст — время по умолчанию 09:00.")
        return default

    headers = [str(h).strip().casefold() for h in rows[0]]
    if TIME_COLUMN_HEADER not in headers:
        logger.warning(f"В '{CONFIG_SHEET_NAME}' нет колонки '{TIME_COLUMN_HEADER}' — время по умолчанию 09:00.")
        return default

    col_index = headers.index(TIME_COLUMN_HEADER)
    for row in rows[1:]:
        if len(row) > col_index and row[col_index].strip():
            parsed = _parse_time_value(row[col_index])
            if parsed:
                return parsed
            logger.warning(f"Не удалось распознать время '{row[col_index]}' — время по умолчанию 09:00.")
            return default

    logger.warning(f"В колонке '{TIME_COLUMN_HEADER}' нет значения — время по умолчанию 09:00.")
    return default


def _task_exists(task_name: str) -> bool:
    result = subprocess.run(
        ["schtasks", "/query", "/tn", task_name],
        capture_output=True, text=True,
    )
    return result.returncode == 0


def _get_task_current_time(task_name: str) -> str:
    """Возвращает текущее время старта задачи в формате HH:MM, или '' если не удалось узнать."""
    result = subprocess.run(
        ["schtasks", "/query", "/tn", task_name, "/fo", "LIST", "/v"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        return ""
    for line in result.stdout.splitlines():
        if line.strip().lower().startswith("start time:"):
            return line.split(":", 1)[1].strip()
    return ""


def sync_task(hour: int, minute: int, python_exe: str, script_path: str, base_dir: str) -> None:
    time_str = f"{hour:02d}:{minute:02d}"
    task_command = f'"{python_exe}" "{script_path}"'

    if not _task_exists(TASK_NAME):
        logger.info(f"Задача '{TASK_NAME}' не найдена — создаю с временем {time_str}.")
        result = subprocess.run(
            [
                "schtasks", "/create", "/tn", TASK_NAME,
                "/tr", task_command,
                "/sc", "daily",
                "/st", time_str,
                "/f",  # перезаписать, если вдруг уже существует с этим именем
            ],
            capture_output=True, text=True,
        )
        if result.returncode == 0:
            logger.info(f"Задача '{TASK_NAME}' создана: ежедневный запуск в {time_str}.")
        else:
            logger.error(f"Не удалось создать задачу '{TASK_NAME}': {result.stderr.strip()}")
        return

    current_time = _get_task_current_time(TASK_NAME)
    # Windows часто возвращает время в формате "14:00:00" — сравниваем по первым 5 символам.
    if current_time[:5] == time_str:
        logger.info(f"Задача '{TASK_NAME}' уже настроена на {time_str} — менять нечего.")
        return

    logger.info(f"Время в задаче '{TASK_NAME}' сейчас '{current_time}', в Config — '{time_str}'. Обновляю.")
    result = subprocess.run(
        ["schtasks", "/change", "/tn", TASK_NAME, "/st", time_str],
        capture_output=True, text=True,
    )
    if result.returncode == 0:
        logger.info(f"Задача '{TASK_NAME}' обновлена: теперь запускается в {time_str}.")
    else:
        logger.error(f"Не удалось обновить время задачи '{TASK_NAME}': {result.stderr.strip()}")


def main():
    if sys.platform != "win32":
        logger.error("Этот скрипт работает только на Windows (использует schtasks).")
        sys.exit(1)

    import gspread

    base_dir = Path(__file__).parent
    key_file = find_key_file(base_dir)
    if not key_file:
        logger.error(f"Не найден файл ключа Google service account (искал: {KEY_FILE_CANDIDATES}). Прерываю.")
        sys.exit(1)

    gc = gspread.service_account(filename=key_file)
    spreadsheet = gc.open(SHEET_NAME)

    hour, minute = get_schedule_time_from_config(spreadsheet)

    python_exe = base_dir / ".venv" / "Scripts" / "python.exe"
    script_path = base_dir / "parser_not_test.py"

    sync_task(hour, minute, str(python_exe), str(script_path), str(base_dir))


if __name__ == "__main__":
    main()
