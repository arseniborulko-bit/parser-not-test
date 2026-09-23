"""Вкладка «Пары конкурентов»: просмотр для всех, добавление и удаление — после входа в «Управление»."""

from __future__ import annotations

import pandas as pd
import streamlit as st

import access
import pairs_store
from schedule_store import TZ

_ACTION_LABELS = {"add": "добавлена", "enable": "возвращена", "disable": "отключена"}
# Иначе таблица пар — единственное место в дашборде с английскими заголовками из базы.
_PAIR_COLUMNS = {
    "marketplace": "Страна", "our_asin": "Наш ASIN", "our_product": "Наш товар",
    "comp_asin": "ASIN конкурента", "competitor_name": "Конкурент", "active": "Активна",
}
_STATUS_ALL = "Все"
_STATUS_ACTIVE = "Активные"
_STATUS_OFF = "Отключённые"
_SAME_MARKET = "— как у этого товара в базе —"
_CONFIRM_ABOVE = 25
_ERRORS = (ValueError, access.AccessDenied, pairs_store.PairsStoreError)


def _flash(kind: str, message: str) -> None:
    st.session_state["pairs_flash"] = (kind, message)


def _matches(frame: pd.DataFrame, query: str) -> pd.DataFrame:
    if not query.strip():
        return frame
    needle = query.strip().casefold()
    text = frame.astype(str).apply(lambda column: column.str.casefold().str.contains(needle, regex=False, na=False))
    return frame[text.any(axis=1)]


def _summary(pairs: pd.DataFrame) -> None:
    active = pairs[pairs["active"]]
    asins = set(active["our_asin"]) | set(active["comp_asin"])
    cells = st.columns(4)
    cells[0].metric("Активных пар", len(active))
    cells[1].metric("Наших товаров", active["our_asin"].nunique())
    cells[2].metric("ASIN в каждом сборе", len(asins))
    cells[3].metric("Отключено", len(pairs) - len(active))


def _add_callback(connect, actor: str, role: str, max_active: int) -> None:
    state = st.session_state
    pick = state.get("pairs_market", _SAME_MARKET)
    try:
        plan = pairs_store.make_plan(
            connect, state.get("pairs_our", ""), None if pick == _SAME_MARKET else pick,
            state.get("pairs_comps", ""), max_active=max_active,
        )
        result = pairs_store.apply_plan(connect, plan, actor_role=role, actor=actor)
    except _ERRORS as exc:
        _flash("error", str(exc))
        return
    st.cache_data.clear()
    state["pairs_comps"] = ""
    parts = []
    if result["add"]:
        parts.append(f"добавлено {result['add']}")
    if result["enable"]:
        parts.append(f"возвращено {result['enable']}")
    _flash("success", "Готово: " + ", ".join(parts) + ". Пары попадут в ближайший сбор." if parts else "Ничего не изменилось.")


def _toggle_callback(connect, keys, active: bool, actor: str, role: str) -> None:
    try:
        changed = pairs_store.set_pairs_active(connect, keys, active, actor_role=role, actor=actor)
    except _ERRORS as exc:
        _flash("error", str(exc))
        return
    st.cache_data.clear()
    verb = "возвращено" if active else "отключено"
    _flash("success", f"Готово: {verb} {changed}. " + ("Пары попадут в ближайший сбор." if active else "В следующем сборе их уже не будет."))


def _render_plan(plan: pairs_store.Plan) -> None:
    for error in plan.errors:
        st.error(error)
    if plan.our_asin and plan.market:
        product = plan.our_product or "название появится после первого сбора"
        st.caption(f"Наш товар: {plan.our_asin} · {plan.market} · {product}")
    if plan.to_add or plan.to_enable or plan.already or plan.invalid or plan.rejected:
        cells = st.columns(4)
        cells[0].metric("Новых", len(plan.to_add))
        cells[1].metric("Вернутся", len(plan.to_enable))
        cells[2].metric("Уже есть", len(plan.already))
        cells[3].metric("Не подошло", len(plan.invalid) + len(plan.rejected))
    if plan.changes and not plan.errors:
        st.caption(f"В каждом сборе добавится ASIN: {plan.new_asins}. Активных пар станет {plan.active_now + plan.changes}.")
    if plan.rejected:
        st.warning("Пропущены: " + "; ".join(plan.rejected))
    if plan.invalid:
        st.warning("Не распознано (нужен ASIN вида B0XXXXXXXX или ссылка Amazon):")
        st.code("\n".join(plan.invalid[:20]) + ("\n…" if len(plan.invalid) > 20 else ""), language=None)
    if plan.repeats:
        st.caption(f"Повторов в тексте отброшено: {plan.repeats}.")


def _render_add(connect, actor: str, role: str, max_active: int) -> None:
    st.text_input("Наш товар (ASIN или ссылка)", key="pairs_our", placeholder="B0XXXXXXXX или ссылка на страницу Amazon")
    st.selectbox(
        "Маркетплейс", [_SAME_MARKET, *pairs_store.MARKETS], key="pairs_market",
        help="Нужен только для нового товара без ссылки: ссылка сама определяет страну.",
    )
    st.text_area(
        "Конкуренты (ASIN или ссылки, по одному в строке или через запятую)", key="pairs_comps", height=110,
        placeholder="B0XXXXXXX1, B0XXXXXXX2, https://www.amazon.com/dp/B0XXXXXXX3",
    )
    plan = None
    if st.session_state.get("pairs_our", "").strip() or st.session_state.get("pairs_comps", "").strip():
        pick = st.session_state.get("pairs_market", _SAME_MARKET)
        try:
            plan = pairs_store.make_plan(
                connect, st.session_state.get("pairs_our", ""), None if pick == _SAME_MARKET else pick,
                st.session_state.get("pairs_comps", ""), max_active=max_active,
            )
        except _ERRORS as exc:
            st.error(str(exc))
    if plan is not None:
        _render_plan(plan)
    label = f"Добавить пар: {plan.changes}" if plan is not None and plan.changes else "Добавить"
    st.button(
        label, type="primary", key="pairs_add", disabled=plan is None or not plan.can_apply,
        on_click=_add_callback, args=(connect, actor, role, max_active),
    )


def _render_remove(connect, pairs: pd.DataFrame, actor: str, role: str) -> None:
    active = pairs[pairs["active"]]
    if active.empty:
        st.caption("Активных пар нет.")
        return
    products = active.drop_duplicates("our_asin").set_index("our_asin")["our_product"].to_dict()
    ours = sorted(products)
    pick = st.selectbox(
        "Наш товар", ours, key="pairs_remove_our",
        format_func=lambda asin: f"{asin} · {str(products.get(asin) or '')[:60]}",
    )
    subset = active[active["our_asin"] == pick][["marketplace", "our_asin", "comp_asin", "competitor_name"]].reset_index(drop=True)
    everything = st.checkbox("Отметить все пары этого товара", key=f"pairs_remove_all_{pick}")
    table = subset.assign(Убрать=everything)
    edited = st.data_editor(
        table, key=f"pairs_remove_editor_{pick}_{int(everything)}", hide_index=True, use_container_width=True,
        disabled=["marketplace", "our_asin", "comp_asin", "competitor_name"],
        column_config={"marketplace": "Страна", "our_asin": None, "comp_asin": "ASIN конкурента", "competitor_name": "Конкурент",
                       "Убрать": st.column_config.CheckboxColumn("Убрать")},
    )
    chosen = edited[edited["Убрать"]]
    count = len(chosen)
    confirmed = True
    if count > _CONFIRM_ABOVE:
        typed = st.text_input(f"Отключается {count} пар. Введите УБРАТЬ для подтверждения", key="pairs_remove_confirm")
        confirmed = typed.strip().upper() == "УБРАТЬ"
    keys = [(row.marketplace, row.our_asin, row.comp_asin) for row in chosen.itertuples()]
    st.button(
        f"Отключить отмеченные ({count})", key="pairs_remove_btn", disabled=count == 0 or not confirmed,
        on_click=_toggle_callback, args=(connect, keys, False, actor, role),
    )
    st.caption("Пара не удаляется: история остаётся, вернуть её можно в разделе «Вернуть отключённые».")


def _render_restore(connect, pairs: pd.DataFrame, actor: str, role: str) -> None:
    inactive = pairs[~pairs["active"]]
    if inactive.empty:
        st.caption("Отключённых пар нет.")
        return
    query = st.text_input("Поиск по ASIN или названию", key="pairs_restore_search")
    shown = _matches(inactive, query).head(200)[["marketplace", "our_asin", "comp_asin", "competitor_name"]].reset_index(drop=True)
    edited = st.data_editor(
        shown.assign(Вернуть=False), key=f"pairs_restore_editor_{abs(hash(query)) % 10**6}", hide_index=True, use_container_width=True,
        disabled=["marketplace", "our_asin", "comp_asin", "competitor_name"],
        column_config={"marketplace": "Страна", "our_asin": "Наш ASIN", "comp_asin": "ASIN конкурента", "competitor_name": "Конкурент",
                       "Вернуть": st.column_config.CheckboxColumn("Вернуть")},
    )
    chosen = edited[edited["Вернуть"]]
    keys = [(row.marketplace, row.our_asin, row.comp_asin) for row in chosen.itertuples()]
    st.button(
        f"Вернуть отмеченные ({len(chosen)})", key="pairs_restore_btn", disabled=not len(chosen),
        on_click=_toggle_callback, args=(connect, keys, True, actor, role),
    )
    if len(inactive) > 200 or (query and len(_matches(inactive, query)) > 200):
        st.caption("Показаны первые 200: уточните поиск.")


def _render_log(connect) -> None:
    try:
        if not pairs_store.journal_exists(connect):
            st.caption("Журнал изменений ещё не подключён: изменения применяются, но пока не записываются в журнал.")
            return
        changes = pairs_store.recent_changes(connect, 30)
    except pairs_store.PairsStoreError as exc:
        st.error(str(exc))
        return
    if not changes:
        st.caption("Изменений пока не было.")
        return
    st.dataframe(
        pd.DataFrame([
            {
                "Когда (Киев)": change["at"].astimezone(TZ).strftime("%d.%m %H:%M"),
                "Кто": change["actor"],
                "Что": _ACTION_LABELS.get(change["action"], change["action"]),
                "Страна": change["marketplace"],
                "Наш ASIN": change["our_asin"],
                "ASIN конкурента": change["comp_asin"],
            }
            for change in changes
        ]),
        use_container_width=True, hide_index=True,
    )


def render_pairs_tab(connect, pairs: pd.DataFrame, actor: str | None, role: str | None, max_active: int) -> None:
    flash = st.session_state.pop("pairs_flash", None)
    if flash:
        getattr(st, flash[0])(flash[1])
    _summary(pairs)

    left, right = st.columns([1, 2])
    status = left.radio("Показать", [_STATUS_ACTIVE, _STATUS_OFF, _STATUS_ALL], horizontal=True, key="pairs_status")
    query = right.text_input("Поиск", placeholder="ASIN, товар, конкурент…", key="pairs_search")
    shown = pairs
    if status == _STATUS_ACTIVE:
        shown = pairs[pairs["active"]]
    elif status == _STATUS_OFF:
        shown = pairs[~pairs["active"]]
    found = _matches(shown, query)
    if found.empty:
        # Пустой st.dataframe рисует английское "empty" — для владельца это выглядит как сбой.
        st.info("Ничего не найдено: попробуйте изменить поиск или переключить «Показать».")
    else:
        st.dataframe(found, use_container_width=True, hide_index=True, height=350,
                     column_config=_PAIR_COLUMNS)

    if not access.has_role(role, access.ROLE_EDITOR):
        st.caption("Чтобы добавлять и убирать пары, откройте «🔒 Управление» вверху страницы.")
        return
    st.caption("Изменения попадают в следующий сбор. Лист Competitors в Google Таблице парсер больше не читает.")
    with st.expander("➕ Добавить конкурентов", expanded=True):
        _render_add(connect, actor or "?", role, max_active)
    with st.expander("🗑 Убрать пары"):
        _render_remove(connect, pairs, actor or "?", role)
    with st.expander("↩ Вернуть отключённые"):
        _render_restore(connect, pairs, actor or "?", role)
    with st.expander("📜 Журнал изменений"):
        _render_log(connect)
