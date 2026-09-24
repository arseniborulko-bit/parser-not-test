"""Справочник ASIN (bsr_radar.asins) — ASIN сам по себе, без пары. Без Streamlit.

Зачем отдельная таблица: до неё ASIN существовал только внутри пары «наш товар — конкурент»,
поэтому завести ASIN заранее или держать его отдельно от пар было негде.

Важное ограничение, которое здесь НЕ решается: справочник описывает ASIN, но не управляет сбором.
Сбор по-прежнему идёт по активным парам, поэтому ASIN, заведённый только в справочнике, сам по себе
собираться не начнёт. Связать одно с другим — отдельное решение, потому что каждый лишний ASIN это
платный запрос за каждый сбор.

Убранный ASIN не удаляется, а получает active=FALSE: так же ведут себя пары, и история остаётся.
"""

from __future__ import annotations

import logging
from typing import Dict, List, Optional, Sequence, Tuple

import access
import dbutil
from dbutil import Connect
from pairs_store import DOMAIN_BY_MARKET, MAX_NAME, _ASIN_RE, parse_asin_batch

log = logging.getLogger(__name__)

KINDS = ("ours", "competitor")
KIND_LABELS = {"ours": "наш", "competitor": "конкурент"}
MAX_ADD = 200
MAX_TOGGLE = 500
_WHAT = "Операция со справочником ASIN"


class AsinStoreError(RuntimeError):
    """База недоступна или данные некорректны (без деталей драйвера)."""


def registry_exists(connect: Connect) -> bool:
    """Миграция 008 могла ещё не применяться: тогда справочника просто нет, и интерфейс
    обязан обойтись без него, а не упасть."""
    rows = dbutil.run_sql(
        connect, "SELECT to_regclass('bsr_radar.asins') IS NOT NULL;", (),
        fetch=True, error=AsinStoreError, what=_WHAT,
    )
    return bool(rows and rows[0][0])


def load_asins(connect: Connect) -> List[dict]:
    rows = dbutil.run_sql(
        connect,
        "SELECT marketplace, asin, name, kind, active FROM bsr_radar.asins "
        "ORDER BY marketplace, asin;",
        (), fetch=True, error=AsinStoreError, what=_WHAT,
    )
    return [
        {"marketplace": market, "asin": asin, "name": name or "", "kind": kind, "active": active}
        for market, asin, name, kind, active in rows
    ]


def _check_key(market: object, asin: object) -> Tuple[str, str]:
    market = str(market or "").strip().upper()
    asin = str(asin or "").strip().upper()
    if market not in DOMAIN_BY_MARKET:
        raise ValueError("Неизвестная страна.")
    if not _ASIN_RE.fullmatch(asin):
        raise ValueError(f"Не похоже на ASIN: {asin or 'пусто'}")
    return market, asin


def _check_name(value: object) -> str:
    name = " ".join(str(value or "").split())
    if len(name) > MAX_NAME:
        raise ValueError(f"Название длиннее {MAX_NAME} символов.")
    return name


def add_asins(connect: Connect, market: str, text: object, kind: str, *,
              actor_role: Optional[str], actor: str) -> Dict[str, int]:
    """Добавляет ASIN пачкой из вставленного текста (как и пары: ссылки и мусор разбираются).

    Уже заведённый ASIN не дублируется: если он был убран, он возвращается обратно.
    """
    access.require_role(actor_role, access.ROLE_EDITOR)
    if kind not in KINDS:
        raise ValueError("Укажите, наш это товар или конкурент.")
    market = str(market or "").strip().upper()
    if market not in DOMAIN_BY_MARKET:
        raise ValueError("Неизвестная страна.")

    parsed = parse_asin_batch(text)
    # У ссылки на amazon.de рынок известен из самой ссылки — он важнее выбранного в списке.
    wanted = list(dict.fromkeys((item.market or market, item.asin.upper()) for item in parsed.items))
    if not wanted:
        raise ValueError("В тексте нет ни одного ASIN.")
    if len(wanted) > MAX_ADD:
        raise ValueError(f"За один раз можно добавить не больше {MAX_ADD} ASIN.")
    for item_market, asin in wanted:
        _check_key(item_market, asin)

    rows = dbutil.run_sql(
        connect,
        """
        INSERT INTO bsr_radar.asins (marketplace, asin, kind, active)
        SELECT m, a, %s, TRUE FROM unnest(%s::text[], %s::text[]) AS t(m, a)
        ON CONFLICT (marketplace, asin) DO UPDATE
            SET active = TRUE, kind = EXCLUDED.kind, updated_at = now()
            WHERE NOT bsr_radar.asins.active OR bsr_radar.asins.kind IS DISTINCT FROM EXCLUDED.kind
        RETURNING (xmax = 0) AS inserted;
        """,
        (kind, [item[0] for item in wanted], [item[1] for item in wanted]),
        fetch=True, error=AsinStoreError, what=_WHAT,
    )
    result = {
        "added": sum(1 for (inserted,) in rows if inserted),
        "restored": sum(1 for (inserted,) in rows if not inserted),
        "skipped": len(wanted) - len(rows),
    }
    log.info("Справочник ASIN (%s): добавлено %d, возвращено %d, без изменений %d; %s",
             actor, result["added"], result["restored"], result["skipped"], market)
    return result


def set_active(connect: Connect, keys: Sequence[Tuple[str, str]], active: bool, *,
               actor_role: Optional[str], actor: str) -> int:
    """Убирает ASIN из справочника (active=FALSE) или возвращает обратно. Строка не удаляется."""
    access.require_role(actor_role, access.ROLE_EDITOR)
    unique = list(dict.fromkeys(_check_key(market, asin) for market, asin in keys))
    if not unique:
        return 0
    if len(unique) > MAX_TOGGLE:
        raise ValueError(f"За один раз можно изменить не больше {MAX_TOGGLE} ASIN.")
    rows = dbutil.run_sql(
        connect,
        """
        UPDATE bsr_radar.asins AS a SET active = %s, updated_at = now()
        FROM unnest(%s::text[], %s::text[]) AS i(marketplace, asin)
        WHERE a.marketplace = i.marketplace AND a.asin = i.asin
          AND a.active IS DISTINCT FROM %s
        RETURNING 1;
        """,
        (bool(active), [key[0] for key in unique], [key[1] for key in unique], bool(active)),
        fetch=True, error=AsinStoreError, what=_WHAT,
    )
    log.info("Справочник ASIN (%s): %s %d", actor, "возвращено" if active else "убрано", len(rows))
    return len(rows)


def rename(connect: Connect, key: Tuple[str, str], name: object, *,
           actor_role: Optional[str], actor: str) -> int:
    """Меняет подпись ASIN. Сам ASIN и страна — ключ, они не меняются."""
    access.require_role(actor_role, access.ROLE_EDITOR)
    market, asin = _check_key(*key)
    text = _check_name(name)
    rows = dbutil.run_sql(
        connect,
        "UPDATE bsr_radar.asins SET name = %s, updated_at = now() "
        "WHERE marketplace = %s AND asin = %s AND name IS DISTINCT FROM %s RETURNING 1;",
        (text, market, asin, text), fetch=True, error=AsinStoreError, what=_WHAT,
    )
    log.info("Справочник ASIN (%s): подпись изменена у %d строк; %s %s", actor, len(rows), market, asin)
    return len(rows)
