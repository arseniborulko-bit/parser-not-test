"""Read-only Streamlit dashboard for BSR_Competitors_Tracker.

This module deliberately has no imports from the parser. It only uses the
Google Sheets read-only API, so viewing this page cannot start ScrapingDog
requests or change any spreadsheet cell.
"""

from __future__ import annotations

import json
from datetime import datetime
from html import escape

import gspread
import pandas as pd
import streamlit as st
from google.oauth2.service_account import Credentials


DEFAULT_SPREADSHEET_NAME = "BSR_Competitors_Tracker"
READONLY_SCOPES = (
    "https://www.googleapis.com/auth/spreadsheets.readonly",
    "https://www.googleapis.com/auth/drive.readonly",
)


st.set_page_config(page_title="Amazon Parser Dashboard", page_icon="📦", layout="wide")


def _apply_design() -> None:
    """Visual language inspired by the supplied reference, built with Streamlit."""
    st.markdown(
        """
        <style>
        .stApp { background: #f6f7fb; color: #121826; }
        .block-container { max-width: 1320px; padding-top: 2.25rem; padding-bottom: 3rem; }
        [data-testid="stSidebar"] { background: #ffffff; }
        .brand { font-size: 2.35rem; font-weight: 800; letter-spacing: -0.045em; margin: 0; }
        .brand-subtitle { color: #64748b; font-size: .87rem; margin: .25rem 0 1.5rem; }
        .status-box { background: #dbeafe; color: #2563eb; border-radius: 10px; padding: 1rem 1.1rem; min-height: 80px; }
        .status-box strong { color: #1d4ed8; }
        .metric-card { background: #ffffff; border: 1px solid #e5e7eb; border-radius: 15px; padding: 1rem 1.15rem; min-height: 120px; box-shadow: 0 1px 2px rgba(15,23,42,.025); }
        .metric-label { color: #64748b; font-size: .72rem; letter-spacing: .065em; text-transform: uppercase; }
        .metric-value { color: #111827; font-size: 1.85rem; font-weight: 800; line-height: 1.25; margin: .25rem 0; }
        .metric-detail { color: #64748b; font-size: .78rem; }
        .section-title { font-size: 1.55rem; font-weight: 750; margin: 1.75rem 0 .2rem; }
        .section-note { color: #64748b; font-size: .86rem; margin-bottom: .6rem; }
        div[data-testid="stSelectbox"] label, div[data-testid="stTextInput"] label { color: #64748b; font-size: .78rem; }
        div[data-testid="stSelectbox"] div[data-baseweb="select"] > div { background: #252832; color: #fff; border-radius: 8px; border: 0; }
        div[data-testid="stTabs"] [data-baseweb="tab-list"] { gap: 0; border-bottom: 1px solid #dfe3ea; }
        div[data-testid="stTabs"] button { background: #171923; color: #fff; border-radius: 7px 7px 0 0; padding: .6rem 1rem; margin-right: 2px; }
        div[data-testid="stTabs"] button[aria-selected="true"] { background: #ff5b61; color: #fff; }
        div[data-testid="stTabs"] [data-baseweb="tab-highlight"] { background: #ff5b61; }
        .stButton > button { background: #161925; color: #fff; border: 0; border-radius: 8px; font-weight: 650; }
        .stButton > button:hover { background: #2b3040; color: #fff; }
        </style>
        """,
        unsafe_allow_html=True,
    )


def _unique_headers(row: list[str]) -> list[str]:
    """Make usable, distinct DataFrame column names without dropping columns."""
    result: list[str] = []
    used: dict[str, int] = {}
    for index, value in enumerate(row, start=1):
        base = str(value).strip() or f"Column {index}"
        used[base] = used.get(base, 0) + 1
        result.append(base if used[base] == 1 else f"{base} ({used[base]})")
    return result


def _values_to_frame(values: list[list[str]]) -> pd.DataFrame:
    if not values:
        return pd.DataFrame()
    headers = _unique_headers(values[0])
    rows = [row + [""] * (len(headers) - len(row)) for row in values[1:]]
    return pd.DataFrame([row[: len(headers)] for row in rows], columns=headers).dropna(how="all")


@st.cache_data(ttl=300, show_spinner=False)
def _load_sheet(service_account_json: str, spreadsheet_name: str, worksheet_name: str) -> pd.DataFrame:
    """Read one worksheet. This code has no write/update/create operations."""
    credentials = Credentials.from_service_account_info(json.loads(service_account_json), scopes=READONLY_SCOPES)
    client = gspread.authorize(credentials)
    return _values_to_frame(client.open(spreadsheet_name).worksheet(worksheet_name).get_all_values())


@st.cache_data(ttl=300, show_spinner=False)
def _worksheet_names(service_account_json: str, spreadsheet_name: str) -> list[str]:
    credentials = Credentials.from_service_account_info(json.loads(service_account_json), scopes=READONLY_SCOPES)
    client = gspread.authorize(credentials)
    return [worksheet.title for worksheet in client.open(spreadsheet_name).worksheets()]


def _secret_values() -> tuple[str, str]:
    if "gcp_service_account" not in st.secrets:
        raise KeyError("Не найден раздел [gcp_service_account] в Streamlit Secrets.")
    account = dict(st.secrets["gcp_service_account"])
    dashboard = dict(st.secrets.get("dashboard", {}))
    name = str(dashboard.get("spreadsheet_name", DEFAULT_SPREADSHEET_NAME)).strip()
    return json.dumps(account), name


def _find_column(columns: list[str], words: tuple[str, ...]) -> str | None:
    for column in columns:
        if all(word in column.casefold() for word in words):
            return column
    return None


def _metric_card(label: str, value: str | int, detail: str, color: str = "#111827") -> str:
    return (
        '<div class="metric-card">'
        f'<div class="metric-label">{escape(label)}</div>'
        f'<div class="metric-value" style="color:{color}">{escape(str(value))}</div>'
        f'<div class="metric-detail">{escape(detail)}</div></div>'
    )


def _filter_data(data: pd.DataFrame) -> pd.DataFrame:
    """Top-level read-only filters, using whichever matching columns exist."""
    columns = list(data.columns)
    marketplace_column = _find_column(columns, ("маркет",)) or _find_column(columns, ("market",))
    asin_column = _find_column(columns, ("наш", "asin")) or _find_column(columns, ("our", "asin"))
    date_column = _find_column(columns, ("дата",)) or _find_column(columns, ("date",))
    filter_columns = st.columns(4)
    with filter_columns[0]:
        asins = ["Все"]
        if asin_column:
            asins += sorted(value for value in data[asin_column].dropna().astype(str).unique() if value)
        asin = st.selectbox("ASIN", asins)
    with filter_columns[1]:
        search = st.text_input("Поиск", placeholder="ASIN, товар, бренд…")
    with filter_columns[2]:
        period = st.selectbox("Период", ["Всё время", "7 дней", "30 дней", "90 дней"])
    with filter_columns[3]:
        markets = ["Все"]
        if marketplace_column:
            markets += sorted(value for value in data[marketplace_column].dropna().astype(str).unique() if value)
        market = st.selectbox("Маркетплейс", markets)

    result = data.copy()
    if asin != "Все" and asin_column:
        result = result[result[asin_column].astype(str) == asin]
    if market != "Все" and marketplace_column:
        result = result[result[marketplace_column].astype(str) == market]
    if search.strip():
        contains = result.astype(str).apply(lambda column: column.str.contains(search, case=False, na=False))
        result = result[contains.any(axis=1)]
    if period != "Всё время" and date_column:
        dates = pd.to_datetime(result[date_column], errors="coerce")
        days = int(period.split()[0])
        result = result[dates >= (datetime.now() - pd.Timedelta(days=days))]
    if asin_column:
        st.caption(f"Уникальных ASIN в выборке: {result[asin_column].nunique()}")
    return result


def _render_overview(data: pd.DataFrame) -> None:
    columns = list(data.columns)
    date_column = _find_column(list(data.columns), ("дата",)) or _find_column(list(data.columns), ("date",))
    our_asin_column = _find_column(columns, ("наш", "asin")) or _find_column(columns, ("our", "asin"))
    competitor_asin_column = _find_column(columns, ("asin", "конкур")) or _find_column(columns, ("competitor", "asin"))
    marketplace_column = _find_column(columns, ("маркет",)) or _find_column(columns, ("market",))
    latest = "—"
    if date_column and not data.empty:
        dates = pd.to_datetime(data[date_column], errors="coerce")
        if dates.notna().any():
            latest = dates.max().strftime("%d.%m.%Y")
    cards = st.columns(5)
    values = [
        _metric_card("Всего записей", f"{len(data):,}".replace(",", " "), "в выбранном срезе"),
        _metric_card("Наших ASIN", data[our_asin_column].nunique() if our_asin_column else "—", "уникальных товаров", "#168a50"),
        _metric_card("Конкурентов", data[competitor_asin_column].nunique() if competitor_asin_column else "—", "уникальных ASIN", "#d97706"),
        _metric_card("Стран", data[marketplace_column].nunique() if marketplace_column else "—", "маркетплейсов"),
        _metric_card("Последняя дата", latest, "дата в таблице", "#2563eb"),
    ]
    for column, card in zip(cards, values):
        column.markdown(card, unsafe_allow_html=True)

    if date_column and not data.empty:
        chart_data = data.copy()
        chart_data[date_column] = pd.to_datetime(chart_data[date_column], errors="coerce")
        chart_data = chart_data.dropna(subset=[date_column])
        if not chart_data.empty:
            st.caption("Количество записей по датам")
            st.bar_chart(chart_data.groupby(date_column).size())


def main() -> None:
    _apply_design()
    left, right = st.columns([3, 2])
    with left:
        st.markdown('<p class="brand">📡 Rating Radar</p>', unsafe_allow_html=True)
        st.markdown('<p class="brand-subtitle">Мониторинг Amazon-конкурентов и аналитика портфеля</p>', unsafe_allow_html=True)
    with right:
        st.markdown('<div class="status-box"><strong>Режим просмотра</strong><br>Google Sheets читается безопасно. Парсер и ScrapingDog не запускаются.</div>', unsafe_allow_html=True)
    try:
        account_json, spreadsheet_name = _secret_values()
        sheet_names = _worksheet_names(account_json, spreadsheet_name)
    except Exception as exc:
        st.error("Не удалось подключиться к Google Sheets.")
        st.code(str(exc))
        st.info("Добавьте [gcp_service_account] в Streamlit Secrets и откройте доступ к таблице для client_email сервисного аккаунта.")
        return

    if not sheet_names:
        st.warning("В таблице нет листов для отображения.")
        return

    preferred = [name for name in ("Current", "History", "Competitors", "Матрица") if name in sheet_names]
    selected_sheet = st.selectbox("Источник данных", sheet_names, index=sheet_names.index(preferred[0]) if preferred else 0)
    if st.button("↻ Обновить данные"):
        _load_sheet.clear()
        _worksheet_names.clear()
        st.rerun()

    try:
        data = _load_sheet(account_json, spreadsheet_name, selected_sheet)
    except Exception as exc:
        st.error(f"Не удалось прочитать лист «{selected_sheet}».")
        st.code(str(exc))
        return

    if data.empty:
        st.info(f"Лист «{selected_sheet}» пуст или содержит только заголовки.")
        return

    _render_overview(data)
    st.markdown('<p class="section-title">Мониторинг конкурентов</p>', unsafe_allow_html=True)
    st.markdown('<p class="section-note">Фильтруйте сохранённые данные по ASIN, стране и периоду. Никаких запросов к Amazon не выполняется.</p>', unsafe_allow_html=True)
    shown = _filter_data(data)
    overview_tab, chart_tab, table_tab = st.tabs(["📋 Обзор", "📈 Динамика", "🧾 Данные"])
    with overview_tab:
        st.caption(f"Показано строк: {len(shown)} из {len(data)}")
        st.dataframe(shown, use_container_width=True, hide_index=True, height=450)
    with chart_tab:
        date_column = _find_column(list(shown.columns), ("дата",)) or _find_column(list(shown.columns), ("date",))
        if date_column:
            series = pd.to_datetime(shown[date_column], errors="coerce").dropna().value_counts().sort_index()
            if not series.empty:
                st.line_chart(series)
            else:
                st.info("Для выбранных строк нет корректных дат.")
        else:
            st.info("В этом листе нет столбца даты для построения динамики.")
    with table_tab:
        st.download_button("Скачать отображаемые данные CSV", shown.to_csv(index=False).encode("utf-8-sig"), file_name=f"{selected_sheet}.csv", mime="text/csv")
        st.dataframe(shown, use_container_width=True, hide_index=True)


if __name__ == "__main__":
    main()

