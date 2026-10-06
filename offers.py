"""Продавцы на карточках наших товаров (ScrapingDog Amazon Offers API): кто продаёт и у кого Buy Box.

Зачем: увидеть чужого продавца на нашей карточке (хайджекер) и потерю Buy Box. Один запрос на ASIN =
1 кредит ScrapingDog, как у Product API. «Наш» продавец задаётся секретами OUR_SELLER_IDS (id продавца
Amazon, вида A2NEM58BFPMEIL) и/или OUR_SELLER_NAMES (название магазина) — через запятую. Пока ни один не
задан, продавцы записываются и показываются, но «чужой / наш» не определяется и тревога не уходит.

Без Streamlit: модуль используют и сбор (check_offers.py), и дашборд.
"""

from __future__ import annotations

import html
import logging
import re
from dataclasses import dataclass, field
from typing import Iterable, List, Mapping, Optional, Sequence

import requests

import dbutil
from dbutil import Connect

log = logging.getLogger(__name__)

OFFERS_URL = "https://api.scrapingdog.com/amazon/offers"
_TEXT_MAX = 200


class OffersStoreError(RuntimeError):
    """Таблица продавцов недоступна (без деталей драйвера)."""


@dataclass(frozen=True)
class Offer:
    seller_id: str
    seller_name: str
    price: Optional[float]
    currency: str
    is_new: Optional[bool]
    fba: Optional[bool]
    buybox: bool


@dataclass
class Check:
    """Итог проверки одного нашего ASIN. offers пустой — на карточке нет ни одного предложения."""
    marketplace: str
    asin: str
    product: str
    offers: List[Offer]
    ours: List[bool] = field(default_factory=list)  # по offers: True — наш продавец; пусто — «наш» не задан

    @property
    def knows_ours(self) -> bool:
        return bool(self.ours)

    @property
    def foreign(self) -> List[Offer]:
        return [offer for offer, mine in zip(self.offers, self.ours) if not mine]

    @property
    def buybox(self) -> Optional[Offer]:
        return next((offer for offer in self.offers if offer.buybox), None)

    @property
    def buybox_lost(self) -> bool:
        """Buy Box есть, и он не у нас. Без знания «нашего» — False: судить не по чему."""
        if not self.knows_ours:
            return False
        return any(offer.buybox and not mine for offer, mine in zip(self.offers, self.ours))


def _text(value: object) -> str:
    return " ".join(str(value).split())[:_TEXT_MAX] if isinstance(value, (str, int, float)) else ""


def _number(value: object) -> Optional[float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _flag(value: object) -> Optional[bool]:
    return value if isinstance(value, bool) else None


def parse_offers(data: object) -> List[Offer]:
    """Предложения из ответа Offers API. Кривые элементы пропускаются, а не роняют разбор."""
    items = data.get("offers") if isinstance(data, dict) else None
    result: List[Offer] = []
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict):
            continue
        price = item.get("price") if isinstance(item.get("price"), dict) else {}
        seller = item.get("seller") if isinstance(item.get("seller"), dict) else {}
        condition = item.get("condition") if isinstance(item.get("condition"), dict) else {}
        delivery = item.get("delivery") if isinstance(item.get("delivery"), dict) else {}
        result.append(Offer(
            seller_id=_text(seller.get("id")),
            seller_name=_text(seller.get("name")),
            price=_number(price.get("value")),
            currency=_text(price.get("currency")),
            is_new=_flag(condition.get("is_new")),
            fba=_flag(delivery.get("fulfilled_by_amazon")),
            buybox=item.get("buybox_winner") is True,
        ))
    return result


def fetch_offers(asin: str, domain: str, country: str, token: str,
                 session: Optional[requests.Session] = None, timeout: float = 60) -> Optional[dict]:
    """Ровно один запрос (1 кредит). None — сеть, не-200 или битый JSON; токен в лог не попадает."""
    params = {"api_key": token, "asin": asin, "domain": domain, "country": country}
    try:
        response = (session or requests).get(OFFERS_URL, params=params, timeout=timeout)
    except requests.RequestException as exc:
        log.warning("[%s] Offers: ошибка сети — %s", asin, str(exc).replace(token, "***") if token else exc)
        return None
    if response.status_code != 200:
        log.warning("[%s] Offers: ответ %s — %s", asin, response.status_code, response.text[:200].replace(token, "***"))
        return None
    try:
        return response.json()
    except ValueError:
        log.warning("[%s] Offers: невалидный JSON.", asin)
        return None


def _split(text: object) -> List[str]:
    return [part.strip() for part in re.split(r"[,;\n]+", text) if part.strip()] if isinstance(text, str) else []


def _name_key(name: str) -> str:
    return " ".join(name.lower().split())


@dataclass(frozen=True)
class OurSellers:
    ids: frozenset
    names: frozenset

    @classmethod
    def from_environment(cls, environment: Mapping[str, str]) -> "OurSellers":
        return cls(
            ids=frozenset(part.upper() for part in _split(environment.get("OUR_SELLER_IDS"))),
            names=frozenset(_name_key(part) for part in _split(environment.get("OUR_SELLER_NAMES"))),
        )

    @property
    def known(self) -> bool:
        return bool(self.ids or self.names)

    def is_ours(self, offer: Offer) -> bool:
        return (bool(offer.seller_id) and offer.seller_id.upper() in self.ids) or \
            (bool(offer.seller_name) and _name_key(offer.seller_name) in self.names)


def make_check(marketplace: str, asin: str, product: str, offers: List[Offer], sellers: OurSellers) -> Check:
    ours = [sellers.is_ours(offer) for offer in offers] if sellers.known else []
    return Check(marketplace, asin, product, offers, ours)


def _amazon_link(asin: str, domain: str) -> str:
    return f"https://www.amazon.{domain}/dp/{asin}"


def _price(offer: Offer) -> str:
    if offer.price is None:
        return "цена ?"
    return f"{offer.price:.2f} {offer.currency}".strip()


def alert_lines(checks: Iterable[Check], domains: Mapping[str, str]) -> List[str]:
    """Строки тревоги для Telegram (HTML): только ASIN с чужими продавцами или потерянным Buy Box."""
    lines: List[str] = []
    for check in checks:
        foreign = check.foreign
        if not foreign and not check.buybox_lost:
            continue
        domain = domains.get(check.marketplace, "com")
        title = html.escape(check.product or check.asin)
        lines.append(f'<b>{html.escape(check.marketplace)}</b> · <a href="{_amazon_link(check.asin, domain)}">'
                     f"{html.escape(check.asin)}</a> · {title}")
        if check.buybox_lost and check.buybox is not None:
            lines.append(f"   ⚠️ Buy Box у «{html.escape(check.buybox.seller_name or '?')}» — {_price(check.buybox)}")
        for offer in foreign:
            fulfil = " · FBA" if offer.fba else ""
            lines.append(f"   🚨 {html.escape(offer.seller_name or offer.seller_id or 'без имени')} — {_price(offer)}{fulfil}")
    return lines


# --- База: bsr_radar.offer_snapshots (миграция 016) ---

def _run(connect: Connect, sql: str, params: tuple = (), *, fetch: bool = False):
    return dbutil.run_sql(connect, sql, params, fetch=fetch, error=OffersStoreError, what="Операция с таблицей продавцов")


def table_exists(connect: Connect) -> bool:
    return bool(_run(connect, "SELECT to_regclass('bsr_radar.offer_snapshots') IS NOT NULL;", fetch=True)[0][0])


def save_checks(connect: Connect, checks: Sequence[Check]) -> int:
    """Одна транзакция на весь прогон. ASIN без предложений — строка с пустым продавцом: «проверено, пусто»."""
    statements = []
    sql = ("INSERT INTO bsr_radar.offer_snapshots (marketplace, asin, product, seller_id, seller_name, price, "
           "currency, is_new, fba, buybox, ours) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s);")
    for check in checks:
        if not check.offers:
            statements.append((sql, (check.marketplace, check.asin, check.product, None, None, None, None,
                                      None, None, False, None), False))
        for index, offer in enumerate(check.offers):
            mine = check.ours[index] if check.knows_ours else None
            statements.append((sql, (check.marketplace, check.asin, check.product, offer.seller_id or None,
                                      offer.seller_name or None, offer.price, offer.currency or None,
                                      offer.is_new, offer.fba, offer.buybox, mine), False))
    if statements:
        dbutil.run_many(connect, statements, error=OffersStoreError, what="Запись продавцов")
    return len(statements)


def latest(connect: Connect) -> List[dict]:
    """Строки последней проверки каждого ASIN (по стране) — для дашборда."""
    rows = _run(
        connect,
        """
        WITH last AS (
            SELECT marketplace, asin, max(checked_at) AS checked_at
            FROM bsr_radar.offer_snapshots GROUP BY marketplace, asin
        )
        SELECT s.marketplace, s.asin, s.product, s.seller_id, s.seller_name, s.price, s.currency,
               s.is_new, s.fba, s.buybox, s.ours, s.checked_at
        FROM bsr_radar.offer_snapshots s
        JOIN last USING (marketplace, asin, checked_at)
        ORDER BY s.marketplace, s.asin, s.buybox DESC, s.price NULLS LAST;
        """,
        fetch=True,
    )
    keys = ("marketplace", "asin", "product", "seller_id", "seller_name", "price", "currency",
            "is_new", "fba", "buybox", "ours", "checked_at")
    return [dict(zip(keys, row)) for row in rows]
