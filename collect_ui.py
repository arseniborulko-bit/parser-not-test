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


def _run_callback(connect, token: str, repo: str, can_edit: bool) -> None:
    if not can_edit:
        _flash("error", "Запускать сбор могут только с открытым управлением.")
        return
    if _now_ts() - st.session_state.get("collect_dispatched_at", 0) < COOLDOWN_SECONDS:
        _flash("info", "Запуск уже отправлен: подождите пару минут.")
        return
    try:
        reason = run_control.admission_preview(connect, datetime.now(TZ))
    except run_control.RunControlError as exc:
        _flash("error", str(exc))
        return
    if reason:
        _flash("warning", f"Не запускаю: {reason}")
        return
    result = github_dispatch.dispatch_collection(token, repo)
    if not result.ok:
        _flash("error", result.message)
        return
    st.session_state["collect_dispatched_at"] = _now_ts()
    _flash("success", "Запуск отправлен в GitHub. Сбор начнётся в течение минуты и идёт около 10–20 минут; ход виден в «Последних запусках» ниже.")


def render_run_block(connect, pairs: pd.DataFrame, preview: Callable[[], Optional[str]], token: str, repo: str,
                     can_edit: bool) -> None:
    flash = st.session_state.pop("collect_flash", None)
    if flash:
        getattr(st, flash[0])(flash[1])
    st.markdown('<p class="section-title">Запустить сбор</p>', unsafe_allow_html=True)
    if pairs.empty:
        positions = run_control.Positions(0, 0, 0, {})
    else:
        positions = run_control.positions_summary(zip(pairs["marketplace"], pairs["our_asin"], pairs["comp_asin"], pairs["active"]))
    st.caption(run_control.format_positions(positions))
    st.caption("Каждый ASIN запрашивается один раз за сбор, даже если он входит в несколько пар.")

    since = _now_ts() - st.session_state.get("collect_dispatched_at", 0)
    cooling = since < COOLDOWN_SECONDS
    reason: Optional[str] = None
    error: Optional[str] = None
    if can_edit and token and not cooling:
        try:
            reason = preview()
        except run_control.RunControlError as exc:
            error = str(exc)
    st.button(
        "🚀 Собрать сейчас — на серверах GitHub", type="primary", key="collect_run", use_container_width=True,
        disabled=not can_edit or not token or cooling or reason is not None or error is not None,
        on_click=_run_callback, args=(connect, token, repo, can_edit),
    )
    if not can_edit:
        st.caption("Чтобы запускать сбор, откройте «🔒 Управление» вверху страницы.")
    elif not token:
        st.caption("Кнопка включится, когда в секретах Streamlit появится токен GitHub (GITHUB_DISPATCH_TOKEN). Пока сбор идёт по расписанию, а вручную его можно запустить в GitHub → Actions.")
    elif cooling:
        st.caption(f"Запуск отправлен {int(since)} с назад: сбор начнётся в течение минуты.")
    elif error:
        st.warning(error)
    elif reason:
        st.info(reason)
    else:
        st.caption("Сбор идёт на серверах GitHub около 10–20 минут, вкладку можно закрыть. Второй успешный сбор за день не запустится: это защита от лишних трат.")


def render_spot_check(token: str, can_edit: bool) -> None:
    st.markdown('<p class="section-title">Точечная проверка</p>', unsafe_allow_html=True)
    if not token:
        st.info("Точечная проверка выключена: в секретах Streamlit не задан токен ScrapingDog (SCRAPINGDOG_TOKEN).")
        return
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
