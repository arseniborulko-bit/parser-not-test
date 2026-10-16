"""Список ASIN раздела «Best Seller подкатегории» — своя схема best_seller в базе дашборда, отдельно от таблиц
мониторинга BSR (bsr_radar): раздел независимый, но отдельную базу не заводим (решения владельца 16.10.2026).
Схема — best_seller_db/001_watchlist.sql.

Ключ строки — (страна, ASIN). Удаление — отключение, его можно вернуть. Каждое изменение пишется в журнал
best_seller.asin_changes в той же транзакции. Без Streamlit.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

import access
import dbutil
from dbutil import Connect

MAX_BATCH = 500
_WHAT = "Операция со списком ASIN Best Seller"
Key = Tuple[str, str]  # (страна, ASIN)


class BestSellerStoreError(RuntimeError):
    """База Best Seller недоступна или запись не подтверждена (без деталей драйвера)."""


@dataclass
class AddPlan:
    to_add: List[Key] = field(default_factory=list)
    to_enable: List[Key] = field(default_factory=list)
    already: List[Key] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)

    @property
    def changes(self) -> int:
        return len(self.to_add) + len(self.to_enable)


def _run(connect: Connect, sql: str, params: tuple = (), *, fetch: bool = False):
    return dbutil.run_sql(connect, sql, params, fetch=fetch, error=BestSellerStoreError, what=_WHAT)


def table_exists(connect: Connect) -> bool:
    return bool(_run(connect, "SELECT to_regclass('best_seller.asins') IS NOT NULL;", fetch=True)[0][0])


def list_asins(connect: Connect) -> List[dict]:
    rows = _run(connect, "SELECT marketplace, asin, active, added_by, updated_at FROM best_seller.asins "
                         "ORDER BY marketplace, asin;", fetch=True)
    return [{"marketplace": m, "asin": a, "active": act, "added_by": by, "updated_at": at} for m, a, act, by, at in rows]


def plan_add(existing: Sequence[dict], keys: Sequence[Key]) -> AddPlan:
    """Что сделает «Добавить»: новые — добавить, отключённые — вернуть, активные — уже в списке."""
    plan = AddPlan()
    unique = list(dict.fromkeys(keys))
    if not unique:
        plan.errors.append("Вставьте ссылки на товары Amazon.")
        return plan
    if len(unique) > MAX_BATCH:
        plan.errors.append(f"За раз можно добавить не больше {MAX_BATCH} ASIN.")
        return plan
    state: Dict[Key, bool] = {(row["marketplace"], row["asin"]): row["active"] for row in existing}
    for key in unique:
        if key not in state:
            plan.to_add.append(key)
        elif state[key]:
            plan.already.append(key)
        else:
            plan.to_enable.append(key)
    return plan


_UPSERT_SQL = """
WITH input AS (
    SELECT * FROM unnest(%s::text[], %s::text[]) AS t(marketplace, asin)
), changed AS (
    INSERT INTO best_seller.asins AS a (marketplace, asin, active, added_by)
    SELECT marketplace, asin, TRUE, %s FROM input
    ON CONFLICT (marketplace, asin) DO UPDATE SET active = TRUE, updated_at = now()
    WHERE NOT a.active
    RETURNING a.marketplace, a.asin, (xmax = 0) AS inserted
)
INSERT INTO best_seller.asin_changes (actor, action, marketplace, asin)
SELECT %s, CASE WHEN inserted THEN 'add' ELSE 'enable' END, marketplace, asin FROM changed
RETURNING action;
"""

_SET_ACTIVE_SQL = """
WITH input AS (
    SELECT * FROM unnest(%s::text[], %s::text[]) AS t(marketplace, asin)
), changed AS (
    UPDATE best_seller.asins AS a SET active = %s, updated_at = now()
    FROM input i
    WHERE a.marketplace = i.marketplace AND a.asin = i.asin AND a.active IS DISTINCT FROM %s
    RETURNING a.marketplace, a.asin
)
INSERT INTO best_seller.asin_changes (actor, action, marketplace, asin)
SELECT %s, %s, marketplace, asin FROM changed
RETURNING 1;
"""


def add(connect: Connect, keys: Sequence[Key], *, actor_role: Optional[str], actor: str) -> Dict[str, int]:
    access.require_role(actor_role, access.ROLE_EDITOR)
    unique = list(dict.fromkeys(keys))
    if not unique:
        return {"add": 0, "enable": 0}
    if len(unique) > MAX_BATCH:
        raise ValueError(f"За раз можно добавить не больше {MAX_BATCH} ASIN.")
    rows = _run(connect, _UPSERT_SQL, ([m for m, _ in unique], [a for _, a in unique], actor, actor), fetch=True)
    return {"add": sum(1 for (action,) in rows if action == "add"),
            "enable": sum(1 for (action,) in rows if action == "enable")}


def set_active(connect: Connect, keys: Sequence[Key], active: bool, *, actor_role: Optional[str], actor: str) -> int:
    """Отключает (False) или возвращает (True) ASIN по ключам (страна, ASIN). Возвращает число изменённых."""
    access.require_role(actor_role, access.ROLE_EDITOR)
    unique = list(dict.fromkeys(tuple(key) for key in keys))
    if not unique:
        return 0
    if len(unique) > MAX_BATCH:
        raise ValueError(f"За раз можно изменить не больше {MAX_BATCH} ASIN.")
    action = "enable" if active else "disable"
    rows = _run(connect, _SET_ACTIVE_SQL,
                ([m for m, _ in unique], [a for _, a in unique], active, active, actor, action), fetch=True)
    return len(rows)


# --- поиск подкатегорий (best_seller_db/002_search.sql) ------------------------------------------------------

def search_tables_exist(connect: Connect) -> bool:
    return bool(_run(connect, "SELECT to_regclass('best_seller.searches') IS NOT NULL "
                              "AND to_regclass('best_seller.node_cache') IS NOT NULL;", fetch=True)[0][0])


class DbNodeCache:
    """Кэш подкатегорий на неделю в best_seller.node_cache (тот же интерфейс, что best_seller_search.MemoryCache)."""

    def __init__(self, connect: Connect) -> None:
        self.connect = connect

    def get(self, market: str, node: str):
        import best_seller_search as bss

        rows = _run(self.connect, "SELECT node, dept, name, leader_asin, leader_bsr, leader_badge, item_count, top, "
                                  "linked, fetched_at FROM best_seller.node_cache WHERE marketplace = %s AND node = %s;",
                    (market, node), fetch=True)
        if not rows:
            return None
        node, dept, name, leader, bsr, badge, count, top, linked, fetched = rows[0]
        return bss.NodeInfo(node=node, dept=dept, name=name, leader_asin=leader, leader_bsr=bsr, leader_badge=badge,
                            item_count=count, top=list(top or []), fetched_at=fetched,
                            linked=[bss.Subcategory(x["node"], x.get("dept", ""), x.get("name", ""))
                                    for x in (linked or []) if isinstance(x, dict) and x.get("node")])

    def put(self, market: str, info) -> None:
        linked = [{"node": x.node, "dept": x.dept, "name": x.name} for x in info.linked]
        _run(self.connect, """
            INSERT INTO best_seller.node_cache AS c (marketplace, node, dept, name, leader_asin, leader_bsr,
                                                    leader_badge, item_count, top, linked, fetched_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s::jsonb, %s)
            ON CONFLICT (marketplace, node) DO UPDATE SET dept = EXCLUDED.dept, name = EXCLUDED.name,
                leader_asin = EXCLUDED.leader_asin, leader_bsr = EXCLUDED.leader_bsr,
                leader_badge = EXCLUDED.leader_badge, item_count = EXCLUDED.item_count, top = EXCLUDED.top,
                linked = EXCLUDED.linked, fetched_at = EXCLUDED.fetched_at;
            """, (market, info.node, info.dept, info.name, info.leader_asin, info.leader_bsr, info.leader_badge,
                  info.item_count, json.dumps(info.top, ensure_ascii=False), json.dumps(linked, ensure_ascii=False),
                  info.fetched_at))


_SAVE_SEARCH_SQL = """
WITH s AS (
    INSERT INTO best_seller.searches (marketplace, asin, actor, title, our_bsr, target_bsr, status, error, own,
                                      requests, finished_at)
    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s, now())
    RETURNING id
), r AS (
    INSERT INTO best_seller.search_results (search_id, node, name, path, leader_bsr, k1, k2, k3, k4, relevance,
                                            status, priority, reason)
    SELECT s.id, t.* FROM s, unnest(%s::text[], %s::text[], %s::text[], %s::int[], %s::bool[], %s::bool[],
                                    %s::bool[], %s::bool[], %s::text[], %s::text[], %s::text[], %s::text[]) AS t
)
SELECT id FROM s;
"""


def save_search(connect: Connect, result, *, actor_role: Optional[str], actor: str) -> int:
    """Поиск и решения по подкатегориям — одним запросом (одна транзакция). Возвращает id поиска."""
    access.require_role(actor_role, access.ROLE_EDITOR)
    own = [{"node": x.node, "name": x.name, "rank": x.rank} for x in result.own]
    cs = result.candidates
    columns = (
        [c.sub.node for c in cs], [c.sub.name[:300] for c in cs], [c.sub.path[:500] for c in cs],
        [c.info.leader_bsr if c.info else None for c in cs],
        [c.verdict.k1 for c in cs], [c.verdict.k2 for c in cs], [c.verdict.k3 for c in cs], [c.verdict.k4 for c in cs],
        [c.relevance for c in cs], [c.verdict.status for c in cs], [c.verdict.priority for c in cs],
        [c.verdict.reason for c in cs],
    )
    rows = _run(connect, _SAVE_SEARCH_SQL,
                (result.market, result.asin, actor, result.title[:500], result.our_bsr, result.target_bsr,
                 result.status, result.error[:500], json.dumps(own, ensure_ascii=False), result.requests, *columns),
                fetch=True)
    return rows[0][0]


def latest_search(connect: Connect, market: str, asin: str) -> Optional[dict]:
    """Последний поиск по стране и ASIN с решениями по подкатегориям (подходящие сверху) или None."""
    rows = _run(connect, """
        SELECT id, title, our_bsr, target_bsr, status, error, own, requests, actor, finished_at
        FROM best_seller.searches WHERE marketplace = %s AND asin = %s ORDER BY started_at DESC, id DESC LIMIT 1;
        """, (market, asin), fetch=True)
    if not rows:
        return None
    sid, title, our, target, status, error, own, requests, actor, finished = rows[0]
    results = _run(connect, """
        SELECT node, name, path, leader_bsr, k1, k2, k3, k4, relevance, status, priority, reason
        FROM best_seller.search_results WHERE search_id = %s
        ORDER BY CASE status WHEN 'suitable' THEN 0 WHEN 'pending' THEN 1 ELSE 2 END, leader_bsr DESC NULLS LAST;
        """, (sid,), fetch=True)
    keys = ("node", "name", "path", "leader_bsr", "k1", "k2", "k3", "k4", "relevance", "status", "priority", "reason")
    return {"id": sid, "market": market, "asin": asin, "title": title, "our_bsr": our, "target_bsr": target,
            "status": status, "error": error, "own": list(own or []), "requests": requests, "actor": actor,
            "finished_at": finished, "results": [dict(zip(keys, row)) for row in results]}
