"""Запуск сбора через GitHub API: форма запроса, сообщения и то, что токен не утекает. Без сети."""

import pytest
import requests

import github_dispatch

TOKEN = "fake-github-token-value"


class FakeResponse:
    def __init__(self, status):
        self.status_code = status
        self.text = f"body with {TOKEN} inside that must never be shown"


def recorder(status=204):
    calls = []

    def post(url, **kwargs):
        calls.append((url, kwargs))
        return FakeResponse(status)

    return post, calls


def test_success_sends_exactly_one_authorized_dispatch_without_inputs():
    post, calls = recorder(204)
    result = github_dispatch.dispatch_collection(TOKEN, post=post)
    assert result.ok and "отправлен" in result.message
    (url, kwargs), = calls
    assert url == "https://api.github.com/repos/arseniborulko-bit/parser-not-test/actions/workflows/collect.yml/dispatches"
    assert kwargs["json"] == {"ref": "main"}
    assert kwargs["headers"]["Authorization"] == f"Bearer {TOKEN}"
    assert kwargs["headers"]["Accept"] == "application/vnd.github+json"
    assert kwargs["headers"]["X-GitHub-Api-Version"] == "2022-11-28"
    assert kwargs["headers"]["User-Agent"]
    assert kwargs["timeout"] == 20


def test_force_can_never_be_sent_from_here():
    post, calls = recorder()
    github_dispatch.dispatch_collection(TOKEN, post=post)
    assert "inputs" not in calls[0][1]["json"]


@pytest.mark.parametrize("token", [None, "", "   "[:0]])
def test_without_a_token_nothing_is_sent(token):
    post, calls = recorder()
    result = github_dispatch.dispatch_collection(token, post=post)
    assert not result.ok and "GITHUB_DISPATCH_TOKEN" in result.message and calls == []


@pytest.mark.parametrize("status, expected", [
    (401, "не принял токен"),
    (403, "Actions: Read and write"),
    (404, "не нашёл репозиторий"),
    (422, "ветка main"),
    (500, "кодом 500"),
    (200, "кодом 200"),
])
def test_error_statuses_get_a_plain_message_without_the_response_body(status, expected):
    post, _ = recorder(status)
    result = github_dispatch.dispatch_collection(TOKEN, post=post)
    assert not result.ok and expected in result.message
    assert TOKEN not in result.message and "body with" not in result.message


@pytest.mark.parametrize("error, expected", [
    (requests.Timeout(f"timed out {TOKEN}"), "не ответил вовремя"),
    (requests.ConnectionError(f"failed {TOKEN}"), "Не удалось связаться"),
])
def test_network_errors_do_not_leak_the_token(error, expected):
    def post(url, **kwargs):
        raise error

    result = github_dispatch.dispatch_collection(TOKEN, post=post)
    assert not result.ok and expected in result.message and TOKEN not in result.message


@pytest.mark.parametrize("kwargs", [
    {"repo": "a/b/c"}, {"repo": "../etc"}, {"repo": "./etc"}, {"repo": "owner/.."}, {"repo": "owner/."}, {"repo": "no-slash"},
    {"repo": "owner/repo;rm"}, {"repo": ""},
    {"workflow": "collect.txt"}, {"workflow": "../collect.yml"}, {"workflow": ".yml"}, {"workflow": ""},
    {"ref": "main; drop"}, {"ref": ""}, {"ref": "a b"}, {"ref": "../main"},
])
def test_names_that_could_change_the_request_path_are_refused(kwargs):
    post, calls = recorder()
    result = github_dispatch.dispatch_collection(TOKEN, post=post, **kwargs)
    assert not result.ok and "Некорректное" in result.message and calls == []


def test_a_custom_repository_and_branch_are_used():
    post, calls = recorder()
    github_dispatch.dispatch_collection(TOKEN, "acme/tracker", workflow="run.yaml", ref="release/1", post=post)
    assert calls[0][0] == "https://api.github.com/repos/acme/tracker/actions/workflows/run.yaml/dispatches"
    assert calls[0][1]["json"] == {"ref": "release/1"}
