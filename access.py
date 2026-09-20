"""Роли дашборда и список пользователей. Без Streamlit — чтобы правила доступа проверялись тестами.

Вход выполняет Streamlit (Google, OIDC); здесь только решение, что этому человеку можно.
Админы-«затравка» берутся из секрета ADMIN_EMAILS, остальные — из parser_not_test.dashboard_users.
Всё неподтверждённое (нет письма, email не подтверждён, нет в списке, база недоступна) — без прав.
"""

from __future__ import annotations

import hmac
import re
import threading
import time
from collections import deque
from typing import Callable, Iterable, List, Mapping, Optional

import dbutil
from dbutil import Connect

ROLE_ADMIN = "admin"
ROLE_EDITOR = "editor"
_RANK = {ROLE_EDITOR: 1, ROLE_ADMIN: 2}

_EMAIL_RE = re.compile(r"^[^@\s,;]+@[^@\s,;]+\.[^@\s,;]+$")
_MAX_EMAIL_LENGTH = 254
MIN_PASSWORD_LENGTH = 8


class AccessDenied(PermissionError):
    """Не хватает роли для действия."""


class AccessStoreError(RuntimeError):
    """Список пользователей недоступен или запись не подтверждена (без деталей драйвера)."""


def normalize_email(value: object) -> Optional[str]:
    if not isinstance(value, str):
        return None
    email = value.strip().lower()
    if not email or len(email) > _MAX_EMAIL_LENGTH or not _EMAIL_RE.match(email):
        return None
    return email


def parse_email_list(text: object) -> frozenset:
    if not isinstance(text, str):
        return frozenset()
    found = (normalize_email(part) for part in re.split(r"[,;\s]+", text))
    return frozenset(email for email in found if email)


def verified_email(user: Optional[Mapping]) -> Optional[str]:
    # Только строгое True: провайдер без claim email_verified не считается подтверждающим.
    if not user or user.get("is_logged_in") is not True:
        return None
    if user.get("email_verified") is not True:
        return None
    return normalize_email(user.get("email"))


def resolve_role(user: Optional[Mapping], admin_emails: Iterable[str], db_roles: Mapping[str, str]) -> Optional[str]:
    email = verified_email(user)
    if email is None:
        return None
    if email in set(admin_emails):
        return ROLE_ADMIN
    role = db_roles.get(email)
    return role if role in _RANK else None


def has_role(role: Optional[str], required: str) -> bool:
    return role in _RANK and required in _RANK and _RANK[role] >= _RANK[required]


def require_role(role: Optional[str], required: str) -> None:
    if not has_role(role, required):
        raise AccessDenied(f"Нужна роль «{required}».")


def password_matches(candidate: object, expected: object) -> bool:
    """Общий пароль команды. Пустой или короткий эталон никогда не совпадает — иначе «нет пароля» открывало бы вход."""
    if not isinstance(candidate, str) or not isinstance(expected, str) or len(expected) < MIN_PASSWORD_LENGTH:
        return False
    return hmac.compare_digest(candidate.encode("utf-8"), expected.encode("utf-8"))


def clean_actor_name(value: object) -> Optional[str]:
    if not isinstance(value, str):
        return None
    name = " ".join(value.split())
    if not 2 <= len(name) <= 40 or not name.isprintable():
        return None
    return name


class AttemptLimiter:
    """Не даёт перебирать пароль: не больше max_failures неудач за окно window секунд, потом вход закрыт."""

    def __init__(self, max_failures: int = 5, window: float = 600.0, clock: Callable[[], float] = time.monotonic):
        self._max_failures = max_failures
        self._window = window
        self._clock = clock
        self._failures: deque = deque()
        self._lock = threading.Lock()

    def _prune(self) -> None:
        cutoff = self._clock() - self._window
        while self._failures and self._failures[0] <= cutoff:
            self._failures.popleft()

    def allowed(self) -> bool:
        with self._lock:
            self._prune()
            return len(self._failures) < self._max_failures

    def retry_after(self) -> int:
        with self._lock:
            self._prune()
            if len(self._failures) < self._max_failures:
                return 0
            return max(1, int(self._failures[0] + self._window - self._clock()) + 1)

    def record_failure(self) -> None:
        with self._lock:
            self._failures.append(self._clock())

    def record_success(self) -> None:
        with self._lock:
            self._failures.clear()


def _run(connect: Connect, sql: str, params: tuple = (), *, fetch: bool = False):
    return dbutil.run_sql(
        connect, sql, params, fetch=fetch, error=AccessStoreError, what="Операция со списком пользователей",
    )


def active_user_roles(connect: Connect) -> dict:
    rows = _run(connect, "SELECT email, role FROM parser_not_test.dashboard_users WHERE active;", fetch=True)
    return {email: role for email, role in rows if role in _RANK}


def list_users(connect: Connect) -> List[dict]:
    rows = _run(
        connect,
        "SELECT email, role, active, added_by, created_at FROM parser_not_test.dashboard_users ORDER BY email;",
        fetch=True,
    )
    return [
        {"email": email, "role": role, "active": active, "added_by": added_by, "created_at": created_at}
        for email, role, active, added_by, created_at in rows
    ]


def add_user(connect: Connect, email: str, role: str, *, actor_role: Optional[str], actor_email: str) -> str:
    require_role(actor_role, ROLE_ADMIN)
    clean = normalize_email(email)
    if clean is None:
        raise ValueError("Некорректный email.")
    if role not in _RANK:
        raise ValueError("Некорректная роль.")
    if clean == normalize_email(actor_email) and role != ROLE_ADMIN:
        raise ValueError("Нельзя понизить самого себя.")
    _run(
        connect,
        """
        INSERT INTO parser_not_test.dashboard_users (email, role, active, added_by)
        VALUES (%s, %s, TRUE, %s)
        ON CONFLICT (email) DO UPDATE SET role = EXCLUDED.role, active = TRUE, updated_at = now();
        """,
        (clean, role, actor_email),
    )
    return clean


def set_user_active(connect: Connect, email: str, active: bool, *, actor_role: Optional[str], actor_email: str) -> None:
    require_role(actor_role, ROLE_ADMIN)
    clean = normalize_email(email)
    if clean is None:
        raise ValueError("Некорректный email.")
    if not active and clean == normalize_email(actor_email):
        raise ValueError("Нельзя отключить самого себя.")
    changed = _run(
        connect,
        "UPDATE parser_not_test.dashboard_users SET active = %s, updated_at = now() WHERE email = %s;",
        (bool(active), clean),
    )
    if changed != 1:
        raise ValueError("Пользователь не найден.")
