"""
Разовый скрипт: создаёт в Планировщике Windows задачу "SyncParserSchedule",
которая каждый час запускает sync_schedule_trigger.py (лёгкая сверка времени
с Google Таблицей, без запуска самого парсера).

Запускается один раз:
    python create_sync_task.py

Использует subprocess.run со списком аргументов — это позволяет обойти
проблемы с экранированием кавычек в PowerShell, когда путь к проекту
содержит пробелы (например, "parser not test").
"""

import subprocess
import sys
from pathlib import Path

TASK_NAME = "SyncParserSchedule"


def main():
    if sys.platform != "win32":
        print("Этот скрипт работает только на Windows.")
        sys.exit(1)

    base_dir = Path(__file__).parent
    python_exe = base_dir / ".venv" / "Scripts" / "python.exe"
    script_path = base_dir / "sync_schedule_trigger.py"

    if not python_exe.exists():
        print(f"Не найден python.exe по пути: {python_exe}")
        sys.exit(1)
    if not script_path.exists():
        print(f"Не найден sync_schedule_trigger.py по пути: {script_path}")
        sys.exit(1)

    task_command = f'"{python_exe}" "{script_path}"'

    cmd = [
        "schtasks", "/create",
        "/tn", TASK_NAME,
        "/tr", task_command,
        "/sc", "hourly",
        "/st", "00:05",
        "/f",
    ]

    result = subprocess.run(cmd, capture_output=True, text=True, encoding="cp866", errors="replace")

    if result.returncode == 0:
        print(f"Готово. Задача '{TASK_NAME}' создана: запуск каждый час, начиная с 00:05.")
        print(result.stdout)
    else:
        print(f"Ошибка при создании задачи '{TASK_NAME}':")
        print(result.stderr or result.stdout)
        sys.exit(1)


if __name__ == "__main__":
    main()
