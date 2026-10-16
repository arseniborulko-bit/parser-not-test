"""Движок поиска подкатегорий Best Seller: разбор ответов ScrapingDog и порядок проверок с ранней остановкой.
Без сети: страницы и карточки — короткие копии настоящей разметки (разведка 16.10.2026)."""

import json
from datetime import datetime, timedelta, timezone

import best_seller
import best_seller_search as s

NOW = datetime(2026, 10, 16, 12, 0, tzinfo=timezone.utc)
OURS = "B0H9LK9T39"


def bs_page(name, items, siblings=(), parents=()):
    """Страница Best Sellers: места в data-client-recs-list, заголовки карточек, меню (родители и соседи)."""
    recs = json.dumps([{"id": asin, "metadataMap": {"render.zg.rank": str(rank)}} for rank, asin in items])
    cards = "".join(f'<div id="gridItemRoot"><div data-asin="{asin}"><div class="_cDEzb_p13n-sc-css-line-clamp-3_g3dy1">'
                    f"Title of {asin}</div></div></div>" for _, asin in items)
    nav = "".join(f'<a href="/Best/zgbs/fashion/{n}/ref=zg_bs_unv_fashion_2_1">{label}</a>' for n, label in parents)
    nav += "".join(f'<a href="/Best/zgbs/fashion/{n}/ref=zg_bs_nav_fashion_4_1">{label}</a>' for n, label in siblings)
    return (f"<html><title>Amazon.com Best Sellers: Best {name}</title><body>{nav}"
            f'<div data-client-recs-list="{recs.replace(chr(34), "&quot;")}"></div>{cards}</body></html>')


def leader_page(bsr, badge_text=""):
    return (f'<div id="zeitgeistBadge_feature_div" class="celwidget"><span>{badge_text}</span></div>'
            f'<div id="unifiedPrice_feature_div"></div><table><tr><th>Best Sellers Rank</th>'
            f"<td>#{bsr:,} in Clothing, Shoes &amp; Jewelry #1 in Sub</td></tr></table>")


PRODUCT = {
    "title": "Merino Sun Hat",
    "best_sellers_rank": [
        {"rank": 90000, "category": "Clothing, Shoes & Jewelry", "link": "https://www.amazon.com/gp/bestsellers/fashion/ref=x"},
        {"rank": 8, "category": "Men's Novelty Sun Hats", "link": "https://www.amazon.com/gp/bestsellers/fashion/9057034011/ref=x"},
        {"rank": 12, "category": "Women's Novelty Sun Hats", "link": "https://www.amazon.com/gp/bestsellers/fashion/9056981011/ref=x"},
    ],
    "categories": [{"node": "7141123011"}, {"node": "9056985011"}],
}


class Fake:
    def __init__(self, pages, product=PRODUCT):
        self.pages, self.product, self.urls = pages, product, []

    def fetch_product(self, asin, domain):
        self.urls.append(f"product {asin} {domain}")
        return self.product

    def fetch_page(self, url):
        self.urls.append(url)
        return self.pages.get(url)


def top(prefix, n, start=1):
    return [(r, f"B0{prefix}{r:06d}") for r in range(start, start + n)]


URL = "https://www.amazon.com/gp/bestsellers/fashion/"


def test_parse_bestsellers_reads_ranks_titles_and_only_sibling_links():
    page = s.parse_bestsellers(bs_page("Men's Novelty Sun Hats", top("AA", 50), siblings=[("111", "Visors")],
                                       parents=[("7147440011", "Women")]))
    assert page.name == "Men's Novelty Sun Hats" and len(page.ranked) == 50 and page.ranked[0] == (1, "B0AA000001")
    assert page.titles["B0AA000001"] == "Title of B0AA000001"
    assert [(x.node, x.name) for x in page.linked] == [("111", "Visors")]  # родитель «Women» не кандидат


def test_parse_leader_badge_only_from_its_own_block():
    assert s.parse_leader(leader_page(994, "#1 Best Seller in Women's Skullies")) == (994, True)
    assert s.parse_leader(leader_page(994, "#1 New Release in Women's Novelty Sun Hats")) == (994, False)
    assert s.parse_leader(leader_page(994, "")) == (994, False)
    assert s.parse_leader('<p>#1 Best Seller</p><table><tr><th>Best Sellers Rank</th><td>#5 in X</td></tr></table>') \
        == (5, None)  # блока бейджа нет — не знаем, а не «нет»
    assert s.parse_leader("Amazon Bestseller-Rang: Nr. 1.531 in Fashion")[0] == 1531


def test_own_subcategories_from_product_api():
    main, subs = s.own_subcategories(PRODUCT)
    assert main == 90000
    assert [(x.node, x.dept, x.rank) for x in subs] == [("9057034011", "fashion", 8), ("9056981011", "fashion", 12)]


def test_already_in_a_top_three_needs_no_search_and_costs_one_request():
    product = {**PRODUCT, "best_sellers_rank": PRODUCT["best_sellers_rank"][:1] + [
        {"rank": 3, "category": "X", "link": "https://www.amazon.com/gp/bestsellers/fashion/1/ref=x"}]}
    fake = Fake({}, product)
    result = s.search(OURS, "US", fetch_product=fake.fetch_product, fetch_page=fake.fetch_page, cache=s.MemoryCache())
    assert result.status == "not_needed" and result.requests == 1 and fake.urls == [f"product {OURS} com"]


def test_full_search_with_early_stop_on_k1_and_relevance_only_when_needed():
    weak, strong = "9057034011", "9056981011"
    pages = {
        URL + weak: bs_page("Men's Novelty Sun Hats", top("AA", 50), siblings=[("222", "Visors")]),
        URL + weak + "?pg=2": bs_page("Men's Novelty Sun Hats", top("AA", 50, 51)),
        "https://www.amazon.com/dp/B0AA000001": leader_page(460779, "#1 Best Seller in Men's Novelty Sun Hats"),
        URL + strong: bs_page("Women's Novelty Sun Hats", top("BB", 50)),
        "https://www.amazon.com/dp/B0BB000001": leader_page(358, "#1 Best Seller in X"),
        URL + "222": bs_page("Visors", top("CC", 50)),
        "https://www.amazon.com/dp/B0CC000001": leader_page(35598),
    }
    fake, asked = Fake(pages), []

    def relevance(our_title, titles):
        asked.append((our_title, len(titles)))
        return best_seller.RELEVANT

    result = s.search(OURS, "US", fetch_product=fake.fetch_product, fetch_page=fake.fetch_page,
                      cache=s.MemoryCache(), target_bsr=60000, relevance=relevance, now=NOW)
    assert result.status == "done" and result.our_bsr == 90000 and result.target_bsr == 60000
    by_node = {c.sub.node: c for c in result.candidates}
    assert set(by_node) == {weak, strong, "222"}  # свои подкатегории + сосед из меню
    ok = by_node[weak].verdict
    assert (ok.k1, ok.k2, ok.k3, ok.k4, ok.status, ok.priority) == (True, True, True, True, best_seller.SUITABLE, best_seller.HIGH)
    assert by_node[weak].info.item_count == 100
    assert by_node[strong].verdict.reason == "отклонена по К1" and by_node["222"].verdict.reason == "отклонена по К1"
    assert result.candidates[0].sub.node == weak  # подходящие сверху
    # ?pg=2 запрошен только для прошедшей К1, ИИ спрошен только для неё; итого 1 + 3×2 + 1 = 8 запросов.
    assert [u for u in fake.urls if "pg=2" in u] == [URL + weak + "?pg=2"]
    assert asked == [("Merino Sun Hat", 100)]
    assert result.requests == 8


def test_fresh_cache_saves_requests_and_old_cache_is_refreshed():
    weak = "9057034011"
    pages = {URL + weak: bs_page("Men's Novelty Sun Hats", top("AA", 50)),
             "https://www.amazon.com/dp/B0AA000001": leader_page(30000)}
    product = {**PRODUCT, "best_sellers_rank": PRODUCT["best_sellers_rank"][:2]}
    cache = s.MemoryCache()
    kw = dict(cache=cache, target_bsr=60000, now=NOW)
    first = Fake(pages, product)
    s.search(OURS, "US", fetch_product=first.fetch_product, fetch_page=first.fetch_page, **kw)
    again = Fake(pages, product)
    result = s.search(OURS, "US", fetch_product=again.fetch_product, fetch_page=again.fetch_page, **kw)
    assert result.requests == 1 and again.urls == [f"product {OURS} com"]  # подкатегория из кэша
    later = Fake(pages, product)
    s.search(OURS, "US", fetch_product=later.fetch_product, fetch_page=later.fetch_page, cache=cache,
             target_bsr=60000, now=NOW + timedelta(days=s.CACHE_DAYS, minutes=1))
    assert URL + weak in later.urls  # неделя прошла — топ запрошен заново


def test_without_relevance_the_candidate_waits_for_k3_and_failures_are_reported():
    weak = "9057034011"
    pages = {URL + weak: bs_page("X", top("AA", 50)), URL + weak + "?pg=2": bs_page("X", top("AA", 50, 51)),
             "https://www.amazon.com/dp/B0AA000001": leader_page(460779, "#1 Best Seller in X")}
    product = {**PRODUCT, "best_sellers_rank": PRODUCT["best_sellers_rank"][:3]}
    fake = Fake(pages, product)
    result = s.search(OURS, "US", fetch_product=fake.fetch_product, fetch_page=fake.fetch_page,
                      cache=s.MemoryCache(), target_bsr=60000, now=NOW)
    by_node = {c.sub.node: c.verdict for c in result.candidates}
    assert by_node[weak].status == best_seller.PENDING and by_node[weak].reason == "нужна оценка релевантности"
    assert by_node["9056981011"].reason == "страница подкатегории не получена"
    failed = Fake({}, None)
    bad = s.search(OURS, "US", fetch_product=failed.fetch_product, fetch_page=failed.fetch_page, cache=s.MemoryCache())
    assert bad.status == "error" and bad.error
    assert s.search(OURS, "XX", fetch_product=fake.fetch_product, fetch_page=fake.fetch_page,
                    cache=s.MemoryCache()).status == "error"


def test_max_candidates_caps_spending():
    siblings = [(str(1000 + i), f"S{i}") for i in range(30)]
    pages = {URL + "9057034011": bs_page("X", top("AA", 50), siblings=siblings)}
    product = {**PRODUCT, "best_sellers_rank": PRODUCT["best_sellers_rank"][:2]}
    fake = Fake(pages, product)
    result = s.search(OURS, "US", fetch_product=fake.fetch_product, fetch_page=fake.fetch_page,
                      cache=s.MemoryCache(), target_bsr=60000, now=NOW, max_candidates=5)
    assert len(result.candidates) == 5


def test_a_dropped_request_is_retried():
    calls = []

    def flaky_product(asin, domain):
        calls.append(asin)
        return None if len(calls) < 3 else PRODUCT

    result = s.search(OURS, "US", fetch_product=flaky_product, fetch_page=lambda url: None,
                      cache=s.MemoryCache(), max_candidates=1)
    assert len(calls) == 3 and result.status == "done"
    calls.clear()
    assert s.search(OURS, "US", fetch_product=lambda a, d: calls.append(a), fetch_page=lambda url: None,
                    cache=s.MemoryCache(), attempts=2).status == "error"
    assert len(calls) == 2


def test_our_listing_can_be_read_from_its_product_page():
    page = ('<span id="productTitle"> Classic Straw Hat </span>'
            '<div id="wayfinding-breadcrumbs_feature_div"><ul><a href="/b?node=7141123011">C</a>'
            '<a href="/b?node=9056985011">M</a></ul></div>'
            "<table><tr><th>Best Sellers Rank</th><td>#859,857 in Clothing (See Top 100) <ul>"
            "<li>#8 in <a href='/gp/bestsellers/fashion/9057034011/ref=pd_zg_hrsr_fashion'>Men's Novelty Sun Hats</a></li>"
            "<li>#12 in <a href='/gp/bestsellers/fashion/9056981011/ref=pd_zg_hrsr_fashion'>Women's Novelty Sun Hats</a></li>"
            "</ul></td></tr></table>")
    product = s.product_from_page(page)
    assert product["title"] == "Classic Straw Hat" and product["categories"] == [{"node": "7141123011"}, {"node": "9056985011"}]
    main, subs = s.own_subcategories(product)
    assert main == 859857 and [(x.node, x.name, x.rank) for x in subs] == [
        ("9057034011", "Men's Novelty Sun Hats", 8), ("9056981011", "Women's Novelty Sun Hats", 12)]
    assert s.product_from_page(None) is None and s.product_from_page("<html></html>") is None
