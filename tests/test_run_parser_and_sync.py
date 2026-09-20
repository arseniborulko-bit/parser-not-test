"""
Изолированные тесты допуска и кода завершения main() в run_parser_and_sync.py.

Ничего реальное не трогается: подменены _run_step (никакой subprocess,
никакой parser_not_test.py/sync_sheets_to_db.py, значит ни Sheets, ни
ScrapingDog, ни Telegram), db_runs.log_start/log_finish (никакого
подключения к Postgres) и STATUS_FILE (пишет во временную папку pytest,
а не в боевой run_status.json).

Первые четыре теста проверяют то, что main() возвращает правильный код
(0 при успехе, не 0 при ошибке любого из двух шагов) — то есть возвращаемое
значение функции main() внутри процесса, а НЕ код завершения отдельного
процесса, который получит вызывающая сторона (это разные вещи: то, что
main() возвращает 1, ещё не проверяет, что `python run_parser_and_sync.py`
как отдельный процесс завершится с exit code 1 - это гарантируется только
строкой `sys.exit(main())` в конце файла, которую эти тесты не исполняют
напрямую, а лишь читают как код).
"""

import importlib
import json
from types import SimpleNamespace

import pytest


@pytest.fixture
def runner(monkeypatch, tmp_path):
    module = importlib.import_module("run_parser_and_sync")
    for name in ("GITHUB_ACTIONS", "GITHUB_EVENT_NAME", "GITHUB_REPOSITORY", "GITHUB_RUN_ID",
                 "GITHUB_RUN_ATTEMPT", "COLLECTION_RUN_ID", "FORCE_COLLECTION"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("DATABASE_URL", "unused-test-dsn")
    monkeypatch.setattr(module, "STATUS_FILE", tmp_path / "run_status.json")
    monkeypatch.setattr(module, "LOG_FILE", tmp_path / "last_run.log")

    def forbidden(*args, **kwargs):
        raise AssertionError("Real database/subprocess calls are forbidden in unit tests")

    monkeypatch.setattr(module.db_runs.psycopg2, "connect", forbidden)
    monkeypatch.setattr(module.subprocess, "run", forbidden)
    events = []
    codes = {"parser": 0, "sync": 0}

    def admit(invocation):
        events.append(("admit", invocation))
        return module.db_runs.Admission(True, "test", 42)

    def claim(run_id, invocation):
        events.append(("claim", run_id, invocation))

    def start(step):
        events.append(("start", step))
        return 50

    def finish(run_id, status, error=None):
        events.append(("finish", run_id, status))

    def fake_run_step(name, args):
        events.append(("child", name))
        return codes[name]

    monkeypatch.setattr(module.db_runs, "admit_parser_run", admit)
    monkeypatch.setattr(module.db_runs, "claim_parser_run", claim)
    monkeypatch.setattr(module.db_runs, "log_start", start)
    monkeypatch.setattr(module.db_runs, "log_finish", finish)
    monkeypatch.setattr(module, "_run_step", fake_run_step)
    return SimpleNamespace(module=module, events=events, codes=codes)


def _run_with_fakes(runner, parser_code, sync_code):
    runner.codes.update(parser=parser_code, sync=sync_code)
    result = runner.module.main()
    expected = [("child", "parser")] if parser_code != 0 else [("child", "parser"), ("child", "sync")]
    assert [e for e in runner.events if e[0] == "child"] == expected
    assert not runner.module.LOG_FILE.exists()
    return result


def test_both_steps_succeed_returns_zero(runner):
    assert _run_with_fakes(runner, parser_code=0, sync_code=0) == 0


def test_parser_fails_returns_nonzero(runner):
    assert _run_with_fakes(runner, parser_code=1, sync_code=0) != 0


def test_sync_fails_returns_nonzero(runner):
    assert _run_with_fakes(runner, parser_code=0, sync_code=1) != 0


def test_both_fail_returns_nonzero(runner):
    assert _run_with_fakes(runner, parser_code=1, sync_code=1) != 0


def test_parser_failure_skips_sync_and_closes_the_attempt_as_error(runner):
    runner.codes.update(parser=1)
    assert runner.module.main() == 1
    assert ("finish", 42, "error") in runner.events
    assert ("start", "sync") not in runner.events
    assert ("child", "sync") not in runner.events
    assert not any(e[0] == "finish" and e[2] == "done" for e in runner.events)
    status = json.loads(runner.module.STATUS_FILE.read_text(encoding="utf-8"))
    assert status["state"] == "error" and status["step"] == "parser" and "пропущена" in status["error"]


def test_sync_failure_after_a_good_parser_run_keeps_the_parser_attempt_done(runner):
    runner.codes.update(sync=1)
    assert runner.module.main() == 1
    assert ("finish", 42, "done") in runner.events and ("finish", 50, "error") in runner.events
    status = json.loads(runner.module.STATUS_FILE.read_text(encoding="utf-8"))
    assert status["state"] == "error" and status["step"] == "sync"


def test_claim_precedes_child_and_sync_registration_precedes_parser_finish(runner):
    assert runner.module.main() == 0
    events = runner.events
    assert [e[0] for e in events[:3]] == ["admit", "claim", "child"]
    assert events.index(("start", "sync")) < events.index(("finish", 42, "done"))
    assert events.index(("finish", 42, "done")) < events.index(("child", "sync"))
    status = json.loads(runner.module.STATUS_FILE.read_text(encoding="utf-8"))
    assert status["state"] == "done" and status["run_id"] == 42


def test_denial_never_launches_child_or_overwrites_active_status(runner, monkeypatch):
    runner.module.STATUS_FILE.write_text("existing status", encoding="utf-8")
    monkeypatch.setattr(runner.module.db_runs, "admit_parser_run",
                        lambda invocation: runner.module.db_runs.Admission(False, "blocked"))
    assert runner.module.main() == 0
    assert runner.events == []
    assert runner.module.STATUS_FILE.read_text(encoding="utf-8") == "existing status"


@pytest.mark.parametrize("method", ["admit_parser_run", "claim_parser_run"])
def test_admission_or_claim_failure_blocks_paid_child(runner, monkeypatch, method):
    def broken(*args, **kwargs):
        raise runner.module.db_runs.RunStoreError("not confirmed")
    monkeypatch.setattr(runner.module.db_runs, method, broken)
    assert runner.module.main() == 1
    assert not any(e[0] == "child" for e in runner.events)
    assert not runner.module.STATUS_FILE.exists()


def github_context(monkeypatch):
    for key, value in dict(GITHUB_ACTIONS="true", GITHUB_EVENT_NAME="workflow_dispatch",
                           GITHUB_REPOSITORY="example/project", GITHUB_RUN_ID="100",
                           GITHUB_RUN_ATTEMPT="1").items():
        monkeypatch.setenv(key, value)


def test_github_uses_external_id_without_another_reservation(runner, monkeypatch):
    github_context(monkeypatch)
    monkeypatch.setenv("COLLECTION_RUN_ID", "42")
    assert runner.module.main() == 0
    assert not any(e[0] == "admit" for e in runner.events)
    assert runner.events[0][0:2] == ("claim", 42)
    assert runner.events[0][2].owner_key == "github:example/project:100:1"


@pytest.mark.parametrize("value", [None, "", "not-an-id", "-1"])
def test_github_missing_or_bad_id_does_not_self_admit(runner, monkeypatch, value):
    github_context(monkeypatch)
    if value is not None:
        monkeypatch.setenv("COLLECTION_RUN_ID", value)
    assert runner.module.main() == 1
    assert runner.events == []


def test_duplicate_external_id_blocks_second_child(runner, monkeypatch):
    github_context(monkeypatch)
    monkeypatch.setenv("COLLECTION_RUN_ID", "42")
    used = set()

    def once(run_id, invocation):
        if run_id in used:
            raise runner.module.db_runs.RunStoreError("already used")
        used.add(run_id)

    monkeypatch.setattr(runner.module.db_runs, "claim_parser_run", once)
    assert runner.module.main() == 0
    runner.events.clear()
    assert runner.module.main() == 1
    assert runner.events == []


def test_local_external_id_is_rejected(runner, monkeypatch):
    monkeypatch.setenv("COLLECTION_RUN_ID", "42")
    assert runner.module.main() == 1
    assert runner.events == []


@pytest.mark.parametrize("failure", ["sync_start", "parser_finish", "sync_finish"])
def test_history_failure_returns_error_without_fabricating_completion(runner, monkeypatch, failure):
    def broken_start(step):
        raise runner.module.db_runs.RunStoreError("start not confirmed")

    def broken_finish(run_id, status, error=None):
        if (failure == "parser_finish" and run_id == 42) or (failure == "sync_finish" and run_id == 50):
            raise runner.module.db_runs.RunStoreError("finish not confirmed")

    if failure == "sync_start":
        monkeypatch.setattr(runner.module.db_runs, "log_start", broken_start)
    else:
        monkeypatch.setattr(runner.module.db_runs, "log_finish", broken_finish)
    assert runner.module.main() == 1
    children = [e[1] for e in runner.events if e[0] == "child"]
    assert children == (["parser", "sync"] if failure == "sync_finish" else ["parser"])
    status = json.loads(runner.module.STATUS_FILE.read_text(encoding="utf-8"))
    assert status["state"] == "error"


def test_unexpected_child_exception_keeps_database_attempt_unfinished(runner, monkeypatch, capsys):
    def broken(*args):
        raise RuntimeError("PRIVATE_VALUE_DO_NOT_PRINT")
    monkeypatch.setattr(runner.module, "_run_step", broken)
    assert runner.module.main() == 1
    assert not any(e[0] == "finish" for e in runner.events)
    assert "PRIVATE_VALUE" not in capsys.readouterr().err
