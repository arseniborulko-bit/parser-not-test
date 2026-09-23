"""
SaaS-дашборд, читающий данные из Postgres (schema bsr_radar), а не из
Google Sheets API напрямую. Данные в базу попадают через sync_sheets_to_db.py.

Пока читает из базы каждый раз при обновлении (без записи куда-либо) — кнопка
запуска парсера и панель расписания добавляются отдельными следующими шагами.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime
from html import escape
from pathlib import Path
from typing import Iterable

import pandas as pd
import psycopg2
import streamlit as st
from dotenv import load_dotenv

import access
import collect_ui
import github_dispatch
import pairs_store
import pairs_ui
import run_control
import schedule_store

load_dotenv()


def _database_url() -> str:
    """Локально приходит из .env (os.environ). На Streamlit Cloud секреты
    доступны через st.secrets и не всегда автоматически попадают в
    os.environ — проверяем оба места, чтобы деплой не падал молча."""
    value = os.environ.get("DATABASE_URL")
    if value:
        return value
    try:
        value = st.secrets.get("DATABASE_URL")
    except Exception:
        value = None
    if not value:
        raise RuntimeError("DATABASE_URL не найден ни в переменных окружения, ни в st.secrets.")
    return value


def _connect():
    return psycopg2.connect(_database_url())


def _secret(name: str) -> str:
    value = os.environ.get(name)
    if value:
        return value
    try:
        return str(st.secrets.get(name) or "")
    except Exception:
        return ""


def _max_active() -> int:
    raw = _secret("MAX_ACTIVE_PAIRS").strip()
    return int(raw) if raw.isdigit() and int(raw) > 0 else pairs_store.DEFAULT_MAX_ACTIVE


_AUTH_KEYS = ("client_id", "client_secret", "cookie_secret", "redirect_uri", "server_metadata_url")
_ROLE_LABELS = {access.ROLE_ADMIN: "админ", access.ROLE_EDITOR: "редактор"}


def _auth_configured() -> bool:
    """Без полной секции [auth] в секретах вход выключен, дашборд остаётся открытым для просмотра."""
    try:
        auth = st.secrets.get("auth")
        return all(auth.get(key) for key in _AUTH_KEYS)
    except Exception:
        return False


def _current_user() -> dict:
    if not _auth_configured():
        return {}
    try:
        return st.user.to_dict()
    except Exception:
        return {}


def _resolve_access() -> tuple[dict, str | None, str | None]:
    """(данные пользователя, подтверждённый email, роль); роль None — управлять нельзя."""
    user = _current_user()
    email = access.verified_email(user)
    if email is None:
        return user, None, None
    admin_emails = access.parse_email_list(_secret("ADMIN_EMAILS"))
    db_roles: dict = {}
    if email not in admin_emails:
        try:
            db_roles = access.active_user_roles(_connect)
        except access.AccessStoreError as exc:
            st.warning(f"{exc} Доступ к управлению временно закрыт.")
    return user, email, access.resolve_role(user, admin_emails, db_roles)


PROJECT_DIR = Path(__file__).resolve().parent
STATUS_FILE = PROJECT_DIR / "run_status.json"
RUNNER_SCRIPT = PROJECT_DIR / "run_parser_and_sync.py"

AMAZON_DOMAINS = {
    "US": "com", "CA": "ca", "UK": "co.uk", "DE": "de", "FR": "fr",
    "ES": "es", "IT": "it", "MX": "com.mx", "JP": "co.jp", "AU": "com.au",
}

st.set_page_config(page_title="BSR Radar — мониторинг конкурентов", page_icon="📦", layout="wide")


def _apply_design() -> None:
    st.markdown(
        """
        <style>
        .stApp { background: #f6f7fb; color: #121826; }
        .block-container { max-width: 1320px; padding-top: 2.25rem; padding-bottom: 3rem; }
        [data-testid="stSidebar"] { background: #ffffff; }
        .brand { font-size: 2.35rem; font-weight: 800; letter-spacing: -0.045em; margin: 0; }
        .brand-title { font-size: 1.9rem; font-weight: 800; color: #111827; margin: 0; line-height: 1.2; }
        .brand-subtitle { color: #64748b; font-size: .87rem; margin: .25rem 0 1.5rem; }
        .status-box { background: #dbeafe; color: #2563eb; border-radius: 10px; padding: 1rem 1.1rem; }
        .status-box strong { color: #1d4ed8; }
        .metric-card { background: #ffffff; border: 1px solid #e5e7eb; border-radius: 15px; padding: 1rem 1.15rem; min-height: 120px; box-shadow: 0 1px 2px rgba(15,23,42,.025); }
        .metric-label { color: #64748b; font-size: .72rem; letter-spacing: .065em; text-transform: uppercase; }
        .metric-value { color: #111827; font-size: 1.85rem; font-weight: 800; line-height: 1.25; margin: .25rem 0; }
        .metric-detail { color: #64748b; font-size: .78rem; }
        .section-title { font-size: 1.55rem; font-weight: 750; margin: 1.75rem 0 .2rem; }
        .section-note { color: #64748b; font-size: .86rem; margin-bottom: .6rem; }
        /* Вкладки выбираются по role: в новых версиях Streamlit это уже не <button> и не baseweb. */
        div[data-testid="stTabs"] [role="tablist"] { gap: 3px; border-bottom: 1px solid #dfe3ea; }
        /* Только сами заголовки вкладок. Раньше здесь был ещё и просто "button", из-за чего
        оформление вкладки доставалось каждой кнопке ВНУТРИ вкладки — в том числе кнопке-подсказке
        «?» у полей формы: она становилась тёмным квадратом с белым значком внутри. */
        div[data-testid="stTabs"] [role="tab"] { background: #121826 !important; color: #fff !important; border-radius: 7px 7px 0 0; padding: .6rem 1rem; margin-right: 2px; opacity: 1 !important; }
        div[data-testid="stTabs"] [role="tab"] * { color: #fff !important; opacity: 1 !important; }
        div[data-testid="stTabs"] [role="tab"][aria-selected="true"] { background: #168ed0 !important; color: #fff !important; }
        /* Своя полоска-подчёркивание Streamlit позиционируется скриптом по ширине вкладки, а мы
        меняем вкладкам padding/margin — из-за этого она отставала на шаг и подчёркивала предыдущую
        вкладку. Активную вкладку и так видно по синей заливке, поэтому полоску убираем. */
        div[data-testid="stTabs"] [data-baseweb="tab-highlight"],
        div[data-testid="stTabs"] [data-baseweb="tab-border"] { display: none !important; }
        div[data-testid="stTabs"] button:disabled { opacity: .45 !important; cursor: not-allowed; }
        .stButton > button { background: #168ed0; color: #fff; border: 0; border-radius: 8px; font-weight: 650; }
        .stButton > button:hover { background: #075b9b; color: #fff; }
        /* Скрывает «Fork»/GitHub/Stop в верхнем правом углу — они часть нашего приложения,
        их видно даже при toolbarMode=minimal. Красный значок Streamlit Cloud снизу справа
        (тариф хостинга) отсюда не достать — он рисуется снаружи, самим Streamlit Cloud. */
        [data-testid="stToolbar"] { display: none !important; }
        </style>
        """,
        unsafe_allow_html=True,
    )


@st.cache_data(ttl=60, show_spinner=False)
def load_snapshots() -> pd.DataFrame:
    conn = psycopg2.connect(_database_url())
    try:
        return pd.read_sql(
            """
            SELECT snapshot_date, marketplace, currency, our_asin, our_product, our_price,
                   our_bsr, our_bsr_delta_24h, comp_asin, competitor_name, comp_price,
                   comp_bsr, comp_bsr_delta_24h, comp_stock, price_diff_pct, updated_at
            FROM bsr_radar.snapshots
            ORDER BY snapshot_date DESC
            """,
            conn,
        )
    finally:
        conn.close()


@st.cache_data(ttl=60, show_spinner=False)
def load_current() -> pd.DataFrame:
    """Последний снимок каждой АКТИВНОЙ пары (our_asin, comp_asin): убранная пара из текущего состояния исчезает.
    our_image_url/comp_image_url — необязательные столбцы (миграция 004); если их в базе ещё нет, запрос
    повторяется без них, а колонки в результате получаются пустыми, вместо того чтобы ломать всю вкладку."""
    conn = psycopg2.connect(_database_url())
    try:
        try:
            return pd.read_sql(
                """
                SELECT DISTINCT ON (s.our_asin, s.comp_asin)
                       s.snapshot_date, s.marketplace, s.currency, s.our_asin, s.our_product, s.our_price,
                       s.our_bsr, s.our_bsr_delta_24h, s.comp_asin, s.competitor_name, s.comp_price,
                       s.comp_bsr, s.comp_bsr_delta_24h, s.comp_stock, s.price_diff_pct, s.updated_at,
                       s.our_image_url, s.comp_image_url
                FROM bsr_radar.snapshots s
                JOIN bsr_radar.competitor_pairs p
                  ON p.our_asin = s.our_asin AND p.comp_asin = s.comp_asin AND p.active
                ORDER BY s.our_asin, s.comp_asin, s.snapshot_date DESC
                """,
                conn,
            )
        except psycopg2.errors.UndefinedColumn:
            conn.rollback()
            data = pd.read_sql(
                """
                SELECT DISTINCT ON (s.our_asin, s.comp_asin)
                       s.snapshot_date, s.marketplace, s.currency, s.our_asin, s.our_product, s.our_price,
                       s.our_bsr, s.our_bsr_delta_24h, s.comp_asin, s.competitor_name, s.comp_price,
                       s.comp_bsr, s.comp_bsr_delta_24h, s.comp_stock, s.price_diff_pct, s.updated_at
                FROM bsr_radar.snapshots s
                JOIN bsr_radar.competitor_pairs p
                  ON p.our_asin = s.our_asin AND p.comp_asin = s.comp_asin AND p.active
                ORDER BY s.our_asin, s.comp_asin, s.snapshot_date DESC
                """,
                conn,
            )
            data["our_image_url"] = ""
            data["comp_image_url"] = ""
            return data
    finally:
        conn.close()


@st.cache_data(ttl=60, show_spinner=False)
def load_competitor_pairs() -> pd.DataFrame:
    conn = psycopg2.connect(_database_url())
    try:
        return pd.read_sql(
            """
            SELECT p.marketplace, p.our_asin,
                   COALESCE(NULLIF(p.our_product, ''), s.our_product, '') AS our_product,
                   p.comp_asin,
                   COALESCE(NULLIF(p.competitor_name, ''), s.competitor_name, '') AS competitor_name,
                   p.active
            FROM bsr_radar.competitor_pairs p
            LEFT JOIN LATERAL (
                SELECT our_product, competitor_name FROM bsr_radar.snapshots
                WHERE our_asin = p.our_asin AND comp_asin = p.comp_asin
                  AND marketplace = p.marketplace  -- иначе название подтянется с чужого рынка
                ORDER BY snapshot_date DESC LIMIT 1
            ) s ON TRUE
            ORDER BY p.marketplace, p.our_asin, p.comp_asin
            """,
            conn,
        )
    finally:
        conn.close()


def _metric_card(label: str, value: object, detail: str, color: str = "#111827") -> str:
    return (
        '<div class="metric-card">'
        f'<div class="metric-label">{escape(label)}</div>'
        f'<div class="metric-value" style="color:{color}">{escape(str(value))}</div>'
        f'<div class="metric-detail">{escape(detail)}</div></div>'
    )


def _amazon_url(asin: object, marketplace: object) -> str:
    if not isinstance(asin, str) or not asin:
        return ""
    domain = AMAZON_DOMAINS.get(str(marketplace).strip().upper(), "com")
    return f"https://www.amazon.{domain}/dp/{asin}"


def _render_overview(data: pd.DataFrame) -> None:
    latest = "—"
    if not data.empty:
        dates = pd.to_datetime(data["snapshot_date"], errors="coerce")
        if dates.notna().any():
            latest = dates.max().strftime("%d.%m.%Y")
    cards = st.columns(5)
    values = [
        _metric_card("Всего записей", f"{len(data):,}".replace(",", " "), "в выбранном срезе"),
        _metric_card("Наших ASIN", data["our_asin"].nunique() if "our_asin" in data else "—", "уникальных товаров", "#168a50"),
        _metric_card("Конкурентов", data["comp_asin"].nunique() if "comp_asin" in data else "—", "уникальных ASIN", "#d97706"),
        _metric_card("Стран", data["marketplace"].nunique() if "marketplace" in data else "—", "маркетплейсов"),
        _metric_card("Последняя дата", latest, "дата в базе", "#2563eb"),
    ]
    for column, card in zip(cards, values):
        column.markdown(card, unsafe_allow_html=True)


@dataclass(frozen=True)
class FilterChoice:
    """Один выбор фильтров на весь дашборд. Раньше фильтры жили внутри каждой вкладки и рисовались
    ПОСЛЕ карточек-итогов, поэтому карточки физически не могли их учесть и всегда показывали всю
    базу, хотя подписаны были «в выбранном срезе». Заодно выбор больше не теряется при переходе
    между вкладками «Текущее состояние» и «История»."""

    asin: str = "Все"
    search: str = ""
    period: str = "Всё время"
    market: str = "Все"


def _options(frames: Iterable[pd.DataFrame], column: str) -> list[str]:
    values: set[str] = set()
    for frame in frames:
        if column in frame:
            values.update(frame[column].dropna().unique())
    return ["Все"] + sorted(values)


def _filter_controls(frames: Iterable[pd.DataFrame], key_prefix: str = "global") -> FilterChoice:
    frames = list(frames)
    filter_columns = st.columns(4)
    with filter_columns[0]:
        asin = st.selectbox("ASIN", _options(frames, "our_asin"), key=f"{key_prefix}_asin")
    with filter_columns[1]:
        search = st.text_input("Поиск", placeholder="ASIN, товар, бренд…", key=f"{key_prefix}_search")
    with filter_columns[2]:
        period = st.selectbox("Период", ["Всё время", "7 дней", "30 дней", "90 дней"], key=f"{key_prefix}_period")
    with filter_columns[3]:
        market = st.selectbox("Маркетплейс", _options(frames, "marketplace"), key=f"{key_prefix}_market")
    return FilterChoice(asin=asin, search=search, period=period, market=market)


def _apply_filter(data: pd.DataFrame, choice: FilterChoice) -> pd.DataFrame:
    result = data.copy()
    if choice.asin != "Все" and "our_asin" in result:
        result = result[result["our_asin"] == choice.asin]
    if choice.market != "Все" and "marketplace" in result:
        result = result[result["marketplace"] == choice.market]
    if choice.search.strip():
        contains = result.astype(str).apply(lambda column: column.str.contains(choice.search, case=False, na=False))
        result = result[contains.any(axis=1)]
    if choice.period != "Всё время" and "snapshot_date" in result:
        dates = pd.to_datetime(result["snapshot_date"], errors="coerce")
        days = int(choice.period.split()[0])
        result = result[dates >= (datetime.now() - pd.Timedelta(days=days))]
    return result


_COLUMN_LABELS = {
    "snapshot_date": "Дата сбора", "updated_at": "Обновлено", "marketplace": "Страна",
    "currency": "Валюта", "our_product": "Наш товар", "our_asin": "Наш ASIN",
    "our_bsr": "BSR наш", "our_price": "Цена наша", "our_bsr_delta_24h": "Δ BSR наш",
    "competitor_name": "Конкурент", "comp_asin": "ASIN конкурента", "comp_bsr": "BSR конкурента",
    "comp_price": "Цена конкурента", "comp_bsr_delta_24h": "Δ BSR конкурента",
    "price_diff_pct": "Разница цен, %", "comp_stock": "Наличие",
}
_IMAGE_URL_LABELS = {"our_image_url": "Фото наш (ссылка)", "comp_image_url": "Фото конкурента (ссылка)"}
# Числовые столбцы (могут быть NaN из базы). Их нельзя чистить через fillna("") вместе с текстовыми —
# смесь float и "" в одном столбце валит сериализацию в Arrow (см. коммит с разбором). Вместо этого
# приводим к строке целиком: пусто для NaN, аккуратный текст для числа.
_NUMERIC_LABELS = ("Цена наша", "BSR наш", "Δ BSR наш", "Цена конкурента", "BSR конкурента",
                   "Δ BSR конкурента", "Разница цен, %")


# Столбцы, где знак и есть смысл: «кто дешевле» и «BSR стал лучше или хуже». Без явного «+»
# (минус-то рисуется сам) по числу не видно направления, на это и жаловались при разборе.
_SIGNED_LABELS = ("Δ BSR наш", "Δ BSR конкурента", "Разница цен, %")


def _format_number_or_blank(value: object) -> str:
    if pd.isna(value):
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def _format_signed_or_blank(value: object) -> str:
    text = _format_number_or_blank(value)
    if not text or not pd.api.types.is_number(value) or pd.isna(value):
        return text
    return f"+{text}" if float(value) > 0 else text  # минус и ноль рисуются сами


def _format_kyiv_time(value: object) -> str:
    if pd.isna(value):
        return ""
    moment = pd.to_datetime(value, errors="coerce")
    if pd.isna(moment):
        return str(value)
    # Наивное время из базы считаем UTC: именно так оно туда и попадало (TIMESTAMPTZ в UTC).
    moment = moment.tz_localize("UTC") if moment.tzinfo is None else moment
    return moment.tz_convert(schedule_store.TZ).strftime("%d.%m %H:%M")


def _present_table(data: pd.DataFrame, *, with_images: bool = False) -> tuple[pd.DataFrame, dict[str, object]]:
    labels = _COLUMN_LABELS
    result = data.rename(columns={k: v for k, v in labels.items() if k in data.columns}).copy()
    link_columns: dict[str, object] = {}
    for source, label in (("our_asin", "Наш ASIN"), ("comp_asin", "ASIN конкурента")):
        if source not in data.columns:
            continue
        result[label] = [_amazon_url(asin, mk) for asin, mk in zip(data[source], data.get("marketplace", ""))]
        link_columns[label] = st.column_config.LinkColumn(
            label, help="Открыть карточку товара на Amazon",
            display_text=r"https://www\.amazon\.[^/]+/dp/([A-Z0-9]{10})", width="small",
        )
    if with_images:
        # Пустая строка рендерится пустой ячейкой; Python None/NaN в этой версии Streamlit
        # ImageColumn рисует как видимый текст "None" — поэтому оставляем "" как есть, не заменяем.
        position = 0
        for source, label in (("our_image_url", "Фото наш"), ("comp_image_url", "Фото конкурента")):
            if source not in data.columns:
                continue
            result.insert(position, label, data[source])
            result = result.drop(columns=[source])  # иначе сырая ссылка останется отдельным текстовым столбцом
            link_columns[label] = st.column_config.ImageColumn(label, width="small")
            position += 1
    # В этой версии Streamlit пустая ячейка (NaN/None, например нераспознанная цена) рисуется видимым
    # текстом "None"; пустая строка — действительно пустой ячейкой. Числовые столбцы форматируем в
    # строку отдельно (см. _NUMERIC_LABELS), остальные (текстовые, уже без чисел) — просто fillna.
    for label in _NUMERIC_LABELS:
        if label in result.columns:
            formatter = _format_signed_or_blank if label in _SIGNED_LABELS else _format_number_or_blank
            result[label] = result[label].map(formatter)
    if "Обновлено" in result.columns:
        # Всё остальное в дашборде — по Киеву; столбец из базы приходил как UTC со смещением.
        result["Обновлено"] = result["Обновлено"].map(_format_kyiv_time)
    result = result.fillna("")
    return result, link_columns


def _table_or_note(data: pd.DataFrame, *, with_images: bool = False) -> None:
    """Пустой st.dataframe рисует английское "empty" — вместо этого объясняем словами."""
    if data.empty:
        st.info("Ничего не найдено: попробуйте снять фильтры выше или расширить период.")
        return
    presented, table_config = _present_table(data, with_images=with_images)
    st.dataframe(presented, use_container_width=True, hide_index=True, height=450, column_config=table_config)


_EXCEL_GOOD_FILL = "C6EFCE"
_EXCEL_WARN_FILL = "FFEB9C"
_EXCEL_BAD_FILL = "FFC7CE"
_EXCEL_STOCK_FILL = {"In Stock": _EXCEL_GOOD_FILL, "Low Stock (<5)": _EXCEL_WARN_FILL, "Out of Stock": _EXCEL_BAD_FILL}


def _as_number(value: object) -> float | None:
    return float(value) if pd.api.types.is_number(value) and not pd.isna(value) else None


@st.cache_data(ttl=60, show_spinner=False)
def _current_to_excel_bytes(data: pd.DataFrame) -> bytes:
    """Тот же срез, что и CSV, но в Excel и с подсветкой: зелёным — что нам выгодно (BSR снизился,
    конкурент дороже нас, товар в наличии), красным — обратное, жёлтым — «Low Stock». Данные и раскраска
    не связаны с Google Sheets — считаются заново из того, что показывает дашборд."""
    import io

    from openpyxl.styles import Font, PatternFill
    from openpyxl.utils import get_column_letter

    all_labels = {**_COLUMN_LABELS, **_IMAGE_URL_LABELS}
    table = data.rename(columns={k: v for k, v in all_labels.items() if k in data.columns})
    # updated_at приходит из Postgres как TIMESTAMPTZ (с часовым поясом) — Excel такие значения
    # не поддерживает вовсе и падает при записи; час не пересчитываем, просто снимаем метку пояса.
    for column in table.columns[table.dtypes.apply(lambda dtype: getattr(dtype, "tz", None) is not None)]:
        table[column] = table[column].dt.tz_localize(None)
    buffer = io.BytesIO()
    with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
        table.to_excel(writer, index=False, sheet_name="Текущее состояние")
        sheet = writer.sheets["Текущее состояние"]
        header_row = {cell.value: cell.column for cell in sheet[1]}
        for cell in sheet[1]:
            cell.font = Font(bold=True)

        fills = {name: PatternFill("solid", fgColor=color) for name, color in
                 (("good", _EXCEL_GOOD_FILL), ("warn", _EXCEL_WARN_FILL), ("bad", _EXCEL_BAD_FILL))}

        def paint(label: str, choose) -> None:
            column = header_row.get(label)
            if not column:
                return
            for row in range(2, sheet.max_row + 1):
                cell = sheet.cell(row=row, column=column)
                key = choose(cell.value)
                if key:
                    cell.fill = fills[key]

        def bsr_delta(value: object) -> str | None:
            number = _as_number(value)
            return None if number is None or number == 0 else ("good" if number < 0 else "bad")

        def price_edge(value: object) -> str | None:
            number = _as_number(value)
            return None if number is None or number == 0 else ("good" if number > 0 else "bad")

        paint("Δ BSR наш", bsr_delta)
        paint("Δ BSR конкурента", bsr_delta)
        paint("Разница цен, %", price_edge)
        paint("Наличие", lambda value: {"In Stock": "good", "Low Stock (<5)": "warn", "Out of Stock": "bad"}.get(value))

        for column_cells in sheet.columns:
            width = max((len(str(cell.value)) for cell in column_cells if cell.value is not None), default=8)
            sheet.column_dimensions[get_column_letter(column_cells[0].column)].width = min(max(width + 2, 8), 45)
    return buffer.getvalue()


def _load_run_status() -> dict:
    if not STATUS_FILE.exists():
        return {}
    try:
        return json.loads(STATUS_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _pid_alive(pid: int) -> bool:
    """Проверка через tasklist — без внешних зависимостей вроде psutil."""
    try:
        output = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}"],
            capture_output=True, text=True, timeout=5,
        ).stdout
        return str(pid) in output
    except Exception:
        return False


def _format_dt(value: str | None) -> str:
    if not value:
        return "—"
    try:
        return datetime.fromisoformat(value).strftime("%d.%m.%Y %H:%M")
    except ValueError:
        return value


def _render_run_control() -> None:
    status = _load_run_status()
    is_running = status.get("state") == "running" and _pid_alive(status.get("pid", -1))

    box_left, box_right = st.columns([1, 3])
    with box_left:
        if st.button("▶ Запустить парсер", disabled=is_running, use_container_width=True):
            subprocess.Popen(
                [sys.executable, str(RUNNER_SCRIPT)],
                cwd=PROJECT_DIR,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
            st.rerun()
        if st.button("↻ Обновить статус", use_container_width=True):
            st.cache_data.clear()
            st.rerun()

    with box_right:
        if is_running:
            step = {"parser": "идёт сбор данных (парсер)", "sync": "синхронизация с базой"}.get(status.get("step"), "выполняется")
            st.info(f"⏳ Запуск в процессе: {step}. Начат {_format_dt(status.get('started_at'))}.")
        elif status.get("state") == "error":
            st.error(f"❌ Последний запуск завершился с ошибкой ({_format_dt(status.get('finished_at'))}): {status.get('error')}")
        elif status.get("state") == "done":
            st.success(f"✅ Последний ручной запуск успешно завершён: {_format_dt(status.get('finished_at'))}. Данные в базе обновлены.")
        else:
            st.caption("Ручных запусков с дашборда ещё не было. Парсер также запускается автоматически по расписанию.")


def _now() -> datetime:
    return datetime.now(schedule_store.TZ)


@st.cache_data(ttl=20, show_spinner=False)
def _admission_preview_cached(scope: str = "all") -> str | None:
    """Решение проверки допуска для подписи под кнопкой запуска (кэш на 20 с на каждую область; при нажатии
    проверяется заново)."""
    return run_control.admission_preview(_connect, _now(), scope=scope)


@st.cache_resource
def _login_limiter() -> access.AttemptLimiter:
    return access.AttemptLimiter()


def _secret_is_nested(name: str) -> bool:
    """Ключ секрета, оказавшийся внутри секции [..] (TOML вкладывает всё, что стоит ниже заголовка секции)."""
    try:
        for key in st.secrets:
            section = st.secrets[key]
            if hasattr(section, "keys") and name in section:
                return True
    except Exception:
        pass
    return False


def _team_password_state() -> tuple[str, str]:
    """('open', '') — секрета нет, управление открыто всем, у кого есть ссылка; ('password', пароль);
    ('locked', причина) — секрет задан неправильно, управление закрыто (не открываем по ошибке)."""
    password = _secret("TEAM_PASSWORD")
    if password:
        if len(password) < access.MIN_PASSWORD_LENGTH:
            return "locked", f"Управление закрыто: пароль команды (секрет TEAM_PASSWORD) короче {access.MIN_PASSWORD_LENGTH} символов."
        return "password", password
    if _secret_is_nested("TEAM_PASSWORD"):
        return "locked", "Управление закрыто: строка TEAM_PASSWORD стоит внутри секции секретов. Поднимите её выше первой секции в квадратных скобках."
    return "open", ""


def _management_open() -> bool:
    return _team_password_state()[0] == "open"


def _manager(email: str | None, google_role: str | None) -> tuple[str | None, str | None]:
    """(кто, роль) для действий по управлению: роль из Google-входа, открытое всем управление или вход по паролю команды."""
    if google_role is not None:
        return email, google_role
    if _management_open():
        return "Команда", access.ROLE_EDITOR
    name = st.session_state.get("manager_name")
    return (name, access.ROLE_EDITOR) if name else (None, None)


def _set_flash(name: str, kind: str, message: str) -> None:
    st.session_state[name] = (kind, message)


def _show_flash(name: str) -> None:
    flash = st.session_state.pop(name, None)
    if flash:
        getattr(st, flash[0])(flash[1])


def _render_unlock_box() -> None:
    state, detail = _team_password_state()
    with st.expander("🔒 Управление", expanded=False):
        name = st.session_state.get("manager_name")
        if name:
            st.caption(f"Управление открыто: {name}")
            if st.button("Закрыть управление", key="lock_btn"):
                st.session_state.pop("manager_name", None)
                st.rerun()
            return
        if state == "locked":
            st.caption(detail)
            return
        password = detail
        with st.form("unlock_form"):
            who = st.text_input("Ваше имя", key="unlock_name")
            typed = st.text_input("Пароль команды", type="password", key="unlock_password")
            submitted = st.form_submit_button("Открыть управление")
        if not submitted:
            return
        limiter = _login_limiter()
        if not limiter.allowed():
            st.error(f"Слишком много неудачных попыток. Повторите через {limiter.retry_after() // 60 + 1} мин.")
            return
        clean = access.clean_actor_name(who)
        if clean is None:
            st.error("Укажите имя (2–40 символов): оно попадёт в журнал изменений.")
        elif access.password_matches(typed, password):
            limiter.record_success()
            st.session_state["manager_name"] = clean
            st.rerun()
        else:
            limiter.record_failure()
            st.error("Неверный пароль.")


_RUN_STATUS_LABELS = {"done": "успешно", "running": "идёт", "error": "ошибка"}


def _runs_table(runs: list[dict], show_errors: bool) -> pd.DataFrame:
    rows = []
    for run in runs:
        started = run["started_at"].astimezone(schedule_store.TZ)
        finished = run["finished_at"]
        row = {
            "Начат (Киев)": started.strftime("%d.%m %H:%M"),
            "Статус": _RUN_STATUS_LABELS.get(run["status"], run["status"]),
            "Длительность, мин": round((finished - run["started_at"]).total_seconds() / 60) if finished else None,
        }
        if show_errors:
            row["Ошибка"] = (run["error"] or "")[:120]
        rows.append(row)
    return pd.DataFrame(rows)


def _render_schedule_tab(actor: str | None, role: str | None) -> None:
    _show_flash("schedule_flash")
    now = _now()
    try:
        overview = schedule_store.load_overview(_connect, now)
    except schedule_store.ScheduleStoreError as exc:
        st.error(str(exc))
        return

    schedule = overview.schedule
    if schedule is None:
        st.info("Автосбор выключен: по расписанию данные не собираются.")
    else:
        st.success(f"Автосбор включён: каждый день после {schedule.hour:02d}:{schedule.minute:02d} (Киев).")
        upcoming = schedule_store.next_run(now, schedule, overview.collected_today)
        if not upcoming.due_now:
            st.caption(f"Следующий запуск: {upcoming.when:%d.%m в %H:%M} (Киев).")
    if any(run["status"] == "running" for run in overview.runs):
        st.warning("Сейчас идёт сбор данных.")

    can_edit = access.has_role(role, access.ROLE_EDITOR)
    if overview.runs:
        st.markdown('<p class="section-note">Последние запуски</p>', unsafe_allow_html=True)
        st.dataframe(_runs_table(overview.runs, show_errors=can_edit), use_container_width=True, hide_index=True)

    if not can_edit:
        st.caption("Чтобы менять время, откройте «🔒 Управление» вверху страницы.")
        return

    hour, minute = schedule_store.to_slot(schedule.hour, schedule.minute) if schedule else (9, 0)
    with st.form("schedule_form"):
        chosen = st.time_input("Время сбора (по Киеву)", value=datetime(2000, 1, 1, hour, minute).time(), step=schedule_store.SLOT_MINUTES * 60, key="schedule_time")
        enabled = st.checkbox("Автосбор включён", value=schedule is not None, key="schedule_enabled")
        submitted = st.form_submit_button("Сохранить")
    st.caption("Сбор идёт раз в день: если сегодня он уже прошёл успешно, новое время сработает завтра.")
    if submitted:
        try:
            schedule_store.save_schedule(_connect, chosen.hour, chosen.minute, enabled, actor_role=role, actor=actor or "?")
        except (ValueError, access.AccessDenied, schedule_store.ScheduleStoreError) as exc:
            st.error(str(exc))
        else:
            text = f"Сохранено: автосбор включён, {chosen:%H:%M} (Киев)." if enabled else "Сохранено: автосбор выключен."
            _set_flash("schedule_flash", "success", text)
            st.rerun()


def _render_auth_bar(user: dict, email: str | None, role: str | None) -> None:
    if not _auth_configured():
        return
    if not user.get("is_logged_in"):
        st.button("Войти через Google", on_click=st.login, key="login_btn")
        return
    if email is None:
        note = "Вход выполнен, но Google не подтвердил email — доступ к управлению закрыт."
    else:
        status = _ROLE_LABELS.get(role, "нет доступа к управлению — попросите админа добавить этот email")
        note = f"{email} · {status}"
    st.markdown(f'<div class="section-note">{escape(note)}</div>', unsafe_allow_html=True)
    st.button("Выйти", on_click=st.logout, key="logout_btn")


def _render_users_panel(actor_email: str, actor_role: str) -> None:
    _show_flash("users_flash")
    st.markdown(
        '<p class="section-note">Просмотр открыт всем. Управлять могут только люди из этого списка '
        "(вход через Google) и админы из секрета ADMIN_EMAILS.</p>",
        unsafe_allow_html=True,
    )
    try:
        users = access.list_users(_connect)
    except access.AccessStoreError as exc:
        st.error(str(exc))
        return

    if users:
        table = pd.DataFrame(users).rename(columns={
            "email": "Email", "role": "Роль", "active": "Активен", "added_by": "Добавил", "created_at": "Добавлен",
        })
        table["Роль"] = table["Роль"].map(_ROLE_LABELS)
        table["Активен"] = table["Активен"].map({True: "да", False: "нет"})
        st.dataframe(table, use_container_width=True, hide_index=True)
    else:
        st.info("В базе пока никого нет. Админы из секрета ADMIN_EMAILS работают без записи в базе.")

    with st.form("add_user_form", clear_on_submit=True):
        new_email = st.text_input("Email (с которым человек входит через Google)", key="add_user_email")
        new_role = st.selectbox(
            "Роль", [access.ROLE_EDITOR, access.ROLE_ADMIN], format_func=_ROLE_LABELS.get, key="add_user_role",
        )
        if st.form_submit_button("Добавить или обновить"):
            try:
                saved = access.add_user(_connect, new_email, new_role, actor_role=actor_role, actor_email=actor_email)
            except (ValueError, access.AccessDenied, access.AccessStoreError) as exc:
                st.error(str(exc))
            else:
                _set_flash("users_flash", "success", f"Сохранено: {saved}")
                st.rerun()

    if users:
        with st.form("toggle_user_form"):
            target = st.selectbox("Пользователь", [u["email"] for u in users], key="toggle_user_email")
            action = st.radio("Действие", ["Включить", "Отключить"], horizontal=True, key="toggle_user_action")
            if st.form_submit_button("Применить"):
                enable = action == "Включить"
                try:
                    access.set_user_active(_connect, target, enable, actor_role=actor_role, actor_email=actor_email)
                except (ValueError, access.AccessDenied, access.AccessStoreError) as exc:
                    st.error(str(exc))
                else:
                    _set_flash("users_flash", "success", f"{target}: {'включён' if enable else 'отключён'}")
                    st.rerun()


def main() -> None:
    _apply_design()
    user, email, role = _resolve_access()
    left, right = st.columns([3, 2])
    with left:
        st.markdown('<p class="brand-title">📦 BSR Radar</p>', unsafe_allow_html=True)
        st.markdown('<p class="brand-subtitle">Мониторинг Amazon-конкурентов и аналитика портфеля</p>', unsafe_allow_html=True)
    with right:
        _render_auth_bar(user, email, role)
        if role is None and not _management_open():
            _render_unlock_box()
    actor, manage_role = _manager(email, role)

    if not _management_open() and manage_role is None:
        st.info(
            "Режим просмотра: дашборд показывает данные из PostgreSQL, не запускает парсер и не "
            "меняет Google Sheets. Время автосбора и список пар меняются после входа в «🔒 Управление»."
        )

    try:
        current = load_current()
        history = load_snapshots()
        pairs = load_competitor_pairs()
    except Exception as exc:
        st.error("Не удалось подключиться к базе данных.")
        st.code(str(exc))
        st.info("Проверьте DATABASE_URL в .env.")
        return

    latest_label = "нет данных"
    if not current.empty:
        dates = pd.to_datetime(current["snapshot_date"], errors="coerce")
        if dates.notna().any():
            latest_label = dates.max().strftime("%d.%m.%Y")
    st.markdown(
        f'<div class="status-box"><strong>Последний сбор в базе: {escape(latest_label)}</strong></div>',
        unsafe_allow_html=True,
    )

    st.markdown('<p class="section-title">Мониторинг конкурентов</p>', unsafe_allow_html=True)
    st.markdown(
        '<p class="section-note">Фильтры ниже применяются к вкладкам «Текущее состояние» и «История».</p>',
        unsafe_allow_html=True,
    )
    choice = _filter_controls([current, history])
    shown = _apply_filter(current, choice)
    shown_history = _apply_filter(history, choice)

    _render_overview(shown)

    tab_titles = ["📋 Текущее состояние", "📅 История", "🥊 Пары конкурентов", "⚙ Сбор и управление"]
    if role == access.ROLE_ADMIN:
        tab_titles.append("👥 Пользователи")
    tabs = st.tabs(tab_titles)
    current_tab, history_tab, pairs_tab, schedule_tab = tabs[:4]

    with current_tab:
        _table_or_note(shown, with_images=True)

    with history_tab:
        _table_or_note(shown_history)

    with pairs_tab:
        pairs_ui.render_pairs_tab(_connect, pairs, actor, manage_role, _max_active())

    with schedule_tab:
        can_edit = access.has_role(manage_role, access.ROLE_EDITOR)
        left, right = st.columns(2)
        with left:
            collect_ui.render_run_block(
                _connect, pairs, _admission_preview_cached,
                _secret("GITHUB_DISPATCH_TOKEN"), _secret("GITHUB_REPO") or github_dispatch.DEFAULT_REPO, can_edit,
            )
            st.markdown('<p class="section-title">Автосбор</p>', unsafe_allow_html=True)
            _render_schedule_tab(actor, manage_role)
        with right:
            collect_ui.render_spot_check(_secret("SCRAPINGDOG_TOKEN"), can_edit)
        collect_ui.render_refresh_button()

    if role == access.ROLE_ADMIN:
        with tabs[4]:
            _render_users_panel(email, role)

    download_left, download_right, _ = st.columns([1, 1, 4])
    with download_left:
        st.download_button(
            "⬇ CSV", current.to_csv(index=False).encode("utf-8-sig"),
            file_name="current.csv", mime="text/csv",
        )
    with download_right:
        st.download_button(
            "⬇ Excel с цветами", _current_to_excel_bytes(current),
            file_name="current.xlsx", mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )


if __name__ == "__main__":
    main()
