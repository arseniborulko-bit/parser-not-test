"""Раздел «Best Seller подкатегории» (этап 19, SOP-AMZ-001) — рядом с «Мониторингом BSR», как в макете. Пока в
разработке, но виден всем сотрудникам.

Сейчас раздел повторяет макет (две страницы: один ASIN и список ASIN) на примерных данных из него —
поиск через ScrapingDog, кэш и запись в таблицу гипотез ещё не подключены, кнопки этих действий неактивны.
Таблица подкатегорий при этом считается настоящими правилами К1–К4 из best_seller.py, а не нарисована.
"""

from __future__ import annotations

from dataclasses import dataclass
from html import escape
from typing import List, Optional, Sequence, Tuple

import streamlit as st

import access

import best_seller
import best_seller_store
import pairs_store

SHEET_NAME = "Получение bs за счет смены под категории"
MODES = ("Один ASIN", "Список ASIN")
STATUS_FILTERS = ("Все", "С подходящими", "Без подходящих", "Ошибки")
NOT_CONNECTED = "Поиск ещё не подключён: сначала разведка ScrapingDog (этап 0)."
WRITE_LATER = "Запись в Google-таблицу гипотез подключим после поиска (этап 3)."
# Как в форме «Добавить пару»: только полная ссылка Amazon, чтобы страна была видна из самой ссылки
# (владелец, 15.10.2026). Голый ASIN не принимается.
LINK_HINT = "Нужна полная ссылка на товар Amazon, например https://www.amazon.de/dp/B07WVYMY7Y — по ней видна страна."


@dataclass(frozen=True)
class Listing:
    asin: str
    title: str
    brand: str
    path: str
    bsr: int
    bsr_category: str
    bsr_30d: Tuple[int, int]
    target: Tuple[int, int]
    own_ranks: Tuple[int, ...]
    own_ranks_label: str
    has_badge: bool


@dataclass(frozen=True)
class Subcategory:
    name: str
    path: str
    node: str
    leader_bsr: int
    item_count: Optional[int] = None
    relevance: Optional[str] = None
    leader_badge: Optional[bool] = None


@dataclass(frozen=True)
class BatchRow:
    asin: str
    market: str
    group: str
    our_bsr: Optional[str]
    best: Optional[str]
    leader_bsr: Optional[int]
    suitable: Optional[int]
    priority: Optional[str]
    status: str


# Пример из макета: листинг B0GNBSDL65 (Scrapingdog), подкатегории и BSR лидеров — из SOP-AMZ-001.
DEMO_LISTING = Listing(
    asin="B0GNBSDL65",
    title="100% Merino Wool Beanie for Women, Men — Skull Cap 17.5um",
    brand="Merino.tech",
    path="Clothing › Women › Accessories › Hats & Caps › Skullies & Beanies",
    bsr=91_233,
    bsr_category="Clothing, Shoes & Jewelry",
    bsr_30d=(61_496, 101_435),
    target=(55_000, 60_000),
    own_ranks=(166, 191),
    own_ranks_label="Women's / Men's Skullies & Beanies",
    has_badge=False,
)
DEMO_SUBCATEGORIES = (
    Subcategory("Men's Novelty Sun Hats", "Clothing › Novelty & More › Men › Accessories", "9057034011",
                460_779, 100, best_seller.RELEVANT, True),
    Subcategory("Women's Ice Skating Clothing", "Clothing › Sport Specific Clothing › Ice Skating", "10208070011",
                77_590, 100, best_seller.SEMI_RELEVANT, True),
    Subcategory("Women's Novelty Sun Hats", "Clothing › Novelty & More › Women", "9056981011", 35_598),
    Subcategory("Men's Hats & Caps", "Clothing › Men › Accessories", "2474954011", 358),
    Subcategory("Women's Sport Headbands", "Clothing › Women › Accessories", "23466430011", 324),
)
# Пример из макета, стр. 2: первые две строки — из SOP, остальные условные.
DEMO_BATCH = (
    BatchRow("B0GNBSDL65", "US", "beanie", "91 233", "Men's Novelty Sun Hats", 460_779, 2, best_seller.HIGH, "готово"),
    BatchRow("B0DG2B7KJN", "US", "beanie + socks", "~51 000", "уже #3 в Women's Novelty Beanies", None, None, None,
             "поиск не нужен"),
    BatchRow("B0XXXXXX03", "US", "neck gaiter", "148 200", "Women's Cold Weather Neck Gaiters", 312_500, 3, best_seller.HIGH,
             "готово"),
    BatchRow("B0XXXXXX04", "US", "boot socks", "205 900", "Women's Soccer Socks", 188_700, 1, best_seller.LOW, "готово"),
    BatchRow("B0XXXXXX05", "US", "kids pants", "310 000", "Girls' Novelty Leggings", 260_400, 2, best_seller.HIGH, "готово"),
    BatchRow("B0XXXXXX06", "US", "beanie", "72 800", "нет подходящих", None, 0, None, "готово"),
    BatchRow("B0XXXXXX07", "US", "boot socks", None, None, None, None, None, "ошибка запроса"),
    BatchRow("B0XXXXXX08", "US", "neck gaiter", None, None, None, None, None, "в очереди"),
)
DEMO_BATCH_TOTAL = 28
DEMO_BATCH_COUNTS = (("С подходящими", 12, "#15803d"), ("Без подходящих", 4, "#111827"),
                     ("Поиск не нужен", 2, "#111827"), ("Ошибки запроса", 1, "#b91c1c"), ("В очереди", 9, "#111827"))

_CSS = """
<style>
.bs-card { background:#fff; border:1px solid #e5e7eb; border-radius:15px; padding:1rem 1.2rem; margin:.4rem 0 .8rem; }
.bs-eyebrow { color:#64748b; font-size:.72rem; letter-spacing:.08em; text-transform:uppercase; font-weight:650; }
.bs-title { font-weight:750; font-size:1.12rem; color:#111827; margin:.2rem 0; }
.bs-meta { color:#64748b; font-size:.85rem; }
.bs-head { display:flex; justify-content:space-between; align-items:flex-start; gap:1rem; }
.bs-tiles { display:grid; grid-template-columns:repeat(auto-fit,minmax(170px,1fr)); gap:.7rem; margin-top:.9rem; }
.bs-tile { background:#f1f5f9; border-radius:10px; padding:.7rem .9rem; }
.bs-tile-label { color:#475569; font-size:.78rem; }
.bs-tile-value { color:#111827; font-size:1.45rem; font-weight:800; line-height:1.25; }
.bs-tile-detail { color:#64748b; font-size:.76rem; }
.bs-pill { display:inline-block; border-radius:999px; padding:.12rem .6rem; font-size:.78rem; font-weight:650; white-space:nowrap; }
.bs-relevant { background:#dcfce7; color:#166534; }
.bs-semi { background:#fef3c7; color:#92400e; }
.bs-risky { background:#ffedd5; color:#9a3412; }
.bs-high { background:#0f4c75; color:#fff; }
.bs-low { background:#e5e7eb; color:#374151; }
.bs-risk { background:#ffedd5; color:#9a3412; }
.bs-warn { background:#fef3c7; color:#92400e; }
.bs-ok { background:#dcfce7; color:#166534; }
.bs-muted { background:#e5e7eb; color:#374151; }
.bs-error { background:#fee2e2; color:#b91c1c; }
.bs-yes { color:#15803d; font-weight:800; }
.bs-no { color:#dc2626; font-weight:800; }
.bs-dash { color:#94a3b8; }
.bs-th { color:#475569; font-size:.8rem; font-weight:650; }
.bs-name { font-weight:650; color:#111827; }
.bs-sub { color:#64748b; font-size:.78rem; }
.bs-num { text-align:right; font-variant-numeric:tabular-nums; }
.bs-rejected { color:#475569; font-size:.85rem; }
.bs-legend { color:#334155; font-size:.82rem; line-height:1.55; margin:.3rem 0; }
.bs-demo { border:1px dashed #94a3b8; border-radius:10px; padding:.6rem .9rem; color:#475569; font-size:.8rem; margin:.6rem 0; }
.bs-table { width:100%; border-collapse:collapse; font-size:.88rem; }
.bs-table th { text-align:left; color:#475569; font-size:.8rem; font-weight:650; padding:.55rem .5rem; background:#f8fafc; border-bottom:1px solid #e5e7eb; }
.bs-table td { padding:.6rem .5rem; border-bottom:1px solid #eef2f7; color:#111827; }
.bs-table td.bs-num, .bs-table th.bs-num { text-align:right; }
.bs-asin { color:#0f4c75; font-weight:650; text-decoration:underline; }
.bs-progress-head { display:flex; justify-content:space-between; gap:1rem; flex-wrap:wrap; }
.bs-bar { height:8px; background:#e5e7eb; border-radius:999px; overflow:hidden; margin:.6rem 0 .2rem; }
.bs-bar > div { height:100%; background:#0f4c75; border-radius:999px; }
.bs-retry { color:#0f4c75; text-decoration:underline; font-size:.82rem; margin-left:.4rem; }
.bs-progress-title { font-weight:750; font-size:1.05rem; color:#111827; }
</style>
"""


def fmt_int(number: Optional[int]) -> str:
    return "–" if number is None else f"{number:,}".replace(",", " ")


def fmt_ratio(ratio: Optional[float]) -> str:
    """«×7,7», «×0,6», «×0,006»: одна цифра после запятой, а у совсем малых — три, чтобы не показывать «×0,0»."""
    if ratio is None:
        return "–"
    digits = 1 if ratio >= 0.1 else 3
    return "×" + f"{ratio:.{digits}f}".replace(".", ",")


def evaluate_demo(listing: Listing, subcategories: Sequence[Subcategory]) -> List[Tuple[Subcategory, best_seller.Verdict]]:
    """Подкатегории примера через настоящие правила К1–К4 (цель — верх диапазона, как в макете), по порядку
    best_seller.ranked."""
    target = listing.target[1]
    pairs = [(sub, best_seller.evaluate(target, sub.leader_bsr, sub.item_count, sub.relevance, sub.leader_badge))
             for sub in subcategories]
    order = best_seller.ranked([verdict for _, verdict in pairs])
    return sorted(pairs, key=lambda pair: next(i for i, v in enumerate(order) if v is pair[1]))


def parse_links(text: object) -> Tuple[List[pairs_store.Item], List[str], int]:
    """ASIN и страна из ссылок Amazon (тот же разбор, что у формы «Добавить пару»); голые ASIN — нераспознанные.
    Повтор — та же страна и тот же ASIN: один ASIN в Германии и в Италии — две разные строки (BSR и подкатегории
    там свои), поэтому каждая ссылка разбирается отдельно, а не общим списком с повтором по одному ASIN."""
    items: List[pairs_store.Item] = []
    invalid: List[str] = []
    repeats = 0
    seen: set = set()
    for token in pairs_store._TOKEN_SPLIT.split(text.strip()) if isinstance(text, str) else []:
        if not token:
            continue
        batch = pairs_store.parse_asin_batch(token, require_link=True)
        invalid += batch.invalid
        for item in batch.items:
            if (item.market, item.asin) in seen:
                repeats += 1
                continue
            seen.add((item.market, item.asin))
            items.append(item)
    return items, invalid, repeats


def links_summary(items: Sequence[pairs_store.Item], repeats: int = 0) -> str:
    if not items:
        return "Ссылок пока нет."
    counts: dict = {}
    for item in items:
        counts[item.market] = counts.get(item.market, 0) + 1
    by_market = " · ".join(f"{m} {n}" for m, n in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])))
    tail = f" · повторов убрано: {repeats}" if repeats else ""
    return f"Распознано {len(items)} ASIN: {by_market}{tail}"


def filter_batch(rows: Sequence[BatchRow], query: str, group: str, status: str) -> List[BatchRow]:
    query = query.strip().upper()
    out = []
    for row in rows:
        if query and query not in row.asin.upper():
            continue
        if group != "Все группы" and row.group != group:
            continue
        if status == "С подходящими" and not row.suitable:
            continue
        if status == "Без подходящих" and not (row.status == "готово" and row.suitable == 0):
            continue
        if status == "Ошибки" and row.status != "ошибка запроса":
            continue
        out.append(row)
    return out


def _pill(text: str, css: str) -> str:
    return f'<span class="bs-pill {css}">{escape(text)}</span>'


_RELEVANCE_CSS = {best_seller.RELEVANT: "bs-relevant", best_seller.SEMI_RELEVANT: "bs-semi",
                  best_seller.RISKY: "bs-risky"}
_PRIORITY_CSS = {best_seller.HIGH: "bs-high", best_seller.LOW: "bs-low", best_seller.RISK: "bs-risk"}
_STATUS_CSS = {"готово": "bs-ok", "поиск не нужен": "bs-muted", "ошибка запроса": "bs-error", "в очереди": "bs-warn"}


def _mark(value: Optional[bool]) -> str:
    if value is None:
        return '<span class="bs-dash">–</span>'
    return '<span class="bs-yes">✓</span>' if value else '<span class="bs-no">✗</span>'


def _soon(target, label: str, key: str, message: str = NOT_CONNECTED, disabled: bool = False) -> None:
    """Кнопка из макета, действие которой ещё не подключено: выглядит как в макете, нажатие только объясняет,
    что будет дальше (неактивная кнопка в оформлении дашборда почти не видна)."""
    if target.button(label, key=key, width="stretch", disabled=disabled):
        st.toast(message)


def _demo_note(text: str) -> None:
    st.markdown(f'<div class="bs-demo">{escape(text)}</div>', unsafe_allow_html=True)


def _listing_card(listing: Listing) -> str:
    badge = _pill("Плашка Best Seller есть", "bs-ok") if listing.has_badge else _pill("Плашки Best Seller нет", "bs-warn")
    tiles = [
        ("Наш BSR сейчас", fmt_int(listing.bsr), listing.bsr_category),
        ("BSR за 30 дней", f"{fmt_int(listing.bsr_30d[0])} – {fmt_int(listing.bsr_30d[1])}", "минимум – максимум"),
        ("Целевой BSR", f"{fmt_int(listing.target[0])} – {fmt_int(listing.target[1])}", "после восстановления, по SOP"),
        ("Ранг в своих подкатегориях", " · ".join(f"#{r}" for r in listing.own_ranks), listing.own_ranks_label),
    ]
    tiles_html = "".join(
        f'<div class="bs-tile"><div class="bs-tile-label">{escape(label)}</div>'
        f'<div class="bs-tile-value">{escape(value)}</div><div class="bs-tile-detail">{escape(detail)}</div></div>'
        for label, value, detail in tiles
    )
    return (
        '<div class="bs-card"><div class="bs-head"><div>'
        '<div class="bs-eyebrow">Наш листинг</div>'
        f'<div class="bs-title">{escape(listing.title)}</div>'
        f'<div class="bs-meta">{escape(listing.asin)} · {escape(listing.brand)} · {escape(listing.path)}</div>'
        f'</div><div>{badge}</div></div><div class="bs-tiles">{tiles_html}</div></div>'
    )


_COLS = [0.45, 3.3, 1.6, 1.05, 1.15, 0.38, 0.38, 0.38, 0.38, 1.5]


def _results(listing: Listing, evaluated: List[Tuple[Subcategory, best_seller.Verdict]]) -> None:
    suitable = sum(v.status == best_seller.SUITABLE for _, v in evaluated)
    rejected = sum(v.status == best_seller.REJECTED for _, v in evaluated)
    with st.container(border=True):
        head, counts = st.columns([3, 2], vertical_alignment="center")
        head.markdown('<div class="bs-title">Результат поиска</div>', unsafe_allow_html=True)
        counts.markdown(
            f'<div style="text-align:right">{_pill(f"Подходят: {suitable}", "bs-ok")} '
            f'{_pill(f"Отклонены: {rejected}", "bs-muted")}</div>',
            unsafe_allow_html=True,
        )
        header = ["", "Подкатегория", "Релевантность", "BSR лидера", "Лидер / наша цель", "К1", "К2", "К3", "К4",
                  "Приоритет"]
        for col, title in zip(st.columns(_COLS), header):
            css = "bs-th bs-num" if title in ("BSR лидера", "Лидер / наша цель") else "bs-th"
            col.markdown(f'<div class="{css}">{escape(title)}</div>', unsafe_allow_html=True)
        selected = 0
        for index, (sub, verdict) in enumerate(evaluated):
            cols = st.columns(_COLS, vertical_alignment="center")
            ok = verdict.status == best_seller.SUITABLE
            if cols[0].checkbox(sub.name, value=ok and verdict.priority == best_seller.HIGH, disabled=not ok,
                                key=f"bs_pick_{listing.asin}_{sub.node}", label_visibility="collapsed"):
                selected += ok
            cols[1].markdown(
                f'<div class="bs-name">{escape(sub.name)}</div>'
                f'<div class="bs-sub">{escape(sub.path)} · {escape(sub.node)}</div>',
                unsafe_allow_html=True,
            )
            relevance = (_pill(best_seller.RELEVANCE_LABELS[sub.relevance], _RELEVANCE_CSS.get(sub.relevance, "bs-muted"))
                         if verdict.k3 else '<span class="bs-dash">–</span>')
            cols[2].markdown(relevance, unsafe_allow_html=True)
            cols[3].markdown(f'<div class="bs-num"><b>{fmt_int(sub.leader_bsr)}</b></div>', unsafe_allow_html=True)
            cols[4].markdown(f'<div class="bs-num">{fmt_ratio(verdict.leader_to_target)}</div>', unsafe_allow_html=True)
            for col, value in zip(cols[5:9], (verdict.k1, verdict.k2, verdict.k3, verdict.k4)):
                col.markdown(_mark(value), unsafe_allow_html=True)
            if ok:
                cols[9].markdown(_pill(verdict.priority, _PRIORITY_CSS[verdict.priority]), unsafe_allow_html=True)
            else:
                cols[9].markdown(f'<span class="bs-rejected">{escape(verdict.reason)}</span>', unsafe_allow_html=True)
        button, note = st.columns([1.3, 3], vertical_alignment="center")
        _soon(button, "Записать выбранные в таблицу гипотез", "bs_write_one", WRITE_LATER, disabled=not selected)
        note.markdown(f'<span class="bs-meta">Выбрано: {selected} → Google-таблица «{escape(SHEET_NAME)}»</span>',
                      unsafe_allow_html=True)


def _legend() -> None:
    st.markdown(
        f'<p class="bs-legend"><b>К1</b> лидер слабее нашей цели (BSR ≥ {fmt_int(best_seller.K1_MIN_LEADER_BSR)} '
        f'и выше целевого) · <b>К2</b> в подкатегории {best_seller.K2_REQUIRED_ITEMS} товаров · <b>К3</b> наш тип '
        'товара подходит · <b>К4</b> у лидера есть значок Best Seller. Если К1 не пройден, остальные критерии не '
        'проверяются, это экономит запросы.</p>'
        '<p class="bs-legend">Релевантность: релевантная — брать; полурелевантная — брать с низким приоритетом; '
        'рискованная — записать, но искать дальше (по SOP).</p>',
        unsafe_allow_html=True,
    )


def _render_single() -> None:
    st.markdown('<p class="section-title">Поиск подкатегорий Best Seller</p>', unsafe_allow_html=True)
    st.markdown(
        '<p class="section-note">По ASIN находит подкатегории Amazon US, где листинг может получить плашку '
        '#1 Best Seller при своём текущем BSR. Процесс описан в SOP-AMZ-001.</p>',
        unsafe_allow_html=True,
    )
    with st.container(border=True):
        link_col, button_col, follow_col = st.columns([3.4, 1.5, 2.6], vertical_alignment="bottom")
        link = link_col.text_input("Ссылка на товар Amazon", value=f"https://www.amazon.com/dp/{DEMO_LISTING.asin}",
                                   key="bs_link", help=LINK_HINT)
        _soon(button_col, "Найти подкатегории", "bs_find")
        if follow_col.checkbox("Следить за этим ASIN (проверка раз в неделю)", key="bs_follow"):
            follow_col.caption("Еженедельная проверка заработает вместе с поиском.")
        items, _, _ = parse_links(link)
        if items:
            st.caption(f"Распознано: ASIN {items[0].asin} · страна {items[0].market}")
        elif link.strip():
            st.warning(LINK_HINT)
    st.markdown(_listing_card(DEMO_LISTING), unsafe_allow_html=True)
    _results(DEMO_LISTING, evaluate_demo(DEMO_LISTING, DEMO_SUBCATEGORIES))
    _legend()
    _demo_note(
        "Пример из макета: данные листинга — из ответа Scrapingdog по B0GNBSDL65 и истории BSR за 30 дней, "
        "подкатегории и BSR лидеров — из SOP-AMZ-001. Критерии и приоритеты посчитаны правилами К1–К4. "
        "В рабочей версии значения будут актуальными. " + NOT_CONNECTED
    )


def _batch_table(rows: Sequence[BatchRow]) -> str:
    body = []
    for row in rows:
        asin = f'<span class="bs-asin">{escape(row.asin)}</span>' if row.status == "готово" else escape(row.asin)
        priority = _pill(row.priority, _PRIORITY_CSS[row.priority]) if row.priority else "–"
        best = escape(row.best) if row.best else "–"
        if row.status != "готово" or not row.suitable:
            best = f'<span class="bs-sub">{best}</span>'
        status = _pill(row.status, _STATUS_CSS.get(row.status, "bs-muted"))
        if row.status == "ошибка запроса":
            status += '<span class="bs-retry">повторить</span>'
        body.append(
            f"<tr><td>{asin}</td><td>{escape(row.market)}</td><td>{escape(row.group)}</td><td class='bs-num'>{escape(row.our_bsr or '–')}</td>"
            f"<td>{best}</td><td class='bs-num'>{fmt_int(row.leader_bsr)}</td>"
            f"<td class='bs-num'><b>{'–' if row.suitable is None else row.suitable}</b></td>"
            f"<td>{priority}</td><td>{status}</td></tr>"
        )
    head = ("<tr><th>ASIN</th><th>Страна</th><th>Группа</th><th class='bs-num'>Наш BSR</th><th>Лучшая подкатегория</th>"
            "<th class='bs-num'>BSR лидера</th><th class='bs-num'>Подходят</th><th>Приоритет</th><th>Статус</th></tr>")
    return f'<table class="bs-table"><thead>{head}</thead><tbody>{"".join(body)}</tbody></table>'


WATCH_MODES = ("Добавить новые", "Управлять существующими")
NO_DATABASE = "База дашборда не подключена: список ASIN Best Seller недоступен."
NO_TABLE = ("Таблиц раздела Best Seller в базе ещё нет: примените best_seller_db/001_watchlist.sql "
            "(своя схема best_seller, таблицы мониторинга не затрагивает).")


def _load_watchlist(connect) -> Tuple[List[dict], Optional[str]]:
    """Строки списка и причина, по которой работать с ним нельзя (None — можно)."""
    if connect is None:
        return [], NO_DATABASE
    try:
        if not best_seller_store.table_exists(connect):
            return [], NO_TABLE
        return best_seller_store.list_asins(connect), None
    except best_seller_store.BestSellerStoreError as exc:
        return [], str(exc)


def _watch_add(connect, rows: List[dict], problem: Optional[str], actor: str, can_edit: bool) -> None:
    text = st.text_area("Ссылки на товары Amazon, по одной в строке", key="bs_batch_input", height=110, help=LINK_HINT,
                        placeholder="https://www.amazon.de/dp/B07WVYMY7Y\nhttps://www.amazon.it/dp/B07WVYMY7Y")
    items, invalid, repeats = parse_links(text)
    st.caption(links_summary(items, repeats))
    if invalid:
        st.warning(f"Не распознано ({len(invalid)}): {', '.join(invalid[:5])}{' …' if len(invalid) > 5 else ''}. "
                   + LINK_HINT)
    if problem:
        st.info(problem)
        return
    if not items:
        return
    plan = best_seller_store.plan_add(rows, [(item.market, item.asin) for item in items])
    for error in plan.errors:
        st.error(error)
    st.caption(f"Будет добавлено {len(plan.to_add)} · вернётся из отключённых {len(plan.to_enable)} · "
               f"уже в списке {len(plan.already)}")
    if not can_edit:
        st.caption("Менять список может сотрудник с правом редактирования.")
    if st.button("Добавить в список", key="bs_watch_add", type="primary",
                 disabled=not can_edit or bool(plan.errors) or not plan.changes):
        try:
            done = best_seller_store.add(connect, plan.to_add + plan.to_enable, actor_role=best_seller_role(can_edit),
                                         actor=actor)
        except (ValueError, best_seller_store.BestSellerStoreError) as exc:
            st.error(str(exc))
        else:
            st.session_state["bs_watch_flash"] = f"Добавлено {done['add']}, возвращено {done['enable']}."
            st.session_state.pop("bs_batch_input", None)
            st.rerun()


def _watch_manage(connect, rows: List[dict], problem: Optional[str], actor: str, can_edit: bool) -> None:
    if problem:
        st.info(problem)
        return
    if not rows:
        st.info("Список пуст: добавьте ASIN во вкладке «Добавить новые».")
        return
    counts: dict = {}
    for row in rows:
        counts[row["marketplace"]] = counts.get(row["marketplace"], 0) + 1
    market_col, off_col = st.columns([1.2, 2], vertical_alignment="bottom")
    market = market_col.selectbox("Страна", sorted(counts), key="bs_watch_market",
                                  format_func=lambda m: f"{m} · {counts[m]} ASIN")
    show_off = off_col.checkbox("Показывать отключённые", key="bs_watch_show_off")
    shown = [r for r in rows if r["marketplace"] == market and (r["active"] or show_off)]
    if not shown:
        st.info("В этой стране нет активных ASIN.")
        return
    table = [{"Выбрать": False, "ASIN": r["asin"], "Статус": "активен" if r["active"] else "отключён",
              "Добавил": r["added_by"], "Ссылка": f"https://www.amazon.{pairs_store.DOMAIN_BY_MARKET.get(market, 'com')}"
              f"/dp/{r['asin']}"} for r in shown]
    edited = st.data_editor(table, key=f"bs_watch_grid_{market}_{show_off}", hide_index=True, width="stretch",
                            disabled=["ASIN", "Статус", "Добавил", "Ссылка"],
                            column_config={"Выбрать": st.column_config.CheckboxColumn(),
                                           "Ссылка": st.column_config.LinkColumn(display_text="открыть")})
    picked = [(market, row["ASIN"], row["Статус"]) for row in edited if row["Выбрать"]]
    off_btn, on_btn, _ = st.columns([1, 1, 2])
    to_off = [(m, a) for m, a, status in picked if status == "активен"]
    to_on = [(m, a) for m, a, status in picked if status == "отключён"]
    for column, label, keys, active in ((off_btn, f"Отключить выбранные ({len(to_off)})", to_off, False),
                                        (on_btn, f"Вернуть выбранные ({len(to_on)})", to_on, True)):
        if column.button(label, key=f"bs_watch_{'on' if active else 'off'}", width="stretch",
                         disabled=not can_edit or not keys):
            try:
                changed = best_seller_store.set_active(connect, keys, active, actor_role=best_seller_role(can_edit),
                                                       actor=actor)
            except (ValueError, best_seller_store.BestSellerStoreError) as exc:
                st.error(str(exc))
            else:
                st.session_state["bs_watch_flash"] = f"{'Возвращено' if active else 'Отключено'}: {changed}."
                st.rerun()


def best_seller_role(can_edit: bool) -> Optional[str]:
    return access.ROLE_EDITOR if can_edit else None


def _render_watchlist(connect, actor: str, can_edit: bool) -> None:
    """Список ASIN раздела — в отдельной базе Best Seller, как пары в «Сборе и управлении»: добавить ссылками,
    управлять по стране (отключить и вернуть)."""
    rows, problem = _load_watchlist(connect)
    active = sum(1 for r in rows if r["active"])
    with st.container(border=True):
        st.markdown('<div class="bs-title">Список ASIN для Best Seller</div>', unsafe_allow_html=True)
        st.caption(f"Отдельная база раздела, с мониторингом BSR не связана. Активных ASIN: {active}.")
        flash = st.session_state.pop("bs_watch_flash", None)
        if flash:
            st.success(flash)
        mode = st.radio("Действие со списком", WATCH_MODES, horizontal=True, key="bs_watch_mode",
                        label_visibility="collapsed")
        if mode == WATCH_MODES[0]:
            _watch_add(connect, rows, problem, actor, can_edit)
        else:
            _watch_manage(connect, rows, problem, actor, can_edit)


def _render_batch(connect=None, actor: str = "", can_edit: bool = False) -> None:
    st.markdown('<p class="section-title">Поиск подкатегорий: список ASIN</p>', unsafe_allow_html=True)
    st.markdown(
        '<p class="section-note">Один поиск на много ASIN. Топ-100 каждой подкатегории берётся один раз и хранится '
        'неделю, поэтому длинный список не умножает запросы.</p>',
        unsafe_allow_html=True,
    )
    _render_watchlist(connect, actor, can_edit)
    run_col, csv_col, _ = st.columns([1, 1, 2])
    _soon(run_col, "Запустить поиск", "bs_batch_run")
    _soon(csv_col, "Загрузить CSV", "bs_batch_csv")

    done = DEMO_BATCH_TOTAL - DEMO_BATCH_COUNTS[-1][1]
    with st.container(border=True):
        st.markdown(
            f'<div class="bs-progress-head"><span class="bs-progress-title">Обработано {done} из {DEMO_BATCH_TOTAL}'
            '</span><span class="bs-meta">Топы подкатегорий из кэша от 12.10.2026 · новых обращений к Amazon: 14'
            '</span></div>',
            unsafe_allow_html=True,
        )
        st.markdown(f'<div class="bs-bar"><div style="width:{done / DEMO_BATCH_TOTAL:.1%}"></div></div>',
                    unsafe_allow_html=True)
        tiles = "".join(
            f'<div class="bs-tile"><div class="bs-tile-label">{escape(label)}</div>'
            f'<div class="bs-tile-value" style="color:{color}">{count}</div></div>'
            for label, count, color in DEMO_BATCH_COUNTS
        )
        st.markdown(f'<div class="bs-tiles" style="margin-top:.3rem">{tiles}</div>', unsafe_allow_html=True)

    with st.container(border=True):
        search_col, group_col, status_col = st.columns([1.2, 1.4, 2.4], vertical_alignment="bottom")
        query = search_col.text_input("Поиск по ASIN", placeholder="B0…", key="bs_batch_query")
        groups = ["Все группы"] + sorted({row.group for row in DEMO_BATCH})
        group = group_col.selectbox("Товарная группа", groups, key="bs_batch_group")
        status = status_col.radio("Показать", STATUS_FILTERS, horizontal=True, key="bs_batch_status",
                                  label_visibility="collapsed")
        rows = filter_batch(DEMO_BATCH, query, group, status)
        if rows:
            st.markdown(_batch_table(rows), unsafe_allow_html=True)
        else:
            st.info("Под фильтр ничего не подходит.")
        write_col, csv_out_col, page_col = st.columns([1.6, 0.9, 1.6], vertical_alignment="center")
        _soon(write_col, "Записать все с высоким приоритетом в таблицу гипотез", "bs_batch_write", WRITE_LATER)
        _soon(csv_out_col, "Выгрузить в CSV", "bs_batch_csv_out")
        page_col.markdown(
            f'<div class="bs-meta" style="text-align:right">Показано {len(rows)} из {DEMO_BATCH_TOTAL} · '
            'страница 1 из 4</div>',
            unsafe_allow_html=True,
        )
    st.markdown(
        '<p class="bs-legend">Клик по ASIN откроет его подробный результат: подкатегории и критерии К1–К4. Если ASIN '
        'уже в топ-3 своей подкатегории, поиск не запускается (шаг 2 SOP).</p>'
        '<p class="bs-legend">Для списка в сотни и тысячи ASIN работает то же: очередь, фильтры, страницы, повтор для '
        'ошибок. Запросы к Amazon тратятся на новые подкатегории, а не на каждый ASIN.</p>',
        unsafe_allow_html=True,
    )
    _demo_note("Пример из макета: первые две строки взяты из SOP-AMZ-001, ASIN B0XXXXXX и остальные значения "
               "условные. " + NOT_CONNECTED)


def render(connect=None, actor: str = "", can_edit: bool = False) -> None:
    """connect — подключение к базе дашборда (таблицы раздела — в схеме best_seller; None — нет базы);
    can_edit — может ли менять список."""
    st.markdown(_CSS, unsafe_allow_html=True)
    st.info("Раздел в разработке: поиск ещё не подключён, ниже — пример из макета SOP-AMZ-001.")
    mode = st.radio("Режим", MODES, horizontal=True, key="bs_mode", label_visibility="collapsed")
    if mode == MODES[0]:
        _render_single()
    else:
        _render_batch(connect, actor, can_edit)
