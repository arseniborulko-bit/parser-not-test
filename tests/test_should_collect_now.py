"""Gate без БД, production-логов и настоящих дочерних процессов."""

import ast
from pathlib import Path

import pytest

import db_runs
import should_collect_now as gate


@pytest.fixture
def context(monkeypatch, tmp_path):
    for key, value in dict(GITHUB_ACTIONS="true", GITHUB_EVENT_NAME="schedule",
                           GITHUB_REPOSITORY="example/project", GITHUB_RUN_ID="100",
                           GITHUB_RUN_ATTEMPT="1", FORCE_COLLECTION="false",
                           DATABASE_URL="unused-test-dsn").items():
        monkeypatch.setenv(key, value)
    output = tmp_path / "github_output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))

    def forbidden(*args, **kwargs):
        raise AssertionError("Real database connection forbidden")
    monkeypatch.setattr(db_runs.psycopg2, "connect", forbidden)
    return output


@pytest.mark.parametrize("event,force", [("schedule", "false"), ("workflow_dispatch", "false"),
                                         ("workflow_dispatch", "true")])
def test_all_supported_events_use_gate(context, monkeypatch, event, force):
    calls = []
    monkeypatch.setenv("GITHUB_EVENT_NAME", event)
    monkeypatch.setenv("FORCE_COLLECTION", force)

    def admit(invocation):
        calls.append(invocation)
        return db_runs.Admission(True, "allowed", 42)

    monkeypatch.setattr(db_runs, "admit_parser_run", admit)
    assert gate.main() == 0
    assert len(calls) == 1 and calls[0].force == (force == "true")
    assert context.read_text(encoding="utf-8") == "should_run=true\nrun_id=42\n"


def test_policy_denial_is_normal_skip(context, monkeypatch):
    monkeypatch.setattr(db_runs, "admit_parser_run", lambda invocation: db_runs.Admission(False, "limit"))
    assert gate.main() == 0
    assert context.read_text(encoding="utf-8") == "should_run=false\nrun_id=\n"


@pytest.mark.parametrize("force", ["false", "true"])
def test_database_failure_is_visible_failure_and_never_permission(context, monkeypatch, force, capsys):
    monkeypatch.setenv("GITHUB_EVENT_NAME", "workflow_dispatch")
    monkeypatch.setenv("FORCE_COLLECTION", force)

    def broken(*args, **kwargs):
        raise RuntimeError("PRIVATE_DSN_DO_NOT_PRINT")

    monkeypatch.setattr(db_runs.psycopg2, "connect", broken)
    assert gate.main() == 1
    assert context.read_text(encoding="utf-8") == "should_run=false\nrun_id=\n"
    assert "PRIVATE_DSN" not in capsys.readouterr().out


@pytest.mark.parametrize("missing", ["GITHUB_ACTIONS", "GITHUB_OUTPUT"])
def test_gate_without_workflow_context_does_not_reserve(context, monkeypatch, missing):
    calls = []
    monkeypatch.delenv(missing)
    monkeypatch.setattr(db_runs, "admit_parser_run", lambda inv: calls.append(inv))
    assert gate.main() == 1
    assert calls == []
    assert not context.exists()


def test_output_failure_never_reports_success(context, monkeypatch):
    monkeypatch.setattr(db_runs, "admit_parser_run", lambda inv: db_runs.Admission(True, "allowed", 42))
    # Каталог вместо файла: исключение должно завершить gate, не разрешать сбор.
    context.mkdir()
    with pytest.raises(OSError):
        gate.main()


def test_workflow_has_no_manual_bypass():
    workflow = (Path(__file__).resolve().parents[1] / ".github/workflows/collect.yml").read_text(encoding="utf-8")
    assert "if: github.event_name" not in workflow
    steps = workflow.split("\n      - ")[1:]
    gated = "if: steps.gate.outputs.should_run == 'true'"
    paid_markers = ("python run_parser_and_sync.py", "GOOGLE_SERVICE_ACCOUNT_JSON", "pip install -r requirements.txt")
    paid_steps = [step for step in steps if any(marker in step for marker in paid_markers)]
    assert len(paid_steps) == 3 and all(gated in step for step in paid_steps)
    gate_step = next(step for step in steps if "python should_collect_now.py" in step)
    assert "if:" not in gate_step
    assert "COLLECTION_RUN_ID: ${{ steps.gate.outputs.run_id }}" in workflow
    assert "default: false" in workflow


@pytest.mark.parametrize("filename", ["run_parser_and_sync.py", "should_collect_now.py"])
def test_dotenv_only_in_cli_entrypoint_and_exit_code_propagated(filename):
    tree = ast.parse((Path(__file__).resolve().parents[1] / filename).read_text(encoding="utf-8"))
    # Статическая проверка, не запуск настоящего скрипта/парсера.
    top_calls = [node.value for node in tree.body if isinstance(node, ast.Expr) and isinstance(node.value, ast.Call)]
    assert not any(isinstance(c.func, ast.Name) and c.func.id == "load_dotenv" for c in top_calls)
    entrypoint = tree.body[-1]
    assert isinstance(entrypoint, ast.If)
    assert ast.unparse(entrypoint.test) == "__name__ == '__main__'"
    assert ast.unparse(entrypoint.body[-1]) == "sys.exit(main())"
