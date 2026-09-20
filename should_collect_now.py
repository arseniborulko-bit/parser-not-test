"""Gate GitHub Actions: разрешение только после подтверждённой регистрации в БД."""

from __future__ import annotations

import os
import sys

from dotenv import load_dotenv

import db_runs


def _set_output(decision: db_runs.Admission) -> None:
    print(decision.reason)
    output_file = os.environ.get("GITHUB_OUTPUT")
    if output_file:
        with open(output_file, "a", encoding="utf-8") as f:
            f.write(f"should_run={'true' if decision.should_run else 'false'}\n")
            f.write(f"run_id={decision.run_id if decision.should_run else ''}\n")


def main() -> int:
    # Без следующего шага-потребителя не создаём зависшее разрешение.
    if os.environ.get("GITHUB_ACTIONS") != "true" or not os.environ.get("GITHUB_OUTPUT"):
        print("Gate предназначен для GitHub Actions; локально используйте run_parser_and_sync.py.",
              file=sys.stderr)
        return 1
    try:
        invocation = db_runs.invocation_from_environment(os.environ)
        decision = db_runs.admit_parser_run(invocation)
        _set_output(decision)
        return 0
    except db_runs.RunStoreError as exc:
        _set_output(db_runs.Admission(False, str(exc)))
        # Сбой защиты — ошибка, а не зелёный штатный пропуск.
        return 1


if __name__ == "__main__":
    load_dotenv()
    sys.exit(main())
