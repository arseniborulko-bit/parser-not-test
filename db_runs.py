"""
Пишет историю запусков в parser_not_test.collection_runs — чтобы статус был
виден из облака (дашборд на Streamlit Cloud, GitHub Actions), а не только в
локальном run_status.json.

Никогда не бросает исключение наружу: сбой логирования не должен ронять сам
прогон парсера/синка.
"""

from __future__ import annotations

import os
from typing import Optional

import psycopg2


def _source() -> str:
    return "github_actions" if os.environ.get("GITHUB_ACTIONS") == "true" else "local"


def log_start(step: str) -> Optional[int]:
    try:
        conn = psycopg2.connect(os.environ["DATABASE_URL"])
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO parser_not_test.collection_runs (source, step, status)
                    VALUES (%s, %s, 'running')
                    RETURNING id;
                    """,
                    (_source(), step),
                )
                run_id = cur.fetchone()[0]
            conn.commit()
            return run_id
        finally:
            conn.close()
    except Exception:
        return None


def log_finish(run_id: Optional[int], status: str, error: Optional[str] = None) -> None:
    if run_id is None:
        return
    try:
        conn = psycopg2.connect(os.environ["DATABASE_URL"])
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE parser_not_test.collection_runs
                    SET status = %s, error = %s, finished_at = now()
                    WHERE id = %s;
                    """,
                    (status, error, run_id),
                )
            conn.commit()
        finally:
            conn.close()
    except Exception:
        pass
