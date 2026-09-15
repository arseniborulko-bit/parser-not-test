"""
SaaS-дашборд, читающий данные из Postgres (schema parser_not_test), а не из
Google Sheets API напрямую. Данные в базу попадают через sync_sheets_to_db.py.

Пока читает из базы каждый раз при обновлении (без записи куда-либо) — кнопка
запуска парсера и панель расписания добавляются отдельными следующими шагами.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime
from html import escape
from pathlib import Path

import pandas as pd
import psycopg2
import streamlit as st
from dotenv import load_dotenv

import scheduler

load_dotenv()

PROJECT_DIR = Path(__file__).resolve().parent
STATUS_FILE = PROJECT_DIR / "run_status.json"
RUNNER_SCRIPT = PROJECT_DIR / "run_parser_and_sync.py"

AMAZON_DOMAINS = {
    "US": "com", "CA": "ca", "UK": "co.uk", "DE": "de", "FR": "fr",
    "ES": "es", "IT": "it", "MX": "com.mx", "JP": "co.jp", "AU": "com.au",
}

st.set_page_config(page_title="Amazon Parser Dashboard", page_icon="📦", layout="wide")


def _apply_design() -> None:
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
        .source-badge { display: inline-flex; align-items: center; gap: .4rem; margin-top: .5rem; padding: .3rem .7rem; background: #ecfdf5; color: #047857; border-radius: 999px; font-weight: 700; font-size: .78rem; }
        .metric-card { background: #ffffff; border: 1px solid #e5e7eb; border-radius: 15px; padding: 1rem 1.15rem; min-height: 120px; box-shadow: 0 1px 2px rgba(15,23,42,.025); }
        .metric-label { color: #64748b; font-size: .72rem; letter-spacing: .065em; text-transform: uppercase; }
        .metric-value { color: #111827; font-size: 1.85rem; font-weight: 800; line-height: 1.25; margin: .25rem 0; }
        .metric-detail { color: #64748b; font-size: .78rem; }
        .section-title { font-size: 1.55rem; font-weight: 750; margin: 1.75rem 0 .2rem; }
        .section-note { color: #64748b; font-size: .86rem; margin-bottom: .6rem; }
        div[data-testid="stTabs"] [data-baseweb="tab-list"] { gap: 3px; border-bottom: 1px solid #dfe3ea; }
        div[data-testid="stTabs"] button,
        div[data-testid="stTabs"] [data-baseweb="tab"] { background: #121826 !important; color: #fff !important; border-radius: 7px 7px 0 0; padding: .6rem 1rem; margin-right: 2px; opacity: 1 !important; }
        div[data-testid="stTabs"] button *,
        div[data-testid="stTabs"] [data-baseweb="tab"] * { color: #fff !important; opacity: 1 !important; }
        div[data-testid="stTabs"] button[aria-selected="true"],
        div[data-testid="stTabs"] [aria-selected="true"] { background: #168ed0 !important; color: #fff !important; }
        .stButton > button { background: #168ed0; color: #fff; border: 0; border-radius: 8px; font-weight: 650; }
        .stButton > button:hover { background: #075b9b; color: #fff; }
        </style>
        """,
        unsafe_allow_html=True,
    )


@st.cache_data(ttl=60, show_spinner=False)
def load_snapshots() -> pd.DataFrame:
    conn = psycopg2.connect(os.environ["DATABASE_URL"])
    try:
        return pd.read_sql(
            """
            SELECT snapshot_date, marketplace, currency, our_asin, our_product, our_price,
                   our_bsr, our_bsr_delta_24h, comp_asin, competitor_name, comp_price,
                   comp_bsr, comp_bsr_delta_24h, comp_stock, price_diff_pct, updated_at
            FROM parser_not_test.snapshots
            ORDER BY snapshot_date DESC
            """,
            conn,
        )
    finally:
        conn.close()


@st.cache_data(ttl=60, show_spinner=False)
def load_current() -> pd.DataFrame:
    """Последний снепшот на каждую пару (our_asin, comp_asin)."""
    conn = psycopg2.connect(os.environ["DATABASE_URL"])
    try:
        return pd.read_sql(
            """
            SELECT DISTINCT ON (our_asin, comp_asin)
                   snapshot_date, marketplace, currency, our_asin, our_product, our_price,
                   our_bsr, our_bsr_delta_24h, comp_asin, competitor_name, comp_price,
                   comp_bsr, comp_bsr_delta_24h, comp_stock, price_diff_pct, updated_at
            FROM parser_not_test.snapshots
            ORDER BY our_asin, comp_asin, snapshot_date DESC
            """,
            conn,
        )
    finally:
        conn.close()


@st.cache_data(ttl=60, show_spinner=False)
def load_competitor_pairs() -> pd.DataFrame:
    conn = psycopg2.connect(os.environ["DATABASE_URL"])
    try:
        return pd.read_sql(
            """
            SELECT marketplace, our_asin, our_product, comp_asin, competitor_name, active
            FROM parser_not_test.competitor_pairs
            ORDER BY marketplace, our_asin
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

    if not data.empty:
        chart_data = data.copy()
        chart_data["snapshot_date"] = pd.to_datetime(chart_data["snapshot_date"], errors="coerce")
        chart_data = chart_data.dropna(subset=["snapshot_date"])
        if not chart_data.empty:
            st.caption("Количество записей по датам")
            st.bar_chart(chart_data.groupby("snapshot_date").size())


def _filter_data(data: pd.DataFrame) -> pd.DataFrame:
    filter_columns = st.columns(4)
    with filter_columns[0]:
        asins = ["Все"] + sorted(data["our_asin"].dropna().unique()) if "our_asin" in data else ["Все"]
        asin = st.selectbox("ASIN", asins)
    with filter_columns[1]:
        search = st.text_input("Поиск", placeholder="ASIN, товар, бренд…")
    with filter_columns[2]:
        period = st.selectbox("Период", ["Всё время", "7 дней", "30 дней", "90 дней"])
    with filter_columns[3]:
        markets = ["Все"] + sorted(data["marketplace"].dropna().unique()) if "marketplace" in data else ["Все"]
        market = st.selectbox("Маркетплейс", markets)

    result = data.copy()
    if asin != "Все":
        result = result[result["our_asin"] == asin]
    if market != "Все":
        result = result[result["marketplace"] == market]
    if search.strip():
        contains = result.astype(str).apply(lambda column: column.str.contains(search, case=False, na=False))
        result = result[contains.any(axis=1)]
    if period != "Всё время":
        dates = pd.to_datetime(result["snapshot_date"], errors="coerce")
        days = int(period.split()[0])
        result = result[dates >= (datetime.now() - pd.Timedelta(days=days))]
    st.caption(f"Уникальных ASIN в выборке: {result['our_asin'].nunique() if 'our_asin' in result else 0}")
    return result


def _present_table(data: pd.DataFrame) -> tuple[pd.DataFrame, dict[str, object]]:
    labels = {
        "snapshot_date": "Дата сбора", "updated_at": "Обновлено", "marketplace": "Страна",
        "currency": "Валюта", "our_product": "Наш товар", "our_asin": "Наш ASIN",
        "our_bsr": "BSR наш", "our_price": "Цена наша", "our_bsr_delta_24h": "Δ BSR наш",
        "competitor_name": "Конкурент", "comp_asin": "ASIN конкурента", "comp_bsr": "BSR конкурента",
        "comp_price": "Цена конкурента", "comp_bsr_delta_24h": "Δ BSR конкурента",
        "price_diff_pct": "Разница цен, %", "comp_stock": "Наличие",
    }
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
    return result, link_columns


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


def _load_schedule() -> tuple[int, int] | None:
    conn = psycopg2.connect(os.environ["DATABASE_URL"])
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT hour, minute FROM parser_not_test.schedule WHERE id = 1;")
            row = cur.fetchone()
            return (row[0], row[1]) if row else None
    finally:
        conn.close()


def _save_schedule(hour: int, minute: int) -> None:
    conn = psycopg2.connect(os.environ["DATABASE_URL"])
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO parser_not_test.schedule (id, hour, minute, updated_at)
                VALUES (1, %s, %s, now())
                ON CONFLICT (id) DO UPDATE SET hour = EXCLUDED.hour, minute = EXCLUDED.minute, updated_at = now();
                """,
                (hour, minute),
            )
        conn.commit()
    finally:
        conn.close()


def _render_schedule_panel() -> None:
    with st.expander("⏰ Расписание автозапуска парсера", expanded=False):
        saved = _load_schedule()
        default_time = datetime.strptime(f"{saved[0]:02d}:{saved[1]:02d}", "%H:%M").time() if saved else datetime.strptime("09:00", "%H:%M").time()

        task_time = scheduler.get_task_current_time() if scheduler.task_exists() else ""
        if task_time:
            st.caption(f"Сейчас в Планировщике Windows: ежедневно в {task_time}.")
        else:
            st.caption("Задача в Планировщике Windows ещё не создана — появится после первого сохранения.")

        chosen = st.time_input("Время ежедневного запуска (по системным часам этого ПК)", value=default_time, step=60)
        if st.button("Сохранить расписание"):
            _save_schedule(chosen.hour, chosen.minute)
            python_exe, script_path = scheduler.default_python_and_script()
            ok, msg = scheduler.sync_task(chosen.hour, chosen.minute, python_exe, script_path)
            st.cache_data.clear()
            if ok:
                st.success(f"Сохранено. {msg}")
            else:
                st.error(f"Время сохранено в базе, но Планировщик обновить не удалось: {msg}")


def main() -> None:
    _apply_design()
    left, right = st.columns([3, 2])
    with left:
        st.markdown('<p class="brand-subtitle">Мониторинг Amazon-конкурентов и аналитика портфеля</p>', unsafe_allow_html=True)
    with right:
        st.markdown('<div class="source-badge">🗄 Источник: база данных (parser_not_test), не Google Sheets</div>', unsafe_allow_html=True)

    _render_run_control()
    _render_schedule_panel()

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
        f'<div class="status-box"><strong>Последний сбор в базе: {escape(latest_label)}</strong><br>'
        f'Активных пар: {len(current)}. Данные читаются напрямую из Postgres.</div>',
        unsafe_allow_html=True,
    )

    _render_overview(current)

    st.markdown('<p class="section-title">Мониторинг конкурентов</p>', unsafe_allow_html=True)
    st.markdown('<p class="section-note">Фильтруйте сохранённые данные по ASIN, стране и периоду.</p>', unsafe_allow_html=True)

    current_tab, history_tab, pairs_tab = st.tabs(["📋 Текущее состояние", "📅 История", "🥊 Пары конкурентов"])

    with current_tab:
        shown = _filter_data(current)
        presented, table_config = _present_table(shown)
        st.dataframe(presented, use_container_width=True, hide_index=True, height=450, column_config=table_config)

    with history_tab:
        shown_history = _filter_data(history)
        presented_h, config_h = _present_table(shown_history)
        st.dataframe(presented_h, use_container_width=True, hide_index=True, height=450, column_config=config_h)

    with pairs_tab:
        st.dataframe(pairs, use_container_width=True, hide_index=True, height=450)

    st.download_button(
        "Скачать текущий срез CSV",
        current.to_csv(index=False).encode("utf-8-sig"),
        file_name="current.csv",
        mime="text/csv",
    )


if __name__ == "__main__":
    main()
