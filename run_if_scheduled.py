"""
Проверяет лист "Config" на время автозапуска (колонка "Vol", часовой пояс —
Europe/Kyiv) и запускает парсер РОВНО ОДИН РАЗ в день, как только это время
наступило.

Как это работает:
- Время читается из Config при КАЖДОМ запуске этого скрипта — то есть можно
  поменять время прямо в таблице (ячейка под заголовком "Vol", формат "14-00"
  или "14:00"), и на следующей проверке оно подхватится само, без перезапуска
  чего-либо на компьютере.
- Чтобы не запускать парсер повторно много раз подряд (если сегодняшнее время
  уже прошло), рядом создаётся файл last_run_date.txt с датой последнего
  запуска (по Киеву) — если сегодняшняя дата там уже есть, скрипт просто
  ничего не делает и выходит.

Этот скрипт НЕ висит в фоне сам — его нужно запускать периодически (например,
раз в 10 минут) через Планировщик заданий Windows. Инструкция по настройке —
в чате.

Однократный ручной запуск (для проверки, что всё читается верно):
    python run_if_scheduled.py
    python run_if_scheduled.py --force    # запустить парсер прямо сейчас, игнорируя время/отметку
"""

import argparse
import re
import sys
from datetime import date, datetime
from pathlib import Path

try:
    from zoneinfo import ZoneInfo
except ImportError:
    print("Нужен Python 3.9+ (модуль zoneinfo). Проверьте версию: python --version")
    sys.exit(1)

from config import KEY_FILE_CANDIDATES, SHEET_NAME
from utils import find_key_file, logger

KYIV_TZ = ZoneInfo("Europe/Kyiv")
CONFIG_SHEET_NAME = "Config"
TIME_COLUMN_HEADER = "vol"  # заголовок колонки со временем в Config (регистр не важен)
LAST_RUN_MARKER_FILE = "last_run_date.txt"


def _parse_time_value(raw: str):
    """Парсит '14-00', '14:00', '14.00' и т.п. -> (hour, minute) или None."""
    match = re.search(r"(\d{1,2})\D+(\d{2})", str(raw))
    if not match:
        return None
    hour, minute = int(match.group(1)), int(match.group(2))
    if 0 <= hour <= 23 and 0 <= minute <= 59:
        return hour, minute
    return None


def get_scheduled_time(spreadsheet):
    """Читает время автозапуска из листа Config (колонка 'Vol'). Возвращает (hour, minute) или None."""
    try:
        worksheet = spreadsheet.worksheet(CONFIG_SHEET_NAME)
        rows = worksheet.get_all_values()
    except Exception as exc:
        logger.warning(f"Не удалось прочитать лист '{CONFIG_SHEET_NAME}': {exc}")
        return None

    if len(rows) < 2:
        logger.warning(f"Лист '{CONFIG_SHEET_NAME}' пуст или без данных.")
        return None

    headers = [str(h).strip().casefold() for h in rows[0]]
    if TIME_COLUMN_HEADER not in headers:
        logger.warning(f"В листе '{CONFIG_SHEET_NAME}' не найдена колонка '{TIME_COLUMN_HEADER}' (искал в заголовках: {headers}).")
        return None

    col_index = headers.index(TIME_COLUMN_HEADER)
    for row in rows[1:]:
        if len(row) > col_index and row[col_index].strip():
            parsed = _parse_time_value(row[col_index])
            if parsed:
                return parsed
            logger.warning(f"Не удалось распознать время из значения {row[col_index]!r} в листе '{CONFIG_SHEET_NAME}'.")
            return None
    logger.warning(f"В колонке '{TIME_COLUMN_HEADER}' листа '{CONFIG_SHEET_NAME}' нет значения.")
    return None


def _read_last_run_date(base_dir: Path) -> str:
    marker = base_dir / LAST_RUN_MARKER_FILE
    if marker.exists():
        return marker.read_text(encoding="utf-8").strip()
    return ""


def _write_last_run_date(base_dir: Path, value: str) -> None:
    (base_dir / LAST_RUN_MARKER_FILE).write_text(value, encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description="Запуск парсера по расписанию из Config (Europe/Kyiv)")
    parser.add_argument("--force", action="store_true", help="Запустить парсер прямо сейчас, игнорируя время и отметку 'уже запускался сегодня'")
    args = parser.parse_args()

    import gspread

    base_dir = Path(__file__).parent
    key_file = find_key_file(base_dir)
    if not key_file:
        print(f"Не найден файл ключа Google service account (искал: {KEY_FILE_CANDIDATES}). Прерываю.")
        sys.exit(1)

    gc = gspread.service_account(filename=key_file)
    spreadsheet = gc.open(SHEET_NAME)

    now_kyiv = datetime.now(KYIV_TZ)
    today_str = now_kyiv.date().isoformat()

    if args.force:
        print(f"--force: запускаю парсер прямо сейчас ({now_kyiv.strftime('%Y-%m-%d %H:%M:%S')} Europe/Kyiv), игнорируя расписание.")
        from parser_not_test import run_parser
        run_parser()
        _write_last_run_date(base_dir, today_str)
        return

    scheduled = get_scheduled_time(spreadsheet)
    if scheduled is None:
        print("Не удалось прочитать время из Config — выхожу без запуска.")
        return

    hour, minute = scheduled
    last_run_date = _read_last_run_date(base_dir)

    print(f"Сейчас (Europe/Kyiv): {now_kyiv.strftime('%Y-%m-%d %H:%M:%S')}. "
          f"Время из Config: {hour:02d}:{minute:02d}. Последний запуск: {last_run_date or '—'}")

    if last_run_date == today_str:
        print("Сегодня парсер уже запускался — выхожу.")
        return

    scheduled_today = now_kyiv.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if now_kyiv < scheduled_today:
        print(f"Ещё рано — запуск запланирован на {scheduled_today.strftime('%H:%M')}. Выхожу, попробую на следующей проверке.")
        return

    print(f"Время наступило — запускаю парсер ({now_kyiv.strftime('%Y-%m-%d %H:%M:%S')} Europe/Kyiv).")
    from parser_not_test import run_parser
    run_parser()
    _write_last_run_date(base_dir, today_str)
    print("Готово. Отметка о запуске сегодня сохранена.")


if __name__ == "__main__":
    main()
