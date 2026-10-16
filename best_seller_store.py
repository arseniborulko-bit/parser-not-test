"""Список ASIN раздела «Best Seller подкатегории» — в ОТДЕЛЬНОЙ базе Postgres (секрет BEST_SELLER_DATABASE_URL),
не в базе мониторинга BSR: раздел независимый (решение владельца 16.10.2026). Схема — best_seller_db/001_watchlist.sql.

Ключ строки — (страна, ASIN). Удаление — отключение, его можно вернуть. Каждое изменение пишется в журнал
best_seller.asin_changes в той же транзакции. Без Streamlit.
"""

from __future__ import annotations

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
