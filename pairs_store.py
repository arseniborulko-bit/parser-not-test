"""Пары «наш ASIN — конкурент» в Postgres: разбор вставленного текста, проверка перед записью и сама запись.
Без Streamlit — чтобы правила проверялись тестами.

Парсер и раскладка листа Current берут пары из базы (PAIR_SOURCE=database), лист Competitors не читается.
Убранная пара не удаляется, а получает active=FALSE: история снимков остаётся, пару можно вернуть.
Каждое изменение пишется в pair_changes в той же транзакции.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

import access
import dbutil
from dbutil import Connect

log = logging.getLogger(__name__)

# Должны совпадать с sheets._amazon_domain (это проверяет тест): неизвестный рынок парсер молча запросил бы на amazon.com.
DOMAIN_BY_MARKET = {
    "US": "com", "CA": "ca", "UK": "co.uk", "DE": "de", "FR": "fr",
    "ES": "es", "IT": "it", "MX": "com.mx", "JP": "co.jp", "AU": "com.au",
}
MARKET_BY_DOMAIN = {domain: market for market, domain in DOMAIN_BY_MARKET.items()}
MARKETS = tuple(DOMAIN_BY_MARKET)

# Тот же шаблон, что config.ASIN_PATTERN: другие ASIN парсер и синк молча пропускают.
_ASIN_RE = re.compile(r"\b(B0[A-Z0-9]{8})\b", re.IGNORECASE)
_HOST_RE = re.compile(r"amazon\.([a-z]+(?:\.[a-z]+)?)(?=[/?#:]|$)", re.IGNORECASE)
_TOKEN_SPLIT = re.compile(r"[\s,;]+")
_TOKEN_STRIP = "()[]<>\"'«»"

MAX_BATCH = 200
MAX_TOGGLE = 500
DEFAULT_MAX_ACTIVE = 1000
_WHAT = "Операция с парами"


class PairsStoreError(RuntimeError):
    """База недоступна или запись не подтверждена (без деталей драйвера)."""


@dataclass(frozen=True)
class Item:
    asin: str
    market: Optional[str]


@dataclass
class ParsedBatch:
    items: List[Item] = field(default_factory=list)
    invalid: List[str] = field(default_factory=list)
    repeats: int = 0


@dataclass
class Plan:
    market: Optional[str] = None
    our_asin: Optional[str] = None
    our_product: str = ""
    to_add: List[str] = field(default_factory=list)
    to_enable: List[str] = field(default_factory=list)
    already: List[str] = field(default_factory=list)
    invalid: List[str] = field(default_factory=list)
    rejected: List[str] = field(default_factory=list)
    repeats: int = 0
    new_asins: int = 0
    active_now: int = 0
    errors: List[str] = field(default_factory=list)

    @property
    def changes(self) -> int:
        return len(self.to_add) + len(self.to_enable)

    @property
    def can_apply(self) -> bool:
        return not self.errors and self.changes > 0


def parse_asin_batch(text: object) -> ParsedBatch:
    """Достаёт ASIN из вставленного текста: чистые ASIN, ссылки Amazon, ASIN:РЫНОК. Повторы отбрасываются."""
    batch = ParsedBatch()
    if not isinstance(text, str):
        return batch
    seen: set = set()
    for raw in _TOKEN_SPLIT.split(text.strip()):
        token = raw.strip(_TOKEN_STRIP)
        if not token:
            continue
        market: Optional[str] = None
        if "amazon." in token.lower():
            host = _HOST_RE.search(token)
            market = MARKET_BY_DOMAIN.get(host.group(1).lower()) if host else None
            if market is None:
                batch.invalid.append(raw)
                continue
        elif ":" in token:
            head, _, tail = token.rpartition(":")
            if tail.isalpha():
                if tail.upper() not in DOMAIN_BY_MARKET:
                    batch.invalid.append(raw)
                    continue
                token, market = head, tail.upper()
        match = _ASIN_RE.search(token)
        if not match:
            batch.invalid.append(raw)
            continue
        asin = match.group(1).upper()
        if asin in seen:
            batch.repeats += 1
            continue
        seen.add(asin)
        batch.items.append(Item(asin, market))
    return batch


def _run(connect: Connect, sql: str, params: tuple = (), *, fetch: bool = False):
    return dbutil.run_sql(connect, sql, params, fetch=fetch, error=PairsStoreError, what=_WHAT)


def make_plan(connect: Connect, our_text: object, market_choice: Optional[str], competitors_text: object, *,
              max_active: int = DEFAULT_MAX_ACTIVE) -> Plan:
    """Что случится при добавлении: новые, возвращаемые, уже существующие, ошибки. Ничего не записывает."""
    plan = Plan()
    ours = parse_asin_batch(our_text)
    if len(ours.items) != 1 or ours.invalid:
        plan.errors.append("Наш товар: нужен ровно один ASIN (B0XXXXXXXX) или ссылка на него.")
        return plan
    our = ours.items[0]
    plan.our_asin = our.asin

    comps = parse_asin_batch(competitors_text)
    plan.invalid, plan.repeats = list(comps.invalid), comps.repeats
    if len(comps.items) > MAX_BATCH:
        plan.errors.append(f"За один раз можно добавить не больше {MAX_BATCH} конкурентов.")
        return plan
    if not comps.items:
        plan.errors.append("Вставьте ASIN или ссылки конкурентов.")
        return plan

    known_rows, active_now = dbutil.run_many(
        connect,
        [
            ("SELECT marketplace, COALESCE(our_product, '') FROM parser_not_test.competitor_pairs "
             "WHERE our_asin = %s ORDER BY active DESC, id;", (our.asin,), True),
            ("SELECT count(*) FROM parser_not_test.competitor_pairs WHERE active;", (), True),
        ],
        error=PairsStoreError, what=_WHAT,
    )
    plan.active_now = active_now[0][0]
    known_markets = list(dict.fromkeys(row[0] for row in known_rows))
    if our.market:
        market = our.market
    elif market_choice in DOMAIN_BY_MARKET:
        market = market_choice
    elif len(known_markets) == 1:
        market = known_markets[0]
    else:
        plan.errors.append("Выберите маркетплейс: по этому ASIN нельзя определить страну.")
        return plan
    if market not in DOMAIN_BY_MARKET:
        plan.errors.append(f"Маркетплейс «{market}» не поддерживается сбором.")
        return plan
    plan.market = market
    plan.our_product = next((row[1] for row in known_rows if row[0] == market and row[1]), "")

    candidates: List[str] = []
    for item in comps.items:
        if item.market and item.market != market:
            plan.rejected.append(f"{item.asin} ({item.market}, а наш товар — {market})")
        elif item.asin == our.asin:
            plan.rejected.append(f"{item.asin} (совпадает с нашим товаром)")
        else:
            candidates.append(item.asin)

    if candidates:
        status_rows, collected_rows = dbutil.run_many(
            connect,
            [
                ("SELECT comp_asin, active FROM parser_not_test.competitor_pairs "
                 "WHERE marketplace = %s AND our_asin = %s AND comp_asin = ANY(%s);", (market, our.asin, candidates), True),
                ("SELECT a FROM unnest(%s::text[]) AS a WHERE EXISTS (SELECT 1 FROM parser_not_test.competitor_pairs "
                 "WHERE active AND (our_asin = a OR comp_asin = a));", (candidates + [our.asin],), True),
            ],
            error=PairsStoreError, what=_WHAT,
        )
        status = {asin: active for asin, active in status_rows}
        for asin in candidates:
            if asin not in status:
                plan.to_add.append(asin)
            elif not status[asin]:
                plan.to_enable.append(asin)
            else:
                plan.already.append(asin)
        collected = {row[0] for row in collected_rows}
        plan.new_asins = len(({our.asin} | set(plan.to_add) | set(plan.to_enable)) - collected)

    if plan.active_now + plan.changes > max_active:
        plan.errors.append(
            f"Активных пар станет {plan.active_now + plan.changes}, а лимит — {max_active}: каждая пара тратит кредиты сбора."
        )
    return plan


_ADD_SQL = """
WITH input AS (
    SELECT * FROM unnest(%s::text[], %s::text[], %s::text[], %s::text[]) AS t(marketplace, our_asin, our_product, comp_asin)
), changed AS (
    INSERT INTO parser_not_test.competitor_pairs AS p (marketplace, our_asin, our_product, comp_asin, active)
    SELECT marketplace, our_asin, our_product, comp_asin, TRUE FROM input
    ON CONFLICT (marketplace, our_asin, comp_asin) DO UPDATE SET active = TRUE
    WHERE NOT p.active
    RETURNING p.marketplace, p.our_asin, p.comp_asin, (xmax = 0) AS inserted
)
INSERT INTO parser_not_test.pair_changes (actor, action, marketplace, our_asin, comp_asin)
SELECT %s, CASE WHEN inserted THEN 'add' ELSE 'enable' END, marketplace, our_asin, comp_asin FROM changed
RETURNING action;
"""

_SET_ACTIVE_SQL = """
WITH input AS (
    SELECT * FROM unnest(%s::text[], %s::text[], %s::text[]) AS t(marketplace, our_asin, comp_asin)
), changed AS (
    UPDATE parser_not_test.competitor_pairs AS p SET active = %s
    FROM input i
    WHERE p.marketplace = i.marketplace AND p.our_asin = i.our_asin AND p.comp_asin = i.comp_asin
      AND p.active IS DISTINCT FROM %s
    RETURNING p.marketplace, p.our_asin, p.comp_asin
)
INSERT INTO parser_not_test.pair_changes (actor, action, marketplace, our_asin, comp_asin)
SELECT %s, %s, marketplace, our_asin, comp_asin FROM changed
RETURNING 1;
"""


def apply_plan(connect: Connect, plan: Plan, *, actor_role: Optional[str], actor: str) -> Dict[str, int]:
    access.require_role(actor_role, access.ROLE_EDITOR)
    if plan.errors:
        raise ValueError(plan.errors[0])
    comps = plan.to_add + plan.to_enable
    if not comps:
        return {"add": 0, "enable": 0}
    n = len(comps)
    rows = _run(
        connect, _ADD_SQL,
        ([plan.market] * n, [plan.our_asin] * n, [plan.our_product] * n, comps, actor),
        fetch=True,
    )
    result = {"add": sum(1 for (action,) in rows if action == "add"), "enable": sum(1 for (action,) in rows if action == "enable")}
    log.info("Пары (%s): добавлено %d, возвращено %d; наш ASIN %s, %s", actor, result["add"], result["enable"], plan.our_asin, plan.market)
    return result


def set_pairs_active(connect: Connect, keys: Sequence[Tuple[str, str, str]], active: bool, *,
                     actor_role: Optional[str], actor: str) -> int:
    """Отключает (active=False) или возвращает (True) пары по ключам (маркетплейс, наш ASIN, ASIN конкурента)."""
    access.require_role(actor_role, access.ROLE_EDITOR)
    unique = list(dict.fromkeys(tuple(key) for key in keys))
    if not unique:
        return 0
    if len(unique) > MAX_TOGGLE:
        raise ValueError(f"За один раз можно изменить не больше {MAX_TOGGLE} пар.")
    for market, our, comp in unique:
        if market not in DOMAIN_BY_MARKET or not _ASIN_RE.fullmatch(our) or not _ASIN_RE.fullmatch(comp):
            raise ValueError("Некорректный ключ пары.")
    rows = _run(
        connect, _SET_ACTIVE_SQL,
        ([k[0] for k in unique], [k[1] for k in unique], [k[2] for k in unique],
         bool(active), bool(active), actor, "enable" if active else "disable"),
        fetch=True,
    )
    log.info("Пары (%s): %s %d", actor, "возвращено" if active else "отключено", len(rows))
    return len(rows)


def recent_changes(connect: Connect, limit: int = 30) -> List[dict]:
    rows = _run(
        connect,
        "SELECT at, actor, action, marketplace, our_asin, comp_asin FROM parser_not_test.pair_changes "
        "ORDER BY at DESC, id DESC LIMIT %s;",
        (int(limit),), fetch=True,
    )
    return [
        {"at": at, "actor": actor, "action": action, "marketplace": market, "our_asin": our, "comp_asin": comp}
        for at, actor, action, market, our, comp in rows
    ]
