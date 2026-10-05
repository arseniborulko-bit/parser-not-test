"""Роли дашборда и список пользователей. Без Streamlit — чтобы правила доступа проверялись тестами.

Вход выполняет Streamlit (Google, OIDC); здесь только решение, что этому человеку можно.
Дашборд целиком открыт только сотрудникам (employee_email); журнал входов — record_login.
Админы-«затравка» берутся из секрета ADMIN_EMAILS, остальные — из bsr_radar.dashboard_users.
Всё неподтверждённое (нет письма, email не подтверждён, нет в списке, база недоступна) — без прав.
"""

from __future__ import annotations

import hmac
import logging
import re
import threading
import time
from collections import OrderedDict, deque
from typing import Callable, Iterable, List, Mapping, Optional

import dbutil
from dbutil import Connect

log = logging.getLogger(__name__)

# Дашборд открыт только рабочим Google-аккаунтам этого домена (не секрет: это не пароль, а правило).
EMPLOYEE_DOMAIN = "maximumstores.online"

ROLE_ADMIN = "admin"
ROLE_EDITOR = "editor"
_RANK = {ROLE_EDITOR: 1, ROLE_ADMIN: 2}

_EMAIL_RE = re.compile(r"^[^@\s,;]+@[^@\s,;]+\.[^@\s,;]+$")
_DOMAIN_RE = re.compile(r"^[a-z0-9-]+(\.[a-z0-9-]+)+$")
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


def parse_domain_list(text: object) -> frozenset:
    """Домены рабочей почты из секрета: через запятую или пробел, «@» в начале необязателен."""
    if not isinstance(text, str):
        return frozenset()
    found = (part.lower().lstrip("@") for part in re.split(r"[,;\s]+", text))
    return frozenset(domain for domain in found if _DOMAIN_RE.match(domain))


def corporate_email(value: object, domains: Iterable[str]) -> Optional[str]:
    """Адрес на одном из рабочих доменов. Домен сверяется целиком: поддомен и похожее имя не подходят."""
    email = normalize_email(value)
    if email is None or email.rsplit("@", 1)[1] not in set(domains):
        return None
    return email


def verified_email(user: Optional[Mapping]) -> Optional[str]:
    # Только строгое True: провайдер без claim email_verified не считается подтверждающим.
    if not user or user.get("is_logged_in") is not True:
        return None
    if user.get("email_verified") is not True:
        return None
    return normalize_email(user.get("email"))


def employee_email(user: Optional[Mapping], domain: str = EMPLOYEE_DOMAIN) -> Optional[str]:
    """Почта сотрудника или None. Нужно всё сразу: вход выполнен, Google подтвердил почту, адрес ровно на
    домене компании (поддомен и «…@domain.attacker.com» не подходят) и аккаунт — рабочий аккаунт этого
    домена (claim hd, его Google ставит только аккаунтам Google Workspace)."""
    email = verified_email(user)
    if email is None or corporate_email(email, {domain}) is None:
        return None
    hosted = user.get("hd")
    if not isinstance(hosted, str) or hosted.strip().lower() != domain:
        return None
    return email


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


def login_key(user: Optional[Mapping], email: str) -> Optional[str]:
    """Отпечаток одного входа через Google: кто (sub) и когда Google выдал вход (iat). Перерисовка
    страницы, обновление вкладки и вторая вкладка дают тот же ключ, новый вход после «Выйти» — новый.
    Сами токены не используются. Нет iat — None: тогда повторы отсеиваются только в пределах сессии."""
    issued = (user or {}).get("iat")
    if isinstance(issued, bool) or not isinstance(issued, (int, float, str)) or str(issued).strip() == "":
        return None
    subject = (user or {}).get("sub")
    who = subject if isinstance(subject, str) and subject.strip() else email
    return f"{who}:{issued}"


class LoginRegistry:
    """Входы, уже записанные этим процессом сервера (общие для всех сессий). Ограничен по размеру:
    самые старые ключи забываются — в худшем случае после перезапуска вход запишется ещё раз."""

    def __init__(self, max_size: int = 5000):
        self._seen: OrderedDict = OrderedDict()
        self._max_size = max_size
        self._lock = threading.Lock()

    def first_time(self, key: str) -> bool:
        with self._lock:
            if key in self._seen:
                return False
            self._seen[key] = None
            while len(self._seen) > self._max_size:
                self._seen.popitem(last=False)
            return True


def record_login(connect: Connect, email: str) -> bool:
    """Строка в журнал входов. Ошибка не мешает войти: возвращает False, причина — только в логе сервера
    и без текста драйвера (в нём бывают параметры подключения)."""
    clean = normalize_email(email)
    if clean is None:
        return False
    try:
        _run(connect, "INSERT INTO bsr_radar.login_log (email) VALUES (%s);", (clean,))
    except AccessStoreError as exc:
        log.warning("Журнал входов: вход %s не записан — %s", clean, exc)
        return False
    return True


def recent_logins(connect: Connect, limit: int = 200) -> List[dict]:
    """Последние входы из журнала, новые сверху: для служебной панели админа."""
    rows = _run(
        connect,
        "SELECT email, logged_in_at FROM bsr_radar.login_log ORDER BY logged_in_at DESC, id DESC LIMIT %s;",
        (int(limit),),
        fetch=True,
    )
    return [{"email": email, "logged_in_at": logged_in_at} for email, logged_in_at in rows]


def active_user_roles(connect: Connect) -> dict:
    rows = _run(connect, "SELECT email, role FROM bsr_radar.dashboard_users WHERE active;", fetch=True)
    return {email: role for email, role in rows if role in _RANK}


def list_users(connect: Connect) -> List[dict]:
    rows = _run(
        connect,
        "SELECT email, role, active, added_by, created_at FROM bsr_radar.dashboard_users ORDER BY email;",
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
        INSERT INTO bsr_radar.dashboard_users (email, role, active, added_by)
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
        "UPDATE bsr_radar.dashboard_users SET active = %s, updated_at = now() WHERE email = %s;",
        (bool(active), clean),
    )
    if changed != 1:
        raise ValueError("Пользователь не найден.")
