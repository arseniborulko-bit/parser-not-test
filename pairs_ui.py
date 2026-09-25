"""Вкладка «Пары конкурентов»: просмотр для всех, добавление и удаление — после входа в «Управление»."""

from __future__ import annotations

import logging

import pandas as pd
import streamlit as st

import access
import asins_store
import pairs_store
from pairs_store import DOMAIN_BY_MARKET
from schedule_store import TZ

log = logging.getLogger(__name__)

_ACTION_LABELS = {"add": "добавлена", "enable": "возвращена", "disable": "отключена"}
# Иначе таблица пар — единственное место в дашборде с английскими заголовками из базы.
_ASIN_LINK_TEXT = r"https://www\.amazon\.[^/]+/dp/([A-Z0-9]{10})"
_LINK_COLUMNS = {
    "our_asin": st.column_config.LinkColumn("Наш ASIN", display_text=_ASIN_LINK_TEXT, width="small"),
    "comp_asin": st.column_config.LinkColumn("ASIN конкурента", display_text=_ASIN_LINK_TEXT, width="small"),
}
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


def _add_callback(connect, actor: str, role: str, max_active: int, key_prefix: str = "pairs") -> None:
    state = st.session_state
    our_key, market_key, comps_key = f"{key_prefix}_our", f"{key_prefix}_market", f"{key_prefix}_comps"
    pick = state.get(market_key, _SAME_MARKET)
    try:
        plan = pairs_store.make_plan(
            connect, state.get(our_key, ""), None if pick == _SAME_MARKET else pick,
            state.get(comps_key, ""), max_active=max_active,
        )
        result = pairs_store.apply_plan(connect, plan, actor_role=role, actor=actor)
    except _ERRORS as exc:
        _flash("error", str(exc))
        return
    st.cache_data.clear()
    state[comps_key] = ""
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


def _render_add(connect, actor: str, role: str, max_active: int, key_prefix: str = "pairs") -> None:
    our_key, market_key, comps_key = f"{key_prefix}_our", f"{key_prefix}_market", f"{key_prefix}_comps"
    st.text_input("Наш товар (ASIN или ссылка)", key=our_key, placeholder="B0XXXXXXXX или ссылка на страницу Amazon")
    st.selectbox(
        "Маркетплейс", [_SAME_MARKET, *pairs_store.MARKETS], key=market_key,
        help="Нужен только для нового товара без ссылки: ссылка сама определяет страну.",
    )
    st.text_area(
        "Конкуренты (ASIN или ссылки, по одному в строке или через запятую)", key=comps_key, height=110,
        placeholder="B0XXXXXXX1, B0XXXXXXX2, https://www.amazon.com/dp/B0XXXXXXX3",
    )
    plan = None
    if st.session_state.get(our_key, "").strip() or st.session_state.get(comps_key, "").strip():
        pick = st.session_state.get(market_key, _SAME_MARKET)
        try:
            plan = pairs_store.make_plan(
                connect, st.session_state.get(our_key, ""), None if pick == _SAME_MARKET else pick,
                st.session_state.get(comps_key, ""), max_active=max_active,
            )
        except _ERRORS as exc:
            st.error(str(exc))
    if plan is not None:
        _render_plan(plan)
    label = f"Добавить пар: {plan.changes}" if plan is not None and plan.changes else "Добавить"
    st.button(
        label, type="primary", key=f"{key_prefix}_add", disabled=plan is None or not plan.can_apply,
        on_click=_add_callback, args=(connect, actor, role, max_active, key_prefix),
    )


def _asin_url(asin: object, marketplace: object) -> str:
    domain = DOMAIN_BY_MARKET.get(str(marketplace).strip().upper(), "com")
    asin = str(asin or "").strip()
    return f"https://www.amazon.{domain}/dp/{asin}" if asin else ""


def _with_links(frame: pd.DataFrame) -> pd.DataFrame:
    """Те же ASIN, но кликабельные: карточку товара видно, не копируя ASIN руками."""
    result = frame.copy()
    for column in ("our_asin", "comp_asin"):
        if column in result:
            result[column] = [_asin_url(asin, market)
                              for asin, market in zip(result[column], result.get("marketplace", ""))]
    return result


MAX_EDIT_ROWS = 200


def _edit_callback(connect, changes, actor: str, role: str) -> None:
    """Сохраняет все изменённые строки сетки за одно нажатие."""
    if not changes:
        st.session_state["pairs_flash"] = ("info", "Ничего не изменено.")
        return
    saved = 0
    try:
        for key, our_product, competitor_name in changes:
            saved += pairs_store.edit_pair_names(
                connect, key, our_product, competitor_name, actor_role=role, actor=actor,
            )
    except _ERRORS as exc:
        st.session_state["pairs_flash"] = (
            "error", f"Сохранено строк: {saved}, дальше ошибка — {exc}" if saved else str(exc),
        )
        return
    st.session_state["pairs_flash"] = ("success", f"Сохранено строк: {saved}.")


def _render_edit(connect, pairs: pd.DataFrame, actor: str, role: str) -> None:
    """Правка названий сеткой: правишь сколько нужно строк и сохраняешь одним нажатием.

    Ключ пары (рынок и оба ASIN) не редактируется: это же ключ снимков, и его смена означала бы
    другую пару, без прежней истории. Поэтому ASIN здесь — ссылка, а не поле ввода."""
    if pairs.empty:
        st.info("Пар пока нет.")
        return

    top = st.columns([3, 2])
    query = top[0].text_input("ASIN или часть названия", key="pairs_edit_search")
    only_empty = top[1].checkbox("Только без названия", key="pairs_edit_only_empty")

    shown = _matches(pairs, query)
    if only_empty:
        blank = shown["our_product"].fillna("").str.strip().eq("") | shown["competitor_name"].fillna("").str.strip().eq("")
        shown = shown[blank]
    if shown.empty:
        st.info("Ничего не найдено.")
        return
    total_found = len(shown)
    shown = shown.head(MAX_EDIT_ROWS).reset_index(drop=True)

    table = pd.DataFrame({
        "Страна": shown["marketplace"],
        "Наш ASIN": [_asin_url(a, m) for a, m in zip(shown["our_asin"], shown["marketplace"])],
        "Наш товар": shown["our_product"].fillna(""),
        "ASIN конкурента": [_asin_url(a, m) for a, m in zip(shown["comp_asin"], shown["marketplace"])],
        "Конкурент": shown["competitor_name"].fillna(""),
    })
    edited = st.data_editor(
        table, key="pairs_edit_grid", hide_index=True, use_container_width=True, height=400,
        disabled=["Страна", "Наш ASIN", "ASIN конкурента"],
        column_config={
            "Наш ASIN": st.column_config.LinkColumn("Наш ASIN", display_text=_ASIN_LINK_TEXT, width="small"),
            "ASIN конкурента": st.column_config.LinkColumn("ASIN конкурента", display_text=_ASIN_LINK_TEXT, width="small"),
            "Наш товар": st.column_config.TextColumn("Наш товар", max_chars=pairs_store.MAX_NAME),
            "Конкурент": st.column_config.TextColumn("Конкурент", max_chars=pairs_store.MAX_NAME),
        },
    )

    changes = []
    for position, row in enumerate(shown.itertuples()):
        our_now = str(edited["Наш товар"].iloc[position] or "")
        comp_now = str(edited["Конкурент"].iloc[position] or "")
        if our_now != str(row.our_product or "") or comp_now != str(row.competitor_name or ""):
            changes.append(((row.marketplace, row.our_asin, row.comp_asin), our_now, comp_now))

    st.button(
        f"Сохранить изменения ({len(changes)})", key="pairs_edit_btn", disabled=not changes,
        on_click=_edit_callback, args=(connect, changes, actor, role),
    )
    if total_found > MAX_EDIT_ROWS:
        st.caption(f"Показаны первые {MAX_EDIT_ROWS}: уточните поиск.")


def _save_pairs_grid_callback(connect, name_changes, active_changes: dict[bool, list], actor: str, role: str) -> None:
    """Сохраняет и правки названий, и включения/отключения — за одно нажатие «Сохранить изменения»."""
    saved_names = 0
    saved_active = 0
    try:
        for key, our_product, competitor_name in name_changes:
            saved_names += pairs_store.edit_pair_names(
                connect, key, our_product, competitor_name, actor_role=role, actor=actor,
            )
        for active_value, keys in active_changes.items():
            if keys:
                saved_active += pairs_store.set_pairs_active(connect, keys, active_value, actor_role=role, actor=actor)
    except _ERRORS as exc:
        saved = saved_names + saved_active
        _flash("error", f"Сохранено строк: {saved}, дальше ошибка — {exc}" if saved else str(exc))
        return
    st.cache_data.clear()
    _flash("success", f"Сохранено: названий {saved_names}, статусов {saved_active}.")


def _render_pairs_grid(connect, pairs: pd.DataFrame, actor: str, role: str, key_prefix: str = "pairs") -> None:
    """Одна таблица на добавление, правку и отключение: страна и поиск сверху, название и
    активность правятся прямо в сетке, сохранение — одной кнопкой на все изменённые строки.

    Ключ пары (страна и оба ASIN) не редактируется: это же ключ снимков, и его смена означала бы
    другую пару, без прежней истории. Поэтому ASIN здесь — ссылка, а не поле ввода; чтобы сменить
    ASIN, пару отключают и заводят заново через «Добавить пару»."""
    if pairs.empty:
        st.info("Пар пока нет.")
        return

    top = st.columns([2, 3])
    market = top[0].selectbox("Страна", ["Все", *sorted(pairs["marketplace"].dropna().unique())],
                              key=f"{key_prefix}_grid_market")
    query = top[1].text_input("ASIN или часть названия", key=f"{key_prefix}_grid_search")

    shown = pairs if market == "Все" else pairs[pairs["marketplace"] == market]
    shown = _matches(shown, query)
    if shown.empty:
        st.info("Ничего не найдено.")
        return
    shown = shown.head(MAX_EDIT_ROWS).reset_index(drop=True)

    table = pd.DataFrame({
        "Страна": shown["marketplace"],
        "Наш ASIN": [_asin_url(a, m) for a, m in zip(shown["our_asin"], shown["marketplace"])],
        "Наш товар": shown["our_product"].fillna(""),
        "ASIN конкурента": [_asin_url(a, m) for a, m in zip(shown["comp_asin"], shown["marketplace"])],
        "Конкурент": shown["competitor_name"].fillna(""),
        "Активна": shown["active"],
    })
    edited = st.data_editor(
        table, key=f"{key_prefix}_grid_editor", hide_index=True, use_container_width=True, height=420,
        disabled=["Страна", "Наш ASIN", "ASIN конкурента"],
        column_config={
            "Наш ASIN": st.column_config.LinkColumn("Наш ASIN", display_text=_ASIN_LINK_TEXT, width="small"),
            "ASIN конкурента": st.column_config.LinkColumn("ASIN конкурента", display_text=_ASIN_LINK_TEXT, width="small"),
            "Наш товар": st.column_config.TextColumn("Наш товар", max_chars=pairs_store.MAX_NAME),
            "Конкурент": st.column_config.TextColumn("Конкурент", max_chars=pairs_store.MAX_NAME),
            "Активна": st.column_config.CheckboxColumn("Активна"),
        },
    )

    name_changes = []
    active_changes: dict[bool, list] = {True: [], False: []}
    for position, row in enumerate(shown.itertuples()):
        key = (row.marketplace, row.our_asin, row.comp_asin)
        our_now = str(edited["Наш товар"].iloc[position] or "")
        comp_now = str(edited["Конкурент"].iloc[position] or "")
        if our_now != str(row.our_product or "") or comp_now != str(row.competitor_name or ""):
            name_changes.append((key, our_now, comp_now))
        active_now = bool(edited["Активна"].iloc[position])
        if active_now != bool(row.active):
            active_changes[active_now].append(key)

    total = len(name_changes) + len(active_changes[True]) + len(active_changes[False])
    confirmed = True
    if len(active_changes[False]) > _CONFIRM_ABOVE:
        typed = st.text_input(
            f"Отключается {len(active_changes[False])} пар. Введите УБРАТЬ для подтверждения",
            key=f"{key_prefix}_grid_confirm",
        )
        confirmed = typed.strip().upper() == "УБРАТЬ"
    st.button(
        f"Сохранить изменения ({total})", key=f"{key_prefix}_grid_save",
        disabled=not total or not confirmed,
        on_click=_save_pairs_grid_callback, args=(connect, name_changes, active_changes, actor, role),
    )
    if len(shown) >= MAX_EDIT_ROWS:
        st.caption(f"Показаны первые {MAX_EDIT_ROWS}: уточните страну или поиск.")


def render_pairs_management(connect, pairs: pd.DataFrame, actor: str | None, role: str | None,
                            max_active: int, key_prefix: str = "pairs") -> None:
    """Добавление и правка пар — публичная точка входа для других вкладок дашборда (например,
    «Сбор и управление»), не только для вкладки «Пары конкурентов»."""
    st.markdown('<p class="section-title">Добавить пару</p>', unsafe_allow_html=True)
    _render_add(connect, actor or "?", role, max_active, key_prefix)
    st.markdown('<p class="section-title">Пары — правка и отключение</p>', unsafe_allow_html=True)
    _render_pairs_grid(connect, pairs, actor or "?", role, key_prefix)


def _asin_registry(pairs: pd.DataFrame) -> pd.DataFrame:
    """Все ASIN одним списком: каждый ASIN один раз, с ролью и числом пар, в которых он участвует.

    В нашей базе ASIN сам по себе не хранится — он существует только внутри пары. Поэтому список
    собирается из пар, а «стереть» означает отключить все пары, где этот ASIN участвует."""
    if pairs.empty:
        return pd.DataFrame(columns=["marketplace", "asin", "name", "role", "pairs", "active_pairs"])
    sides = pd.concat([
        pd.DataFrame({"marketplace": pairs["marketplace"], "asin": pairs["our_asin"],
                      "name": pairs["our_product"], "role": "наш", "active": pairs["active"]}),
        pd.DataFrame({"marketplace": pairs["marketplace"], "asin": pairs["comp_asin"],
                      "name": pairs["competitor_name"], "role": "конкурент", "active": pairs["active"]}),
    ], ignore_index=True)
    sides = sides[sides["asin"].astype(str).str.strip().ne("")]
    grouped = sides.groupby(["marketplace", "asin"], as_index=False).agg(
        name=("name", lambda values: next((str(v) for v in values if str(v or "").strip()), "")),
        role=("role", "min"),
        pairs=("asin", "size"),
        active_pairs=("active", "sum"),
    )
    return grouped.sort_values(["marketplace", "asin"], ignore_index=True)


def _erase_callback(connect, pairs: pd.DataFrame, targets, actor: str, role: str) -> None:
    """«Стереть» ASIN — отключить все активные пары, где он участвует."""
    chosen = {(market, asin) for market, asin in targets}
    keys = [
        (row.marketplace, row.our_asin, row.comp_asin)
        for row in pairs.itertuples()
        if row.active and ((row.marketplace, row.our_asin) in chosen or (row.marketplace, row.comp_asin) in chosen)
    ]
    if not keys:
        st.session_state["pairs_flash"] = ("info", "Активных пар с этими ASIN нет.")
        return
    try:
        count = pairs_store.set_pairs_active(connect, keys, False, actor_role=role, actor=actor)
    except _ERRORS as exc:
        st.session_state["pairs_flash"] = ("error", str(exc))
        return
    st.session_state["pairs_flash"] = ("success", f"Отключено пар: {count}.")


def _add_asins_callback(connect, market: str, text: str, kind: str, actor: str, role: str) -> None:
    try:
        result = asins_store.add_asins(connect, market, text, kind, actor_role=role, actor=actor)
    except (*_ERRORS, asins_store.AsinStoreError) as exc:
        st.session_state["pairs_flash"] = ("error", str(exc))
        return
    parts = []
    if result["added"]:
        parts.append(f"добавлено {result['added']}")
    if result["restored"]:
        parts.append(f"возвращено {result['restored']}")
    if result["skipped"]:
        parts.append(f"уже было {result['skipped']}")
    st.session_state["pairs_flash"] = ("success", "ASIN: " + (", ".join(parts) or "без изменений"))


def _save_registry_callback(connect, changes, actor: str, role: str) -> None:
    """Сохраняет правки названий и исходных ссылок за одно нажатие."""
    saved = 0
    try:
        for key, name, source_url in changes:
            saved += asins_store.rename(connect, key, name, actor_role=role, actor=actor,
                                        source_url=source_url)
    except (*_ERRORS, asins_store.AsinStoreError) as exc:
        st.session_state["pairs_flash"] = (
            "error", f"Сохранено строк: {saved}, дальше ошибка — {exc}" if saved else str(exc),
        )
        return
    st.session_state["pairs_flash"] = ("success", f"Сохранено строк: {saved}.")


def _drop_asins_callback(connect, keys, actor: str, role: str) -> None:
    try:
        count = asins_store.set_active(connect, keys, False, actor_role=role, actor=actor)
    except (*_ERRORS, asins_store.AsinStoreError) as exc:
        st.session_state["pairs_flash"] = ("error", str(exc))
        return
    st.session_state["pairs_flash"] = ("success", f"Убрано ASIN: {count}.")


def _render_add_asin_form(connect, actor: str, role: str, key_prefix: str = "pairs") -> None:
    """Вписать ASIN прямо в справочник, без пары."""
    columns = st.columns([2, 1, 1])
    text = columns[0].text_input("Вписать ASIN или ссылки", key=f"{key_prefix}_asin_add_text",
                                 placeholder="B0XXXXXXXX, ссылка на Amazon…")
    market = columns[1].selectbox("Страна", pairs_store.MARKETS, key=f"{key_prefix}_asin_add_market")
    kind = columns[2].selectbox(
        "Это", asins_store.KINDS, key=f"{key_prefix}_asin_add_kind",
        format_func=lambda value: asins_store.KIND_LABELS[value],
    )
    st.button("Вписать", key=f"{key_prefix}_asin_add_btn", disabled=not text.strip(),
              on_click=_add_asins_callback, args=(connect, market, text, kind, actor, role))


def _render_registry_from_store(connect, actor: str, role: str, key_prefix: str = "pairs") -> bool:
    """Справочник ASIN: вписать и убрать. Возвращает False, если миграции 008 ещё нет.

    key_prefix отличает ключи виджетов, когда список показан на нескольких вкладках сразу
    («Пары конкурентов» и «Сбор и управление») — иначе Streamlit ловит одинаковые ключи."""
    try:
        if not asins_store.registry_exists(connect):
            return False
        rows = asins_store.load_asins(connect)
    except asins_store.AsinStoreError:
        # Справочник — надстройка: если он недоступен, показываем тот же список, собранный из пар,
        # вместо ошибки во вкладке. Сбой самой базы виден выше, на уровне всей страницы.
        log.warning("Справочник ASIN недоступен, показываю список из пар")
        return False

    _render_add_asin_form(connect, actor, role, key_prefix)
    if not rows:
        st.info("В справочнике пока пусто.")
        return True

    registry = pd.DataFrame(rows)
    query = st.text_input("ASIN или часть названия", key=f"{key_prefix}_asin_registry_search")
    show_off = st.checkbox("Показывать убранные", key=f"{key_prefix}_asin_registry_show_off")
    shown = _matches(registry, query)
    if not show_off:
        shown = shown[shown["active"]]
    if shown.empty:
        st.info("Ничего не найдено.")
        return True
    shown = shown.head(MAX_EDIT_ROWS).reset_index(drop=True)

    # Исходная ссылка, как её вписали: собранная из ASIN теряет параметры и вариант товара.
    # Если ссылки нет, показываем собранную — чтобы товар всё равно открывался.
    sources = [url or _asin_url(a, m)
               for url, a, m in zip(shown["source_url"], shown["asin"], shown["marketplace"])]
    table = pd.DataFrame({
        "Страна": shown["marketplace"],
        "ASIN": shown["asin"],
        "Ссылка": sources,
        "Название": shown["name"],
        "Роль": [asins_store.KIND_LABELS.get(kind, kind) for kind in shown["kind"]],
        "В работе": shown["active"],
        "Убрать": False,
    })
    edited = st.data_editor(
        table, key=f"{key_prefix}_asin_registry_grid", hide_index=True, use_container_width=True, height=400,
        disabled=["Страна", "ASIN", "Роль", "В работе"],
        column_config={
            "Ссылка": st.column_config.LinkColumn("Ссылка", display_text="открыть", width="small"),
            "Название": st.column_config.TextColumn("Название", max_chars=pairs_store.MAX_NAME),
            "В работе": st.column_config.CheckboxColumn("В работе"),
            "Убрать": st.column_config.CheckboxColumn("Убрать"),
        },
    )

    changes = []
    for position in range(len(shown)):
        name_now = str(edited["Название"].iloc[position] or "")
        url_now = str(edited["Ссылка"].iloc[position] or "")
        was_url = str(shown["source_url"].iloc[position] or "")
        # Подставленную из ASIN ссылку за правку не считаем: пользователь её не трогал.
        url_changed = url_now != (was_url or sources[position])
        if name_now != str(shown["name"].iloc[position] or "") or url_changed:
            changes.append((
                (shown["marketplace"].iloc[position], shown["asin"].iloc[position]),
                name_now,
                url_now if url_changed else None,
            ))

    buttons = st.columns(2)
    marked = edited[edited["Убрать"]]
    keys = [(shown["marketplace"].iloc[i], shown["asin"].iloc[i]) for i in marked.index]
    with buttons[0]:
        st.button(f"Сохранить изменения ({len(changes)})", key=f"{key_prefix}_asin_registry_save",
                  disabled=not changes, on_click=_save_registry_callback,
                  args=(connect, changes, actor, role))
    with buttons[1]:
        st.button(f"Убрать отмеченные ({len(keys)})", key=f"{key_prefix}_asin_registry_drop", disabled=not keys,
                  on_click=_drop_asins_callback, args=(connect, keys, actor, role))
    if len(_matches(registry, query)) > MAX_EDIT_ROWS:
        st.caption(f"Показаны первые {MAX_EDIT_ROWS}: уточните поиск.")
    return True


def _render_registry(connect, pairs: pd.DataFrame, actor: str, role: str, key_prefix: str = "pairs") -> None:
    if _render_registry_from_store(connect, actor, role, key_prefix):
        return
    # Справочника в базе ещё нет (миграция 008 не применена) — показываем то же самое, собранное
    # из пар: список и «стереть» работают, вписать отдельный ASIN пока некуда.
    registry = _asin_registry(pairs)
    if registry.empty:
        st.info("ASIN пока нет.")
        return

    top = st.columns([3, 2])
    query = top[0].text_input("ASIN или часть названия", key=f"{key_prefix}_registry_search")
    only_active = top[1].checkbox("Только участвующие в сборе", value=True, key=f"{key_prefix}_registry_active")

    shown = _matches(registry, query)
    if only_active:
        shown = shown[shown["active_pairs"] > 0]
    if shown.empty:
        st.info("Ничего не найдено.")
        return
    shown = shown.head(MAX_EDIT_ROWS).reset_index(drop=True)

    table = pd.DataFrame({
        "Страна": shown["marketplace"],
        "ASIN": [_asin_url(a, m) for a, m in zip(shown["asin"], shown["marketplace"])],
        "Название": shown["name"].fillna(""),
        "Роль": shown["role"],
        "Пар в сборе": shown["active_pairs"].astype(int),
        "Стереть": False,
    })
    edited = st.data_editor(
        table, key=f"{key_prefix}_registry_grid", hide_index=True, use_container_width=True, height=400,
        disabled=["Страна", "ASIN", "Название", "Роль", "Пар в сборе"],
        column_config={
            "ASIN": st.column_config.LinkColumn("ASIN", display_text=_ASIN_LINK_TEXT, width="small"),
            "Стереть": st.column_config.CheckboxColumn("Стереть"),
        },
    )
    marked = edited[edited["Стереть"]]
    targets = [(shown["marketplace"].iloc[i], shown["asin"].iloc[i]) for i in marked.index]
    affected = int(shown.loc[marked.index, "active_pairs"].sum()) if len(marked) else 0

    confirmed = True
    if affected > _CONFIRM_ABOVE:
        typed = st.text_input(f"Отключится {affected} пар. Введите СТЕРЕТЬ для подтверждения",
                              key=f"{key_prefix}_registry_confirm")
        confirmed = typed.strip().upper() == "СТЕРЕТЬ"
    st.button(
        f"Стереть отмеченные ({len(targets)})", key=f"{key_prefix}_registry_btn",
        disabled=not targets or not confirmed,
        on_click=_erase_callback, args=(connect, pairs, targets, actor, role),
    )
    if len(_matches(registry, query)) > MAX_EDIT_ROWS:
        st.caption(f"Показаны первые {MAX_EDIT_ROWS}: уточните поиск.")


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


def render_asin_registry(connect, pairs: pd.DataFrame, actor: str | None, role: str | None,
                         key_prefix: str = "pairs") -> None:
    """Список всех ASIN со ссылками, названием и правкой — публичная точка входа для других вкладок
    дашборда (например, «Сбор и управление»), не только для вкладки «Пары конкурентов».

    key_prefix обязателен, если список показывается на нескольких вкладках одной страницы сразу:
    у виджетов должны быть разные ключи, иначе Streamlit откажет на дублирующемся key."""
    _render_registry(connect, pairs, actor or "?", role, key_prefix)


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
        st.dataframe(_with_links(found), use_container_width=True, hide_index=True, height=350,
                     column_config=_PAIR_COLUMNS | _LINK_COLUMNS)

    if not access.has_role(role, access.ROLE_EDITOR):
        st.caption("Чтобы добавлять и убирать пары, откройте «🔒 Управление» вверху страницы.")
        return
    st.caption("Изменения попадают в следующий сбор. Лист Competitors в Google Таблице парсер больше не читает.")
    with st.expander("➕ Добавить конкурентов", expanded=True):
        _render_add(connect, actor or "?", role, max_active)
    with st.expander("📇 Все ASIN"):
        _render_registry(connect, pairs, actor or "?", role)
    with st.expander("✏️ Исправить названия"):
        _render_edit(connect, pairs, actor or "?", role)
    with st.expander("🗑 Убрать пары"):
        _render_remove(connect, pairs, actor or "?", role)
    with st.expander("↩ Вернуть отключённые"):
        _render_restore(connect, pairs, actor or "?", role)
    with st.expander("📜 Журнал изменений"):
        _render_log(connect)
