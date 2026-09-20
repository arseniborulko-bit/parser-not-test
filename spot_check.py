"""Точечная проверка ASIN: один запрос ScrapingDog на ASIN, результат только на экране (в базу и таблицу не пишется).

Каждый ASIN стоит кредит, а дашборд открыт всем, у кого есть ссылка, поэтому расход ограничен: не больше
MAX_PER_CHECK за раз и HOUR_LIMIT в час / DAY_LIMIT в сутки на весь сайт. Без токена ScrapingDog в секретах
проверка выключена.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Tuple

import pairs_store

MAX_PER_CHECK = 5
HOUR_LIMIT = 20
DAY_LIMIT = 60


class SpotBudgetError(RuntimeError):
    """Лимит точечных проверок исчерпан."""


class SpotBudget:
    """Скользящие окна «в час» и «в сутки» на весь процесс: перезапуск сайта их обнуляет, но открытая ссылка
    не позволяет сжечь больше DAY_LIMIT кредитов за сутки."""

    def __init__(self, hour: int = HOUR_LIMIT, day: int = DAY_LIMIT, clock: Callable[[], float] = time.monotonic):
        self._limits = ((3600.0, hour), (86400.0, day))
        self._clock = clock
        self._events: deque = deque()
        self._lock = threading.Lock()

    def _prune(self) -> None:
        cutoff = self._clock() - self._limits[-1][0]
        while self._events and self._events[0] <= cutoff:
            self._events.popleft()

    def _used(self, window: float) -> int:
        cutoff = self._clock() - window
        return sum(1 for moment in self._events if moment > cutoff)

    def remaining(self) -> int:
        with self._lock:
            self._prune()
            return max(0, min(limit - self._used(window) for window, limit in self._limits))

    def consume(self, count: int) -> None:
        """Занимает count проверок или поднимает SpotBudgetError, ничего не занимая."""
        with self._lock:
            self._prune()
            for window, limit in self._limits:
                if self._used(window) + count > limit:
                    label = "в час" if window == 3600.0 else "в сутки"
                    raise SpotBudgetError(f"Лимит точечных проверок исчерпан: не больше {limit} ASIN {label} на весь сайт (каждый стоит кредит).")
            now = self._clock()
            self._events.extend([now] * count)


@dataclass
class SpotPlan:
    items: List[Tuple[str, str]] = field(default_factory=list)
    invalid: List[str] = field(default_factory=list)
    repeats: int = 0
    errors: List[str] = field(default_factory=list)


def plan_spot_check(text: object, default_market: str) -> SpotPlan:
    plan = SpotPlan()
    batch = pairs_store.parse_asin_batch(text)
    plan.invalid, plan.repeats = list(batch.invalid), batch.repeats
    if default_market not in pairs_store.DOMAIN_BY_MARKET:
        plan.errors.append("Выберите маркетплейс.")
        return plan
    if not batch.items:
        plan.errors.append("Вставьте ASIN или ссылки на товары.")
        return plan
    if len(batch.items) > MAX_PER_CHECK:
        plan.errors.append(f"За один раз можно проверить не больше {MAX_PER_CHECK} ASIN.")
        return plan
    plan.items = [(item.asin, item.market or default_market) for item in batch.items]
    return plan


def _load_scraping():
    import scraping  # тянет config (load_dotenv) и requests — только когда проверка реально запускается

    return scraping


def _row(asin: str, market: str, fetch, parse, token: str) -> Dict[str, object]:
    row: Dict[str, object] = {"ASIN": asin, "Страна": market, "Статус": "не получено"}
    try:
        data = fetch(asin, domain=pairs_store.DOMAIN_BY_MARKET[market], token=token)
    except Exception:  # noqa: BLE001 — токен и детали запроса в сообщения не выносим
        row["Статус"] = "ошибка запроса"
        return row
    if not data:
        return row
    try:
        product = parse(data, asin)
    except Exception:  # noqa: BLE001
        row["Статус"] = "не удалось разобрать ответ"
        return row
    row.update({
        "Статус": "ок", "Название": product.title, "Цена": product.price, "BSR": product.bsr,
        "Наличие": product.stock_status, "Оценка": product.stars, "Отзывы": product.reviews,
        "Категория": product.category, "Бренд": product.brand,
    })
    return row


def run_spot_check(plan: SpotPlan, token: Optional[str], budget: SpotBudget, *,
                   loader: Optional[Callable] = None) -> List[Dict[str, object]]:
    if plan.errors or not plan.items:
        raise ValueError(plan.errors[0] if plan.errors else "Нечего проверять.")
    if not token:
        raise ValueError("Точечная проверка выключена: не задан токен ScrapingDog.")
    budget.consume(len(plan.items))
    scraping = (loader or _load_scraping)()
    with ThreadPoolExecutor(max_workers=min(MAX_PER_CHECK, len(plan.items))) as pool:
        return list(pool.map(
            lambda item: _row(item[0], item[1], scraping.fetch_product, scraping.parse_product, token), plan.items,
        ))
