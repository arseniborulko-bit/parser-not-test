"""
SaaS-дашборд, читающий данные из Postgres (schema bsr_radar), а не из
Google Sheets API напрямую. Данные в базу попадают через sync_sheets_to_db.py.

Пока читает из базы каждый раз при обновлении (без записи куда-либо) — кнопка
запуска парсера и панель расписания добавляются отдельными следующими шагами.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from html import escape
from pathlib import Path
from typing import Iterable

import altair as alt
import numpy as np
import pandas as pd
import psycopg2
import streamlit as st
from dotenv import load_dotenv

import access
import activity
import collect_ui
import github_dispatch
import pairs_store
import pairs_ui
import run_control
import schedule_store
import usage_log

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


# Права любого вошедшего сотрудника. Решение владельца 01.10.2026: все сотрудники могут всё — пары,
# время сбора, запуск сбора; админы (вкладка «Активность дашборда», раздел «Пользователи и роли») — по-прежнему из ADMIN_EMAILS и базы.
# None — сотрудник только смотрит, пока ему не дадут роль или он не откроет «🔒 Управление».
EMPLOYEE_ROLE: str | None = access.ROLE_EDITOR


def _resolve_access() -> tuple[dict, str | None, str | None]:
    """(данные пользователя, почта сотрудника, роль). Почта None — это не сотрудник (или вход не
    выполнен): дальше _require_employee дашборд не пустит. Роль None — управлять нельзя."""
    user = _current_user()
    email = access.employee_email(user)
    if email is None:
        return user, None, None
    admin_emails = access.parse_email_list(_secret("ADMIN_EMAILS"))
    db_roles: dict = {}
    if email not in admin_emails:
        try:
            db_roles = access.active_user_roles(_connect)
        except access.AccessStoreError as exc:
            if EMPLOYEE_ROLE is None:
                st.warning(f"{exc} Доступ к управлению временно закрыт.")
    return user, email, access.resolve_role(user, admin_emails, db_roles) or EMPLOYEE_ROLE


@st.cache_resource
def _login_registry() -> access.LoginRegistry:
    return access.LoginRegistry()


def _record_login_once(user: dict, email: str) -> None:
    """Пишет вход в bsr_radar.login_log один раз на настоящий вход, а не на каждую перерисовку:
    в пределах сессии — по session_state, между вкладками и обновлениями страницы — по отпечатку входа
    (sub + время выдачи входа Google). Ошибка записи вход не ломает: попытка одна, причина — в логе."""
    key = access.login_key(user, email)
    session_key = key or f"{email}:session"
    if st.session_state.get("login_recorded") == session_key:
        return
    st.session_state["login_recorded"] = session_key
    if key is not None and not _login_registry().first_time(key):
        return
    access.record_login(_connect, email)


def _render_brand() -> None:
    # Одним блоком, а не двумя отдельными абзацами: иначе Streamlit ставит между ними
    # собственный отступ и значок с названием расходятся по высоте.
    st.markdown(
        '<div class="brand">'
        '<span class="brand-mark">📡</span>'
        '<div><p class="brand-title">Competitor BSR</p>'
        '<p class="brand-subtitle">Мониторинг Amazon-конкурентов и аналитика портфеля</p></div>'
        '</div>',
        unsafe_allow_html=True,
    )


def _require_employee(user: dict, email: str | None) -> None:
    """Проверка входа — до любых данных и действий. Не сотрудник — st.stop(): дальше main() не идёт,
    ни один запрос к базе за данными дашборда не выполняется. Вход не настроен — дашборд закрыт,
    а не открыт (решение владельца 01.10.2026)."""
    if not _auth_configured():
        middle = _render_gate()
        middle.error("Вход через Google не настроен: в Secrets нет полной секции [auth]. Дашборд закрыт.")
        st.stop()
    if not user.get("is_logged_in"):
        middle = _render_gate()
        middle.button("Войти через Google", on_click=st.login, key="login_btn", type="primary",
                      use_container_width=True)
        st.stop()
    if email is None:
        middle = _render_gate("Вы вошли не рабочим аккаунтом. Выйдите и войдите заново через "
                              f"@{access.EMPLOYEE_DOMAIN}.")
        middle.error(f"Доступ только для сотрудников {access.EMPLOYEE_DOMAIN}")
        middle.button("Выйти", on_click=st.logout, key="logout_btn", use_container_width=True)
        st.stop()
    _record_login_once(user, email)


def _render_gate(note: str = "") -> st.delta_generator.DeltaGenerator:
    """Экран входа как в Rating Radar: шапка (название слева, бот справа) и карточка по центру.
    Данных из базы здесь нет и быть не должно — до входа дашборд базу не читает. Возвращает
    среднюю колонку: кнопку или сообщение кладёт туда вызывающий."""
    left, right = st.columns([3, 2])
    with left:
        _render_brand()
    with right:
        st.markdown(_bot_link_html(), unsafe_allow_html=True)
    st.markdown(
        '<div class="gate"><div class="gate-mark">📡</div><p class="gate-title">Competitor BSR</p>'
        f'<p class="gate-text">Доступ только для сотрудников {escape(access.EMPLOYEE_DOMAIN)}</p>'
        + (f'<p class="gate-note">{escape(note)}</p>' if note else "")
        + "</div>",
        unsafe_allow_html=True,
    )
    _, middle, _ = st.columns([1, 1.1, 1])
    return middle


PROJECT_DIR = Path(__file__).resolve().parent
STATUS_FILE = PROJECT_DIR / "run_status.json"
RUNNER_SCRIPT = PROJECT_DIR / "run_parser_and_sync.py"

AMAZON_DOMAINS = {
    "US": "com", "CA": "ca", "UK": "co.uk", "DE": "de", "FR": "fr",
    "ES": "es", "IT": "it", "MX": "com.mx", "JP": "co.jp", "AU": "com.au",
}

st.set_page_config(page_title="Competitor BSR — мониторинг конкурентов", page_icon="📡", layout="wide")


# Прятать ли собственную шапку Streamlit («Share», «Fork», значок GitHub, меню приложения).
# Владельцу она нужна изредка: чтобы вернуть — поставить False, менять больше ничего не нужно.
# Настройки приложения от этого не закрываются: адрес и прочее меняются на share.streamlit.io
# (список приложений → ⋮ → Settings), а не через эту шапку.
# Значки Streamlit Cloud в правом нижнем углу этим флагом НЕ управляются: они рисуются вне
# нашего iframe (div._streamlitAppContainer — сосед, а не потомок) и коду приложения недоступны.
HIDE_STREAMLIT_CHROME = True

# Проверено на живом сайте: кнопка «Fork» лежит в
# header[data-testid="stHeader"] > [data-testid="stToolbar"] > stToolbarActions.
# Шапку целиком не прячем: в том же stToolbar лежит стрелка, разворачивающая боковую панель
# (stExpandSidebarButton), а display: none у родителя спрятал бы и её. Поэтому шапка прозрачная
# и не перехватывает клики, а прячутся только кнопки «Share»/«Fork», меню и «Deploy».
_CHROME_CSS = """
<style>
header[data-testid="stHeader"], .stAppHeader { background: transparent !important; pointer-events: none; }
[data-testid="stDecoration"],
[data-testid="stToolbarActions"], [data-testid="stToolbarActionButton"],
[data-testid="stMainMenu"], [data-testid="stAppDeployButton"] { display: none !important; }
[data-testid="stExpandSidebarButton"] { pointer-events: auto; }
</style>
"""


def _apply_design() -> None:
    st.markdown(
        """
        <style>
        .stApp { background: #f6f7fb; color: #121826; }
        .block-container { max-width: 1320px; padding-top: 2.25rem; padding-bottom: 3rem; }
        [data-testid="stSidebar"] { background: #ffffff; }
        .brand { display: flex; align-items: center; gap: .7rem; margin: 0 0 1.4rem; }
        .brand-mark { font-size: 2.6rem; line-height: 1; }
        /* !important: правило Streamlit для абзацев внутри st.markdown сильнее класса — без него
        заголовок рисовался обычным текстом. */
        .brand-title { font-size: 2.35rem !important; font-weight: 800; letter-spacing: -0.03em; color: #111827; margin: 0; line-height: 1.1; }
        .brand-subtitle { color: #64748b; font-size: .9rem !important; margin: .15rem 0 0; }
        /* Streamlit красит ссылки своими правилами по [data-testid], поэтому цвет и отсутствие
        подчёркивания задаём принудительно, иначе получается чужая синяя ссылка с подчёркиванием. */
        .bot-row { display: flex; justify-content: flex-end; }
        a.bot-link, .stMarkdown a.bot-link {
            display: inline-flex; align-items: center; gap: .4rem;
            margin: .55rem 0 0; padding: .42rem .95rem;
            background: #0f6ea8; border-radius: 999px;
            color: #ffffff !important; text-decoration: none !important;
            font-size: .86rem; font-weight: 650; line-height: 1.4;
        }
        a.bot-link:hover, .stMarkdown a.bot-link:hover { background: #0b5988; }
        .bot-hint { color: #64748b; font-size: .8rem; margin: .3rem 0 0; text-align: right; }
        .status-box { background: #dbeafe; color: #2563eb; border-radius: 10px; padding: 1rem 1.1rem; }
        .status-box strong { color: #1d4ed8; }
        /* Раньше была фиксированная min-height: 120px. Пока в детали помещалась одна короткая
        строка ("маркетплейсов"), это работало; с разбивкой по странам («Стран» стала занимать
        две строки) Streamlit растягивает все карточки в ряду по высоте самой высокой — и у
        остальных внизу появлялось пустое место. Убираем фиксированную высоту и padding, чтобы
        карточки были размером с содержимое, а не с самую длинную деталь. */
        .gate { text-align: center; margin: 5rem 0 2rem; }
        .gate-mark { font-size: 4.2rem; line-height: 1; margin-bottom: 1.4rem; }
        .gate-title { font-size: 2.6rem !important; font-weight: 800; letter-spacing: -0.03em; color: #111827; margin: 0 0 1rem; }
        .gate-text { color: #334155; font-size: 1.15rem !important; margin: 0; }
        .gate-note { color: #64748b; font-size: .92rem !important; margin: .6rem 0 0; }
        .metric-card { background: #ffffff; border: 1px solid #e5e7eb; border-radius: 15px; padding: .7rem .9rem; box-shadow: 0 1px 2px rgba(15,23,42,.025); }
        .metric-label { color: #64748b; font-size: .7rem; letter-spacing: .065em; text-transform: uppercase; }
        .metric-value { color: #111827; font-size: 1.5rem; font-weight: 800; line-height: 1.2; margin: .15rem 0; }
        .metric-detail { color: #64748b; font-size: .74rem; line-height: 1.3; }
        .section-title { font-size: 1.55rem; font-weight: 750; margin: 1.75rem 0 .2rem; }
        .section-note { color: #64748b; font-size: .86rem; margin-bottom: .6rem; }
        .field-label { font-weight: 650; color: #111827; font-size: .95rem; margin-bottom: .35rem; }
        .field-badge {
            display: inline-block; background: #e0f2fe; color: #0369a1; font-size: .72rem;
            font-weight: 650; padding: .12rem .55rem; border-radius: 999px; margin-left: .5rem;
            vertical-align: middle;
        }
        /* Вкладки выбираются по role: в новых версиях Streamlit это уже не <button> и не baseweb. */
        div[data-testid="stTabs"] [role="tablist"] { gap: 3px; border-bottom: 1px solid #dfe3ea; }
        /* Только сами заголовки вкладок. Раньше здесь был ещё и просто "button", из-за чего
        оформление вкладки доставалось каждой кнопке ВНУТРИ вкладки — в том числе кнопке-подсказке
        «?» у полей формы: она становилась тёмным квадратом с белым значком внутри. */
        div[data-testid="stTabs"] [role="tab"] { background: #121826 !important; color: #fff !important; border-radius: 7px 7px 0 0; padding: .6rem 1rem; margin-right: 2px; opacity: 1 !important; }
        div[data-testid="stTabs"] [role="tab"] * { color: #fff !important; opacity: 1 !important; }
        div[data-testid="stTabs"] [role="tab"][aria-selected="true"] { background: #168ed0 !important; color: #fff !important; }
        /* Красная полоска-указатель активной вкладки позиционируется скриптом по ширине вкладки,
        а мы вкладкам меняем padding/margin — из-за этого она отставала на шаг и подчёркивала
        предыдущую вкладку. Активную вкладку и так видно по синей заливке, поэтому полоску убираем.
        Селектор подсмотрен в живом DOM: div.react-aria-SelectionIndicator (2px, rgb(255,75,75)).
        Прежние [data-baseweb="tab-highlight"]/[data-baseweb="tab-border"] в этой версии Streamlit
        не существуют вовсе — оставлены на случай отката к старой разметке. */
        div[data-testid="stTabs"] .react-aria-SelectionIndicator,
        div[data-testid="stTabs"] [data-baseweb="tab-highlight"],
        div[data-testid="stTabs"] [data-baseweb="tab-border"] { display: none !important; }
        div[data-testid="stTabs"] button:disabled { opacity: .45 !important; cursor: not-allowed; }
        .stButton > button { background: #168ed0; color: #fff; border: 0; border-radius: 8px; font-weight: 650; }
        .stButton > button:hover { background: #075b9b; color: #fff; }

        .how-hero { margin: .3rem 0 1.6rem; }
        .how-title { font-size: 2.3rem; font-weight: 800; letter-spacing: -.03em; color: #111827; margin: 0 0 .45rem; line-height: 1.15; }
        .how-subtitle { color: #64748b; font-size: .98rem; line-height: 1.55; max-width: 760px; margin: 0; }
        .how-eyebrow { color: #94a3b8; font-size: .72rem; letter-spacing: .12em; text-transform: uppercase; font-weight: 650; margin: 1.7rem 0 .7rem; }
        .how-flow { display: flex; align-items: stretch; }
        .how-flow-step { flex: 1; background: #ffffff; border: 1px solid #e5e7eb; border-radius: 12px; padding: .8rem .9rem; text-align: center; }
        .how-flow-step.is-live { border-color: #168ed0; box-shadow: inset 0 0 0 1px #168ed0; }
        .how-flow-step-title { font-weight: 700; color: #111827; font-size: .92rem; }
        .how-flow-step-detail { color: #64748b; font-size: .76rem; margin-top: .3rem; }
        .how-flow-step.is-live .how-flow-step-detail { color: #168ed0; }
        .how-flow-connector { flex: 0 0 30px; position: relative; display: flex; align-items: center; justify-content: center; }
        .how-flow-connector::before { content: ""; position: absolute; left: 0; right: 0; top: 50%; height: 1px; background: #dbe0ea; }
        .how-flow-connector span { width: 6px; height: 6px; border-radius: 50%; background: #168ed0; position: relative; }
        .how-flow-legend { display: flex; justify-content: space-between; color: #94a3b8; font-size: .78rem; margin: .55rem .2rem 0; }
        .how-flow-legend-mid { flex: 1; text-align: center; }
        .how-card { display: flex; gap: 1rem; background: #ffffff; border: 1px solid #e5e7eb; border-radius: 14px; padding: 1rem 1.2rem; margin-bottom: .65rem; }
        .how-card-num { flex: 0 0 30px; height: 30px; border-radius: 50%; background: #121826; color: #fff; font-weight: 700; font-size: .82rem; display: flex; align-items: center; justify-content: center; }
        .how-card-title { font-weight: 700; color: #111827; font-size: .98rem; margin-bottom: .25rem; }
        .how-card-text { color: #4b5563; font-size: .89rem; line-height: 1.55; }
        .how-card-text b { color: #111827; }
        </style>
        """,
        unsafe_allow_html=True,
    )
    if HIDE_STREAMLIT_CHROME:
        st.markdown(_CHROME_CSS, unsafe_allow_html=True)


# Необязательные столбцы снимков: появляются миграциями и могут отсутствовать на боевой базе.
# Спрашиваем базу, какие из них есть, вместо «попробуй запрос, а если упал — попробуй короче»:
# иначе отсутствие рейтинга (миграция 009) утащило бы за собой и фото (миграция 004).
_OPTIONAL_SNAPSHOT_COLUMNS = (
    "our_image_url", "comp_image_url",
    "our_rating", "our_reviews_count", "comp_rating", "comp_reviews_count",
)


def _present_snapshot_columns(conn) -> list[str]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = 'bsr_radar' AND table_name = 'snapshots' "
            "AND column_name = ANY(%s);",
            (list(_OPTIONAL_SNAPSHOT_COLUMNS),),
        )
        return [row[0] for row in cur.fetchall()]


def _with_missing_optional_columns(data: pd.DataFrame, present: list[str]) -> pd.DataFrame:
    """Колонки, которых в базе ещё нет, добавляем пустыми: интерфейс не должен о них знать."""
    for column in _OPTIONAL_SNAPSHOT_COLUMNS:
        if column not in present:
            data[column] = "" if column.endswith("_url") else pd.NA
    return data


@st.cache_data(ttl=60, show_spinner=False)
def load_snapshots() -> pd.DataFrame:
    conn = psycopg2.connect(_database_url())
    try:
        present = _present_snapshot_columns(conn)
        extra = "".join(f", {column}" for column in present)
        data = pd.read_sql(
            f"""
            SELECT snapshot_date, marketplace, currency, our_asin, our_product, our_price,
                   our_bsr, our_bsr_delta_24h, comp_asin, competitor_name, comp_price,
                   comp_bsr, comp_bsr_delta_24h, comp_stock, price_diff_pct, updated_at{extra}
            FROM bsr_radar.snapshots
            ORDER BY snapshot_date DESC
            """,
            conn,
        )
        return _with_missing_optional_columns(data, present)
    finally:
        conn.close()


@st.cache_data(ttl=60, show_spinner=False)
def load_current() -> pd.DataFrame:
    """Последний снимок каждой АКТИВНОЙ пары на каждом рынке: убранная пара отсюда исчезает.

    Необязательные столбцы (фото — миграция 004, рейтинг и отзывы — 009) могут отсутствовать:
    спрашиваем базу, какие есть, и недостающие добавляем пустыми."""
    conn = psycopg2.connect(_database_url())
    try:
        present = _present_snapshot_columns(conn)
        extra = "".join(f", s.{column}" for column in present)
        data = pd.read_sql(
            f"""
            SELECT DISTINCT ON (s.marketplace, s.our_asin, s.comp_asin)
                   s.snapshot_date, s.marketplace, s.currency, s.our_asin, s.our_product, s.our_price,
                   s.our_bsr, s.our_bsr_delta_24h, s.comp_asin, s.competitor_name, s.comp_price,
                   s.comp_bsr, s.comp_bsr_delta_24h, s.comp_stock, s.price_diff_pct, s.updated_at{extra}
            FROM bsr_radar.snapshots s
            JOIN bsr_radar.competitor_pairs p
              ON p.our_asin = s.our_asin AND p.comp_asin = s.comp_asin
             AND p.marketplace = s.marketplace AND p.active
            ORDER BY s.marketplace, s.our_asin, s.comp_asin, s.snapshot_date DESC
            """,
            conn,
        )
        return _with_missing_optional_columns(data, present)
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


def _asins_per_country(data: pd.DataFrame) -> list[tuple[str, int]]:
    """Сколько уникальных ASIN (наших и конкурентов вместе) приходится на каждую страну,
    по убыванию — по образцу карточки «Стран» у Rating Radar."""
    if "marketplace" not in data:
        return []
    sides = []
    if "our_asin" in data:
        sides.append(data[["marketplace", "our_asin"]].rename(columns={"our_asin": "asin"}))
    if "comp_asin" in data:
        sides.append(data[["marketplace", "comp_asin"]].rename(columns={"comp_asin": "asin"}))
    if not sides:
        return []
    combined = pd.concat(sides, ignore_index=True).dropna(subset=["marketplace", "asin"])
    combined = combined[combined["asin"].astype(str).str.strip().ne("")]
    counts = combined.drop_duplicates(["marketplace", "asin"]).groupby("marketplace").size()
    return sorted(counts.items(), key=lambda item: (-item[1], item[0]))


def _country_breakdown_text(data: pd.DataFrame) -> str:
    breakdown = _asins_per_country(data)
    if not breakdown:
        return "маркетплейсов"
    return " · ".join(f"{country} {count}" for country, count in breakdown)


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
        _metric_card("Стран", data["marketplace"].nunique() if "marketplace" in data else "—", _country_breakdown_text(data)),
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
    # Пустой набор = все страны. Так «ничего не выбрано» и «выбрано всё» — одно и то же,
    # и отдельный пункт «Все» в списке не нужен.
    markets: tuple[str, ...] = ()


def _values(frames: Iterable[pd.DataFrame], column: str) -> list[str]:
    values: set[str] = set()
    for frame in frames:
        if column in frame:
            values.update(frame[column].dropna().unique())
    return sorted(values)


def _options(frames: Iterable[pd.DataFrame], column: str) -> list[str]:
    return ["Все"] + _values(frames, column)


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
        markets = st.multiselect(
            "Страны", _values(frames, "marketplace"), key=f"{key_prefix}_market",
            placeholder="Все страны",
        )
    return FilterChoice(asin=asin, search=search, period=period, markets=tuple(markets))


def _apply_filter(data: pd.DataFrame, choice: FilterChoice) -> pd.DataFrame:
    result = data.copy()
    if choice.asin != "Все" and "our_asin" in result:
        result = result[result["our_asin"] == choice.asin]
    if choice.markets and "marketplace" in result:
        result = result[result["marketplace"].isin(choice.markets)]
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
    "our_rating": "Рейтинг наш", "our_reviews_count": "Отзывов наш",
    "comp_rating": "Рейтинг конкурента", "comp_reviews_count": "Отзывов конкурента",
}
_IMAGE_URL_LABELS = {"our_image_url": "Фото наш (ссылка)", "comp_image_url": "Фото конкурента (ссылка)"}
# Числовые столбцы (могут быть NaN из базы). Их нельзя чистить через fillna("") вместе с текстовыми —
# смесь float и "" в одном столбце валит сериализацию в Arrow (см. коммит с разбором). Вместо этого
# приводим к строке целиком: пусто для NaN, аккуратный текст для числа.
_NUMERIC_LABELS = ("Цена наша", "BSR наш", "Δ BSR наш", "Цена конкурента", "BSR конкурента",
                   "Δ BSR конкурента", "Разница цен, %",
                   "Рейтинг наш", "Отзывов наш", "Рейтинг конкурента", "Отзывов конкурента")
# Рейтинг показываем с одной цифрой после запятой; отсутствие данных остаётся пустым,
# а ноль отзывов — нулём: это разные вещи (см. migrations/009).
_RATING_LABELS = ("Рейтинг наш", "Рейтинг конкурента")


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


def _format_rating_or_blank(value: object) -> str:
    """Рейтинг — одна цифра после запятой. Пусто остаётся пустым: «нет данных» не ноль."""
    if pd.isna(value) or not pd.api.types.is_number(value):
        return "" if pd.isna(value) else str(value)
    return f"{float(value):.1f}"


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
            if label in _SIGNED_LABELS:
                formatter = _format_signed_or_blank
            elif label in _RATING_LABELS:
                formatter = _format_rating_or_blank
            else:
                formatter = _format_number_or_blank
            result[label] = result[label].map(formatter)
    if "Обновлено" in result.columns:
        # Всё остальное в дашборде — по Киеву; столбец из базы приходил как UTC со смещением.
        result["Обновлено"] = result["Обновлено"].map(_format_kyiv_time)
    result = result.fillna("")
    return result, link_columns


# Имя бота публичное (в отличие от токена) — его видно в самом Telegram. Секретом не является.
# Переопределяется секретом TELEGRAM_BOT_USERNAME, если бота когда-нибудь заменят.
DEFAULT_BOT_USERNAME = "BSR_Competitors_Trackerbot"


def _bot_username() -> str:
    return (_secret("TELEGRAM_BOT_USERNAME") or DEFAULT_BOT_USERNAME).strip().lstrip("@")


def _bot_link_html() -> str:
    """Внутри блока шапки, а не отдельным элементом: иначе между ними встаёт отступ Streamlit,
    и ссылка повисает сама по себе."""
    username = _bot_username()
    if not username:
        return ""
    return (
        f'<div class="bot-row"><a class="bot-link" href="https://t.me/{escape(username)}" '
        f'target="_blank" rel="noopener">✈️ @{escape(username)}</a></div>'
        f'<div class="bot-hint">Нажмите «Подписаться», затем отправьте боту /start</div>'
    )


_HOW_IT_WORKS = """
**Откуда берутся данные.** Раз в сутки по каждому ASIN запрашиваются позиция в категории (BSR),
цена, наличие и фото. Запрос идёт через ScrapingDog, результаты складываются в Google Таблицу,
оттуда переносятся в базу, а дашборд показывает уже базу.

**Когда идёт сбор.** Время задаётся на вкладке «Сбор и управление», в блоке «Автосбор». Внешний сервис каждые 5 минут стучится в
GitHub, а проверка решает, пора ли: своё расписание GitHub срабатывает нерегулярно, поэтому на него
не полагаемся. Указанное время — «не раньше», старт обычно в пределах 5 минут после него.

**Защита от лишних трат.** Успешный сбор возможен один раз в сутки (по Киеву), попыток не больше
трёх. Кнопки «Собрать наши» и «Собрать конкурентов» считаются отдельно, у каждой свой дневной лимит.
Пока идёт сбор, новый не начнётся. При любой ошибке проверки сбор не стартует — лучше пропустить,
чем заплатить дважды.

**Что значит «@».** ASIN проверяли, но данные получить не удалось: товар недоступен, страница не
открылась или ответ пришёл пустой. Это не то же самое, что пустая ячейка — пустая означает, что эту
сторону пары в этом сборе вообще не проверяли (например, собирали только конкурентов).

**Цифры.** BSR — место в категории: чем **меньше**, тем лучше. «Δ BSR» — изменение с прошлого
замера, со знаком: «−1500» значит поднялись, «+1500» — опустились. «Разница цен» считается между
нашей ценой и ценой конкурента.

**Фильтры.** Стоят над вкладками и действуют сразу на «Текущее состояние» и на «Историю», в том
числе на карточки-итоги. В «Странах» можно отметить несколько сразу; если не отмечено ничего,
показываются все.

**Текущее состояние.** Последняя строка по каждой паре, отдельно по каждой стране. Отключённая
пара отсюда пропадает.

**История.** Сверху таблица «ASIN × даты»: строка — ASIN на своём рынке, колонки — дни,
переключатель показывает BSR или цену. Ниже обычная таблица, над ней выбор дня — чтобы посмотреть
один сбор, не выискивая дату. Выбор дня к верхней таблице не относится: от одного дня она
превратилась бы в одну колонку.

**Пары конкурентов.** Здесь заводят и убирают пары, а также правят уже внесённое:
«Исправить названия» — правка подписей сеткой, сразу по многим строкам. Сами ASIN и страна
в паре не меняются: это ключ, по которому лежит история, и его смена означала бы другую пару.

**Добавление и правка пар на вкладке «Сбор и управление».** Та же форма добавления и та же
сетка правки (название, активность, отключение), что и на «Пары конкурентов» — только здесь всё
на одном экране вместе со сбором, чтобы не переключаться между вкладками. Список пар можно
сузить по стране и по своему товару, а включить или выключить все показанные — двумя кнопками,
не отмечая по одной.

**Прогноз.** Два графика вместо таблицы чисел. Сверху — столбцы «Кто быстрее всего растёт»: у кого
BSR падает быстрее всего за день (зелёные) и у кого растёт (красные). Снизу — график выбранных
ASIN: фактическая история BSR сплошной линией и проекция на выбранный срок пунктиром. Можно выбрать
страну, найти ASIN по коду или названию, отметить несколько или «Выбрать все» сразу; когда
линий несколько, шкала BSR логарифмическая — иначе товары с BSR 1 000 и 400 000 не поместились бы
на одном графике. Изменение в
день считается как медиана дневных изменений, а не как прямая по всем точкам: BSR скачет, и один
выброс иначе задавал бы весь тренд. Это экстраполяция, а не предсказание — она не знает про акции,
сезон и новинки. ASIN, у которого меньше трёх замеров за окно, в прогноз не попадает.

**Что не пропадает.** Отключённая пара не удаляется, снимки не удаляются никогда. Поэтому история
по ней остаётся в «Истории», а вернуть пару можно в разделе «Вернуть отключённые» на вкладке
«Пары конкурентов».
"""


def _markdown_bold_to_html(text: str) -> str:
    """Только `**жирный**` → `<b>`, весь остальной текст экранируется — этого хватает для того,
    что реально встречается в _HOW_IT_WORKS (никаких ссылок, списков и прочей разметки)."""
    parts = text.split("**")
    return "".join(f"<b>{escape(part)}</b>" if i % 2 else escape(part) for i, part in enumerate(parts))


def _how_it_works_sections() -> list[tuple[str, str]]:
    """Разбирает _HOW_IT_WORKS на (заголовок, текст) для карточек — один источник правды с текстом,
    который уже проверяют tests/test_dashboard_how_it_works.py."""
    sections = []
    for block in _HOW_IT_WORKS.strip().split("\n\n"):
        block = re.sub(r"\s+", " ", block.strip())
        match = re.match(r"^\*\*(.+?)\*\*\s*(.*)$", block)
        title, body = match.groups() if match else ("", block)
        sections.append((title.rstrip("."), body))
    return sections


def _how_it_works_flow(pairs: pd.DataFrame, data: pd.DataFrame) -> str:
    """Живая схема потока: сколько пар в матрице, когда был последний сбор, сколько строк и стран
    сейчас в базе — те же числа, что и в карточках «Текущее состояние», просто в виде потока."""
    active_pairs = int(pairs["active"].sum()) if "active" in pairs else 0
    total_pairs = len(pairs)
    latest = "нет сборов"
    if not data.empty and "snapshot_date" in data:
        dates = pd.to_datetime(data["snapshot_date"], errors="coerce")
        if dates.notna().any():
            latest = dates.max().strftime("%d.%m.%Y")
    records = len(data)
    countries = data["marketplace"].nunique() if "marketplace" in data else 0

    steps = [
        ("Матрица пар", f"{active_pairs} активных из {total_pairs}", False),
        ("Сбор", "раз в сутки, ScrapingDog", False),
        ("База", f"последний: {latest}", True),
        ("Дашборд", f"{records} строк · {countries} стран", False),
    ]
    cells = []
    for index, (title, detail, live) in enumerate(steps):
        if index:
            cells.append('<div class="how-flow-connector"><span></span></div>')
        live_class = " is-live" if live else ""
        cells.append(
            f'<div class="how-flow-step{live_class}">'
            f'<div class="how-flow-step-title">{escape(title)}</div>'
            f'<div class="how-flow-step-detail">{escape(detail)}</div></div>'
        )
    return (
        '<div class="how-eyebrow">Как течёт поток · живая схема</div>'
        f'<div class="how-flow">{"".join(cells)}</div>'
        '<div class="how-flow-legend">'
        '<span>← пару добавляет человек</span>'
        '<span class="how-flow-legend-mid">данные текут сами</span>'
        '<span>смотрит и правит человек →</span>'
        '</div>'
    )


def _render_how_it_works(pairs: pd.DataFrame | None = None, data: pd.DataFrame | None = None) -> None:
    st.markdown(
        '<div class="how-hero">'
        '<h1 class="how-title">Как это работает</h1>'
        '<p class="how-subtitle">Competitor BSR раз в сутки собирает позицию в категории (BSR), цену и '
        'наличие по каждой паре «наш товар — конкурент» на Amazon и показывает, куда идёт каждый BSR.</p>'
        '</div>',
        unsafe_allow_html=True,
    )
    if pairs is not None and data is not None:
        st.markdown(_how_it_works_flow(pairs, data), unsafe_allow_html=True)

    st.markdown('<div class="how-eyebrow">Порядок работы</div>', unsafe_allow_html=True)
    for number, (title, body) in enumerate(_how_it_works_sections(), start=1):
        st.markdown(
            '<div class="how-card">'
            f'<div class="how-card-num">{number}</div>'
            '<div>'
            f'<div class="how-card-title">{escape(title)}</div>'
            f'<div class="how-card-text">{_markdown_bold_to_html(body)}</div>'
            '</div></div>',
            unsafe_allow_html=True,
        )


_MATRIX_METRICS = {"BSR": ("our_bsr", "comp_bsr"), "Цена": ("our_price", "comp_price")}
# На боевых данных таблица выходит 683 строки на 39 дней — помещается целиком; предел нужен
# только чтобы страница не встала, если история вырастет в разы.
MAX_MATRIX_ROWS = 800


def _history_matrix(data: pd.DataFrame, metric: str) -> pd.DataFrame:
    """Таблица «ASIN × даты»: строка — ASIN на своём рынке, колонки — дни, в клетках значение.

    Строим по ASIN, а не по парам: один и тот же ASIN на одном рынке даёт одинаковые цифры во
    всех парах, где он участвует, поэтому по парам таблица была бы втрое больше без новых данных.
    """
    ours, theirs = _MATRIX_METRICS[metric]
    if "snapshot_date" not in data:
        return pd.DataFrame()
    parts = []
    for asin_column, value_column in (("our_asin", ours), ("comp_asin", theirs)):
        if asin_column not in data or value_column not in data:
            continue
        parts.append(pd.DataFrame({
            "ASIN": data[asin_column],
            "Страна": data["marketplace"] if "marketplace" in data else "",
            "Дата": pd.to_datetime(data["snapshot_date"], errors="coerce"),
            "Значение": pd.to_numeric(data[value_column], errors="coerce"),
        }))
    if not parts:
        return pd.DataFrame()
    long = pd.concat(parts, ignore_index=True).dropna(subset=["Дата"])
    long = long[long["ASIN"].astype(str).str.strip().ne("")]
    long = long.dropna(subset=["Значение"])
    if long.empty:
        return pd.DataFrame()
    # Один ASIN на одном рынке за один день — одно значение, дубли из разных пар схлопываем.
    matrix = long.pivot_table(index=["ASIN", "Страна"], columns="Дата", values="Значение", aggfunc="last")
    matrix = matrix.sort_index(axis=1)
    matrix.columns = [column.strftime("%d.%m") for column in matrix.columns]
    result = matrix.reset_index()
    # Пустая клетка (день без данных) иначе рисуется видимым текстом "None" — см. _NUMERIC_LABELS.
    for column in result.columns:
        if column not in ("ASIN", "Страна"):
            result[column] = result[column].map(_format_number_or_blank)
    return result


def _render_history_matrix(data: pd.DataFrame) -> None:
    if data.empty or "snapshot_date" not in data:
        return
    metric = st.radio("Параметр", list(_MATRIX_METRICS), horizontal=True, key="history_matrix_metric")
    matrix = _history_matrix(data, metric)
    if matrix.empty:
        return
    shown = matrix.head(MAX_MATRIX_ROWS)
    st.dataframe(shown, use_container_width=True, hide_index=True, height=420)
    if len(matrix) > MAX_MATRIX_ROWS:
        st.caption(f"Показаны первые {MAX_MATRIX_ROWS} из {len(matrix)}: сузьте фильтры выше.")


_PIVOT_METRICS: dict[str, tuple[str, str]] = {
    "BSR": ("our_bsr", "comp_bsr"), "Цена": ("our_price", "comp_price"),
    "Рейтинг": ("our_rating", "comp_rating"), "Отзывы": ("our_reviews_count", "comp_reviews_count"),
}
_RANK_SCALE = ["#1f8a4c", "#7ec97e", "#d9f0d3", "#ffe9b3", "#ffc09b", "#e8534f"]
_RATING_SCALE = [(4.5, "#1f8a4c"), (4.0, "#7ec97e"), (3.5, "#ffe9b3"), (3.0, "#ffc09b"), (0.0, "#e8534f")]


def _asin_day_pivot(data: pd.DataFrame, our_col: str, comp_col: str) -> pd.DataFrame:
    """ASIN × день -> значение метрики, независимо от того, наш это ASIN или конкурент.

    Как и в _history_matrix: один ASIN в один день даёт одно число во всех парах, где он
    участвует, поэтому дубли по парам здесь схлопываются через aggfunc='last'."""
    parts = []
    for asin_column, value_column in (("our_asin", our_col), ("comp_asin", comp_col)):
        if asin_column not in data or value_column not in data:
            continue
        parts.append(pd.DataFrame({
            "asin": data[asin_column],
            "day": pd.to_datetime(data["snapshot_date"], errors="coerce"),
            "value": pd.to_numeric(data[value_column], errors="coerce"),
        }))
    if not parts:
        return pd.DataFrame()
    long = pd.concat(parts, ignore_index=True).dropna(subset=["day"])
    long = long[long["asin"].astype(str).str.strip().ne("")]
    return long.pivot_table(index="asin", columns="day", values="value", aggfunc="last")


def _pivot_value_fmt(metric: str, value: object) -> str:
    if pd.isna(value):
        return ""
    if metric == "Рейтинг":
        return f"{float(value):.1f}"
    if metric == "Цена":
        return f"{float(value):,.2f}".replace(",", " ")
    return f"{int(value):,}".replace(",", " ")


def _pivot_rating_css(value: float) -> str:
    for threshold, color in _RATING_SCALE:
        if value >= threshold:
            fg = ";color:#fff" if color in ("#1f8a4c", "#e8534f") else ""
            return f"background:{color}{fg}"
    return ""


def _pivot_change_css(metric: str, value: float, prev: float | None) -> str:
    if metric == "Рейтинг":
        return _pivot_rating_css(value)
    if prev is None or pd.isna(prev) or value == prev:
        return ""
    if metric in ("BSR", "Цена"):  # меньше — лучше; насыщенность по силе изменения
        delta = abs(prev - value) / prev if prev else 0
        strong = delta > (0.15 if metric == "BSR" else 0.1)
        if value < prev:
            return "background:#57d957" if strong and metric == "BSR" else "background:#c8f7c5"
        return "background:#e8534f;color:#fff" if strong else "background:#ffcdd2"
    if metric == "Отзывы":
        return "background:#c8f7c5" if value > prev else "background:#ffe0b2"
    return ""


def _pivot_rank_css(pos: int, total: int) -> str:
    if total <= 1:
        return ""
    idx = int(pos / max(1, total - 1) * (len(_RANK_SCALE) - 1))
    bg = _RANK_SCALE[idx]
    fg = ";color:#fff" if idx in (0, len(_RANK_SCALE) - 1) else ""
    return f"background:{bg}{fg}"


def _pivot_group_rank_css(metric: str, asin: str, day, present: list[str], table: pd.DataFrame) -> str:
    """Место позиции среди конкурентов под тем же нашим товаром в этот же день."""
    value = table.loc[asin, day] if asin in table.index else None
    if value is None or pd.isna(value):
        return ""
    vals = [(a, table.loc[a, day]) for a in present if a in table.index and pd.notnull(table.loc[a, day])]
    if len(vals) < 2:
        return ""
    better_first = metric in ("BSR", "Цена")
    vals.sort(key=lambda kv: kv[1], reverse=not better_first)
    pos = [a for a, _ in vals].index(asin)
    return _pivot_rank_css(pos, len(vals))


_PIVOT_CSS = """
<style>
.cmp-wrap { max-height:800px; overflow:auto; border:1px solid #e5e5ea; border-radius:12px; background:#fff; }
.cmp { border-collapse:separate; border-spacing:0; font-size:12.5px; min-width:100%; }
.cmp th { position:sticky; top:0; background:#f5f5f7; color:#6e6e73; font-weight:600;
   padding:8px 10px; border-bottom:1px solid #e5e5ea; white-space:nowrap; z-index:2; text-align:right; }
.cmp th:nth-child(-n+2) { text-align:left; }
.cmp td { padding:6px 10px; border-bottom:1px solid #f0f0f2; text-align:right; white-space:nowrap; }
.cmp td:nth-child(-n+2) { text-align:left; }
.cmp td.c-asin { position:sticky; left:0; background:#fff; z-index:1; min-width:130px;
          font-family:ui-monospace,Menlo,monospace; font-weight:600; }
.cmp td.c-name { position:sticky; left:130px; background:#fff; z-index:1; min-width:180px;
          max-width:220px; overflow:hidden; text-overflow:ellipsis; color:#6e6e73; }
.cmp td.c-asin a { color:#0071e3; text-decoration:none; }
.cmp td.nodata { background:#fff4f4; }
.cmp td.nodata a { color:#c5221f; }
.cmp tr.mine td { background:#eaf4ff; font-weight:600; }
.cmp tr.mine td.c-asin { background:#eaf4ff; box-shadow: inset 3px 0 0 #0071e3; }
.cmp tr.mine td.c-name { background:#eaf4ff; color:#0056b3; }
.cmp th:nth-child(1) { position:sticky; left:0; z-index:3; }
.cmp th:nth-child(2) { position:sticky; left:130px; z-index:3; }
.cmp tr.ghead td { background:#1d1d1f; color:#fff; font-size:13.5px; font-weight:650;
            padding:9px 12px; position:sticky; left:0; }
.cmp tr.mhead td { background:#e8e8ed; font-weight:650; padding:6px 12px;
            border-top:1px solid #c7c7cc; position:sticky; left:0; }
.cmp tr.sep td { background:#fafafa; color:#8e8e93; font-size:11.5px; padding:3px 12px;
          border-top:1px dashed #c7c7cc; position:sticky; left:0; }
</style>
"""


def _render_competitors_pivot(history: pd.DataFrame, pairs: pd.DataFrame) -> None:
    """Вкладка «Конкуренты»: как в Rating Radar — ASIN сгруппированы (там по товарной группе,
    здесь, за неимением групп в этой схеме, по нашему товару), метрики блоками, даты колонками,
    ячейки подсвечены по изменению к предыдущему замеру или по месту среди конкурентов."""
    if pairs.empty or "active" not in pairs or not pairs["active"].any():
        st.info("Активных пар пока нет — добавьте их во вкладке «⚙ Сбор и управление».")
        return
    active_pairs = pairs[pairs["active"]]

    markets = sorted(active_pairs["marketplace"].dropna().unique())
    f1, f2, f3, f4, f5 = st.columns([1.1, 1.8, 0.9, 1.4, 1.4])
    sel_market = f1.selectbox("Страна", markets, key="cmp_market")

    in_market = active_pairs[active_pairs["marketplace"] == sel_market]
    our_options = in_market.drop_duplicates("our_asin").set_index("our_asin")["our_product"].to_dict()
    sel_ours = f2.multiselect(
        "Наши товары", list(our_options), default=list(our_options), key=f"cmp_our_{sel_market}",
        format_func=lambda a: f"{a} · {str(our_options.get(a) or '')[:40]}",
        placeholder="все товары этой страны",
    )
    period = f3.selectbox("Период", [7, 14, 30, 60, 90], index=0,
                          format_func=lambda d: f"{d} дн.", key="cmp_period")
    available_metrics = [name for name, (o, c) in _PIVOT_METRICS.items()
                         if o in history.columns and c in history.columns]
    sel_metrics = f4.multiselect("Метрики", available_metrics, default=available_metrics, key="cmp_metrics")
    color_mode = f5.selectbox(
        "Раскраска", ["Изменение к прошлому замеру", "Место среди конкурентов"], key="cmp_color",
        help="«Изменение» — стало лучше или хуже со предыдущего замера. "
             "«Место среди конкурентов» — как позиция выглядит на фоне остальных под этим же нашим товаром.",
    )

    q = st.text_input("Поиск", key=f"cmp_q_{sel_market}", label_visibility="collapsed",
                      placeholder="🔍 поиск: ASIN, наш товар или конкурент")

    use_ours = sel_ours or list(our_options)
    view = in_market[in_market["our_asin"].isin(use_ours)]
    if q.strip():
        ql = q.strip().lower()
        hay = (view["our_asin"].astype(str) + " " + view["our_product"].astype(str) + " "
              + view["comp_asin"].astype(str) + " " + view["competitor_name"].astype(str)).str.lower()
        keep = set(view.loc[hay.str.contains(ql, na=False, regex=False), "our_asin"])
        use_ours = [a for a in use_ours if a in keep]
        view = view[view["our_asin"].isin(use_ours)]
        if not use_ours:
            st.warning(f"По запросу «{q}» ничего не найдено")
            return

    st.markdown(f"#### {sel_market} · {len(use_ours)} наших товаров"
               + (f" · фильтр: «{q}»" if q.strip() else ""))
    if not use_ours or not sel_metrics:
        st.info("Нечего показать: проверьте фильтры выше.")
        return

    # snapshot_date из базы приходит tz-naive (как и в _apply_filter выше) — сравниваем с ним
    # tz-naive курсором, иначе pandas падает на сравнении naive/aware.
    cutoff = datetime.now() - pd.Timedelta(days=int(period))
    hist = history[(history["marketplace"] == sel_market)
                  & (history["our_asin"].isin(use_ours))
                  & pd.to_datetime(history["snapshot_date"], errors="coerce").ge(cutoff)].copy()
    if hist.empty:
        st.warning("По этой стране и периоду ещё нет замеров.")
        return

    piv = {metric: _asin_day_pivot(hist, *_PIVOT_METRICS[metric]) for metric in sel_metrics}
    days_c = sorted({d for table in piv.values() for d in table.columns})
    if not days_c:
        st.warning("По этой стране и периоду ещё нет замеров.")
        return
    labels = [pd.Timestamp(d).strftime("%d.%m") for d in days_c]

    meta_comp = view.drop_duplicates("comp_asin").set_index("comp_asin")
    dom = pairs_store.DOMAIN_BY_MARKET.get(sel_market, "com")

    ncols = 2 + len(labels)
    rows_html: list[str] = []
    for our_asin in use_ours:
        group = view[view["our_asin"] == our_asin]
        if group.empty:
            continue
        product = str(group["our_product"].iloc[0] or "") if not group.empty else ""
        members = [our_asin] + list(group["comp_asin"])
        rows_html.append(
            f"<tr class='ghead'><td colspan='{ncols}'>▸ {escape(product) or escape(our_asin)} "
            f"<span style='font-weight:400;color:#6e6e73'>· {escape(our_asin)} · {escape(sel_market)} · "
            f"{len(members) - 1} конкурент(ов)</span></td></tr>"
        )
        for metric in sel_metrics:
            table = piv[metric]
            present = [a for a in members if a in table.index]
            if not present:
                continue
            rows_html.append(f"<tr class='mhead'><td colspan='{ncols}'>{escape(metric)}</td></tr>")
            for i_row, asin in enumerate(present):
                if i_row == 1:
                    rows_html.append(f"<tr class='sep'><td colspan='{ncols}'>— конкуренты —</td></tr>")
                mine = asin == our_asin
                name = product if mine else str(meta_comp.loc[asin, "competitor_name"] or "") if asin in meta_comp.index else ""
                no_data = all(pd.isna(table.loc[asin, d]) for d in days_c)
                flag = " ⚠️" if no_data else ""
                cls = "c-asin" + (" nodata" if no_data else "")
                tds = [
                    f"<td class='{cls}'><a href='https://www.amazon.{dom}/dp/{escape(asin)}' "
                    f"target='_blank'>{escape(asin)}</a>{flag}</td>",
                    f"<td class='c-name'>{escape((name or '—')[:40])}</td>",
                ]
                prev = None
                for d in days_c:
                    value = table.loc[asin, d] if d in table.columns else np.nan
                    css = (_pivot_change_css(metric, value, prev) if color_mode.startswith("Изменение")
                          else _pivot_group_rank_css(metric, asin, d, present, table))
                    tds.append(f"<td style='{css}'>{_pivot_value_fmt(metric, value)}</td>")
                    if pd.notnull(value):
                        prev = value
                rows_html.append(f"<tr class='{'mine' if mine else ''}'>{''.join(tds)}</tr>")

    th = "".join(f"<th>{escape(c)}</th>" for c in ["ASIN", "Название"] + labels)
    st.markdown(
        f"{_PIVOT_CSS}<div class='cmp-wrap'><table class='cmp'><thead><tr>{th}</tr></thead>"
        f"<tbody>{''.join(rows_html)}</tbody></table></div>",
        unsafe_allow_html=True,
    )
    st.caption(f"Замеров: {len(hist)} · дней: {len(days_c)} · наших товаров: {len(use_ours)}")


_FORECAST_WINDOWS = {"7 дней": 7, "14 дней": 14, "30 дней": 30}
_FORECAST_HORIZONS = {"7 дней": 7, "14 дней": 14, "30 дней": 30}
# Меньше трёх замеров — это не тренд, а две точки и совпадение. Такой ASIN в прогноз не берём.
MIN_FORECAST_POINTS = 3


def _daily_series(data: pd.DataFrame, value_column: str, asin_column: str) -> pd.DataFrame:
    """Значения по дням для одной стороны пары, приведённые к виду «ASIN, страна, день, число»."""
    if asin_column not in data or value_column not in data or "snapshot_date" not in data:
        return pd.DataFrame(columns=["ASIN", "Страна", "Дата", "Значение"])
    return pd.DataFrame({
        "ASIN": data[asin_column],
        "Страна": data["marketplace"] if "marketplace" in data else "",
        "Дата": pd.to_datetime(data["snapshot_date"], errors="coerce"),
        "Значение": pd.to_numeric(data[value_column], errors="coerce"),
    })


def _trend(days: pd.Series, values: pd.Series) -> float:
    """Типичное изменение BSR в день — медиана дневных изменений, а не прямая по всем точкам.

    На боевых данных BSR скачет на сотни тысяч за сутки, и метод наименьших квадратов давал
    наклоны вроде −180000 в день: проекция улетала в минус и упиралась в ноль почти у всех.
    Медиана не даёт одному выбросу задать тренд. Дни делим на реальный разрыв, поэтому
    пропущенный день не удваивает скорость.
    """
    gaps = days.diff().dt.days.astype(float)
    changes = values.astype(float).diff()
    daily = (changes / gaps).replace([np.inf, -np.inf], np.nan).dropna()
    if daily.empty:
        return 0.0
    return float(daily.median())


def _forecast_long(data: pd.DataFrame, window_days: int) -> pd.DataFrame:
    """Дневные точки BSR (наши и конкурентов вместе) за окно — общий срез для таблицы и графиков."""
    parts = [
        _daily_series(data, "our_bsr", "our_asin"),
        _daily_series(data, "comp_bsr", "comp_asin"),
    ]
    long = pd.concat([part for part in parts if not part.empty], ignore_index=True) if any(
        not part.empty for part in parts) else pd.DataFrame()
    if long.empty:
        return pd.DataFrame()
    long = long.dropna(subset=["Дата", "Значение"])
    long = long[long["ASIN"].astype(str).str.strip().ne("")]
    if long.empty:
        return pd.DataFrame()

    # Один ASIN на одном рынке за день — одно значение (он же повторяется в разных парах).
    long = long.groupby(["ASIN", "Страна", "Дата"], as_index=False)["Значение"].last()
    last_day = long["Дата"].max()
    return long[long["Дата"] > last_day - pd.Timedelta(days=window_days)]


def _forecast_table(data: pd.DataFrame, window_days: int, horizon_days: int) -> pd.DataFrame:
    """Куда идёт BSR каждого ASIN: сегодняшнее значение, изменение в день и простая проекция.

    Это прямая экстраполяция тренда, а не модель: она не знает про сезон, акции и новинки.
    Поэтому рядом всегда показывается, на скольких замерах она построена.
    """
    long = _forecast_long(data, window_days)
    if long.empty:
        return pd.DataFrame()

    rows = []
    for (asin, market), group in long.groupby(["ASIN", "Страна"]):
        group = group.sort_values("Дата")
        if len(group) < MIN_FORECAST_POINTS:
            continue
        slope = _trend(group["Дата"], group["Значение"])
        current = float(group["Значение"].iloc[-1])
        projected = current + slope * horizon_days
        rows.append({
            "Страна": market,
            "ASIN": asin,
            "BSR сейчас": current,
            "Изменение в день": slope,
            f"Прогноз через {horizon_days} дн.": max(projected, 0.0),
            "Замеров": len(group),
            # BSR: чем меньше, тем лучше, поэтому падение наклона — это рост позиций.
            "Тренд": "растём" if slope < 0 else ("падаем" if slope > 0 else "без движения"),
        })
    if not rows:
        return pd.DataFrame()
    return pd.DataFrame(rows).sort_values(["Изменение в день", "ASIN"], ignore_index=True)


_FORECAST_LEADERS = 15


def _forecast_leaders_chart(table: pd.DataFrame) -> alt.Chart:
    """Кто быстрее всего меняет позиции: столбцы «Изменение в день», худшие/лучшие сверху.

    Таблица уже отсортирована по возрастанию «Изменение в день» — растущие (BSR падает) первые."""
    leaders = table.head(_FORECAST_LEADERS).copy()
    leaders["Метка"] = leaders["Страна"] + " · " + leaders["ASIN"]
    return alt.Chart(leaders).mark_bar().encode(
        x=alt.X("Изменение в день:Q", title="Изменение BSR в день"),
        y=alt.Y("Метка:N", sort=list(leaders["Метка"]), title=None),
        color=alt.condition("datum['Изменение в день'] < 0", alt.value("#2e7d32"), alt.value("#c62828")),
        tooltip=["Страна", "ASIN", alt.Tooltip("BSR сейчас:Q", format=",.0f", title="BSR сейчас"),
                 alt.Tooltip("Изменение в день:Q", format="+,.0f", title="Изменение в день"), "Замеров"],
    ).properties(height=max(220, 26 * len(leaders)))


def _series_from_group(group: pd.DataFrame, horizon_days: int) -> pd.DataFrame:
    """История одного ASIN плюс прогнозная точка на горизонте. Последняя фактическая точка
    повторяется как начало прогнозной линии, чтобы пунктир продолжал сплошную линию, а не висел
    отдельной точкой в воздухе."""
    group = group.sort_values("Дата")
    if len(group) < MIN_FORECAST_POINTS:
        return pd.DataFrame()
    slope = _trend(group["Дата"], group["Значение"])
    current = float(group["Значение"].iloc[-1])
    last_day = group["Дата"].max()
    projected = max(current + slope * horizon_days, 0.0)
    fact = pd.DataFrame({"Дата": group["Дата"], "BSR": group["Значение"], "Тип": "Факт"})
    forecast = pd.DataFrame({
        "Дата": [last_day, last_day + pd.Timedelta(days=horizon_days)],
        "BSR": [current, projected],
        "Тип": "Прогноз",
    })
    return pd.concat([fact, forecast], ignore_index=True)


def _asin_forecast_series(data: pd.DataFrame, window_days: int, horizon_days: int,
                          asin: str, market: str) -> pd.DataFrame:
    """История BSR одного ASIN за окно плюс прогнозная точка на горизонте — для линейного графика."""
    long = _forecast_long(data, window_days)
    if long.empty:
        return pd.DataFrame()
    return _series_from_group(long[(long["ASIN"] == asin) & (long["Страна"] == market)], horizon_days)


def _forecast_series_many(data: pd.DataFrame, window_days: int, horizon_days: int,
                          labels: dict[tuple[str, str], str]) -> pd.DataFrame:
    """То же для нескольких ASIN сразу (ключ — (страна, ASIN)); у каждой линии своя «Метка».
    Срез по дням строится один раз на все ASIN, а не заново на каждый: при «Все ASIN» их сотни."""
    long = _forecast_long(data, window_days)
    if long.empty or not labels:
        return pd.DataFrame()
    long = long[[key in labels for key in zip(long["Страна"], long["ASIN"])]]
    parts = []
    for (asin, market), group in long.groupby(["ASIN", "Страна"]):
        series = _series_from_group(group, horizon_days)
        if not series.empty:
            parts.append(series.assign(**{"Метка": labels[(market, asin)]}))
    return pd.concat(parts, ignore_index=True) if parts else pd.DataFrame()


# Больше линий легенда не вмещает — тогда какая линия чья, видно по подсказке при наведении.
_FORECAST_LEGEND_MAX = 12


def _asin_forecast_line_chart(series: pd.DataFrame) -> alt.Chart:
    lines = series["Метка"].nunique() if "Метка" in series else 1
    if lines <= 1:
        return alt.Chart(series).mark_line(point=True, color="#1f6feb").encode(
            x=alt.X("Дата:T", title=None),
            y=alt.Y("BSR:Q", title="BSR", scale=alt.Scale(zero=False)),
            strokeDash=alt.StrokeDash("Тип:N", sort=["Факт", "Прогноз"], legend=alt.Legend(title=None)),
            tooltip=["Дата:T", alt.Tooltip("BSR:Q", format=",.0f", title="BSR"), "Тип"],
        ).properties(height=320)
    # У разных ASIN BSR отличается в сотни раз (1 200 и 450 000): на обычной шкале все, кроме
    # самых больших, слиплись бы в линию у нуля. Логарифмической шкале нужен BSR от 1 —
    # прогноз, упёршийся в ноль, рисуем на единице.
    chart_data = series.assign(BSR=series["BSR"].clip(lower=1))
    legend = alt.Legend(title=None) if lines <= _FORECAST_LEGEND_MAX else None
    return alt.Chart(chart_data).mark_line(point=lines <= _FORECAST_LEGEND_MAX).encode(
        x=alt.X("Дата:T", title=None),
        y=alt.Y("BSR:Q", title="BSR (логарифмическая шкала)", scale=alt.Scale(type="log")),
        color=alt.Color("Метка:N", legend=legend),
        detail="Метка:N",
        strokeDash=alt.StrokeDash("Тип:N", sort=["Факт", "Прогноз"], legend=alt.Legend(title=None)),
        tooltip=["Метка", "Дата:T", alt.Tooltip("BSR:Q", format=",.0f", title="BSR"), "Тип"],
    ).properties(height=420)


def _forecast_names(data: pd.DataFrame) -> dict[tuple[str, str], str]:
    """Название для подписи ASIN (наш товар или конкурент): последнее непустое по дате сбора —
    чтобы ASIN можно было найти в списке и по названию, а не только по коду."""
    names: dict[tuple[str, str], str] = {}
    if "marketplace" not in data:
        return names
    frame = data.sort_values("snapshot_date") if "snapshot_date" in data else data
    for asin_column, name_column in (("our_asin", "our_product"), ("comp_asin", "competitor_name")):
        if asin_column not in frame or name_column not in frame:
            continue
        for market, asin, name in zip(frame["marketplace"], frame[asin_column], frame[name_column]):
            if isinstance(name, str) and name.strip():
                names[(market, asin)] = " ".join(name.split())
    return names


_FORECAST_NAME_LENGTH = 40
_ALL_MARKETS = "Все страны"


def _forecast_label(market: str, asin: str, name: str | None) -> str:
    label = f"{market} · {asin}"
    if name:
        label += " · " + (name if len(name) <= _FORECAST_NAME_LENGTH else name[:_FORECAST_NAME_LENGTH - 1] + "…")
    return label


def _render_forecast(data: pd.DataFrame) -> None:
    if data.empty or "snapshot_date" not in data:
        st.info("Нет данных для прогноза.")
        return
    controls = st.columns(3)
    window = controls[0].selectbox("Считать по", list(_FORECAST_WINDOWS), index=1, key="forecast_window")
    horizon = controls[1].selectbox("Прогноз на", list(_FORECAST_HORIZONS), key="forecast_horizon")
    window_days, horizon_days = _FORECAST_WINDOWS[window], _FORECAST_HORIZONS[horizon]

    table = _forecast_table(data, window_days, horizon_days)
    markets = sorted(table["Страна"].dropna().unique()) if not table.empty else []
    market = controls[2].selectbox("Страна", [_ALL_MARKETS] + markets, key="forecast_market")
    if table.empty:
        st.info(f"Недостаточно замеров: для прогноза нужно хотя бы {MIN_FORECAST_POINTS} дня с данными.")
        return
    if market != _ALL_MARKETS:
        table = table[table["Страна"] == market].reset_index(drop=True)

    st.markdown('<p class="section-title">Кто быстрее всего растёт</p>', unsafe_allow_html=True)
    st.altair_chart(_forecast_leaders_chart(table), use_container_width=True)

    st.markdown('<p class="section-title">История и прогноз по ASIN</p>', unsafe_allow_html=True)
    names = _forecast_names(data)
    label_keys = {
        _forecast_label(row_market, asin, names.get((row_market, asin))): (row_market, asin)
        for row_market, asin in zip(table["Страна"], table["ASIN"])
    }
    labels = list(label_keys)
    # Ключи виджетов зависят от страны: у каждой страны свой список ASIN, и выбор из одной
    # страны не должен оставаться в списке другой.
    pick_all = st.checkbox(f"Выбрать все ({len(labels)})", key=f"forecast_all_{market}")
    if pick_all:
        picked = labels
    else:
        picked = st.multiselect(
            "ASIN", labels, default=labels[:1], key=f"forecast_asins_{market}",
            placeholder="Введите ASIN или название",
        )
    if not picked:
        st.info("Выберите хотя бы один ASIN.")
        return
    series = _forecast_series_many(
        data, window_days, horizon_days, {label_keys[label]: label for label in picked},
    )
    if series.empty:
        st.info("Недостаточно данных для графика.")
        return
    st.altair_chart(_asin_forecast_line_chart(series), use_container_width=True)


_ALL_DAYS = "Все дни"


def _pick_day(data: pd.DataFrame) -> pd.DataFrame:
    """Выбор конкретного дня: посмотреть срез за дату, не выискивая её в общей таблице.

    Самые свежие даты сверху — чаще всего нужен последний сбор или соседний с ним."""
    if data.empty or "snapshot_date" not in data:
        return data
    days = pd.to_datetime(data["snapshot_date"], errors="coerce").dropna()
    available = sorted({moment.date() for moment in days}, reverse=True)
    if not available:
        return data
    labels = {day.strftime("%d.%m.%Y"): day for day in available}
    picked = st.selectbox("День", [_ALL_DAYS, *labels], key="history_day")
    if picked == _ALL_DAYS:
        return data
    chosen = labels[picked]
    return data[pd.to_datetime(data["snapshot_date"], errors="coerce").dt.date == chosen]


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


def _corporate_domains() -> tuple[frozenset, str]:
    """(домены рабочей почты, причина блокировки). Оба пустые — секрета CORPORATE_EMAIL_DOMAINS нет,
    почту не спрашиваем. Секрет задан, но доменов в нём не нашлось, — управление закрыто."""
    raw = _secret("CORPORATE_EMAIL_DOMAINS")
    if raw:
        domains = access.parse_domain_list(raw)
        if not domains:
            return frozenset(), "Управление закрыто: в секрете CORPORATE_EMAIL_DOMAINS нет ни одного домена почты."
        return domains, ""
    if _secret_is_nested("CORPORATE_EMAIL_DOMAINS"):
        return frozenset(), "Управление закрыто: строка CORPORATE_EMAIL_DOMAINS стоит внутри секции секретов. Поднимите её выше первой секции в квадратных скобках."
    return frozenset(), ""


def _team_password_state() -> tuple[str, str]:
    """('open', '') — секретов нет, управление открыто всем, у кого есть ссылка; ('password', пароль);
    ('email', '') — пароля нет, но задан CORPORATE_EMAIL_DOMAINS: управление после ввода рабочей почты;
    ('locked', причина) — секрет задан неправильно, управление закрыто (не открываем по ошибке)."""
    _, domains_problem = _corporate_domains()
    if domains_problem:
        return "locked", domains_problem
    password = _secret("TEAM_PASSWORD")
    if password:
        if len(password) < access.MIN_PASSWORD_LENGTH:
            return "locked", f"Управление закрыто: пароль команды (секрет TEAM_PASSWORD) короче {access.MIN_PASSWORD_LENGTH} символов."
        return "password", password
    if _secret_is_nested("TEAM_PASSWORD"):
        return "locked", "Управление закрыто: строка TEAM_PASSWORD стоит внутри секции секретов. Поднимите её выше первой секции в квадратных скобках."
    if _corporate_domains()[0]:
        return "email", ""
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
        domains, _ = _corporate_domains()
        needs_password = state == "password"
        with st.form("unlock_form"):
            who = st.text_input("Рабочая почта" if domains else "Ваше имя", key="unlock_name")
            typed = st.text_input("Пароль команды", type="password", key="unlock_password") if needs_password else None
            submitted = st.form_submit_button("Открыть управление")
        if not submitted:
            return
        limiter = _login_limiter()
        if needs_password and not limiter.allowed():
            st.error(f"Слишком много неудачных попыток. Повторите через {limiter.retry_after() // 60 + 1} мин.")
            return
        # Домены в подсказке не называем: сайт публичный, а подсказка объяснила бы постороннему, что вписать.
        clean = access.corporate_email(who, domains) if domains else access.clean_actor_name(who)
        if clean is None and domains:
            st.error("Укажите свою рабочую (корпоративную) почту: личные адреса не подходят.")
        elif clean is None:
            st.error("Укажите имя (2–40 символов): оно попадёт в журнал изменений.")
        elif not needs_password or access.password_matches(typed, password):
            if needs_password:
                limiter.record_success()
            st.session_state["manager_name"] = clean
            usage_log.record(_connect, clean, "login")
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


def _render_recent_runs(overview: schedule_store.Overview, can_edit: bool) -> None:
    """Таблица последних запусков — в самом низу вкладки «Сбор и управление», после всего
    остального: это журнал для проверки, а не то, что нужно видеть первым."""
    if not overview.runs:
        return
    st.markdown('<p class="section-note">Последние запуски</p>', unsafe_allow_html=True)
    st.dataframe(_runs_table(overview.runs, show_errors=can_edit), use_container_width=True, hide_index=True)


def _render_schedule_tab(actor: str | None, role: str | None) -> schedule_store.Overview | None:
    _show_flash("schedule_flash")
    now = _now()
    try:
        overview = schedule_store.load_overview(_connect, now)
    except schedule_store.ScheduleStoreError as exc:
        st.error(str(exc))
        return None

    slots = [s for s in (overview.schedule, overview.schedule2) if s is not None]
    if not slots:
        st.info("Автосбор выключен: по расписанию данные не собираются.")
    else:
        parts = [f"после {s.hour:02d}:{s.minute:02d}" for s in sorted(slots, key=lambda s: (s.hour, s.minute))]
        st.success(f"Автосбор включён: каждый день {' и '.join(parts)} (Киев).")
        # Ранг слота среди включённых по времени — какой по счёту успех сегодня его закрывает
        # (db_runs.admission_block_reason считает так же: наступивший слот номер N открыт, пока
        # успехов сегодня меньше N).
        captions = []
        for rank, slot in enumerate(sorted(slots, key=lambda s: (s.hour, s.minute)), start=1):
            upcoming = schedule_store.next_run(now, slot, overview.collected_count >= rank)
            if not upcoming.due_now:
                captions.append(f"{upcoming.when:%d.%m в %H:%M}")
        if captions:
            st.caption(f"Следующий запуск: {'; '.join(captions)} (Киев).")
    if any(run["status"] == "running" for run in overview.runs):
        st.warning("Сейчас идёт сбор данных.")

    can_edit = access.has_role(role, access.ROLE_EDITOR)
    if not can_edit:
        st.caption("Чтобы менять время, откройте «🔒 Управление» вверху страницы.")
        return overview

    hour1, minute1 = schedule_store.to_slot(overview.schedule.hour, overview.schedule.minute) if overview.schedule else (9, 0)
    hour2, minute2 = schedule_store.to_slot(overview.schedule2.hour, overview.schedule2.minute) if overview.schedule2 else (18, 0)
    with st.form("schedule_form"):
        chosen1 = st.time_input("Время сбора 1 (по Киеву)", value=datetime(2000, 1, 1, hour1, minute1).time(), step=schedule_store.SLOT_MINUTES * 60, key="schedule_time")
        enabled1 = st.checkbox("Слот 1 включён", value=overview.schedule is not None, key="schedule_enabled")
        chosen2 = st.time_input("Время сбора 2 (по Киеву)", value=datetime(2000, 1, 1, hour2, minute2).time(), step=schedule_store.SLOT_MINUTES * 60, key="schedule_time2")
        enabled2 = st.checkbox("Слот 2 включён", value=overview.schedule2 is not None, key="schedule_enabled2")
        submitted = st.form_submit_button("Сохранить")
    if submitted:
        try:
            schedule_store.save_schedule(_connect, chosen1.hour, chosen1.minute, enabled1, slot=1, actor_role=role, actor=actor or "?")
            schedule_store.save_schedule(_connect, chosen2.hour, chosen2.minute, enabled2, slot=2, actor_role=role, actor=actor or "?")
        except (ValueError, access.AccessDenied, schedule_store.ScheduleStoreError) as exc:
            st.error(str(exc))
        else:
            usage_log.record(_connect, actor, "schedule_save")
            parts = []
            if enabled1:
                parts.append(f"слот 1 — {chosen1:%H:%M}")
            if enabled2:
                parts.append(f"слот 2 — {chosen2:%H:%M}")
            text = "Сохранено: автосбор включён, " + ", ".join(parts) + " (Киев)." if parts else "Сохранено: автосбор выключен."
            _set_flash("schedule_flash", "success", text)
            st.rerun()
    return overview


_USAGE_DAYS = 30
_USAGE_LOG_ROWS = 200
_USAGE_ACTION_LABELS = {
    "collect_all": "Сбор: всё", "collect_ours": "Сбор: наши", "collect_competitors": "Сбор: конкуренты",
    "login": "Вход в управление", "spot_check": "Точечная проверка", "schedule_save": "Время автосбора",
    "pairs_add": "Пары: добавление", "pairs_enable": "Пары: возврат", "pairs_disable": "Пары: отключение",
    "pairs_edit": "Пары: правка", "asins_add": "ASIN: добавление", "asins_edit": "ASIN: правка",
    "asins_disable": "ASIN: удаление", "export_csv": "Выгрузка CSV", "export_excel": "Выгрузка Excel",
}


def _usage_log_table(rows: list[dict]) -> pd.DataFrame:
    return pd.DataFrame([
        {
            "Когда (Киев)": row["created_at"].astimezone(schedule_store.TZ).strftime("%d.%m %H:%M"),
            "Кто": _short_name(row["user_name"]),
            "Что": _USAGE_ACTION_LABELS.get(row["action"], row["action"]),
            "Объём": f"{row['volume']} {row['unit'] or ''}".strip() if row["volume"] is not None else "",
        }
        for row in rows[:_USAGE_LOG_ROWS]
    ])


def _render_auth_bar(user: dict, email: str, role: str | None) -> None:
    """Кто вошёл: имя из Google, почта, роль — и «Выйти». Сюда попадает только сотрудник."""
    name = user.get("name")
    name_line = f"<strong>{escape(' '.join(name.split()))}</strong><br>" if isinstance(name, str) and name.strip() else ""
    status = _ROLE_LABELS.get(role, "только просмотр")
    st.markdown(f'<div class="section-note">{name_line}{escape(email)} · {escape(status)}</div>', unsafe_allow_html=True)
    st.button("Выйти", on_click=st.logout, key="logout_btn")


_JOURNAL_DAYS = 30
_JOURNAL_WEEKS = 12
_JOURNAL_PERIODS = (7, 14, 30, 60)


@dataclass
class LoginJournal:
    """Входы за период: сводка по сотрудникам (с процентами) и сами входы, время киевское."""
    summary: pd.DataFrame
    log: pd.DataFrame
    period_days: int
    weekly: pd.DataFrame | None = None


def _login_journal(rows: list, today, days: int = _JOURNAL_DAYS) -> LoginJournal:
    """Сводка за последние days дней (как в Rating Radar — весь выбранный период). «Доля входов» —
    сколько из всех входов команды пришлось на человека, «Активность» — в сколько процентов дней
    периода он заходил хотя бы раз."""
    log = pd.DataFrame(rows, columns=["email", "logged_in_at"])
    if log.empty:
        return LoginJournal(pd.DataFrame(), log, 0, pd.DataFrame())
    # Домен у всех один и тот же — без него таблицы читаются легче.
    log["email"] = log["email"].map(_short_name)
    log["logged_in_at"] = pd.to_datetime(log["logged_in_at"], utc=True).dt.tz_convert(schedule_store.TZ)
    log["day"] = log["logged_in_at"].dt.date
    weekly = _weekly_users(log, today)
    period_days = max(1, days)
    # Неделям нужна история длиннее периода; сводка и список — только за последние days дней.
    log = log[log["day"] > today - timedelta(days=days)].reset_index(drop=True)
    if log.empty:
        return LoginJournal(pd.DataFrame(), log.drop(columns="day"), period_days, weekly)
    summary = log.groupby("email").agg(
        logins=("logged_in_at", "count"), active_days=("day", "nunique"), last=("logged_in_at", "max"),
    ).reset_index()
    summary["share"] = (summary["logins"] / summary["logins"].sum() * 100).round().astype(int)
    summary["activity"] = (summary["active_days"] / period_days * 100).round().clip(upper=100).astype(int)
    summary = summary.sort_values(["logins", "last"], ascending=False, ignore_index=True)
    summary["last"] = summary["last"].dt.strftime("%d.%m %H:%M")
    log = log.drop(columns="day").sort_values("logged_in_at", ascending=False, ignore_index=True)
    log["logged_in_at"] = log["logged_in_at"].dt.strftime("%d.%m.%Y %H:%M")
    return LoginJournal(summary, log, period_days, weekly)


def _weekly_users(log: pd.DataFrame, today, weeks: int = _JOURNAL_WEEKS) -> pd.DataFrame:
    """Сколько разных сотрудников входило за каждую неделю (с понедельника, по Киеву). Начинается с
    недели первого записанного входа — пустые недели до начала журнала выглядели бы как «никого»."""
    week_of = lambda day: day - timedelta(days=day.weekday())  # noqa: E731
    this_week = week_of(today)
    first = max(min(week_of(day) for day in log["day"]), this_week - timedelta(weeks=weeks - 1))
    starts = [first + timedelta(weeks=i) for i in range((this_week - first).days // 7 + 1)]
    users = log.assign(week=log["day"].map(week_of)).groupby("week")["email"].nunique()
    return pd.DataFrame({
        "week": [start.strftime("%d.%m") + (" (идёт)" if start == this_week else "") for start in starts],
        "users": [int(users.get(start, 0)) for start in starts],
    })


def _weekly_users_chart(weekly: pd.DataFrame) -> alt.Chart:
    return alt.Chart(weekly).mark_bar(
        color="#168ed0", cornerRadiusTopLeft=4, cornerRadiusTopRight=4, size=26,
    ).encode(
        x=alt.X("week:O", sort=list(weekly["week"]), title="Неделя с", axis=alt.Axis(labelAngle=0)),
        y=alt.Y("users:Q", title="Сотрудников", axis=alt.Axis(tickMinStep=1, format="d")),
        tooltip=[alt.Tooltip("week:O", title="Неделя с"), alt.Tooltip("users:Q", title="Сотрудников")],
    ).properties(height=220)


def _load_logins() -> list | str:
    """Входы за _JOURNAL_WEEKS недель или текст ошибки: недоступный журнал не должен ломать дашборд."""
    try:
        return access.recent_logins(_connect, _JOURNAL_WEEKS * 7)
    except access.AccessStoreError as exc:
        return str(exc)


def _load_allowed() -> list | str | None:
    """Допущенные для Scorecard; None — таблицы ещё нет (миграция 015); текст — ошибка."""
    try:
        if not access.allowed_table_exists(_connect):
            return None
        return access.list_allowed(_connect)
    except access.AccessStoreError as exc:
        return str(exc)


_SCORECARD_WORKDAYS = 5


@dataclass
class Scorecard:
    """Scorecard за последние 7 дней — как в Rating Radar. «Регулярность» = в среднем дней со входом на
    человека / 5 рабочих дней × 100; «Зашли» — сколько допущенных заходили хоть раз. last_* — то же за
    предыдущие 7 дней. by_dates — те же цифры с шагом в неделю назад (старые сверху)."""
    regularity: int
    last_regularity: int
    avg_days: float
    seen: int
    total: int
    pct: int
    by_dates: pd.DataFrame
    # Кто в знаменателе: «допущенных» (список allowed_users) или, пока списка нет, все заходившие.
    base: str = "допущенных"

    @property
    def delta(self) -> int:
        """Изменение регулярности к прошлой неделе, в процентных пунктах."""
        return self.regularity - self.last_regularity


def _scorecard(rows: list, allowed: Iterable[str], now: datetime, weeks: int = _JOURNAL_WEEKS) -> Scorecard | None:
    """Считаются только люди из списка: зашедший не из списка не поднимает цифры выше 100%."""
    allowed = {email for email in (access.normalize_email(item) for item in allowed) if email}
    if not allowed:
        return None
    logins = [
        (access.normalize_email(row["email"]), pd.Timestamp(row["logged_in_at"]).tz_convert("UTC"))
        for row in rows
    ]
    now = pd.Timestamp(now).tz_convert("UTC")
    week = pd.Timedelta(days=7)

    def window(end) -> tuple[int, float]:
        """(сколько допущенных заходили, в среднем дней со входом на допущенного) за 7 суток до end."""
        days: dict[str, set] = {}
        for email, at in logins:
            if email in allowed and end - week < at <= end:
                days.setdefault(email, set()).add(at.tz_convert(schedule_store.TZ).date())
        return len(days), sum(len(d) for d in days.values()) / len(allowed)

    def regularity(avg_days: float) -> int:
        return min(100, round(avg_days / _SCORECARD_WORKDAYS * 100))

    first = min((at for _, at in logins), default=now)
    dates = []
    for k in range(weeks):
        end = now - k * week
        if k and end <= first:
            break
        seen, avg = window(end)
        dates.append({
            "date": end.tz_convert(schedule_store.TZ).strftime("%d.%m") + (" (сейчас)" if k == 0 else ""),
            "users": seen, "regularity": regularity(avg),
        })
    seen, avg = window(now)
    _, last_avg = window(now - week)
    return Scorecard(
        regularity=regularity(avg), last_regularity=regularity(last_avg), avg_days=avg, seen=seen,
        total=len(allowed), pct=round(100 * seen / len(allowed)),
        by_dates=pd.DataFrame(dates[::-1], columns=["date", "users", "regularity"]),
    )


def _render_scorecard(card: Scorecard) -> None:
    st.markdown('<p class="section-title">% для Scorecard — последние 7 дней</p>', unsafe_allow_html=True)
    left, middle, right = st.columns(3)
    left.metric(
        "Регулярность", f"{card.regularity}%", delta=f"{card.delta:+d} п.п. к прошлой неделе",
        delta_color="normal" if card.delta else "off",
        help=f"В среднем дней со входом на человека / {_SCORECARD_WORKDAYS} рабочих дней × 100.",
    )
    middle.metric("Зашли", f"{card.seen} из {card.total}",
                  help=f"Сколько {card.base} заходили хотя бы раз за 7 дней.")
    right.metric("В среднем дней", f"{card.avg_days:.1f} из {_SCORECARD_WORKDAYS}",
                 help="Сколько разных дней за неделю в среднем заходил один человек.")


def _render_scorecard_dates(card: Scorecard) -> None:
    st.markdown('<p class="section-title">% для Scorecard по датам</p>', unsafe_allow_html=True)
    st.dataframe(
        card.by_dates.rename(columns={"date": "Дата колонки Scorecard", "users": "Зашли",
                                      "regularity": "Регулярность, %"}),
        use_container_width=True, hide_index=True,
        column_config={"Регулярность, %": st.column_config.ProgressColumn(format="%d%%", min_value=0, max_value=100)},
    )


def _render_allowed_admin(allowed: list | str | None, actor_email: str, actor_role: str) -> None:
    """Список допущенных для Scorecard — только админ. Вход на сайт он не ограничивает.
    Пока таблицы нет (миграция 015), раздела не видно вовсе."""
    if allowed is None:
        return
    st.markdown('<p class="section-title">Допущенные для Scorecard</p>', unsafe_allow_html=True)
    if isinstance(allowed, str):
        st.error(allowed)
        return
    _show_flash("allowed_flash")
    if allowed:
        st.dataframe(pd.DataFrame({"Почта": allowed}), use_container_width=True, hide_index=True)
    with st.form("allowed_add_form", clear_on_submit=True):
        new_email = st.text_input(f"Добавить (рабочий адрес @{access.EMPLOYEE_DOMAIN})", key="allowed_add_email")
        if st.form_submit_button("Добавить"):
            try:
                saved = access.add_allowed(_connect, new_email, actor_role=actor_role, actor_email=actor_email)
            except (ValueError, access.AccessDenied, access.AccessStoreError) as exc:
                st.error(str(exc))
            else:
                _set_flash("allowed_flash", "success", f"Добавлен: {saved}")
                st.rerun()
    if allowed:
        with st.form("allowed_remove_form"):
            target = st.selectbox("Убрать из списка", allowed, key="allowed_remove_email")
            if st.form_submit_button("Убрать"):
                try:
                    access.remove_allowed(_connect, target, actor_role=actor_role)
                except (ValueError, access.AccessDenied, access.AccessStoreError) as exc:
                    st.error(str(exc))
                else:
                    _set_flash("allowed_flash", "success", f"Убран: {target}")
                    st.rerun()


TAB_TITLES = ["📋 Текущее состояние", "📅 История", "📈 Прогноз", "🥊 Пары конкурентов",
              "⚙ Сбор и управление", "ℹ️ Как это работает", "📊 Активность дашборда"]
_TAB_KEY = "main_tab"
_SECTION_RENAMES = {"📒 Журнал": TAB_TITLES[-1]}
# Действия чаще раза в 15 секунд в журнал не пишутся (кроме открытия раздела): на время сессии
# это почти не влияет, а базу на каждый клик не дёргает.
_ACTIVITY_PING_SECONDS = 15


@st.cache_resource(ttl=600)
def _activity_on() -> bool:
    """Есть ли таблица журнала действий (миграция 014). Проверяется раз в 10 минут, а не на каждое действие."""
    try:
        return activity.table_exists(_connect)
    except activity.ActivityError:
        return False


def _record_activity(email: str) -> None:
    """Действие на странице — строка в журнал. Сессия — эта открытая страница; раздел — открытая вкладка
    (её Streamlit сообщает через key вкладок). Сбой журнала работе не мешает."""
    if not _activity_on():
        return
    state = st.session_state
    if "activity_session" not in state:
        state["activity_session"] = uuid.uuid4().hex
    section = state.get(_TAB_KEY) or TAB_TITLES[0]
    now = time.monotonic()
    if section != state.get("activity_section"):
        state["activity_section"] = section
    elif now - state.get("activity_at", -_ACTIVITY_PING_SECONDS) < _ACTIVITY_PING_SECONDS:
        return
    else:
        section = None
    state["activity_at"] = now
    activity.record(_connect, state["activity_session"], email, section)


def _load_activity() -> list | str | None:
    """Строки журнала действий; None — журнал ещё не подключён; текст — ошибка."""
    if not _activity_on():
        return None
    try:
        return activity.recent(_connect, max(_JOURNAL_PERIODS))
    except activity.ActivityError as exc:
        return str(exc)


def _short_name(email: str) -> str:
    return email.removesuffix(f"@{access.EMPLOYEE_DOMAIN}")


def _render_time_spent(rows: list) -> None:
    st.markdown('<p class="section-title">Сколько времени проводят</p>', unsafe_allow_html=True)
    st.markdown(
        '<p class="section-note">Сессия — от первого до последнего действия на открытой странице '
        "(клик, фильтр, вкладка); перерыв больше 30 минут начинает новую. Открыл и закрыл — «0 сек»; "
        "если человек только читает, ничего не нажимая, это время не видно.</p>",
        unsafe_allow_html=True,
    )
    table = activity.time_by_employee(activity.sessions(rows))
    if table.empty:
        st.info("Сессий пока не записано.")
        return
    for column in ("avg", "longest", "total"):
        table[column] = table[column].map(activity.format_duration)
    table["email"] = table["email"].map(_short_name)
    st.dataframe(
        table.rename(columns={
            "email": "Сотрудник", "sessions": "Сессий", "avg": "В среднем", "longest": "Самая долгая",
            "total": "Всего", "short": "Короче минуты",
        }),
        use_container_width=True, hide_index=True,
    )


def _render_sections(rows: list) -> None:
    st.markdown('<p class="section-title">Какие разделы открывают</p>', unsafe_allow_html=True)
    # Вкладка «Активность дашборда» раньше называлась «📒 Журнал»: старые записи считаем под новым именем.
    rows = [{**row, "section": _SECTION_RENAMES.get(row["section"], row["section"])} for row in rows]
    # Открытия вкладок, которых больше нет (например, убранной «🛡 Продавцы»), не показываем.
    rows = [row for row in rows if row["section"] is None or row["section"] in TAB_TITLES]
    table = activity.sections(rows, TAB_TITLES)
    table["last"] = [
        moment.tz_convert(schedule_store.TZ).strftime("%d.%m %H:%M") if pd.notna(moment) else "—"
        for moment in table["last"]
    ]
    opened = table[table["opens"] > 0]
    if not opened.empty:
        st.altair_chart(_sections_chart(opened), use_container_width=True)
    # В таблице и неоткрытые разделы — с нулём: по ним видно, что можно убрать.
    st.dataframe(
        table[["section", "opens", "employees", "last"]].rename(columns={
            "section": "Раздел", "opens": "Открытий", "employees": "Людей", "last": "Последний раз",
        }),
        use_container_width=True, hide_index=True,
    )


def _sections_chart(opened: pd.DataFrame) -> alt.Chart:
    bars = alt.Chart(opened).encode(
        y=alt.Y("section:N", sort=list(opened["section"]), title=None, axis=alt.Axis(labelLimit=260)),
        x=alt.X("opens:Q", title=None, axis=alt.Axis(tickMinStep=1, format="d")),
        tooltip=[alt.Tooltip("section:N", title="Раздел"), alt.Tooltip("opens:Q", title="Открытий"),
                 alt.Tooltip("employees:Q", title="Людей")],
    )
    return (
        bars.mark_bar(color="#168ed0", cornerRadiusTopRight=4, cornerRadiusBottomRight=4, size=22)
        + bars.mark_text(align="left", dx=4, color="#334155").encode(text="opens:Q")
    ).properties(height=max(90, 38 * len(opened)))


# Что считается правкой, а что запуском или выгрузкой. Вход по паролю команды — не правка, в счёт не идёт.
_USAGE_KINDS = {
    "edits": {"schedule_save", "pairs_add", "pairs_enable", "pairs_disable", "pairs_edit",
              "asins_add", "asins_edit", "asins_disable"},
    "runs": {"collect_all", "collect_ours", "collect_competitors", "spot_check"},
    "exports": {"export_csv", "export_excel"},
}


def _changes_by_person(rows: list[dict]) -> pd.DataFrame:
    """По сотруднику: правок, запусков, выгрузок и когда была последняя правка. Больше правок — выше."""
    people: dict[str, dict] = {}
    for row in rows:
        kind = next((name for name, actions in _USAGE_KINDS.items() if row["action"] in actions), None)
        if kind is None:
            continue
        entry = people.setdefault(_short_name(row["user_name"]),
                                  {"edits": 0, "runs": 0, "exports": 0, "last_edit": None})
        entry[kind] += 1
        if kind == "edits" and (entry["last_edit"] is None or row["created_at"] > entry["last_edit"]):
            entry["last_edit"] = row["created_at"]
    ordered = sorted(people.items(), key=lambda item: (-item[1]["edits"], -item[1]["runs"], item[0]))
    return pd.DataFrame([
        {
            "Сотрудник": name, "Правок": entry["edits"], "Запусков": entry["runs"], "Выгрузок": entry["exports"],
            "Последняя правка": entry["last_edit"].astimezone(schedule_store.TZ).strftime("%d.%m %H:%M")
            if entry["last_edit"] else "—",
        }
        for name, entry in ordered
    ], columns=["Сотрудник", "Правок", "Запусков", "Выгрузок", "Последняя правка"])


def _load_usage() -> list | str | None:
    """Журнал действий-правок (public.usage_logs); None — таблицы ещё нет; текст — ошибка."""
    try:
        if not usage_log.log_exists(_connect):
            return None
        return usage_log.recent(_connect, max(_JOURNAL_PERIODS))
    except usage_log.UsageLogError as exc:
        return str(exc)


def _render_changes(rows: list | str | None) -> None:
    st.markdown('<p class="section-title">Кто что меняет</p>', unsafe_allow_html=True)
    if rows is None:
        st.info("Журнал правок ещё не подключён: действия выполняются, но пока не записываются.")
        return
    if isinstance(rows, str):
        st.error(rows)
        return
    table = _changes_by_person(rows)
    if table.empty:
        st.info("Правок и запусков пока не было.")
        return
    st.dataframe(table, use_container_width=True, hide_index=True)
    with st.expander("Все действия по времени"):
        st.dataframe(_usage_log_table(rows), use_container_width=True, hide_index=True)


def _render_journal_tab(email: str, role: str | None) -> None:
    """Порядок как в Rating Radar: заголовок с «Обновить», Scorecard за 7 дней, период, кто пользуется,
    разделы; ниже — наши дополнительные разделы и управление для админа."""
    head, refresh = st.columns([5, 1])
    head.markdown('<p class="section-title">Активность дашборда</p>', unsafe_allow_html=True)
    if refresh.button("🔄 Обновить", key="journal_refresh", use_container_width=True):
        _activity_on.clear()  # журнал действий могли только что подключить
    logins, allowed = _load_logins(), _load_allowed()
    card = None
    if isinstance(logins, list):
        card = _scorecard(logins, allowed, _now()) if isinstance(allowed, list) and allowed else None
        if card is None:
            # Пока список допущенных не заведён (нет таблицы или он пуст), знаменатель — все, кто заходил
            # за _JOURNAL_WEEKS недель: цифры видно сразу, а с заполненным списком они перейдут на него.
            card = _scorecard(logins, [row["email"] for row in logins], _now())
            if card is not None:
                card.base = f"заходивших за {_JOURNAL_WEEKS} нед."
    if card is not None:
        _render_scorecard(card)
    days = st.radio("Период", _JOURNAL_PERIODS, index=_JOURNAL_PERIODS.index(_JOURNAL_DAYS), horizontal=True,
                    format_func=lambda value: f"{value} дн.", key="journal_period")
    journal = logins if isinstance(logins, str) else _login_journal(logins, _now().date(), days)
    if isinstance(allowed, str) and role != access.ROLE_ADMIN:
        allowed = None  # ошибку списка допущенных видит админ в своём разделе; остальным она ни к чему
    since = _now() - timedelta(days=days)
    actions, usage = _load_activity(), _load_usage()
    if isinstance(actions, list):
        actions = [row for row in actions if row["at"] >= since]
    if isinstance(usage, list):
        usage = [row for row in usage if row["created_at"] >= since]
    _render_journal(journal, actions, email, role, usage, card, allowed)


def _people_table(summary: pd.DataFrame, actions: list | str | None, usage: list | str | None) -> pd.DataFrame:
    """«Кто пользуется» как в Rating Radar: входы и дни плюс сколько разделов открыл и сколько правок сделал."""
    opened: dict[str, int] = {}
    for row in actions if isinstance(actions, list) else []:
        if row["section"]:
            name = _short_name(row["email"])
            opened[name] = opened.get(name, 0) + 1
    edits: dict[str, int] = {}
    for row in usage if isinstance(usage, list) else []:
        if row["action"] in _USAGE_KINDS["edits"]:
            name = _short_name(row["user_name"])
            edits[name] = edits.get(name, 0) + 1
    return pd.DataFrame({
        "Сотрудник": summary["email"], "Входов": summary["logins"], "Дней": summary["active_days"],
        "Доля входов, %": summary["share"], "Активность, %": summary["activity"],
        "Открыл разделов": [opened.get(name, 0) for name in summary["email"]],
        "Правок": [edits.get(name, 0) for name in summary["email"]],
        "Последний вход": summary["last"],
    })


def _render_journal(journal: LoginJournal | str, actions: list | str | None, email: str, role: str | None,
                    usage: list | str | None = None, card: Scorecard | None = None,
                    allowed: list | str | None = None) -> None:
    """Вкладка «Активность дашборда» после Scorecard и периода. Всё видят все сотрудники, кроме управления
    списком допущенных и ролями — оно только админу."""
    if isinstance(journal, str):
        st.error(journal)
    elif journal.summary.empty and (journal.weekly is None or journal.weekly.empty):
        st.info("Входов пока не записано.")
    elif journal.summary.empty:
        st.info(f"За {journal.period_days} дн. входов не было.")
    else:
        st.markdown('<p class="section-title">Кто пользуется</p>', unsafe_allow_html=True)
        st.dataframe(
            _people_table(journal.summary, actions, usage), use_container_width=True, hide_index=True,
            column_config={
                "Доля входов, %": st.column_config.ProgressColumn(format="%d%%", min_value=0, max_value=100),
                "Активность, %": st.column_config.ProgressColumn(format="%d%%", min_value=0, max_value=100),
            },
        )

    # actions is None — таблицы журнала действий (миграция 014) ещё нет: разделы просто не показываются.
    if isinstance(actions, str):
        st.error(actions)
    elif actions is not None:
        _render_sections(actions)
        _render_time_spent(actions)
    _render_changes(usage)

    if isinstance(journal, LoginJournal) and journal.weekly is not None and not journal.weekly.empty:
        st.markdown('<p class="section-title">Сотрудников со входом по неделям</p>', unsafe_allow_html=True)
        st.altair_chart(_weekly_users_chart(journal.weekly), use_container_width=True)
    if card is not None:
        _render_scorecard_dates(card)

    if isinstance(journal, LoginJournal) and not journal.log.empty:
        st.markdown('<p class="section-title">Все входы</p>', unsafe_allow_html=True)
        st.dataframe(
            journal.log.rename(columns={"email": "Сотрудник", "logged_in_at": "Когда"}),
            use_container_width=True, hide_index=True,
        )
    if role == access.ROLE_ADMIN:
        _render_allowed_admin(allowed, email, role)
        st.markdown('<p class="section-title">Пользователи и роли</p>', unsafe_allow_html=True)
        _render_users_panel(email, role)


def _render_users_panel(actor_email: str, actor_role: str) -> None:
    _show_flash("users_flash")
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
    _require_employee(user, email)
    _record_activity(email)
    left, right = st.columns([3, 2])
    with left:
        _render_brand()
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
    # Кнопка бота — в том же вызове, что и строка статуса: отдельным элементом Streamlit
    # поставил бы перед ней свой отступ, и она повисла бы сама по себе.
    st.markdown(
        f'<div class="status-box"><strong>Последний сбор в базе: {escape(latest_label)}</strong></div>'
        f'{_bot_link_html()}',
        unsafe_allow_html=True,
    )

    choice = _filter_controls([current, history])
    shown = _apply_filter(current, choice)
    shown_history = _apply_filter(history, choice)

    _render_overview(shown)

    tabs = st.tabs(TAB_TITLES, key=_TAB_KEY, on_change="rerun")
    current_tab, history_tab, forecast_tab, pairs_tab, schedule_tab, how_tab, journal_tab = tabs

    with current_tab:
        _table_or_note(shown, with_images=True)

    with history_tab:
        _render_history_matrix(shown_history)
        _table_or_note(_pick_day(shown_history), with_images=True)

    with forecast_tab:
        _render_forecast(shown_history)

    with pairs_tab:
        _render_competitors_pivot(history, pairs)

    with schedule_tab:
        can_edit = access.has_role(manage_role, access.ROLE_EDITOR)
        left, right = st.columns(2)
        with left:
            st.markdown('<p class="section-title">Автосбор</p>', unsafe_allow_html=True)
            overview = _render_schedule_tab(actor, manage_role)
        with right:
            collect_ui.render_spot_check(_secret("SCRAPINGDOG_TOKEN"), can_edit, _connect, actor)
        if can_edit:
            pairs_ui.render_pairs_management(_connect, pairs, actor, manage_role, _max_active(), key_prefix="collect")
            pairs_ui.render_pairs_disable_restore(_connect, pairs, actor, manage_role)
        collect_ui.render_run_block(
            _connect, pairs, _admission_preview_cached,
            _secret("GITHUB_DISPATCH_TOKEN"), _secret("GITHUB_REPO") or github_dispatch.DEFAULT_REPO, can_edit,
            actor,
        )
        if overview is not None:
            _render_recent_runs(overview, can_edit)

    with how_tab:
        _render_how_it_works(pairs, shown_history)

    with journal_tab:
        # Журнал читает базу, поэтому только когда вкладка открыта, а не при каждом действии.
        if journal_tab.open:
            _render_journal_tab(email, role)

    download_left, download_right, _ = st.columns([1, 1, 4])
    with download_left:
        st.download_button(
            "⬇ CSV", current.to_csv(index=False).encode("utf-8-sig"),
            file_name="current.csv", mime="text/csv",
            on_click=usage_log.record, args=(_connect, actor, "export_csv", len(current), "строк"),
        )
    with download_right:
        st.download_button(
            "⬇ Excel с цветами", _current_to_excel_bytes(current),
            file_name="current.xlsx", mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            on_click=usage_log.record, args=(_connect, actor, "export_excel", len(current), "строк"),
        )


if __name__ == "__main__":
    main()
