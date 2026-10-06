"""Проверка продавцов на карточках наших товаров — шаг после ежедневного сбора (run_parser_and_sync.py).

Берёт наши ASIN из активных пар (bsr_radar.competitor_pairs), по каждому делает один запрос к ScrapingDog
Offers API (1 кредит; упавшие — ещё одна попытка), пишет итог в bsr_radar.offer_snapshots (если миграция
016 применена) и шлёт в Telegram тревогу, если на карточке чужой продавец или Buy Box не у нас.

Код выхода 0 — проверка прошла (даже если часть ASIN не ответила); 1 — проверить не удалось вообще.
Основной сбор от этого шага не зависит: он идёт после синка и его сбой данные дня не трогает.
"""

from __future__ import annotations

import logging
import os
import sys
from concurrent.futures import ThreadPoolExecutor
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from dotenv import load_dotenv

import offers

log = logging.getLogger("check_offers")

MAX_ATTEMPTS = 2
WORKERS = 5
ALERT_HEADER = "🛡 Продавцы на наших карточках"

Target = Tuple[str, str, str]  # (marketplace, our_asin, our_product)
Fetch = Callable[[str, str, str], Optional[dict]]  # (asin, domain, country) -> JSON или None


def our_targets(pairs: Sequence[dict]) -> List[Target]:
    """Наши ASIN из активных пар, по одному на (страна, ASIN), в порядке пар."""
    seen: Dict[Tuple[str, str], str] = {}
    for pair in pairs:
        key = (pair["marketplace"], pair["our_asin"])
        if pair["our_asin"] and key not in seen:
            seen[key] = pair.get("our_product") or ""
    return [(market, asin, product) for (market, asin), product in seen.items()]


def collect(targets: Sequence[Target], fetch: Fetch, domain_of: Callable[[str], str],
            country_of: Callable[[str], str], sellers: offers.OurSellers) -> Tuple[List[offers.Check], List[Target]]:
    """Проверки по всем целям и список тех, по кому ответа так и не было (после MAX_ATTEMPTS)."""
    results: Dict[Target, offers.Check] = {}
    pending = list(targets)
    for _ in range(MAX_ATTEMPTS):
        if not pending:
            break

        def task(target: Target):
            market, asin, _ = target
            domain = domain_of(market)
            return target, fetch(asin, domain, country_of(domain))

        failed: List[Target] = []
        with ThreadPoolExecutor(max_workers=min(WORKERS, len(pending))) as pool:
            for target, data in pool.map(task, pending):
                if data is None:
                    failed.append(target)
                    continue
                market, asin, product = target
                results[target] = offers.make_check(market, asin, product, offers.parse_offers(data), sellers)
        pending = failed
    return [results[target] for target in targets if target in results], pending


def _recipients() -> List[str]:
    from db_subscribers import get_active_subscriber_ids_from_db

    recipients: List[str] = []
    try:
        recipients = get_active_subscriber_ids_from_db()
    except Exception as exc:
        log.warning("Подписчики Telegram недоступны: %s", type(exc).__name__)
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if chat_id and chat_id not in recipients:
        recipients.append(chat_id)
    return recipients


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    import psycopg2

    from config import get_scrapingdog_token
    from db_pairs import load_active_competitor_pairs_from_db
    from scraping import DOMAIN_TO_COUNTRY, _get_http_session
    from sheets import _amazon_domain
    from telegram import broadcast_telegram_report

    sellers = offers.OurSellers.from_environment(os.environ)
    if not sellers.known:
        log.warning("OUR_SELLER_IDS / OUR_SELLER_NAMES не заданы: продавцы запишутся, но чужих не отличить — тревоги не будет.")
    try:
        targets = our_targets(load_active_competitor_pairs_from_db())
    except Exception as exc:
        log.error("Не удалось прочитать пары из базы: %s", type(exc).__name__)
        return 1
    if not targets:
        log.info("Наших ASIN в активных парах нет — проверять нечего.")
        return 0

    token = get_scrapingdog_token()
    session = _get_http_session()
    log.info("Проверка продавцов: %s наших ASIN (до %s кредитов ScrapingDog).", len(targets), len(targets) * MAX_ATTEMPTS)
    checks, missing = collect(
        targets,
        lambda asin, domain, country: offers.fetch_offers(asin, domain, country, token, session=session),
        _amazon_domain,
        lambda domain: DOMAIN_TO_COUNTRY.get(domain, "us"),
        sellers,
    )
    log.info("Проверено %s, без ответа %s: %s", len(checks), len(missing), ", ".join(asin for _, asin, _ in missing))
    if not checks:
        return 1

    connect = lambda: psycopg2.connect(os.environ["DATABASE_URL"])  # noqa: E731
    try:
        if offers.table_exists(connect):
            log.info("Записано строк продавцов: %s", offers.save_checks(connect, checks))
        else:
            log.info("Таблицы bsr_radar.offer_snapshots нет (миграция 016) — результат не записан.")
    except offers.OffersStoreError as exc:
        log.warning("%s", exc)

    lines = offers.alert_lines(checks, {market: _amazon_domain(market) for market, _, _ in targets})
    token_bot = os.environ.get("TELEGRAM_BOT_TOKEN")
    if lines and token_bot:
        recipients = _recipients()
        sent = broadcast_telegram_report(lines, recipients, header=ALERT_HEADER, token=token_bot)
        log.info("Тревога о продавцах: %s строк, отправлено частей: %s", len(lines), sent)
    else:
        log.info("Чужих продавцов и потерянного Buy Box нет (или Telegram не настроен).")
    return 0


if __name__ == "__main__":
    load_dotenv()
    sys.exit(main())
