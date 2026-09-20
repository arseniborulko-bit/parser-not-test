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
        /* Вкладки выбираются по role: в новых версиях Streamlit это уже не <button> и не baseweb. */
        div[data-testid="stTabs"] [role="tablist"] { gap: 3px; border-bottom: 1px solid #dfe3ea; }
        div[data-testid="stTabs"] [role="tab"],
        div[data-testid="stTabs"] button { background: #121826 !important; color: #fff !important; border-radius: 7px 7px 0 0; padding: .6rem 1rem; margin-right: 2px; opacity: 1 !important; }
        div[data-testid="stTabs"] [role="tab"] *,
        div[data-testid="stTabs"] button * { color: #fff !important; opacity: 1 !important; }
        div[data-testid="stTabs"] [role="tab"][aria-selected="true"] { background: #168ed0 !important; color: #fff !important; }
        div[data-testid="stTabs"] button:disabled { opacity: .45 !important; cursor: not-allowed; }
        .stButton > button { background: #168ed0; color: #fff; border: 0; border-radius: 8px; font-weight: 650; }
        .stButton > button:hover { background: #075b9b; color: #fff; }
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
            FROM parser_not_test.snapshots
            ORDER BY snapshot_date DESC
            """,
            conn,
        )
    finally:
        conn.close()


@st.cache_data(ttl=60, show_spinner=False)
def load_current() -> pd.DataFrame:
    """Последний снимок каждой АКТИВНОЙ пары (our_asin, comp_asin): убранная пара из текущего состояния исчезает."""
    conn = psycopg2.connect(_database_url())
    try:
        return pd.read_sql(
            """
            SELECT DISTINCT ON (s.our_asin, s.comp_asin)
                   s.snapshot_date, s.marketplace, s.currency, s.our_asin, s.our_product, s.our_price,
                   s.our_bsr, s.our_bsr_delta_24h, s.comp_asin, s.competitor_name, s.comp_price,
                   s.comp_bsr, s.comp_bsr_delta_24h, s.comp_stock, s.price_diff_pct, s.updated_at
            FROM parser_not_test.snapshots s
            JOIN parser_not_test.competitor_pairs p
              ON p.our_asin = s.our_asin AND p.comp_asin = s.comp_asin AND p.active
            ORDER BY s.our_asin, s.comp_asin, s.snapshot_date DESC
            """,
            conn,
        )
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
            FROM parser_not_test.competitor_pairs p
            LEFT JOIN LATERAL (
                SELECT our_product, competitor_name FROM parser_not_test.snapshots
                WHERE our_asin = p.our_asin AND comp_asin = p.comp_asin
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

    if not data.empty:
        chart_data = data.copy()
        chart_data["snapshot_date"] = pd.to_datetime(chart_data["snapshot_date"], errors="coerce")
        chart_data = chart_data.dropna(subset=["snapshot_date"])
        if not chart_data.empty:
            st.caption("Количество записей по датам")
            st.bar_chart(chart_data.groupby("snapshot_date").size())


def _filter_data(data: pd.DataFrame, key_prefix: str) -> pd.DataFrame:
    filter_columns = st.columns(4)
    with filter_columns[0]:
        asins = ["Все"] + sorted(data["our_asin"].dropna().unique()) if "our_asin" in data else ["Все"]
        asin = st.selectbox("ASIN", asins, key=f"{key_prefix}_asin")
    with filter_columns[1]:
        search = st.text_input("Поиск", placeholder="ASIN, товар, бренд…", key=f"{key_prefix}_search")
    with filter_columns[2]:
        period = st.selectbox("Период", ["Всё время", "7 дней", "30 дней", "90 дней"], key=f"{key_prefix}_period")
    with filter_columns[3]:
        markets = ["Все"] + sorted(data["marketplace"].dropna().unique()) if "marketplace" in data else ["Все"]
        market = st.selectbox("Маркетплейс", markets, key=f"{key_prefix}_market")

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


def _now() -> datetime:
    return datetime.now(schedule_store.TZ)


@st.cache_data(ttl=20, show_spinner=False)
def _admission_preview_cached() -> str | None:
    """Решение проверки допуска для подписи под кнопкой запуска (кэш на 20 с; при нажатии проверяется заново)."""
    return run_control.admission_preview(_connect, _now())


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
        return (access.clean_actor_name(st.session_state.get("actor_name")) or "Команда"), access.ROLE_EDITOR
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
    if state == "open":
        st.caption("⚠️ Управление открыто: любой, у кого есть ссылка на сайт, может менять пары и время сбора.")
        with st.expander("⚙️ Ваше имя для журнала", expanded=False):
            st.text_input("Имя (необязательно)", key="actor_name", placeholder="Команда")
            st.caption("Чтобы закрыть управление паролем, задайте секрет TEAM_PASSWORD в настройках Streamlit.")
        return
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
        if upcoming.due_now:
            st.caption("Время уже наступило, а успешного сбора сегодня нет — он начнётся при ближайшей проверке. GitHub запускает проверку нерегулярно (бывает раз в несколько часов), поэтому старт может сильно задержаться.")
        else:
            st.caption(f"Следующий запуск: {upcoming.when:%d.%m в %H:%M} (Киев) или позже: GitHub запускает проверку нерегулярно, задержка бывает до нескольких часов.")
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
        st.markdown('<p class="brand-subtitle">Мониторинг Amazon-конкурентов и аналитика портфеля</p>', unsafe_allow_html=True)
    with right:
        st.markdown('<div class="source-badge">🗄 Источник: база данных (parser_not_test), не Google Sheets</div>', unsafe_allow_html=True)
        _render_auth_bar(user, email, role)
        if role is None:
            _render_unlock_box()
    actor, manage_role = _manager(email, role)

    if _management_open() or manage_role is not None:
        st.info(
            "Дашборд показывает данные из PostgreSQL. Время автосбора и список пар можно менять прямо здесь; "
            "парсер и Google Sheets он не запускает и не меняет."
        )
    else:
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
    active_pairs = int(pairs["active"].sum()) if not pairs.empty else 0
    st.markdown(
        f'<div class="status-box"><strong>Последний сбор в базе: {escape(latest_label)}</strong><br>'
        f'Пар в текущем срезе: {len(current)} из {active_pairs} активных. Данные читаются напрямую из Postgres.</div>',
        unsafe_allow_html=True,
    )

    _render_overview(current)

    st.markdown('<p class="section-title">Мониторинг конкурентов</p>', unsafe_allow_html=True)
    st.markdown('<p class="section-note">Фильтруйте сохранённые данные по ASIN, стране и периоду.</p>', unsafe_allow_html=True)

    tab_titles = ["📋 Текущее состояние", "📅 История", "🥊 Пары конкурентов", "⚙ Сбор и управление"]
    if role == access.ROLE_ADMIN:
        tab_titles.append("👥 Пользователи")
    tabs = st.tabs(tab_titles)
    current_tab, history_tab, pairs_tab, schedule_tab = tabs[:4]

    with current_tab:
        shown = _filter_data(current, key_prefix="current")
        presented, table_config = _present_table(shown)
        st.dataframe(presented, use_container_width=True, hide_index=True, height=450, column_config=table_config)

    with history_tab:
        shown_history = _filter_data(history, key_prefix="history")
        presented_h, config_h = _present_table(shown_history)
        st.dataframe(presented_h, use_container_width=True, hide_index=True, height=450, column_config=config_h)

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

    st.download_button(
        "Скачать текущий срез CSV",
        current.to_csv(index=False).encode("utf-8-sig"),
        file_name="current.csv",
        mime="text/csv",
    )


if __name__ == "__main__":
    main()
