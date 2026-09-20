"""Короткие запросы к Postgres для дашборда. Текст ошибки драйвера в сообщения не попадает: в нём бывают параметры подключения."""

from __future__ import annotations

from typing import Callable, List, Sequence, Tuple, Type

Connect = Callable[[], object]
Statement = Tuple[str, tuple, bool]


def run_many(connect: Connect, statements: Sequence[Statement], *, error: Type[Exception], what: str) -> List[object]:
    """Все запросы идут в одном соединении и одной транзакции. Результат: строки (fetch=True) или rowcount."""
    results: List[object] = []
    try:
        conn = connect()
        try:
            with conn.cursor() as cur:
                for sql, params, fetch in statements:
                    cur.execute(sql, params)
                    results.append(cur.fetchall() if fetch else cur.rowcount)
            conn.commit()
        finally:
            conn.close()
    except Exception as exc:
        raise error(f"{what} не подтверждена ({type(exc).__name__}).") from None
    return results


def run_sql(connect: Connect, sql: str, params: tuple = (), *, fetch: bool = False,
            error: Type[Exception], what: str):
    return run_many(connect, [(sql, params, fetch)], error=error, what=what)[0]
