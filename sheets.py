from datetime import datetime
import re
from typing import Any, Dict, Iterable, List, Optional, Tuple

import gspread

from config import COMPETITOR_ASIN_HEADERS, CONFIG_HEADER, CONFIG_SHEET_NAME, MATRIX_SHEET_NAME, SHEET_NAME
from utils import clean_number, normalize_header, extract_asin_from_text, build_parent_formula, logger

# Кэш "предыдущего" BSR на уровне процесса — см. _get_previous_bsr_snapshot() ниже.
# Живёт, пока жив процесс (один запуск parser_not_test.py = один процесс = кэш
# сбрасывается сам собой при следующем запуске скрипта).
_PREVIOUS_BSR_SNAPSHOT_CACHE: Optional[Dict[Tuple[Optional[str], Optional[str]], Dict[str, Any]]] = None


def get_or_create_worksheet(
    spreadsheet: gspread.Spreadsheet, title: str, rows: int = 1000, cols: int = 26
) -> gspread.Worksheet:
    """Получает лист по названию или создаёт новый, если он не существует."""
    try:
        return spreadsheet.worksheet(title)
    except gspread.WorksheetNotFound:
        logger.info(f"Лист '{title}' не найден, создаю новый.")
        return spreadsheet.add_worksheet(title=title, rows=rows, cols=cols)


def connect_sheet(key_file: str) -> Optional[gspread.Worksheet]:
    """Подключается к таблице и возвращает основной лист с матрицей."""
    spreadsheet = None
    try:
        gc = gspread.service_account(filename=key_file)
        spreadsheet = gc.open(SHEET_NAME)
    except Exception as exc:
        logger.error(f"Не удалось подключиться к Google Sheets: {exc}", exc_info=True)
        # Обработка ошибки SSL для некоторых окружений
        if "CERTIFICATE_VERIFY_FAILED" in str(exc) or "SSLError" in str(exc):
            logger.warning("Обнаружена ошибка SSL, пробую альтернативный метод подключения...")
            try:
                import urllib3
                import requests
                from google.oauth2.service_account import Credentials
                from google.auth.transport.requests import Request, AuthorizedSession

                urllib3.disable_warnings()
                scopes = ["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"]
                creds = Credentials.from_service_account_file(key_file, scopes=scopes)
                session = requests.Session()
                session.verify = False
                creds.refresh(Request(session=session))
                authed_session = AuthorizedSession(creds)
                authed_session.verify = False
                gc = gspread.Client(auth=creds)
                gc.http_client.session = authed_session
                spreadsheet = gc.open(SHEET_NAME)
            except Exception as e:
                logger.critical(f"Альтернативный метод подключения также не удался: {e}", exc_info=True)
                return None
        else:
            return None

    if spreadsheet is None:
        return None

    # Убедимся, что все нужные листы существуют
    get_or_create_worksheet(spreadsheet, MATRIX_SHEET_NAME)
    get_or_create_worksheet(spreadsheet, "Current")
    get_or_create_worksheet(spreadsheet, "History")
    get_or_create_worksheet(spreadsheet, "Competitors")

    current_sheet = get_or_create_worksheet(spreadsheet, MATRIX_SHEET_NAME, rows=1000, cols=20)
    add_amazon_links(current_sheet)

    try:
        current_values = current_sheet.get_all_values()
    except Exception:
        current_values = []
    if not current_values:
        current_sheet.append_row([
            "Дата", "Маркетплейс", "Наш товар", "Наш ASIN", "BSR Наш.", "Наша цена",
            "Конкурент", "ASIN конкурента", "BSR конкурента", "Цена конкурента",
            "Разница, %", "Динамика BSR (24ч).", "Статус наличия (Stock)",
        ])

    clear_matrix_calculation_highlighting(current_sheet)

    return current_sheet


def _amazon_domain(marketplace):
    return {
        "US": "com",
        "CA": "ca",
        "UK": "co.uk",
        "DE": "de",
        "FR": "fr",
        "ES": "es",
        "IT": "it",
        "MX": "com.mx",
        "JP": "co.jp",
        "AU": "com.au",
    }.get(str(marketplace).strip().upper(), "com")


# Валюта по маркетплейсу — тот же набор ключей, что и в _amazon_domain() выше.
# Раньше в refresh_current_matrix была отдельная урезанная копия этого словаря
# только с US/CA/UK ({"US": "USD", "CA": "CAD", "UK": "GBP"}) — для DE/FR/ES/IT
# (основная масса строк по факту) currency всегда получалась пустой строкой.
def _amazon_currency(marketplace):
    return {
        "US": "USD",
        "CA": "CAD",
        "UK": "GBP",
        "DE": "EUR",
        "FR": "EUR",
        "ES": "EUR",
        "IT": "EUR",
        "MX": "MXN",
        "JP": "JPY",
        "AU": "AUD",
    }.get(str(marketplace).strip().upper(), "")


def _amazon_link(asin, marketplace):
    domain = _amazon_domain(marketplace)
    return f'=HYPERLINK("https://www.amazon.{domain}/dp/{asin}", "{asin}")'


def _convert_to_asin_link(value, marketplace):
    if marketplace is None:
        marketplace = "US"
    value_str = str(value).strip()
    if not value_str:
        return value
    if value_str.startswith("=") and "HYPERLINK" in value_str.upper():
        return value
    asin = extract_asin_from_text(value_str)
    return _amazon_link(asin, marketplace) if asin else value


def add_amazon_links(sheet):
    """Заменяет значения ASIN на ссылки Amazon в уже существующих строках."""
    try:
        rows = sheet.get_all_values()
        header_row = None
        headers = []
        for index, row in enumerate(rows[:5]):
            normalized = [str(value).strip().casefold() for value in row]
            if "наш asin" in normalized and "asin конкурента" in normalized:
                header_row = index
                headers = normalized
                break
        if header_row is None:
            return

        our_asin_column = headers.index("наш asin")
        competitor_asin_column = headers.index("asin конкурента")
        marketplace_column = headers.index("маркетплейс") if "маркетплейс" in headers else None
        for row_number, row in enumerate(rows[header_row + 1:], start=header_row + 2):
            marketplace = row[marketplace_column] if marketplace_column is not None and len(row) > marketplace_column else "US"
            for column in (our_asin_column, competitor_asin_column):
                value = row[column] if len(row) > column else ""
                asin = extract_asin_from_text(value)
                if asin and not str(value).startswith("=HYPERLINK"):
                    sheet.update_cell(row_number, column + 1, _amazon_link(asin, marketplace))
    except Exception as exc:
        print(f"Не удалось добавить ссылки Amazon: {exc}")


def apply_history_formatting(history_sheet):
    """Оформляет лист History по шаблону трекера конкурентов."""
    try:
        values = history_sheet.get_all_values()
        last_row = max(3, len(values))
        history_sheet.format(
            "A1:M1",
            {
                "backgroundColor": {"red": 0.96, "green": 0.96, "blue": 0.96},
                "horizontalAlignment": "CENTER",
                "textFormat": {"italic": True, "fontSize": 9},
            },
        )
        history_sheet.format(
            "A2:M2",
            {
                "backgroundColor": {"red": 0.85, "green": 0.92, "blue": 0.83},
                "horizontalAlignment": "CENTER",
                "wrapStrategy": "WRAP",
                "textFormat": {"bold": True},
            },
        )
        yellow_fill = {"backgroundColor": {"red": 1.0, "green": 0.95, "blue": 0.8}}
        history_sheet.format(f"E3:E{last_row}", yellow_fill)
        history_sheet.format(f"I3:I{last_row}", yellow_fill)
        history_sheet.freeze(rows=2)
    except Exception as exc:
        print(f"Не удалось применить оформление History: {exc}")


def _column_letter(column_index: int) -> str:
    """Преобразует номер столбца (с нуля) в букву A1-нотации (A, B, C...)."""
    result = ""
    number = column_index + 1
    while number > 0:
        number, remainder = divmod(number - 1, 26)
        result = chr(65 + remainder) + result
    return result


def _find_matrix_columns(rows):
    """Возвращает строку заголовков и номера нужных столбцов матрицы (с нуля)."""
    required = {
        "asin": COMPETITOR_ASIN_HEADERS + ("comp_asin",),
        "bsr": ("bsr конкурента", "comp_bsr"),
        "price": ("цена конкурента", "comp_price"),
        "difference": ("разница, %", "разница %", "price_diff_pct"),
        "dynamics": ("динамика bsr (24ч).", "динамика bsr (24ч)", "динамика bsr 24ч", "comp_bsr_delta_24h"),
        "stock": ("статус наличия (stock)", "статус наличия", "comp_stock"),
        "date": ("дата", "snapshot_date"),
        "our_asin": ("наш asin", "our asin", "our_asin"),
        "marketplace": ("маркетплейс", "marketplace"),
        "our_price": ("наша цена", "our_price"),
    }
    for row_index, row in enumerate(rows[:5]):
        normalized = [str(value).strip().casefold() for value in row]
        columns = {}
        for key, names in required.items():
            for name in names:
                if name in normalized:
                    columns[key] = normalized.index(name)
                    break
        if "asin" in columns:
            return row_index, columns
    return None, {}


def clear_matrix_calculation_highlighting(sheet):
    """Убирает фоновую заливку из вычисляемых колонок матрицы."""
    try:
        rows = sheet.get_all_values()
        header_row, columns = _find_matrix_columns(rows)
        if header_row is None:
            return
        first_data_row = header_row + 2
        for key in ("our_price", "price", "difference", "dynamics"):
            column = columns.get(key)
            if column is not None:
                letter = _column_letter(column)
                sheet.format(
                    f"{letter}{first_data_row}:{letter}1000",
                    {"backgroundColor": {"red": 1.0, "green": 1.0, "blue": 1.0}},
                )
        competitor_bsr_column = columns.get("bsr")
        if competitor_bsr_column is not None:
            letter = _column_letter(competitor_bsr_column)
            sheet.format(
                f"{letter}12:{letter}1000",
                {"backgroundColor": {"red": 1.0, "green": 1.0, "blue": 1.0}},
            )
    except Exception as exc:
        print(f"Не удалось убрать подсветку в матрице: {exc}")


def _is_snapshot_data_row(row):
    """Отличает строку снимка от вспомогательных подписей в шапке блока."""
    if not row:
        return False
    value = str(row[0]).strip()
    return bool(re.fullmatch(r"\d{4}-\d{2}-\d{2}|\d{2}\.\d{2}\.\d{4}", value))


def _find_current_history_blocks(rows):
    """Находит блоки Current и History на вкладке Матрица."""
    current_title_row = history_title_row = None
    for row_index, row in enumerate(rows):
        label = str(row[0]).strip().casefold() if row else ""
        if label == "current":
            current_title_row = row_index
        elif label == "history":
            history_title_row = row_index

    if history_title_row is None:
        raise ValueError("На листе Матрица не найден блок History")
    # В текущем шаблоне верхний блок Current начинается с первой строки и
    # может не иметь отдельной строки с названием Current.
    if current_title_row is None:
        current_title_row = -1
    if current_title_row >= history_title_row:
        raise ValueError("Блоки Current и History расположены в неверном порядке")

    def find_header(start, end):
        for row_index in range(start + 1, end):
            normalized = [str(value).strip().casefold() for value in rows[row_index]]
            if "snapshot_date" in normalized and "comp_asin" in normalized:
                return row_index
        raise ValueError("Не найдена строка заголовков snapshot_date / comp_asin")

    current_header_row = find_header(current_title_row, history_title_row)
    history_header_row = find_header(history_title_row, len(rows))
    return current_header_row, history_title_row, history_header_row


def _get_current_block_from_matrix(rows: List[List[str]]) -> Tuple[List[str], List[List[str]], int]:
    """Находит блок 'Current' в 'Матрице' и возвращает его заголовки, данные и индекс строки заголовков."""
    current_title_row = None
    for i, row in enumerate(rows):
        if row and str(row[0]).strip().casefold() == "current":
            current_title_row = i
            break
    if current_title_row is None:
        raise ValueError("На листе 'Матрица' не найден блок 'Current'")

    history_title_row = len(rows)
    for i, row in enumerate(rows[current_title_row + 1:], start=current_title_row + 1):
        if row and str(row[0]).strip().casefold() == "history":
            history_title_row = i
            break

    header_row_index = current_title_row + 1
    if header_row_index >= len(rows):
        raise ValueError("Не найдена строка заголовков после 'Current'")

    headers = [str(h).strip() for h in rows[header_row_index]]
    data_rows = rows[header_row_index + 1: history_title_row]
    return headers, data_rows, header_row_index


def append_matrix_history_snapshot(history_sheet, snapshot_date, values, group_headers, headers):
    """Добавляет снимок пары «наш товар — конкурент» одной строкой в History."""
    existing = history_sheet.get_all_values()
    if not existing:
        history_sheet.append_row(group_headers)
        history_sheet.append_row(headers)
    elif existing[0][:len(group_headers)] != group_headers or len(existing) < 2 or existing[1][:len(headers)] != headers:
        # Сохраняем старые данные: добавляем шапку сверху только один раз.
        history_sheet.insert_rows([group_headers, headers], row=1, value_input_option="USER_ENTERED")

    marketplace = values.get("Маркетплейс", "")
    our_asin = extract_asin_from_text(values.get("Наш ASIN", ""))
    competitor_asin = extract_asin_from_text(values.get("ASIN конкурента", ""))
    history_sheet.append_row([
        snapshot_date,
        marketplace,
        values.get("Наш товар", ""),
        _amazon_link(our_asin, marketplace) if our_asin else values.get("Наш ASIN", ""),
        values.get("BSR Наш.", ""),
        values.get("Наша цена", ""),
        values.get("Конкурент", ""),
        _amazon_link(competitor_asin, marketplace) if competitor_asin else values.get("ASIN конкурента", ""),
        values.get("BSR конкурента", ""),
        values.get("Цена конкурента", ""),
        values.get("Разница, %", ""),
        values.get("Динамика BSR (24ч).", ""),
        values.get("Статус наличия (Stock)", ""),
    ])
    apply_history_formatting(history_sheet)


def _history_dedup_key(headers: List[str], row_values: Dict[str, Any]) -> Optional[Tuple[str, str, str]]:
    """
    Строит ключ дедупликации (дата, наш ASIN, ASIN конкурента) для строки History.
    Возвращает None, если в строке нет даты или ASIN конкурента — такую строку
    дедуплицировать нельзя (пропускаем проверку для неё).
    """
    date_value = str(row_values.get("snapshot_date", "")).strip()
    comp_asin = extract_asin_from_text(str(row_values.get("comp_asin", "")))
    if not date_value or not comp_asin:
        return None
    our_asin = extract_asin_from_text(str(row_values.get("our_asin", ""))) or ""
    return (date_value, our_asin, comp_asin)


def _append_checked_rows_to_history(
    spreadsheet: gspread.Spreadsheet,
    records: List[Dict],
    failed_asins_set: Optional[set] = None,
) -> int:
    """
    Добавляет в History только пары, проверенные в текущем запуске (батче).

    failed_asins_set — опциональный набор ASIN, по которым не удалось получить данные
    в этом прогоне. Если передан, только что добавленные строки подсвечиваются жёлтым
    в колонках our_asin/comp_asin (а также любая ячейка со значением "@") — точно так же,
    как это уже делается для листа "Current" в _highlight_failed_asins_in_sheet.
    """
    rows_to_append = [record for record in records if record["checked"]]
    if not rows_to_append:
        return 0

    try:
        history_sheet = get_or_create_worksheet(spreadsheet, "History")
        existing_values = history_sheet.get_all_values() if history_sheet.row_count > 0 else []
    except Exception as e:
        logger.error(f"Не удалось получить доступ к листу 'History': {e}")
        return 0

    headers = list(rows_to_append[0]["values"].keys())

    if not existing_values:
        history_sheet.append_row(headers)
        rows_before_append = 1  # только что записанная строка заголовков
    else:
        rows_before_append = len(existing_values)

    data_to_append = [
        [record["values"].get(header, "") for header in headers] for record in rows_to_append
    ]
    history_sheet.append_rows(data_to_append, value_input_option="USER_ENTERED")
    logger.info(f"Добавлено {len(data_to_append)} записей в лист 'History'.")

    if failed_asins_set:
        columns = {header: index for index, header in enumerate(headers)}
        _highlight_failed_asins_in_sheet(
            history_sheet,
            columns,
            data_to_append,
            failed_asins_set,
            data_start_row=rows_before_append + 1,
        )

    return len(data_to_append)


def _refresh_current_sheet_from_matrix(
    matrix_sheet: gspread.Worksheet,
    products_by_asin: Dict,
    snapshot_date: str,
    failed_asins: Optional[List[str]] = None,
    history_asins: Optional[Iterable[str]] = None,
) -> Dict:
    """
    Копирует структуру из блока Current листа "Матрица", наполняет её свежими данными
    и записывает результат в отдельный лист "Current". Также добавляет проверенные пары в "History".

    products_by_asin / failed_asins — НАКОПЛЕННЫЕ данные с начала всего прогона (используются
    для заполнения строк листа "Current" самыми свежими значениями по каждой паре).

    history_asins — если передан, ограничивает список пар, которые в этом вызове реально
    попадут в "History", только теми, чей our_asin/comp_asin входит в этот набор (например,
    ASIN текущего батча). Это нужно, чтобы при промежуточных сохранениях после каждого батча
    в History не переписывались заново все уже ранее записанные пары, а уходила только дельта
    новых. Если history_asins не передан — используется старое поведение (по всем "checked" парам
    из products_by_asin/failed_asins), это сохранено для обратной совместимости.
    """
    failed_asins_set = set(failed_asins or [])
    history_asins_set = set(history_asins) if history_asins is not None else None
    rows = matrix_sheet.get_all_values()

    try:
        headers, existing_data_rows, _ = _get_current_block_from_matrix(rows)
    except ValueError as e:
        logger.warning(f"Не удалось найти блок 'Current' в листе 'Матрица': {e}. Пропускаю обновление.")
        return {"archived_rows": 0, "records": []}

    if "comp_asin" not in headers:
        raise ValueError("В блоке 'Current' на листе 'Матрица' отсутствует обязательный столбец 'comp_asin'")

    width = len(headers)
    columns = {str(header).strip(): index for index, header in enumerate(headers) if str(header).strip()}

    # --- Читаем ТЕКУЩЕЕ содержимое листа "Current" ДО его перезаписи ---
    # Это единственный надёжный источник "предыдущего" BSR для расчёта delta_24h.
    #
    # ВАЖНО: раньше это читалось заново при КАЖДОМ вызове refresh_current_matrix —
    # а он вызывается до ~60 раз за один прогон (см. parser_not_test.py, промежуточные
    # сохранения после каждого пакета). После первого же пакета лист "Current" уже
    # перезаписан данными ЭТОГО прогона — и все следующие вызовы читали "предыдущий"
    # BSR, который на самом деле был "текущим" (только что записанным в этом же
    # запуске). В итоге new_bsr - old_bsr = X - X = 0 почти для всех пар, кроме
    # обработанных в самом последнем пакете. Теперь читаем это состояние ОДИН РАЗ за
    # весь прогон (кэш на уровне процесса) — оно отражает результат ПРЕДЫДУЩЕГО
    # запуска скрипта, а не текущего.
    previous_bsr_by_pair = _get_previous_bsr_snapshot(matrix_sheet.spreadsheet)

    # Определяем источник данных: лист 'Competitors' имеет приоритет
    source_pairs = load_active_competitor_pairs(matrix_sheet.spreadsheet)
    if source_pairs:
        source_rows = []
        for pair in source_pairs:
            values = {
                "marketplace": pair["marketplace"],
                "currency": _amazon_currency(pair["marketplace"]),
                "our_product": pair["our_product"],
                "our_asin": pair["our_asin"],
                "competitor": pair["competitor"],
                "comp_asin": pair["comp_asin"],
            }
            source_rows.append([values.get(str(header).strip(), "") for header in headers])
    else:
        logger.info("Лист 'Competitors' пуст или не настроен. Используются данные из блока 'Current' на листе 'Матрица'.")
        source_rows = existing_data_rows

    records = []
    refreshed_rows = []
    for source_row in source_rows:
        row = list(source_row[:width]) + [""] * max(0, width - len(source_row))  # Копируем, чтобы не менять оригинал

        our_asin = extract_asin_from_text(row[columns["our_asin"]]) if "our_asin" in columns else None
        comp_asin = extract_asin_from_text(row[columns["comp_asin"]]) if "comp_asin" in columns else None
        previous_pair_values = previous_bsr_by_pair.get((our_asin, comp_asin), {})

        our_product = products_by_asin.get(our_asin)
        comp_product = products_by_asin.get(comp_asin)
        our_checked = our_asin in products_by_asin or our_asin in failed_asins_set
        comp_checked = comp_asin in products_by_asin or comp_asin in failed_asins_set

        # Очищаем старые вычисляемые значения
        for field in (
            "snapshot_date", "our_bsr", "our_price", "our_bsr_delta_24h",
            "comp_bsr", "comp_price", "price_diff_pct",
            "comp_bsr_delta_24h", "comp_stock", "updated_at",
        ):
            if field in columns:
                row[columns[field]] = ""

        if our_product:
            old_bsr = clean_number(previous_pair_values.get("our_bsr"))
            new_bsr = clean_number(our_product.get("bsr"))
            if "our_product" in columns and our_product.get("title"):
                row[columns["our_product"]] = our_product["title"]
            if "our_bsr" in columns:
                row[columns["our_bsr"]] = our_product.get("bsr", "")
            if "our_price" in columns:
                row[columns["our_price"]] = our_product.get("price", "")
            if "our_bsr_delta_24h" in columns:
                row[columns["our_bsr_delta_24h"]] = (
                    new_bsr - old_bsr if new_bsr is not None and old_bsr is not None else ""
                )
        elif our_asin in failed_asins_set and our_asin:
            for field in (
                "our_product",
                "our_bsr",
                "our_price",
                "our_bsr_delta_24h",
            ):
                if field in columns:
                    row[columns[field]] = "@"

        if comp_product:
            old_bsr = clean_number(previous_pair_values.get("comp_bsr"))
            new_bsr = clean_number(comp_product.get("bsr"))
            if "competitor" in columns and comp_product.get("title"):
                row[columns["competitor"]] = comp_product["title"]
            if "comp_bsr" in columns:
                row[columns["comp_bsr"]] = comp_product.get("bsr", "")
            if "comp_price" in columns:
                row[columns["comp_price"]] = comp_product.get("price", "")
            if "comp_stock" in columns:
                row[columns["comp_stock"]] = comp_product.get("stock_status", "")
            if "comp_bsr_delta_24h" in columns:
                row[columns["comp_bsr_delta_24h"]] = (
                    new_bsr - old_bsr if new_bsr is not None and old_bsr is not None else ""
                )
        elif comp_asin in failed_asins_set and comp_asin:
            for field in (
                "competitor",
                "comp_bsr",
                "comp_price",
                "price_diff_pct",
                "comp_bsr_delta_24h",
                "comp_stock",
            ):
                if field in columns:
                    row[columns[field]] = "@"
            # Очищаем поле updated_at, чтобы не оставлять старую дату
            if "updated_at" in columns:
                row[columns["updated_at"]] = ""

        if our_product and comp_product and "price_diff_pct" in columns:
            own_price = clean_number(our_product.get("price"))
            comp_price = clean_number(comp_product.get("price"))
            row[columns["price_diff_pct"]] = (
                f"{(comp_price / own_price - 1):.1%}"
                if own_price not in (None, 0) and comp_price is not None else ""
            )
        if our_checked or comp_checked:
            if "snapshot_date" in columns:
                row[columns["snapshot_date"]] = snapshot_date
            if "updated_at" in columns:
                row[columns["updated_at"]] = datetime.now().strftime("%Y-%m-%d %H:%M")

        marketplace = row[columns["marketplace"]] if "marketplace" in columns else "US"
        for field in ("our_asin", "comp_asin"):
            if field in columns:
                row[columns[field]] = _convert_to_asin_link(row[columns[field]], marketplace)

        # Для листа Current используем "накопленный" признак (our_checked/comp_checked) —
        # там всегда должна быть самая свежая известная информация по паре.
        # Для History же считаем пару "проверенной в этом вызове" только если она
        # относится к текущему батчу (history_asins), иначе на каждом промежуточном
        # сохранении в History заново попадали бы все пары, обработанные с начала прогона.
        if history_asins_set is not None:
            pair_in_this_call = bool(
                (our_asin and our_asin in history_asins_set)
                or (comp_asin and comp_asin in history_asins_set)
            )
        else:
            pair_in_this_call = our_checked or comp_checked

        # Доп. поля ТОЛЬКО для Telegram-отчёта (build_grouped_telegram_report в
        # parser_not_test.py) — рейтинг/отзывы/бренд у ScrapingDog уже есть в
        # products_by_asin, их не нужно заново запрашивать. ВАЖНО: кладём их в
        # ОТДЕЛЬНЫЙ ключ "report_extra", а не в "values" — потому что
        # _append_checked_rows_to_history() берёт список колонок для записи в
        # лист History прямо из ключей record["values"] (headers = list(...values.keys())).
        # Если добавить эти поля в values, они улетят в History как "лишние"
        # столбцы без заголовка (там уже есть свой фиксированный заголовок из
        # прошлых запусков) — это и произошло в проде (см. History!Q:V).
        report_extra = {}
        if our_product:
            if our_product.get("stars") not in (None, "", "Not Found"):
                report_extra["our_stars"] = our_product.get("stars")
            if our_product.get("reviews") not in (None, "", "Not Found"):
                report_extra["our_reviews"] = our_product.get("reviews")
            if our_product.get("brand"):
                report_extra["our_brand"] = our_product.get("brand")
        if comp_product:
            if comp_product.get("stars") not in (None, "", "Not Found"):
                report_extra["comp_stars"] = comp_product.get("stars")
            if comp_product.get("reviews") not in (None, "", "Not Found"):
                report_extra["comp_reviews"] = comp_product.get("reviews")
            if comp_product.get("brand"):
                report_extra["comp_brand"] = comp_product.get("brand")

        refreshed_rows.append(row)
        records.append({
            "asin": comp_asin,
            "checked": pair_in_this_call,
            "values": dict(zip(headers, row)),
            "report_extra": report_extra,
        })

    # --- Записываем результат в отдельный лист "Current" ---
    target_sheet = get_or_create_worksheet(matrix_sheet.spreadsheet, "Current")
    target_sheet.clear()
    target_sheet.update("A1", [["Current"], headers, *refreshed_rows], value_input_option="USER_ENTERED")
    logger.info(f"Данные записаны в отдельный лист 'Current'. Всего {len(refreshed_rows)} строк.")
    _highlight_failed_asins_in_sheet(target_sheet, columns, refreshed_rows, failed_asins_set, data_start_row=3)

    archived_rows = _append_checked_rows_to_history(matrix_sheet.spreadsheet, records, failed_asins_set)
    mark_failed_asins_in_competitors(matrix_sheet.spreadsheet, failed_asins_set, products_by_asin)
    return {"archived_rows": archived_rows, "records": records}


def _highlight_failed_asins_in_sheet(
    worksheet: gspread.Worksheet,
    columns: Dict[str, int],
    rows: List[List[Any]],
    failed_asins_set: set,
    data_start_row: int,
) -> None:
    """
    Подсвечивает жёлтым ячейки our_asin/comp_asin (и любую ячейку со значением
    "@") в уже записанном листе (например, "Current"), если соответствующий
    ASIN входит в failed_asins_set — то есть данные по нему не удалось
    получить в ЭТОМ прогоне.

    ВАЖНО: .clear() в Google Sheets API стирает только значения ячеек, а НЕ
    цвет фона (раньше в комментарии здесь было написано обратное — это было
    ошибкой). Поэтому если явно не сбросить старую жёлтую заливку, она может
    висеть годами даже после того, как ASIN снова начал успешно парситься.
    Ниже — сначала один bulk-запрос сбрасывает ВЕСЬ используемый диапазон в
    белый, и только потом точечно красятся жёлтым реально сбойные ячейки.
    """
    yellow_format = {"backgroundColor": {"red": 1.0, "green": 0.95, "blue": 0.4}}
    white_format = {"backgroundColor": {"red": 1.0, "green": 1.0, "blue": 1.0}}
    format_requests = []

    # 1) Сброс всего диапазона данных в белый — одним запросом, чтобы не
    # упереться в лимиты API на большой таблице.
    if rows and columns:
        last_col_letter = _column_letter(max(columns.values()))
        last_row_number = data_start_row + len(rows) - 1
        format_requests.append({
            "range": f"A{data_start_row}:{last_col_letter}{last_row_number}",
            "format": white_format,
        })

    # 2) Точечная покраска жёлтым только того, что реально не спарсилось СЕЙЧАС.
    if failed_asins_set:
        for offset, row in enumerate(rows):
            row_number = data_start_row + offset

            # ASIN-колонки — подсвечиваем, если сам ASIN попал в failed_asins_set
            for field in ("our_asin", "comp_asin"):
                col_index = columns.get(field)
                if col_index is None or col_index >= len(row):
                    continue
                asin = extract_asin_from_text(row[col_index])
                if asin and asin in failed_asins_set:
                    col_letter = _column_letter(col_index)
                    format_requests.append({"range": f"{col_letter}{row_number}", "format": yellow_format})

            # Любая другая ячейка со значением "@" — то есть поле, которое не удалось
            # получить для битого ASIN (our_product, our_bsr, comp_bsr, comp_price и т.д.)
            for col_index, value in enumerate(row):
                if str(value).strip() == "@":
                    col_letter = _column_letter(col_index)
                    format_requests.append({"range": f"{col_letter}{row_number}", "format": yellow_format})

    if format_requests:
        try:
            worksheet.batch_format(format_requests)
            logger.info(f"В листе '{worksheet.title}' подсвечено {len(format_requests) - 1} ячеек ASIN (после сброса старой подсветки).")
        except Exception as exc:
            logger.warning(f"Ошибка применения формата к листу '{worksheet.title}': {exc}")


def mark_failed_asins_in_competitors(
    spreadsheet: gspread.Spreadsheet, failed_asins: Optional[List[str]] = None, products_by_asin: Optional[Dict] = None
):
    """
    Отмечает битые/ошибочные ASIN жёлтым цветом в листе Competitors.
    Если ASIN позже успешно спарсился, снимает жёлтую подсветку.
    Комментарий в колонку Notes/Комментарии НЕ пишется — только подсветка ячейки.
    """
    if not failed_asins and not products_by_asin:
        return

    try:
        worksheet = spreadsheet.worksheet("Competitors")
        rows = worksheet.get_all_values()
    except Exception as exc:
        logger.warning(f"Не удалось открыть лист Competitors для подсветки ошибок: {exc}")
        return

    if not rows:
        return

    headers = [str(val).strip().casefold() for val in rows[0]]

    our_asin_col = next((i for i, h in enumerate(headers) if h in ("our asin", "our_asin", "наш asin")), None)
    comp_asin_col = next((i for i, h in enumerate(headers) if h in ("competitor asin", "comp_asin", "asin конкурента")), None)

    if our_asin_col is None and comp_asin_col is None:
        return

    failed_set = {str(a).strip().upper() for a in (failed_asins or []) if a}
    scraped_set = {str(a).strip().upper() for a in (products_by_asin or {}).keys() if a}

    format_requests = []

    yellow_format = {"backgroundColor": {"red": 1.0, "green": 0.95, "blue": 0.4}}
    white_format = {"backgroundColor": {"red": 1.0, "green": 1.0, "blue": 1.0}}

    for row_idx, row in enumerate(rows[1:], start=2):
        # Проверяем наш ASIN
        if our_asin_col is not None and len(row) > our_asin_col:
            our_asin = extract_asin_from_text(row[our_asin_col])
            if our_asin:
                col_letter = _column_letter(our_asin_col)
                cell_ref = f"{col_letter}{row_idx}"
                if our_asin in failed_set:
                    format_requests.append({"range": cell_ref, "format": yellow_format})
                elif our_asin in scraped_set:
                    format_requests.append({"range": cell_ref, "format": white_format})

        # Проверяем ASIN конкурента
        if comp_asin_col is not None and len(row) > comp_asin_col:
            comp_asin = extract_asin_from_text(row[comp_asin_col])
            if comp_asin:
                col_letter = _column_letter(comp_asin_col)
                cell_ref = f"{col_letter}{row_idx}"
                if comp_asin in failed_set:
                    format_requests.append({"range": cell_ref, "format": yellow_format})
                elif comp_asin in scraped_set:
                    format_requests.append({"range": cell_ref, "format": white_format})

    if format_requests:
        try:
            worksheet.batch_format(format_requests)
            logger.info(f"В листе Competitors подсвечено {len(format_requests)} ячеек ASIN.")
        except Exception as exc:
            logger.warning(f"Ошибка применения формата к листу Competitors: {exc}")


def _get_previous_bsr_snapshot(
    spreadsheet: gspread.Spreadsheet,
) -> Dict[Tuple[Optional[str], Optional[str]], Dict[str, Any]]:
    """
    Читает состояние листа "Current" КАК ОНО БЫЛО ДО начала текущего прогона —
    и делает это только один раз за весь процесс (см. _PREVIOUS_BSR_SNAPSHOT_CACHE).

    Раньше эта логика была прямо внутри refresh_current_matrix() и выполнялась
    заново при каждом её вызове (до ~60 раз за прогон — см. промежуточные
    сохранения в parser_not_test.py). Из-за этого после первого же пакета
    "предыдущим" BSR для delta_24h становился BSR, только что записанный этим же
    прогоном, а не результат вчерашнего запуска — и дельта схлопывалась в 0
    почти для всех пар, кроме обработанных в самом последнем пакете.
    """
    global _PREVIOUS_BSR_SNAPSHOT_CACHE
    if _PREVIOUS_BSR_SNAPSHOT_CACHE is not None:
        return _PREVIOUS_BSR_SNAPSHOT_CACHE

    previous_bsr_by_pair: Dict[Tuple[Optional[str], Optional[str]], Dict[str, Any]] = {}
    try:
        existing_current_sheet = get_or_create_worksheet(spreadsheet, "Current")
        existing_current_rows = existing_current_sheet.get_all_values()
    except Exception as exc:
        logger.warning(f"Не удалось прочитать текущее содержимое листа 'Current' для расчёта delta_24h: {exc}")
        existing_current_rows = []

    if len(existing_current_rows) >= 2:
        prev_headers = [str(h).strip() for h in existing_current_rows[1]]
        prev_columns = {h: i for i, h in enumerate(prev_headers) if h}
        if "our_asin" in prev_columns and "comp_asin" in prev_columns:
            for prev_row in existing_current_rows[2:]:
                if not prev_row:
                    continue
                prev_our_asin = (
                    extract_asin_from_text(prev_row[prev_columns["our_asin"]])
                    if len(prev_row) > prev_columns["our_asin"] else None
                )
                prev_comp_asin = (
                    extract_asin_from_text(prev_row[prev_columns["comp_asin"]])
                    if len(prev_row) > prev_columns["comp_asin"] else None
                )
                if not prev_comp_asin:
                    continue
                previous_bsr_by_pair[(prev_our_asin, prev_comp_asin)] = {
                    "our_bsr": (
                        prev_row[prev_columns["our_bsr"]]
                        if "our_bsr" in prev_columns and len(prev_row) > prev_columns["our_bsr"] else ""
                    ),
                    "comp_bsr": (
                        prev_row[prev_columns["comp_bsr"]]
                        if "comp_bsr" in prev_columns and len(prev_row) > prev_columns["comp_bsr"] else ""
                    ),
                }

    _PREVIOUS_BSR_SNAPSHOT_CACHE = previous_bsr_by_pair
    return previous_bsr_by_pair


def refresh_current_matrix(current_sheet, products_by_asin, snapshot_date, failed_asins=None, history_asins=None):
    """
    Переносит старый Current в History и полностью перезаписывает Current.

    history_asins — опциональный набор ASIN, ограничивающий, какие пары в этом вызове
    попадут в History (например, ASIN только текущего батча). См. docstring
    _refresh_current_sheet_from_matrix для подробностей.
    """
    if current_sheet.title == MATRIX_SHEET_NAME:
        return _refresh_current_sheet_from_matrix(
            current_sheet, products_by_asin, snapshot_date, failed_asins, history_asins
        )

    rows = current_sheet.get_all_values()
    header_row, history_title_row, history_header_row = _find_current_history_blocks(rows)
    _, columns = _find_matrix_columns([rows[header_row]])
    required = {"asin", "bsr", "price", "difference", "dynamics", "stock", "date", "our_price"}
    missing = required.difference(columns)
    if missing:
        raise ValueError(f"В матрице отсутствуют необходимые столбцы: {', '.join(sorted(missing))}")

    headers = rows[header_row]
    data_row_indexes = [
        row_index for row_index in range(header_row + 1, history_title_row)
        if _is_snapshot_data_row(rows[row_index])
    ]
    data_rows = [
        list(rows[row_index][:len(headers)]) + [""] * max(0, len(headers) - len(rows[row_index]))
        for row_index in data_row_indexes
    ]
    first_data_index = data_row_indexes[0] if data_row_indexes else header_row + 1
    if first_data_index + len(data_rows) > history_title_row:
        raise ValueError("Для Current недостаточно строк до блока History")

    existing_history_rows = [
        row for row in rows[history_header_row + 1:]
        if _is_snapshot_data_row(row)
    ]
    history_append_row = history_header_row + 2 + len(existing_history_rows)
    if data_rows:
        current_sheet.update(f"A{history_append_row}", data_rows, value_input_option="USER_ENTERED")
    archived_rows = len(data_rows)
    refreshed_rows = []
    records = []

    for data_index, old_row in enumerate(data_rows):
        current_row = list(old_row)
        marketplace = (
            current_row[columns["marketplace"]]
            if columns.get("marketplace") is not None and columns["marketplace"] < len(current_row)
            else "US"
        )
        current_row[columns["asin"]] = _convert_to_asin_link(current_row[columns["asin"]], marketplace)
        if "our_asin" in columns:
            current_row[columns["our_asin"]] = _convert_to_asin_link(
                current_row[columns["our_asin"]], marketplace
            )

        asin = extract_asin_from_text(old_row[columns["asin"]])
        product = products_by_asin.get(asin)
        row_number = first_data_index + 1 + data_index

        if product:
            previous_bsr = old_row[columns["bsr"]]
            try:
                bsr_change = int(str(product["bsr"]).replace(",", "")) - int(str(previous_bsr).replace(",", ""))
            except (TypeError, ValueError):
                bsr_change = ""

            current_row[columns["date"]] = snapshot_date
            current_row[columns["bsr"]] = product["bsr"]
            current_row[columns["price"]] = product["price"]
            current_row[columns["difference"]] = (
                f'=IFERROR({_column_letter(columns["price"])}{row_number}/'
                f'{_column_letter(columns["our_price"])}{row_number}-1,"")'
            )
            current_row[columns["dynamics"]] = bsr_change
            current_row[columns["stock"]] = product.get("stock_status", "")

        refreshed_rows.append(current_row)
        records.append({
            "row_number": row_number,
            "asin": asin,
            "values": {
                str(header).strip(): current_row[index] if index < len(current_row) else ""
                for index, header in enumerate(headers) if str(header).strip()
            },
        })

    last_column = _column_letter(len(headers) - 1)
    first_data_row = first_data_index + 1
    current_sheet.batch_clear([f"A{first_data_row}:{last_column}{history_title_row}"])
    if refreshed_rows:
        current_sheet.update(f"A{first_data_row}", refreshed_rows, value_input_option="USER_ENTERED")
    clear_matrix_calculation_highlighting(current_sheet)
    mark_failed_asins_in_competitors(current_sheet.spreadsheet, failed_asins, products_by_asin)
    return {"archived_rows": archived_rows, "records": records}


def update_competitor_matrix(sheet, asin, product, snapshot_date):
    """Сохраняет прошлые показатели конкурента и обновляет его строку в матрице."""
    rows = sheet.get_all_values()
    header_row, columns = _find_matrix_columns(rows)
    required = {"asin", "bsr", "price", "difference", "dynamics", "stock", "date", "our_price"}
    missing = required.difference(columns)
    if header_row is None or missing:
        raise ValueError(f"В матрице отсутствуют необходимые столбцы: {', '.join(sorted(missing))}")

    target_row_index = None
    for row_index, row in enumerate(rows[header_row + 1:], start=header_row + 1):
        value = row[columns["asin"]] if len(row) > columns["asin"] else ""
        if extract_asin_from_text(value) == asin:
            target_row_index = row_index
            break
    if target_row_index is None:
        raise ValueError(f"ASIN конкурента {asin} не найден в матрице")

    old_row = rows[target_row_index]

    def old_value(name):
        column = columns[name]
        return old_row[column] if len(old_row) > column else ""

    previous_bsr = old_value("bsr")
    try:
        bsr_change = int(str(product["bsr"]).replace(",", "")) - int(str(previous_bsr).replace(",", ""))
    except (TypeError, ValueError):
        bsr_change = ""

    row_number = target_row_index + 1
    marketplace = (
        old_row[columns["marketplace"]]
        if columns.get("marketplace") is not None and columns["marketplace"] < len(old_row)
        else "US"
    )
    updates = {
        columns["date"]: snapshot_date,
        columns["bsr"]: product["bsr"],
        columns["price"]: product["price"],
        columns["difference"]: (
            f'=IFERROR({_column_letter(columns["price"])}{row_number}/'
            f'{_column_letter(columns["our_price"])}{row_number}-1,"")'
        ),
        columns["dynamics"]: bsr_change,
        columns["stock"]: product.get("stock_status", ""),
    }
    if columns.get("asin") is not None:
        updates[columns["asin"]] = _convert_to_asin_link(old_value("asin"), marketplace)
    if columns.get("our_asin") is not None:
        updates[columns["our_asin"]] = _convert_to_asin_link(old_value("our_asin"), marketplace)

    for column, value in updates.items():
        sheet.update_cell(row_number, column + 1, value)

    difference_cell = f"{_column_letter(columns['difference'])}{row_number}"
    sheet.format(
        difference_cell,
        {"numberFormat": {"type": "PERCENT", "pattern": "0%"}},
    )

    headers = rows[header_row]
    current_row = list(old_row) + [""] * max(0, len(headers) - len(old_row))
    for column, value in updates.items():
        current_row[column] = value

    own_price = clean_number(old_value("our_price"))
    competitor_price = clean_number(product["price"])
    difference = ""
    if own_price not in (None, 0) and competitor_price is not None:
        difference = f"{(competitor_price / own_price - 1):.0%}"
    current_row[columns["difference"]] = difference

    matrix_values = {
        str(header).strip(): current_row[index] if index < len(current_row) else ""
        for index, header in enumerate(headers) if str(header).strip()
    }
    matrix_values.update({
        "comp_bsr_category": product.get("category", ""),
        "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
    })
    return {
        "row_number": row_number,
        "values": matrix_values,
    }


def load_asins_from_config(spreadsheet, fallback_asins):
    """
    Читает список товаров со вкладки Config той же таблицы.
    Если вкладки нет — создаёт её с заголовком и запасным списком.
    """
    competitor_pairs = load_active_competitor_pairs(spreadsheet)
    if competitor_pairs:
        return list(dict.fromkeys(
            asin for pair in competitor_pairs for asin in (pair["our_asin"], pair["comp_asin"])
        ))

    try:
        config_sheet = spreadsheet.worksheet(CONFIG_SHEET_NAME)
    except gspread.WorksheetNotFound:
        config_sheet = spreadsheet.add_worksheet(title=CONFIG_SHEET_NAME, rows=100, cols=2)
        config_sheet.append_row(CONFIG_HEADER)
        for asin in fallback_asins:
            config_sheet.append_row([asin])
        print(f"Создана вкладка '{CONFIG_SHEET_NAME}' с запасным списком товаров.")
        return list(fallback_asins)

    try:
        rows = config_sheet.get_all_values()
    except Exception as e:
        print(f"Не удалось прочитать вкладку '{CONFIG_SHEET_NAME}': {e}. Использую запасной список.")
        return list(fallback_asins)

    history_boundary = next(
        (index for index, row in enumerate(rows) if row and str(row[0]).strip().casefold() == "history"),
        len(rows),
    )
    source_rows = rows[:history_boundary]

    asin_column = None
    header_row = None
    for row_index, row in enumerate(source_rows):
        for column_index, value in enumerate(row):
            if str(value).strip().casefold() in COMPETITOR_ASIN_HEADERS:
                asin_column = column_index
                header_row = row_index
                break
        if asin_column is not None:
            break

    if asin_column is None:
        print(f"Во вкладке '{CONFIG_SHEET_NAME}' не найден столбец 'ASIN конкурента'. Использую запасной список.")
        return list(fallback_asins)

    our_asin_column = next(
        (
            index for index, value in enumerate(source_rows[header_row])
            if str(value).strip().casefold() in {"our_asin", "наш asin", "our asin"}
        ),
        None,
    )
    asin_columns = [asin_column]
    if our_asin_column is not None and our_asin_column not in asin_columns:
        asin_columns.append(our_asin_column)
    raw_values = [
        row[column] if len(row) > column else ""
        for row in source_rows[header_row + 1:]
        for column in asin_columns
    ]

    asins = []
    for raw in raw_values:
        asin = extract_asin_from_text(raw)
        if asin and asin not in asins:
            asins.append(asin)

    if not asins:
        print(f"Вкладка '{CONFIG_SHEET_NAME}' пуста или без валидных ASIN — использую запасной список.")
        return list(fallback_asins)

    return asins


def load_active_competitor_pairs(spreadsheet):
    """Читает активные пары товаров с листа Competitors."""
    try:
        worksheet = spreadsheet.worksheet("Competitors")
        rows = worksheet.get_all_values()
    except gspread.WorksheetNotFound:
        return []
    if not rows:
        return []

    headers = {str(value).strip().casefold(): index for index, value in enumerate(rows[0])}
    required = {"marketplace", "our product", "our asin", "competitor name", "competitor asin"}
    if not required.issubset(headers):
        return []

    pairs = []
    for row in rows[1:]:
        active = row[headers["active"]].strip().upper() if "active" in headers and len(row) > headers["active"] else "Y"
        if active not in {"Y", "YES", "1", "TRUE"}:
            continue

        def value(name):
            return row[headers[name]].strip() if len(row) > headers[name] else ""

        our_asin = extract_asin_from_text(value("our asin"))
        comp_asin = extract_asin_from_text(value("competitor asin"))
        if not our_asin or not comp_asin:
            continue
        pairs.append({
            "marketplace": value("marketplace").upper(),
            "our_product": value("our product"),
            "our_asin": our_asin,
            "competitor": value("competitor name"),
            "comp_asin": comp_asin,
        })
    return pairs


def build_asin_domain_map(spreadsheet) -> Dict[str, str]:
    """
    Строит словарь {ASIN: домен amazon (com/ca/co.uk/...)} на основе поля
    marketplace каждой пары в листе Competitors.

    Нужен для того, чтобы каждый ASIN запрашивался у ScrapingDog на СВОЁМ
    реальном маркетплейсе, а не всегда на amazon.com (как было раньше через
    глобальный config.DOMAIN). ASIN — идентификатор, привязанный к конкретному
    маркетплейсу: если запросить канадский/британский ASIN на amazon.com,
    ScrapingDog может вернуть ошибку или вообще другой товар, хотя на своём
    маркетплейсе (amazon.ca / amazon.co.uk) он существует и валиден.
    """
    domain_map: Dict[str, str] = {}
    for pair in load_active_competitor_pairs(spreadsheet):
        domain = _amazon_domain(pair["marketplace"])
        if pair["our_asin"]:
            domain_map[pair["our_asin"]] = domain
        if pair["comp_asin"]:
            domain_map[pair["comp_asin"]] = domain
    return domain_map


def find_date_column(sheet, date_str):
    try:
        header = sheet.row_values(1)
    except Exception:
        header = []

    normalized = normalize_header(header)
    if not header:
        sheet.append_row(["Region", "Category", "Parent", "Parameter", date_str])
        return 5

    if len(normalized) < 4 or normalized[:4] != ["region", "category", "parent", "parameter"]:
        sheet.update('A1:D1', [["Region", "Category", "Parent", "Parameter"]])

    if date_str in normalized:
        return normalized.index(date_str) + 1

    if "current" in normalized:
        return normalized.index("current") + 1

    new_col = len(header) + 1
    sheet.update_cell(1, new_col, date_str)
    return new_col


def find_asin_block_start(sheet, asin):
    try:
        parent_values = sheet.col_values(3)
    except Exception:
        parent_values = []

    if parent_values and parent_values[0].strip().lower() == "parent":
        parent_values = parent_values[1:]
        base_row = 2
    else:
        base_row = 1

    for idx, value in enumerate(parent_values):
        if extract_asin_from_text(value) == asin:
            return base_row + idx
    return None


def write_asin_block(sheet, start_row, date_col, region, category, parent_formula,
                      title, price, stars, bsr, reviews):
    if start_row is None:
        empty_cells = [""] * (date_col - 5)
        rows = [
            [region, category, parent_formula, "Title"] + empty_cells + [title],
            ["", "", "", "Price"] + empty_cells + [price],
            ["", "", "", "Rating"] + empty_cells + [stars],
            ["", "", "", "BSR"] + empty_cells + [bsr],
            ["", "", "", "Number of Reviews"] + empty_cells + [reviews],
        ]
        sheet.append_rows(rows, value_input_option='USER_ENTERED')
        return

    updates = [
        (start_row, date_col, title),
        (start_row + 1, date_col, price),
        (start_row + 2, date_col, stars),
        (start_row + 3, date_col, bsr),
        (start_row + 4, date_col, reviews),
    ]
    for row, col, value in updates:
        try:
            sheet.update_cell(row, col, value)
        except Exception as e:
            print(f"Ошибка обновления строки {row}, столбец {col}: {e}")


def append_history_snapshot(sheet, date_str, asin, title, price, rating, bsr, reviews):
    """Добавляет ежедневный снимок товара на лист History."""
    history_sheet = getattr(sheet, "history_sheet", None) or sheet

    try:
        values = history_sheet.get_all_values()
    except Exception as exc:
        print(f"Не удалось прочитать лист History: {exc}")
        return False

    if not values:
        history_sheet.append_row(["Date", "ASIN", "Title", "Price", "Rating", "BSR", "Reviews"])

    history_sheet.append_row([date_str, asin, title, price, rating, bsr, reviews])
    return True


def add_asin_to_config(spreadsheet, asin):
    try:
        config_sheet = spreadsheet.worksheet(CONFIG_SHEET_NAME)
    except gspread.WorksheetNotFound:
        config_sheet = spreadsheet.add_worksheet(title=CONFIG_SHEET_NAME, rows=100, cols=2)
        config_sheet.append_row(CONFIG_HEADER)

    existing = [value.strip() for value in config_sheet.col_values(1)[1:] if value and str(value).strip()]
    if asin in existing:
        return False

    config_sheet.append_row([asin])
    return True


def load_products_from_sheet(sheet):
    """Читает последние значения из таблицы и возвращает список товаров для дашборда."""
    try:
        values = sheet.get_all_values()
    except Exception as exc:
        print(f"Не удалось прочитать Google Sheets: {exc}")
        return []

    if not values:
        return []

    headers = values[0]
    if len(headers) < 5:
        return []

    products = {}
    date_columns = [idx for idx in range(4, len(headers)) if headers[idx].strip()]

    for row_idx, row in enumerate(values[1:], start=1):
        if len(row) < 4:
            continue

        if str(row[3]).strip().lower() != "title":
            continue

        asin = extract_asin_from_text(row[2])
        if not asin:
            continue

        if row_idx + 4 >= len(values):
            continue

        price_row = values[row_idx + 1] if row_idx + 1 < len(values) else []
        rating_row = values[row_idx + 2] if row_idx + 2 < len(values) else []
        bsr_row = values[row_idx + 3] if row_idx + 3 < len(values) else []
        reviews_row = values[row_idx + 4] if row_idx + 4 < len(values) else []

        product = {
            "asin": asin,
            "title": row[4] if len(row) > 4 else "",
            "category": row[1] if len(row) > 1 else "",
            "brand": "",
            "price": "",
            "rating": "",
            "reviews": "",
            "bsr": "",
            "history": [],
        }

        if asin not in products:
            products[asin] = product

        latest_col = max(date_columns) if date_columns else 4
        latest_value = row[latest_col] if len(row) > latest_col else ""
        if latest_value:
            product["title"] = latest_value

        for col_idx in date_columns:
            if len(row) <= col_idx:
                continue
            title_value = row[col_idx] if len(row) > col_idx else ""
            price_value = price_row[col_idx] if len(price_row) > col_idx else ""
            rating_value = rating_row[col_idx] if len(rating_row) > col_idx else ""
            reviews_value = reviews_row[col_idx] if len(reviews_row) > col_idx else ""
            bsr_value = bsr_row[col_idx] if len(bsr_row) > col_idx else ""
            if title_value or price_value or rating_value or reviews_value or bsr_value:
                product["history"].append(
                    {
                        "date": headers[col_idx],
                        "price": price_value,
                        "rating": rating_value,
                        "reviews": reviews_value,
                        "bsr": bsr_value,
                    }
                )

        if date_columns:
            latest_col = date_columns[-1]
            product["price"] = price_row[latest_col] if len(price_row) > latest_col else ""
            product["rating"] = rating_row[latest_col] if len(rating_row) > latest_col else ""
            product["reviews"] = reviews_row[latest_col] if len(reviews_row) > latest_col else ""
            product["bsr"] = bsr_row[latest_col] if len(bsr_row) > latest_col else ""

    return list(products.values())


def apply_conditional_formatting(sheet, key_file):
    try:
        from googleapiclient.discovery import build
        from google.oauth2.service_account import Credentials
    except Exception:
        print("Условное форматирование требует google-api-python-client и google-auth. Пропускаю.")
        return

    try:
        creds = Credentials.from_service_account_file(
            key_file, scopes=["https://www.googleapis.com/auth/spreadsheets"]
        )
        service = build('sheets', 'v4', credentials=creds)
        spreadsheet_id = sheet.spreadsheet.id
        sheet_id = int(sheet._properties.get('sheetId'))
        row_count = int(sheet._properties.get('gridProperties', {}).get('rowCount', 1000))

        requests_body = {
            "requests": [
                {
                    "repeatCell": {
                        "range": {
                            "sheetId": sheet_id,
                            "startRowIndex": 0,
                            "endRowIndex": 1,
                            "startColumnIndex": 0,
                            "endColumnIndex": 30,
                        },
                        "cell": {
                            "userEnteredFormat": {
                                "backgroundColor": {"red": 0.94, "green": 0.94, "blue": 0.94},
                                "textFormat": {"bold": True}
                            }
                        },
                        "fields": "userEnteredFormat(backgroundColor,textFormat)"
                    }
                },
                {
                    "updateSheetProperties": {
                        "properties": {"sheetId": sheet_id, "gridProperties": {"frozenRowCount": 1}},
                        "fields": "gridProperties.frozenRowCount"
                    }
                },
                {
                    "autoResizeDimensions": {
                        "dimensions": {"sheetId": sheet_id, "dimension": "COLUMNS", "startIndex": 0, "endIndex": 30}
                    }
                },
                {
                    "addConditionalFormatRule": {
                        "rule": {
                            "ranges": [
                                {
                                    "sheetId": sheet_id,
                                    "startRowIndex": 1,
                                    "endRowIndex": row_count,
                                    "startColumnIndex": 4,
                                    "endColumnIndex": 30,
                                }
                            ],
                            "booleanRule": {
                                "condition": {
                                    "type": "CUSTOM_FORMULA",
                                    "values": [{"userEnteredValue": "=AND($D2=\"Rating\", $E2>=4.5)"}]
                                },
                                "format": {"backgroundColor": {"red": 0.78, "green": 1.0, "blue": 0.78}}
                            }
                        },
                        "index": 0
                    }
                },
                {
                    "addConditionalFormatRule": {
                        "rule": {
                            "ranges": [
                                {
                                    "sheetId": sheet_id,
                                    "startRowIndex": 1,
                                    "endRowIndex": row_count,
                                    "startColumnIndex": 4,
                                    "endColumnIndex": 30,
                                }
                            ],
                            "booleanRule": {
                                "condition": {
                                    "type": "CUSTOM_FORMULA",
                                    "values": [{"userEnteredValue": "=AND($D2=\"Rating\", $E2>=4.0, $E2<4.5)"}]
                                },
                                "format": {"backgroundColor": {"red": 1.0, "green": 0.96, "blue": 0.58}}
                            }
                        },
                        "index": 1
                    }
                }
            ]
        }
        service.spreadsheets().batchUpdate(spreadsheetId=spreadsheet_id, body=requests_body).execute()
        print("Оформление листа применено, включая выделение строк рейтинга.")
    except Exception as e:
        print(f"Не удалось применить авторасширение столбцов: {e}")