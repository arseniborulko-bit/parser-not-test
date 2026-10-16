"""Поиск подкатегорий Best Seller для одного ASIN (этап 19, SOP-AMZ-001). Без Streamlit и без базы: запросы к
ScrapingDog и кэш подставляются извне, поэтому всё проверяется тестами без сети.

Откуда что берётся (разведка 16.10.2026, ARCHITECTURE.md «Этап 0»):
- наш товар — Product API: поле best_sellers_rank — места товара в его подкатегориях с номером узла и разделом
  (`/gp/bestsellers/fashion/9057034011`). Это кандидаты и правило шага 2 SOP (уже в топ-3 — поиск не нужен);
- подкатегория — страница Best Sellers (общий /scrape, 1 кредит): в data-client-recs-list все 50 мест страницы,
  заголовки карточек — для К3, левое меню — соседние подкатегории (ещё кандидаты, без лишних запросов);
- лидер — его страница (1 кредит): BSR (К1) и блок zeitgeistBadge (К4). На странице подкатегории BSR лидера нет.
Порядок и ранняя остановка — как в best_seller.evaluate: если К1 не пройден, ?pg=2 (К2) не запрашивается.

Источник кандидатов временный — подкатегории самого товара и их соседи; правило из SOP уточняется у заказчика.
"""

from __future__ import annotations

import html as html_lib
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Callable, Dict, List, Optional, Protocol, Sequence, Tuple

import best_seller
import pairs_store
from utils import clean_number

CACHE_DAYS = 7
MAX_CANDIDATES = 12
TOP_SIZE = 100

ProductFetch = Callable[[str, str], Optional[dict]]  # (asin, domain) -> ответ Product API
PageFetch = Callable[[str], Optional[str]]  # url -> HTML или None
# (наш заголовок, заголовки топа подкатегории) -> класс релевантности best_seller.RELEVANT… или None (не оценено)
RelevanceFn = Callable[[str, Sequence[str]], Optional[str]]


@dataclass(frozen=True)
class Subcategory:
    node: str
    dept: str
    name: str
    rank: Optional[int] = None  # место нашего товара в ней (для своих подкатегорий)


@dataclass
class BestsellersPage:
    ranked: List[Tuple[int, str]] = field(default_factory=list)  # (место, ASIN)
    titles: Dict[str, str] = field(default_factory=dict)  # ASIN -> заголовок карточки
    name: str = ""
    linked: List[Subcategory] = field(default_factory=list)  # узлы из левого меню


@dataclass
class NodeInfo:
    """То, что кэшируется на неделю для подкатегории (таблица best_seller.node_cache)."""

    node: str
    dept: str
    name: str = ""
    leader_asin: Optional[str] = None
    leader_bsr: Optional[int] = None
    leader_badge: Optional[bool] = None
    item_count: Optional[int] = None
    top: List[dict] = field(default_factory=list)  # [{"rank", "asin", "title"}]
    linked: List[Subcategory] = field(default_factory=list)
    fetched_at: Optional[datetime] = None


class NodeCache(Protocol):
    def get(self, market: str, node: str) -> Optional[NodeInfo]: ...

    def put(self, market: str, info: NodeInfo) -> None: ...


class MemoryCache:
    """Кэш в памяти (тесты и разовый запуск); на бою — таблица best_seller.node_cache."""

    def __init__(self) -> None:
        self.items: Dict[Tuple[str, str], NodeInfo] = {}

    def get(self, market: str, node: str) -> Optional[NodeInfo]:
        return self.items.get((market, node))

    def put(self, market: str, info: NodeInfo) -> None:
        self.items[(market, info.node)] = info


@dataclass
class Candidate:
    sub: Subcategory
    info: Optional[NodeInfo]
    verdict: best_seller.Verdict
    relevance: Optional[str] = None


@dataclass
class SearchResult:
    asin: str
    market: str
    status: str  # done / not_needed / error
    title: str = ""
    our_bsr: Optional[int] = None
    target_bsr: Optional[int] = None
    own: List[Subcategory] = field(default_factory=list)
    candidates: List[Candidate] = field(default_factory=list)
    requests: int = 0  # сколько запросов к ScrapingDog ушло (≈ кредитов)
    error: str = ""


# --- разбор ответов ------------------------------------------------------------------------------------------

_NODE_LINK = re.compile(r"/(?:gp/bestsellers|zgbs)/([a-z0-9-]+)/(\d+)")
_SKIP_SUBCATEGORY_NAMES = ("see top 100",)


def own_subcategories(product: dict) -> Tuple[Optional[int], List[Subcategory]]:
    """(BSR в главной категории, подкатегории товара с местом) из best_sellers_rank Product API."""
    main_bsr: Optional[int] = None
    subs: List[Subcategory] = []
    for item in product.get("best_sellers_rank") or []:
        if not isinstance(item, dict):
            continue
        rank = clean_number(item.get("rank"))
        match = _NODE_LINK.search(str(item.get("link") or ""))
        if match is None:
            if main_bsr is None and rank:
                main_bsr = int(rank)
            continue
        subs.append(Subcategory(match.group(2), match.group(1), str(item.get("category") or "").strip(),
                                int(rank) if rank else None))
    return main_bsr, subs


def parse_bestsellers(page: str) -> BestsellersPage:
    result = BestsellersPage()
    recs = re.search(r'data-client-recs-list="([^"]+)"', page)
    if recs:
        try:
            for item in json.loads(html_lib.unescape(recs.group(1))):
                rank = (item.get("metadataMap") or {}).get("render.zg.rank")
                if str(rank).isdigit() and item.get("id"):
                    result.ranked.append((int(rank), str(item["id"])))
        except (ValueError, TypeError, AttributeError):
            pass
    for asin, title in re.findall(
            r'data-asin="(B0[A-Z0-9]{8})".*?(?:_cDEzb_p13n-sc-css-line-clamp-\d_\w+">|p13n-sc-truncate[^>]*>)'
            r"([^<]{3,300})<", page, re.S):
        result.titles.setdefault(asin, html_lib.unescape(title).strip())
    title = re.search(r"<title>(.*?)</title>", page, re.S)
    if title:
        name = html_lib.unescape(title.group(1)).strip()
        result.name = re.sub(r"^.*?(?:Best Sellers|Bestseller|meilleures ventes|più venduti|más vendidos)\s*:\s*"
                             r"(?:Best\s+|Die besten\s+|Les meilleurs\s+|I più venduti\s+|Los más vendidos\s+)?",
                             "", name, flags=re.I).strip()
    seen = set()
    # Соседние подкатегории — ссылки меню вниз/вбок (ref=zg_bs_nav_…); родительские разделы (ref=zg_bs_unv_…,
    # «‹ Women», «Hats & Caps») слишком общие — в кандидаты не берутся.
    for dept, node, ref, label in re.findall(
            r'href="[^"]*/zgbs/([a-z0-9-]+)/(\d+)(/ref=[^"]*)?"[^>]*>\s*([^<]{2,120}?)\s*<', page):
        if "zg_bs_unv" in ref or not html_lib.unescape(label).strip():
            continue
        if node not in seen:
            seen.add(node)
            result.linked.append(Subcategory(node, dept, html_lib.unescape(label).strip()))
    return result


_BSR_LABELS = ("best sellers rank", "bestseller-rang", "classement des meilleures ventes",
               "classifica bestseller", "clasificación en los más vendidos")
_BADGE_NUMBER_ONE = re.compile(r"(?:#|nr\.|n°|nº|n\.º|n\.)\s*1\b", re.I)
_NOT_BEST_SELLER = re.compile(r"new release|neuerscheinung|nouveaut|novit|novedad", re.I)


def _text(fragment: str) -> str:
    fragment = re.sub(r"<script.*?</script>|<style.*?</style>", " ", fragment, flags=re.S | re.I)
    return re.sub(r"\s+", " ", html_lib.unescape(re.sub(r"<[^>]+>", " ", fragment))).strip()


def parse_leader(page: str) -> Tuple[Optional[int], Optional[bool]]:
    """(BSR в главной категории, живой бейдж «#1 Best Seller») со страницы товара.

    Бейдж — только блок zeitgeistBadge самого товара: надпись «#1 Best Seller» в других местах страницы относится
    к товарам из блоков похожих. «#1 New Release» — значок новинки, не Best Seller. Блока нет — None (не знаем)."""
    text_lower = _text(page).lower()
    bsr: Optional[int] = None
    for label in _BSR_LABELS:
        at = text_lower.find(label)
        if at >= 0:
            number = re.search(r"(\d[\d.,\s]*\d|\d)", text_lower[at + len(label): at + len(label) + 60])
            if number:
                bsr = int(re.sub(r"[.,\s]", "", number.group(1)))
            break
    at = page.find('id="zeitgeistBadge_feature_div"')
    if at < 0:
        return bsr, None
    start = page.find(">", at) + 1
    block = _text(re.split(r'<div id="(?!zeitgeist)', page[start:start + 4000], maxsplit=1)[0])
    badge = bool(block) and bool(_BADGE_NUMBER_ONE.search(block)) and not _NOT_BEST_SELLER.search(block)
    return bsr, badge


def bestsellers_url(market: str, sub: Subcategory, page: int = 1) -> str:
    domain = pairs_store.DOMAIN_BY_MARKET[market]
    url = f"https://www.amazon.{domain}/gp/bestsellers/{sub.dept}/{sub.node}"
    return url if page == 1 else f"{url}?pg={page}"


def product_url(market: str, asin: str) -> str:
    return f"https://www.amazon.{pairs_store.DOMAIN_BY_MARKET[market]}/dp/{asin}"


# --- поиск ---------------------------------------------------------------------------------------------------

def _fresh(info: Optional[NodeInfo], now: datetime) -> bool:
    return info is not None and info.fetched_at is not None and now - info.fetched_at < timedelta(days=CACHE_DAYS)


def search(asin: str, market: str, *, fetch_product: ProductFetch, fetch_page: PageFetch, cache: NodeCache,
           target_bsr: Optional[int] = None, relevance: Optional[RelevanceFn] = None,
           now: Optional[datetime] = None, max_candidates: int = MAX_CANDIDATES) -> SearchResult:
    """Подкатегории, где ASIN может стать #1 Best Seller. target_bsr — наш целевой BSR (по умолчанию — текущий)."""
    now = now or datetime.now(timezone.utc)
    result = SearchResult(asin=asin, market=market, status="error")
    if market not in pairs_store.DOMAIN_BY_MARKET:
        result.error = f"Неизвестная страна: {market}"
        return result
    domain = pairs_store.DOMAIN_BY_MARKET[market]
    product = fetch_product(asin, domain)
    result.requests += 1
    if not product:
        result.error = "ScrapingDog не вернул карточку товара"
        return result
    result.title = str(product.get("title") or "").strip()
    result.our_bsr, result.own = own_subcategories(product)
    if not best_seller.search_needed(sub.rank for sub in result.own):
        result.status = "not_needed"
        return result
    result.target_bsr = target_bsr or result.our_bsr
    if not result.target_bsr:
        result.error = "У товара нет BSR — не с чем сравнивать лидеров"
        return result
    parents = {str(c.get("node")) for c in product.get("categories") or [] if isinstance(c, dict)}

    queue: List[Subcategory] = list(result.own)
    seen = {sub.node for sub in queue}
    index = 0
    while index < len(queue) and len(result.candidates) < max_candidates:
        sub = queue[index]
        index += 1
        info, used = _check_node(market, sub, fetch_page, cache, result.target_bsr, now)
        result.requests += used
        for linked in (info.linked if info else []):
            if linked.node not in seen and linked.node not in parents:
                seen.add(linked.node)
                queue.append(linked)
        result.candidates.append(_decide(sub, info, result.target_bsr, result.title, relevance))
    order = best_seller.ranked([c.verdict for c in result.candidates])
    result.candidates.sort(key=lambda c: next(i for i, v in enumerate(order) if v is c.verdict))
    result.status = "done"
    return result


def _check_node(market: str, sub: Subcategory, fetch_page: PageFetch, cache: NodeCache, target_bsr: int,
                now: datetime) -> Tuple[Optional[NodeInfo], int]:
    """Данные подкатегории: из кэша, если свежие, иначе — запросы по порядку с ранней остановкой по К1."""
    used = 0
    info = cache.get(market, sub.node)
    if not _fresh(info, now):
        page = fetch_page(bestsellers_url(market, sub))
        used += 1
        if not page:
            return None, used
        parsed = parse_bestsellers(page)
        info = NodeInfo(node=sub.node, dept=sub.dept, name=parsed.name or sub.name, linked=parsed.linked,
                        top=[{"rank": r, "asin": a, "title": parsed.titles.get(a, "")} for r, a in parsed.ranked],
                        fetched_at=now)
        info.leader_asin = next((a for r, a in parsed.ranked if r == 1), None)
        if info.leader_asin:
            leader = fetch_page(product_url(market, info.leader_asin))
            used += 1
            if leader:
                info.leader_bsr, info.leader_badge = parse_leader(leader)
        cache.put(market, info)
    k1 = best_seller.evaluate(target_bsr, info.leader_bsr).k1
    if k1 and info.item_count is None:
        page = fetch_page(bestsellers_url(market, sub, 2))
        used += 1
        if page:
            parsed = parse_bestsellers(page)
            known = {row["asin"] for row in info.top}
            info.top += [{"rank": r, "asin": a, "title": parsed.titles.get(a, "")} for r, a in parsed.ranked
                         if a not in known]
            info.item_count = min(len({row["rank"] for row in info.top}), TOP_SIZE)
            cache.put(market, info)
    return info, used


def _decide(sub: Subcategory, info: Optional[NodeInfo], target_bsr: int, our_title: str,
            relevance: Optional[RelevanceFn]) -> Candidate:
    if info is None:
        verdict = best_seller.Verdict(None, None, None, None, best_seller.PENDING, None, None,
                                      "страница подкатегории не получена")
        return Candidate(sub, None, verdict)
    rel = None
    first = best_seller.evaluate(target_bsr, info.leader_bsr, info.item_count)
    if first.k2 and relevance is not None:  # К3 стоит денег (ИИ) — только если К1 и К2 пройдены
        rel = relevance(our_title, [row["title"] for row in info.top if row.get("title")])
    verdict = best_seller.evaluate(target_bsr, info.leader_bsr, info.item_count, rel, info.leader_badge)
    return Candidate(Subcategory(sub.node, sub.dept, info.name or sub.name, sub.rank), info, verdict, rel)
