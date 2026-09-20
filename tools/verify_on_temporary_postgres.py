"""Проверка SQL и защиты сбора на НАСТОЯЩЕМ временном Postgres. Боевая база не используется и .env не читается.

Что проверяется: схема и миграции 001–003, расписание, «собирали сегодня» по киевским суткам, пользователи
дашборда, пары ASIN (план, добавление, отключение, журнал, гонка, запросы дашборда) и защита от повторных
платных запусков (гонка допусков, одноразовый run_id, лимит попыток, миграция на «старой» таблице, сам
gate-скрипт как его запускает GitHub Actions).

Запуск (Windows; колесо pgserver есть для Python 3.12):
    py -3.12 -m venv .venv-pg
    .venv-pg\\Scripts\\pip install pgserver psycopg2-binary python-dotenv tzdata
    .venv-pg\\Scripts\\python tools\\verify_on_temporary_postgres.py
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import threading
import uuid
from datetime import datetime
from pathlib import Path

import pgserver
import psycopg2

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))

WORK = Path(tempfile.mkdtemp(prefix="verify_pg_"))
server = None
checks: list[tuple[str, bool]] = []


def ensure_timezone_data() -> None:
    """В колесе pgserver нет данных часовых поясов — берём их из пакета tzdata."""
    import tzdata

    target = Path(pgserver.__file__).parent / "pginstall" / "share" / "postgresql" / "timezone"
    if not target.exists():
        shutil.copytree(Path(tzdata.__file__).parent / "zoneinfo", target)


ensure_timezone_data()
server = pgserver.get_server(WORK / "data", cleanup_mode="delete")
URI = server.get_uri()
os.environ["DATABASE_URL"] = URI

import access  # noqa: E402
import db_runs  # noqa: E402
import pairs_store  # noqa: E402
import schedule_store  # noqa: E402

NEW_SCHEMA = (PROJECT / "schema.sql").read_text(encoding="utf-8")
MIGRATIONS = {n: (PROJECT / "migrations" / n).read_text(encoding="utf-8")
              for n in ("001_collection_admission.sql", "002_dashboard_users.sql", "003_pair_changes.sql")}


def check(name: str, condition: object, extra: str = "") -> None:
    checks.append((name, bool(condition)))
    print(("OK   " if condition else "FAIL ") + name + (f"  [{extra}]" if extra else ""))


def q(sql: str, params: tuple = ()):
    conn = psycopg2.connect(URI)
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            return cur.fetchall() if cur.description else None
    finally:
        conn.close()


def connect():
    return psycopg2.connect(URI)


def reset(schema_sql: str = NEW_SCHEMA, schedule: tuple | None = (0, 0), migrations: bool = True) -> None:
    q("DROP SCHEMA IF EXISTS parser_not_test CASCADE;")
    q("CREATE SCHEMA parser_not_test;")
    q(schema_sql)
    if migrations:
        for sql in MIGRATIONS.values():
            q(sql)
    if schedule:
        q("INSERT INTO parser_not_test.schedule (id, hour, minute) VALUES (1, %s, %s);", schedule)


def reset_old() -> None:
    """Схема как на боевой до миграции 001: текущая схема без колонок owner_key и claimed_at."""
    reset(migrations=False)
    q("ALTER TABLE parser_not_test.collection_runs DROP COLUMN owner_key, DROP COLUMN claimed_at;")


def insert_run(step, status, started, finished=None, owner_key=None, claimed=None, source="github_actions", new_columns=True):
    if new_columns:
        q("INSERT INTO parser_not_test.collection_runs (source, step, status, started_at, finished_at, owner_key, claimed_at) "
          "VALUES (%s, %s, %s, %s, %s, %s, %s);", (source, step, status, started, finished, owner_key, claimed))
    else:
        q("INSERT INTO parser_not_test.collection_runs (source, step, status, started_at, finished_at) "
          "VALUES (%s, %s, %s, %s, %s);", (source, step, status, started, finished))


def count(where: str = "TRUE") -> int:
    return q(f"SELECT count(*) FROM parser_not_test.collection_runs WHERE {where};")[0][0]


def gh(run, attempt=1, force=False):
    return db_runs.Invocation("github_actions", f"github:owner/repo:{run}:{attempt}", force)


def local():
    return db_runs.Invocation("local", f"local:{uuid.uuid4().hex}")


def race(func, args):
    barrier = threading.Barrier(len(args))
    out = [None] * len(args)

    def worker(i, arg):
        barrier.wait()
        try:
            out[i] = ("ok", func(arg))
        except Exception as exc:  # noqa: BLE001
            out[i] = ("err", exc)

    threads = [threading.Thread(target=worker, args=(i, a)) for i, a in enumerate(args)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return out


def kyiv(*parts):
    return datetime(*parts, tzinfo=schedule_store.TZ)


def section_schedule_and_users() -> None:
    print("\n== расписание и пользователи дашборда ==")
    editor = dict(actor_role=access.ROLE_EDITOR, actor="Аня")
    reset()
    q("DELETE FROM parser_not_test.schedule;")
    ov = schedule_store.load_overview(connect, kyiv(2026, 9, 21, 8, 0))
    check("пустая база: расписания нет, сегодня не собирали, запусков нет", ov.schedule is None and not ov.collected_today and ov.runs == [])
    schedule_store.save_schedule(connect, 9, 0, True, **editor)
    check("включение записывает время", schedule_store.load_overview(connect, kyiv(2026, 9, 21, 8, 0)).schedule == schedule_store.Schedule(9, 0))
    schedule_store.save_schedule(connect, 10, 30, True, **editor)
    check("повторное сохранение обновляет ту же единственную строку", q("SELECT count(*), min(hour), min(minute) FROM parser_not_test.schedule;")[0] == (1, 10, 30))
    schedule_store.save_schedule(connect, 10, 30, False, **editor)
    check("выключение удаляет строку", schedule_store.load_overview(connect, kyiv(2026, 9, 21, 8, 0)).schedule is None)
    schedule_store.save_schedule(connect, 9, 0, False, **editor)
    check("выключение без строки не падает", True)

    q("INSERT INTO parser_not_test.collection_runs (source, step, status, started_at, finished_at) VALUES "
      "('github_actions','parser','done','2026-09-20 22:30:00+00','2026-09-20 22:42:00+00'),"
      "('github_actions','sync','done','2026-09-20 22:43:00+00','2026-09-20 22:44:00+00'),"
      "('github_actions','parser','error','2026-09-19 06:00:00+00','2026-09-19 06:05:00+00'),"
      "('github_actions','parser','running','2026-09-22 06:00:00+00',NULL);")
    check("сбор в 22:30 UTC 20.09 — это уже 21.09 по Киеву", schedule_store.load_overview(connect, kyiv(2026, 9, 21, 8, 0)).collected_today)
    check("тот же сбор не считается сбором 20.09 по Киеву", not schedule_store.load_overview(connect, kyiv(2026, 9, 20, 12, 0)).collected_today)
    check("сбор с ошибкой не считается успешным", not schedule_store.load_overview(connect, kyiv(2026, 9, 19, 12, 0)).collected_today)
    runs = schedule_store.load_overview(connect, kyiv(2026, 9, 22, 12, 0)).runs
    check("последние запуски: только parser, новые сверху", [r["status"] for r in runs] == ["running", "done", "error"])
    check("незавершённый запуск без finished_at, время с поясом", runs[0]["finished_at"] is None and runs[1]["started_at"].tzinfo is not None)

    admin = dict(actor_role=access.ROLE_ADMIN, actor_email="boss@example.com")
    check("пользователей нет", access.list_users(connect) == [])
    check("добавление нормализует email", access.add_user(connect, " Test@Example.COM ", access.ROLE_EDITOR, **admin) == "test@example.com")
    access.add_user(connect, "test@example.com", access.ROLE_ADMIN, **admin)
    check("повторное добавление обновляет роль без дубля", access.active_user_roles(connect) == {"test@example.com": "admin"} and len(access.list_users(connect)) == 1)
    access.set_user_active(connect, "test@example.com", False, **admin)
    check("отключённый не попадает в активные", access.active_user_roles(connect) == {})
    for label, stmt, params in (
        ("CHECK: email только строчными", "INSERT INTO parser_not_test.dashboard_users (email, role) VALUES (%s, %s);", ("UPPER@EXAMPLE.COM", "editor")),
        ("CHECK: роль только admin/editor", "INSERT INTO parser_not_test.dashboard_users (email, role) VALUES (%s, %s);", ("x@example.com", "superuser")),
        ("CHECK: в расписании допустима только строка id=1", "INSERT INTO parser_not_test.schedule (id, hour, minute) VALUES (2, 9, 0);", ()),
    ):
        try:
            q(stmt, params)
            check(label, False)
        except psycopg2.errors.CheckViolation:
            check(label, True)


def dashboard_sql(function_name: str) -> str:
    """SQL из dashboard_db.py (первый аргумент pd.read_sql), чтобы проверять именно тот запрос, что в дашборде."""
    import ast

    tree = ast.parse((PROJECT / "dashboard_db.py").read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == function_name:
            for call in ast.walk(node):
                if isinstance(call, ast.Call) and getattr(call.func, "attr", "") == "read_sql":
                    return call.args[0].value
    raise LookupError(function_name)


def section_pairs() -> None:
    print("\n== пары ASIN ==")
    ours, mine = "B0OURASIN1", dict(actor_role=access.ROLE_EDITOR, actor="Аня")
    reset()
    q("INSERT INTO parser_not_test.competitor_pairs (marketplace, our_asin, our_product, comp_asin, competitor_name, active) VALUES "
      "('US', %s, 'Our product', 'B0COMPAAA1', 'Comp A1', TRUE),"
      "('US', %s, 'Our product', 'B0COMPAAA2', '', FALSE),"
      "('CA', 'B0OTHERAA1', 'Other', 'B0COMPBBB1', 'Comp B1', TRUE);", (ours, ours))

    plan = pairs_store.make_plan(connect, ours, None, "B0COMPAAA1 B0COMPAAA2 B0COMPAAA3 https://www.amazon.com/dp/B0COMPAAA4 junk")
    check("план на настоящей базе: новые, возвращаемые и уже активные определены верно",
          plan.market == "US" and plan.our_product == "Our product" and sorted(plan.to_add) == ["B0COMPAAA3", "B0COMPAAA4"]
          and plan.to_enable == ["B0COMPAAA2"] and plan.already == ["B0COMPAAA1"] and plan.invalid == ["junk"], str(plan.errors))
    check("«в каждый сбор добавится ASIN»: считаются только ASIN, которых ещё нет среди активных", plan.new_asins == 3)
    check("план ничего не записал", q("SELECT count(*) FROM parser_not_test.competitor_pairs;")[0][0] == 3
          and q("SELECT count(*) FROM parser_not_test.pair_changes;")[0][0] == 0)

    result = pairs_store.apply_plan(connect, plan, **mine)
    check("применение: 2 добавлено, 1 возвращено", result == {"add": 2, "enable": 1}, str(result))
    log = q("SELECT action, actor, marketplace, our_asin FROM parser_not_test.pair_changes ORDER BY id;")
    check("журнал записан в той же транзакции: кто, что, где", sorted(a for a, *_ in log) == ["add", "add", "enable"]
          and {row[1] for row in log} == {"Аня"} and {row[2] for row in log} == {"US"})
    check("новые пары активны и получили название нашего товара",
          q("SELECT count(*) FROM parser_not_test.competitor_pairs WHERE active AND our_product = 'Our product' AND our_asin = %s;", (ours,))[0][0] == 4)
    again = pairs_store.apply_plan(connect, pairs_store.make_plan(connect, ours, None, "B0COMPAAA3 B0COMPAAA2"), **mine)
    check("повторное добавление ничего не меняет и не пишет в журнал", again == {"add": 0, "enable": 0}
          and q("SELECT count(*) FROM parser_not_test.pair_changes;")[0][0] == 3)

    keys = [("US", ours, "B0COMPAAA3"), ("US", ours, "B0COMPAAA4")]
    check("отключение двух пар", pairs_store.set_pairs_active(connect, keys, False, **mine) == 2)
    check("повторное отключение ничего не меняет", pairs_store.set_pairs_active(connect, keys, False, **mine) == 0)
    check("отключённые пары остались в таблице (история цела)", q("SELECT count(*) FROM parser_not_test.competitor_pairs WHERE NOT active;")[0][0] == 2)
    plan2 = pairs_store.make_plan(connect, ours, None, "B0COMPAAA3")
    check("отключённую пару можно вернуть добавлением: это «возврат», а не «новая»", plan2.to_enable == ["B0COMPAAA3"] and not plan2.to_add)
    pairs_store.apply_plan(connect, plan2, **mine)
    check("возврат записан в журнал как enable, не add",
          q("SELECT action FROM parser_not_test.pair_changes ORDER BY id DESC LIMIT 1;")[0][0] == "enable")
    check("последние изменения читаются, новые сверху", pairs_store.recent_changes(connect, 3)[0]["action"] == "enable")

    fresh = "B0COMPNEW1"
    race_plan = pairs_store.make_plan(connect, ours, None, fresh)
    out = race(lambda _: pairs_store.apply_plan(connect, race_plan, **mine), list(range(12)))
    total = sum(r["add"] + r["enable"] for kind, r in out if kind == "ok")
    check("12 одновременных добавлений одной пары: в базе одна строка, в журнале одна запись",
          not [e for kind, e in out if kind == "err"] and total == 1
          and q("SELECT count(*) FROM parser_not_test.competitor_pairs WHERE comp_asin = %s;", (fresh,))[0][0] == 1
          and q("SELECT count(*) FROM parser_not_test.pair_changes WHERE comp_asin = %s;", (fresh,))[0][0] == 1)

    try:
        q("INSERT INTO parser_not_test.pair_changes (actor, action, marketplace, our_asin, comp_asin) VALUES ('x', 'delete', 'US', 'a', 'b');")
        check("CHECK: в журнале допустимы только add/enable/disable", False)
    except psycopg2.errors.CheckViolation:
        check("CHECK: в журнале допустимы только add/enable/disable", True)

    # Запросы дашборда — ровно те, что лежат в dashboard_db.py.
    q("INSERT INTO parser_not_test.snapshots (snapshot_date, marketplace, our_asin, our_product, comp_asin, competitor_name, our_bsr, comp_bsr) VALUES "
      "('2026-09-20', 'US', %s, 'Our title', 'B0COMPAAA1', 'Scraped A1', 100, 200),"
      "('2026-09-21', 'US', %s, 'Our title', 'B0COMPAAA1', 'Scraped A1', 101, 201),"
      "('2026-09-21', 'US', %s, 'Our title', 'B0COMPAAA2', 'Scraped A2', 102, 202),"
      "('2026-09-21', 'US', %s, 'Our title', 'B0COMPAAA4', 'Scraped A4', 103, 203);", (ours, ours, ours, ours))
    current = q(dashboard_sql("load_current"))
    got = sorted((row[3], row[8]) for row in current)
    check("«Текущее состояние»: только активные пары и только последний снимок каждой",
          got == [(ours, "B0COMPAAA1"), (ours, "B0COMPAAA2")] and {row[0].isoformat() for row in current} == {"2026-09-21"}, str(got))
    listing = {row[3]: row for row in q(dashboard_sql("load_competitor_pairs"))}
    check("список пар: пустое название подставляется из последнего снимка, заданное остаётся",
          listing["B0COMPAAA2"][4] == "Scraped A2" and listing["B0COMPAAA1"][4] == "Comp A1" and listing["B0COMPAAA1"][2] == "Our product")
    check("список пар: пара без снимков не пропадает и не даёт пустых значений NULL", listing["B0COMPNEW1"][4] == "" and listing["B0COMPNEW1"][5] is True)
    check("список пар содержит и активные, и отключённые", {row[5] for row in listing.values()} == {True, False})

    reset_old()
    q(MIGRATIONS["003_pair_changes.sql"])
    q(MIGRATIONS["003_pair_changes.sql"])
    check("миграция 003 на «старой» схеме создаёт журнал и индекс и безопасна при повторе",
          q("SELECT count(*) FROM pg_indexes WHERE schemaname = 'parser_not_test' AND indexname IN ('pair_changes_at_idx', 'snapshots_pair_date_idx');")[0][0] == 2
          and q("SELECT to_regclass('parser_not_test.pair_changes') IS NOT NULL;")[0][0])


def section_admission() -> None:
    print("\n== защита от повторных платных запусков ==")
    start = q("SELECT (((now() AT TIME ZONE 'Europe/Kyiv')::date)::timestamp AT TIME ZONE 'Europe/Kyiv');")[0][0]

    reset()
    n = 16
    out = race(db_runs.admit_parser_run, [local() for _ in range(n)])
    errors = [r for kind, r in out if kind == "err"]
    granted = [r for kind, r in out if kind == "ok" and r.should_run]
    denied = [r for kind, r in out if kind == "ok" and not r.should_run]
    check(f"{n} одновременных допусков: разрешение получает ровно один", len(granted) == 1, f"разрешено {len(granted)}, отказов {len(denied)}, ошибок {len(errors)}")
    check("в таблице ровно одна запись running", count("status = 'running' AND step = 'parser'") == 1)
    check("остальные получили отказ «незавершённая попытка», а не ошибку", not errors and all("незавершённая" in r.reason for r in denied))

    reset()
    inv = gh(301)
    decision = db_runs.admit_parser_run(inv)
    out = race(lambda _: db_runs.claim_parser_run(decision.run_id, inv), list(range(n)))
    check(f"{n} одновременных claim одного run_id: успех только один", sum(kind == "ok" for kind, _ in out) == 1)
    try:
        db_runs.claim_parser_run(decision.run_id, inv)
        check("повторный claim отклонён", False)
    except db_runs.RunStoreError:
        check("повторный claim отклонён", True)

    reset()
    decision = db_runs.admit_parser_run(gh(302))
    for label, foreign in (("другой запуск workflow", gh(999)), ("другая попытка того же запуска", gh(302, attempt=2)), ("локальный владелец", local())):
        try:
            db_runs.claim_parser_run(decision.run_id, foreign)
            check(f"чужой владелец не занимает пропуск ({label})", False)
        except db_runs.RunStoreError:
            check(f"чужой владелец не занимает пропуск ({label})", True)
    db_runs.claim_parser_run(decision.run_id, gh(302))
    check("настоящий владелец занимает пропуск после чужих попыток", True)

    reset()
    for run in (401, 402, 403):
        d = db_runs.admit_parser_run(gh(run))
        db_runs.claim_parser_run(d.run_id, gh(run))
        db_runs.log_finish(d.run_id, "error", "проверка")
    fourth = db_runs.admit_parser_run(gh(404))
    check("после 3 неудачных попыток четвёртая обычная блокируется", not fourth.should_run and "Лимит" in fourth.reason, fourth.reason)
    check("force снимает только дневной лимит", db_runs.admit_parser_run(gh(405, force=True)).should_run)

    reset()
    d = db_runs.admit_parser_run(gh(501))
    db_runs.claim_parser_run(d.run_id, gh(501))
    db_runs.log_finish(d.run_id, "done")
    check("после успешного сбора отклонены и обычный допуск, и force",
          not db_runs.admit_parser_run(gh(502)).should_run and not db_runs.admit_parser_run(gh(503, force=True)).should_run)

    for step in ("parser", "sync"):
        reset()
        insert_run(step, "running", q("SELECT now() - interval '3 days';")[0][0], owner_key="github:owner/repo:1:1")
        d, d_force = db_runs.admit_parser_run(gh(601)), db_runs.admit_parser_run(gh(602, force=True))
        check(f"running шага {step} трёхдневной давности блокирует допуск (в т.ч. force)", not d.should_run and not d_force.should_run, d.reason)

    reset()
    for _ in range(3):
        insert_run("parser", "error", start + q("SELECT interval '30 minutes';")[0][0], start + q("SELECT interval '40 minutes';")[0][0])
    check("3 попытки в 00:30 по Киеву сегодня — уже сегодняшний лимит", not db_runs.admit_parser_run(gh(701)).should_run)
    reset()
    for _ in range(3):
        insert_run("parser", "error", start - q("SELECT interval '30 minutes';")[0][0], start - q("SELECT interval '20 minutes';")[0][0])
    check("3 попытки в 23:30 по Киеву вчера не считаются сегодняшними", db_runs.admit_parser_run(gh(702)).should_run)

    reset(schedule=None)
    check("нет строки расписания — допуск отклонён", "не задано" in db_runs.admit_parser_run(gh(801)).reason)
    now_k = q("SELECT now() AT TIME ZONE 'Europe/Kyiv';")[0][0]
    if (now_k.hour, now_k.minute) < (23, 58):
        reset(schedule=(23, 59))
        check("время сбора не наступило — отклонены и обычный допуск, и force",
              not db_runs.admit_parser_run(gh(802)).should_run and not db_runs.admit_parser_run(gh(803, force=True)).should_run)

    reset()
    d = db_runs.admit_parser_run(gh(901))
    for label, call in (("завершить parser без claim", lambda: db_runs.log_finish(d.run_id, "done")),
                        ("некорректный статус", lambda: db_runs.log_finish(d.run_id, "weird")),
                        ("несуществующий id", lambda: db_runs.log_finish(987654, "done")),
                        ("log_start для parser", lambda: db_runs.log_start("parser"))):
        try:
            call()
            check(f"нельзя: {label}", False)
        except db_runs.RunStoreError:
            check(f"нельзя: {label}", True)
    db_runs.claim_parser_run(d.run_id, gh(901))
    sync_id = db_runs.log_start("sync")
    db_runs.log_finish(d.run_id, "done")
    db_runs.log_finish(sync_id, "done")
    try:
        db_runs.log_finish(d.run_id, "error", "повторно")
        check("нельзя закрыть запись второй раз", False)
    except db_runs.RunStoreError:
        check("нельзя закрыть запись второй раз", True)
    check("полный путь: parser и sync закрыты как done", count("status = 'done'") == 2 and count("status = 'running'") == 0)

    reset_old()
    q(MIGRATIONS["002_dashboard_users.sql"])
    try:
        db_runs.admit_parser_run(gh(1001))
        check("новый код на немигрированной базе отказывает, а не работает без защиты", False)
    except db_runs.RunStoreError:
        check("новый код на немигрированной базе отказывает, а не работает без защиты", count() == 0)
    insert_run("parser", "done", q("SELECT now() - interval '9 days';")[0][0], q("SELECT now() - interval '9 days' + interval '10 minutes';")[0][0], new_columns=False)
    insert_run("sync", "running", q("SELECT now() - interval '2 days';")[0][0], None, new_columns=False)
    snapshot = "SELECT id, source, step, status, started_at, finished_at, error FROM parser_not_test.collection_runs ORDER BY id;"
    before = q(snapshot)
    q(MIGRATIONS["001_collection_admission.sql"])
    q(MIGRATIONS["001_collection_admission.sql"])
    check("миграция 001 не меняет существующие строки и безопасна при повторе", before == q(snapshot))
    d = db_runs.admit_parser_run(gh(1002))
    check("старая зависшая running продолжает блокировать после миграции", not d.should_run and "незавершённая" in d.reason)

    copy = WORK / "gate_copy"
    copy.mkdir()
    for name in ("should_collect_now.py", "db_runs.py"):
        shutil.copy(PROJECT / name, copy / name)

    def run_gate(run_id, event="schedule", url=None, extra=None):
        out_file = copy / f"out_{run_id}.txt"
        env = {k: v for k, v in os.environ.items() if k != "DATABASE_URL"}
        env.update(DATABASE_URL=url or URI, GITHUB_ACTIONS="true", GITHUB_EVENT_NAME=event, GITHUB_REPOSITORY="owner/repo",
                   GITHUB_RUN_ID=str(run_id), GITHUB_RUN_ATTEMPT="1", GITHUB_OUTPUT=str(out_file))
        env.update(extra or {})
        result = subprocess.run([sys.executable, "should_collect_now.py"], cwd=copy, env=env, capture_output=True, encoding="utf-8")
        text = out_file.read_text(encoding="utf-8") if out_file.exists() else ""
        return result.returncode, dict(line.split("=", 1) for line in text.splitlines() if "=" in line)

    reset()
    code, out = run_gate(1101)
    check("gate-скрипт: первый запуск разрешает сбор и отдаёт run_id", code == 0 and out.get("should_run") == "true" and out.get("run_id", "").isdigit())
    code, out = run_gate(1102)
    check("gate-скрипт: второй запуск при running отказывает (код 0, пустой run_id)", code == 0 and out.get("should_run") == "false" and out.get("run_id") == "")
    code, out = run_gate(1103, url="postgresql://127.0.0.1:9/none")
    check("gate-скрипт: база недоступна — should_run=false и ненулевой код", code != 0 and out.get("should_run") == "false")
    code, out = run_gate(1104, extra={"FORCE_COLLECTION": "true"})
    check("gate-скрипт: force вне ручного запуска запрещён", code != 0 and out.get("should_run") == "false")
    code, out = run_gate(1105, event="pull_request")
    check("gate-скрипт: неизвестное событие не даёт сбор", code != 0 and out.get("should_run") == "false")
    check("за все проверки gate создана ровно одна запись", count() == 1)


try:
    section_schedule_and_users()
    section_pairs()
    section_admission()
finally:
    failed = [name for name, ok in checks if not ok]
    print(f"\nитого: {len(checks) - len(failed)} из {len(checks)} проверок прошли")
    for name in failed:
        print("  ПРОВАЛ:", name)
    server.cleanup()
    shutil.rmtree(WORK, ignore_errors=True)
sys.exit(1 if failed else 0)
