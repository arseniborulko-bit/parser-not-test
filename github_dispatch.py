"""Запуск workflow «Сбор данных» через GitHub API (workflow_dispatch). Токен нигде не печатается и не попадает в сообщения."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable, Optional

import requests

DEFAULT_REPO = "arseniborulko-bit/parser-not-test"
DEFAULT_WORKFLOW = "collect.yml"
DEFAULT_REF = "main"

_REPO_RE = re.compile(r"^(?!\.{1,2}/)[A-Za-z0-9_.-]+/(?!\.{1,2}$)[A-Za-z0-9_.-]+$")
_WORKFLOW_RE = re.compile(r"^(?!\.)[A-Za-z0-9_.-]+\.ya?ml$")
_REF_RE = re.compile(r"^(?!.*\.\.)[A-Za-z0-9_./-]+$")


@dataclass(frozen=True)
class DispatchResult:
    ok: bool
    message: str


_STATUS_MESSAGES = {
    401: "GitHub не принял токен (неверный или истёк).",
    403: "У токена нет права запускать workflow (нужно «Actions: Read and write») или исчерпан лимит запросов GitHub.",
    404: "GitHub не нашёл репозиторий или workflow (или у токена нет доступа к этому репозиторию).",
    422: "GitHub отклонил запуск: проверьте, что ветка main и файл workflow существуют.",
}


def dispatch_collection(token: Optional[str], repo: str = DEFAULT_REPO, *, workflow: str = DEFAULT_WORKFLOW,
                        ref: str = DEFAULT_REF, post: Optional[Callable] = None, timeout: float = 20) -> DispatchResult:
    """Просит GitHub запустить сбор. Без входных параметров: force снимает только лимит попыток и отсюда недоступен."""
    if not token:
        return DispatchResult(False, "Не задан токен GitHub (секрет GITHUB_DISPATCH_TOKEN).")
    if not (_REPO_RE.match(repo or "") and _WORKFLOW_RE.match(workflow or "") and _REF_RE.match(ref or "")):
        return DispatchResult(False, "Некорректное имя репозитория, workflow или ветки.")
    url = f"https://api.github.com/repos/{repo}/actions/workflows/{workflow}/dispatches"
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "parser-not-test-dashboard",
    }
    try:
        response = (post or requests.post)(url, headers=headers, json={"ref": ref}, timeout=timeout)
    except requests.Timeout:
        return DispatchResult(False, "GitHub не ответил вовремя. Запуск мог не состояться: проверьте вкладку Actions.")
    except requests.RequestException:
        return DispatchResult(False, "Не удалось связаться с GitHub.")
    status = response.status_code
    if status == 204:
        return DispatchResult(True, "Запуск отправлен в GitHub.")
    return DispatchResult(False, _STATUS_MESSAGES.get(status, f"GitHub ответил кодом {status}."))
