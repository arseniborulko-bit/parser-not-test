"""Вкладка «Сбор и управление»: запуск сбора на GitHub, точечная проверка, обновление данных.
Автосбор (время и включатель) рисует dashboard_db.py в той же вкладке."""

from __future__ import annotations

import time
from datetime import datetime
from typing import Callable, Optional

import pandas as pd
import streamlit as st

import github_dispatch
import pairs_store
import run_control
import spot_check
from schedule_store import TZ

COOLDOWN_SECONDS = 120
_now_ts = time.time


def _flash(kind: str, message: str) -> None:
    st.session_state["collect_flash"] = (kind, message)


@st.cache_resource
def _spot_budget() -> spot_check.SpotBudget:
    return spot_check.SpotBudget()


def _run_callback(connect, token: str, repo: str, can_edit: bool, scope: str = "all", force: bool = False) -> None:
    if not can_edit:
        _flash("error", "Запускать сбор могут только с открытым управлением.")
        return
    if _now_ts() - st.session_state.get("collect_dispatched_at", 0) < COOLDOWN_SECONDS:
        _flash("info", "Запуск уже отправлен: подождите пару минут.")
        return
    try:
        # Всегда проверяем заново, вживую: закэшированная подпись под кнопкой — только подсказка,
        # решение принимает этот вызов. Для force это особенно важно — он снимает часть защиты,
        # и решение не должно приниматься по устаревшим на 20 секунд данным.
        reason = run_control.admission_preview(connect, datetime.now(TZ), scope=scope, force=force)
    except run_control.RunControlError as exc:
        _flash("error", str(exc))
        return
    if reason:
        _flash("warning", f"Не запускаю: {reason}")
        return
    result = github_dispatch.dispatch_collection(token, repo, scope=scope, force=force)
    if not result.ok:
        _flash("error", result.message)
        return
    st.session_state["collect_dispatched_at"] = _now_ts()
    verb = "Повторный запуск" if force else "Запуск"
    _flash("success", f"{verb} отправлен в GitHub. Сбор начнётся в течение минуты и идёт около 10–20 минут; ход виден в «Последних запусках» ниже.")


def _scope_status(preview: Callable[[str], Optional[str]], token: str, can_edit: bool, cooling: bool,
                  scope: str) -> tuple[Optional[str], Optional[str]]:
    """(причина отказа допуска, текст ошибки) для конкретной области; обе None — можно жать кнопку."""
    if not (can_edit and token and not cooling):
        return None, None
    try:
        return preview(scope), None
    except run_control.RunControlError as exc:
        return None, str(exc)


def _scope_button(connect, token: str, repo: str, can_edit: bool, cooling: bool, scope: str, label: str, key: str,
                  reason: Optional[str], error: Optional[str], *, primary: bool = False) -> None:
    st.button(
        label, key=key, use_container_width=True, type="primary" if primary else "secondary",
        disabled=not can_edit or not token or cooling or reason is not None or error is not None,
        on_click=_run_callback, args=(connect, token, repo, can_edit, scope),
    )
    if error:
        # Настоящая ошибка (например, база недоступна) — это не пояснение к политике блокировки,
        # а диагностика проблемы; такие сообщения владелец просил оставить, в отличие от «Сегодня
        # уже был успешный сбор» и подобных причин, объясняющих обычную, штатную блокировку.
        st.caption(f"⚠️ {error}")
        return
    if not reason:
        return
    if not (can_edit and token and not cooling):
        return
    # Решение владельца 25.09.2026: «уже был сбор сегодня» и дневной лимит попыток можно обойти
    # вручную — с явным подтверждением, потому что каждое нажатие тратит деньги за новый сбор.
    # Незавершённый сбор и «время ещё не наступило» force не снимает — кнопка всё равно вызывает
    # тот же живой admission_preview, поэтому в таком случае честно откажет, а не соврёт, что сработало.
    confirmed = st.checkbox(
        "Понимаю: это ещё один платный сбор", key=f"{key}_force_confirm",
    )
    st.button(
        "Собрать ещё раз, несмотря на ограничение", key=f"{key}_force", disabled=not confirmed,
        on_click=_run_callback, args=(connect, token, repo, can_edit, scope, True),
    )


def render_run_block(connect, pairs: pd.DataFrame, preview: Callable[[str], Optional[str]], token: str, repo: str,
                     can_edit: bool) -> None:
    flash = st.session_state.pop("collect_flash", None)
    if flash:
        getattr(st, flash[0])(flash[1])
    st.markdown('<p class="section-title">Запустить сбор</p>', unsafe_allow_html=True)
    if pairs.empty:
        positions = run_control.Positions(0, 0, 0, {})
    else:
        positions = run_control.positions_summary(zip(pairs["marketplace"], pairs["our_asin"], pairs["comp_asin"], pairs["active"]))

    since = _now_ts() - st.session_state.get("collect_dispatched_at", 0)
    cooling = since < COOLDOWN_SECONDS

    reason_all, error_all = _scope_status(preview, token, can_edit, cooling, "all")
    _scope_button(connect, token, repo, can_edit, cooling, "all",
                 f"🚀 Собрать всё ({positions.total}) — на серверах GitHub", "collect_run",
                 reason_all, error_all, primary=True)

    left, right = st.columns(2)
    with left:
        reason_ours, error_ours = _scope_status(preview, token, can_edit, cooling, "ours")
        _scope_button(connect, token, repo, can_edit, cooling, "ours",
                     f"Собрать наши ({positions.ours})", "collect_run_ours", reason_ours, error_ours)
    with right:
        reason_comp, error_comp = _scope_status(preview, token, can_edit, cooling, "competitors")
        _scope_button(connect, token, repo, can_edit, cooling, "competitors",
                     f"Собрать конкурентов ({positions.competitors})", "collect_run_competitors", reason_comp, error_comp)

    if not can_edit:
        st.caption("Чтобы запускать сбор, откройте «🔒 Управление» вверху страницы.")
    elif cooling:
        st.caption(f"Запуск отправлен {int(since)} с назад: сбор начнётся в течение минуты.")


def render_spot_check(token: str, can_edit: bool) -> None:
    if not token:
        return
    st.markdown('<p class="section-title">Точечная проверка</p>', unsafe_allow_html=True)
    if not can_edit:
        st.caption("Чтобы проверять ASIN, откройте «🔒 Управление» вверху страницы.")
        return
    st.caption("Разовая проверка ASIN: результат только на экране, в базу и таблицу не пишется. Один ASIN — один кредит ScrapingDog.")
    with st.form("spot_form"):
        text = st.text_area(
            f"ASIN, ASIN:US или ссылки (до {spot_check.MAX_PER_CHECK})", key="spot_text", height=100,
            placeholder="B0XXXXXXXX:US, https://www.amazon.de/dp/B0XXXXXXXX",
        )
        market = st.selectbox("Маркетплейс по умолчанию", list(pairs_store.MARKETS), key="spot_market")
        submitted = st.form_submit_button("Выполнить проверку")
    budget = _spot_budget()
    if submitted:
        plan = spot_check.plan_spot_check(text, market)
        for problem in plan.errors:
            st.error(problem)
        if plan.invalid:
            st.warning("Не распознано: " + ", ".join(plan.invalid[:10]))
        if not plan.errors:
            try:
                with st.spinner("Запрашиваю ScrapingDog…"):
                    st.session_state["spot_result"] = spot_check.run_spot_check(plan, token, budget)
            except (ValueError, spot_check.SpotBudgetError) as exc:
                st.error(str(exc))
    st.caption(f"Осталось проверок: {budget.remaining()} (лимит {spot_check.HOUR_LIMIT} в час и {spot_check.DAY_LIMIT} в сутки на весь сайт).")
    rows = st.session_state.get("spot_result")
    if rows:
        st.dataframe(pd.DataFrame(rows), hide_index=True, use_container_width=True)


def _refresh_callback() -> None:
    st.cache_data.clear()
    _flash("success", "Данные обновлены.")


def render_refresh_button() -> None:
    st.button("🔄 Обновить данные из базы", key="collect_refresh", on_click=_refresh_callback)
