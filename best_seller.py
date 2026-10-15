"""Подбор подкатегорий Best Seller по SOP-AMZ-001: правила К1–К4 и приоритет. Без Streamlit, сети и базы.

Подкатегория подходит, если наш листинг может стать в ней #1 Best Seller при своём целевом BSR:
  К1 — лидер подкатегории слабее нашей цели: BSR лидера не меньше K1_MIN_LEADER_BSR и больше целевого BSR;
  К2 — в подкатегории K2_REQUIRED_ITEMS товаров (полный топ Best Sellers);
  К3 — релевантность нашего товара подкатегории (оценка по заголовкам топа, четыре класса из SOP);
  К4 — у лидера есть живой бейдж «#1 Best Seller».
Критерии проверяются по порядку, и если К1 не пройден, остальные не проверяются: для К1 достаточно BSR
лидера, а К2–К4 стоят лишних запросов. Поэтому непроверенный критерий — None, а не False.

Числа порогов — из SOP и пока не утверждены заказчиком (открытые вопросы 3 и 4 в ТЗ), поэтому вынесены в константы.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, List, Optional

K1_MIN_LEADER_BSR = 50_000
K2_REQUIRED_ITEMS = 100
# Шаг 2 SOP: если ASIN уже в топ-3 своей подкатегории, подбирать новую не нужно.
ALREADY_TOP_RANK = 3

RELEVANT = "relevant"
SEMI_RELEVANT = "semi"
RISKY = "risky"
IRRELEVANT = "irrelevant"
RELEVANCE_LABELS = {
    RELEVANT: "релевантная",
    SEMI_RELEVANT: "полурелевантная",
    RISKY: "рискованная",
    IRRELEVANT: "нерелевантная",
}

# Приоритет по SOP: релевантная — брать; полурелевантная — брать с низким приоритетом;
# рискованная — записать, но искать дальше; нерелевантная — отклонить.
HIGH = "высокий"
LOW = "низкий"
RISK = "риск, искать дальше"
_PRIORITY_BY_RELEVANCE = {RELEVANT: HIGH, SEMI_RELEVANT: LOW, RISKY: RISK}
_PRIORITY_ORDER = {HIGH: 0, LOW: 1, RISK: 2}

PENDING = "pending"
SUITABLE = "suitable"
REJECTED = "rejected"


@dataclass(frozen=True)
class Verdict:
    k1: Optional[bool]
    k2: Optional[bool]
    k3: Optional[bool]
    k4: Optional[bool]
    status: str  # PENDING — не хватает данных для следующего критерия; SUITABLE; REJECTED
    priority: Optional[str]
    leader_to_target: Optional[float]  # BSR лидера / целевой BSR: чем больше, тем слабее лидер относительно нас
    reason: str


def search_needed(own_ranks: Iterable[Optional[int]]) -> bool:
    """False, если ASIN уже в топ-3 хотя бы одной своей подкатегории (шаг 2 SOP): поиск не запускается."""
    return not any(rank is not None and 0 < rank <= ALREADY_TOP_RANK for rank in own_ranks)


def evaluate(
    target_bsr: int,
    leader_bsr: Optional[int],
    item_count: Optional[int] = None,
    relevance: Optional[str] = None,
    leader_badge: Optional[bool] = None,
) -> Verdict:
    """Решение по одной подкатегории из того, что уже известно. Недостающие данные следующего критерия дают
    PENDING с подсказкой, что получить; провал любого критерия — REJECTED, и следующие уже не проверяются."""
    if target_bsr <= 0:
        raise ValueError("Целевой BSR должен быть положительным")
    if relevance is not None and relevance not in RELEVANCE_LABELS:
        raise ValueError(f"Неизвестный класс релевантности: {relevance!r}")

    if leader_bsr is None:
        return Verdict(None, None, None, None, PENDING, None, None, "нужен BSR лидера")
    ratio = leader_bsr / target_bsr
    k1 = leader_bsr >= K1_MIN_LEADER_BSR and leader_bsr > target_bsr
    if not k1:
        return Verdict(False, None, None, None, REJECTED, None, ratio, "отклонена по К1")

    if item_count is None:
        return Verdict(True, None, None, None, PENDING, None, ratio, "нужно число товаров в подкатегории")
    k2 = item_count >= K2_REQUIRED_ITEMS
    if not k2:
        return Verdict(True, False, None, None, REJECTED, None, ratio, "отклонена по К2")

    if relevance is None:
        return Verdict(True, True, None, None, PENDING, None, ratio, "нужна оценка релевантности")
    k3 = relevance != IRRELEVANT
    if not k3:
        return Verdict(True, True, False, None, REJECTED, None, ratio, "отклонена по К3")

    if leader_badge is None:
        return Verdict(True, True, True, None, PENDING, None, ratio, "нужно проверить бейдж лидера")
    if not leader_badge:
        return Verdict(True, True, True, False, REJECTED, None, ratio, "отклонена по К4")

    return Verdict(True, True, True, True, SUITABLE, _PRIORITY_BY_RELEVANCE[relevance], ratio, "подходит")


def ranked(verdicts: Iterable[Verdict]) -> List[Verdict]:
    """Подходящие сверху (высокий → низкий → риск, внутри — лидер слабее относительно нас), затем незавершённые,
    затем отклонённые."""
    status_order = {SUITABLE: 0, PENDING: 1, REJECTED: 2}
    return sorted(
        verdicts,
        key=lambda v: (
            status_order[v.status],
            _PRIORITY_ORDER.get(v.priority, len(_PRIORITY_ORDER)),
            -(v.leader_to_target or 0.0),
        ),
    )
