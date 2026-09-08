"""Делает ASIN во всех блоках отдельного листа History кликабельными."""

import gspread

from config import KEY_FILE_CANDIDATES, SHEET_NAME
from sheets import _amazon_domain
from utils import extract_asin_from_text, find_key_file


def main():
    from pathlib import Path

    base_dir = Path(__file__).parent
    key_file = find_key_file(base_dir)
    if not key_file:
        print(f"Не найден файл ключа Google service account (искал: {KEY_FILE_CANDIDATES}).")
        return

    client = gspread.service_account(filename=key_file)
    sheet = client.open(SHEET_NAME).worksheet("History")
    rows = sheet.get_all_values()
    updates = []

    for header_index, header_row in enumerate(rows):
        headers = [str(value).strip() for value in header_row]
        if "our_asin" not in headers or "comp_asin" not in headers:
            continue
        columns = {header: index for index, header in enumerate(headers)}
        asin_columns = (columns["our_asin"], columns["comp_asin"])

        for data_index in range(header_index + 1, len(rows)):
            row = rows[data_index]
            first_value = str(row[0]).strip().casefold() if row else ""
            if not row or first_value in {"current", "history"}:
                break
            marketplace = row[columns["marketplace"]].strip().upper() if "marketplace" in columns else ""
            # Раньше здесь была своя карта только на 3 маркетплейса (US/CA/UK) —
            # для DE/FR/ES/IT/MX/JP/AU ссылка молча уходила на amazon.com,
            # то есть вела не на тот домен, где реально лежит товар. Теперь
            # используется та же _amazon_domain(), что и весь остальной код
            # (10 маркетплейсов) — единая логика в одном месте.
            domain = _amazon_domain(marketplace)
            for column in asin_columns:
                asin = extract_asin_from_text(row[column] if len(row) > column else "")
                if asin:
                    letter = chr(65 + column)
                    updates.append({
                        "range": f"{letter}{data_index + 1}",
                        "values": [[f'=HYPERLINK("https://www.amazon.{domain}/dp/{asin}", "{asin}")']],
                    })

    if updates:
        sheet.batch_update(updates, value_input_option="USER_ENTERED")
    print(f"Обновлено ссылок: {len(updates)}")


if __name__ == "__main__":
    main()
