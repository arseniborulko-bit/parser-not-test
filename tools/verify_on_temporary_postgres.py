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
from decimal import Decimal
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
import asins_store  # noqa: E402
import db_runs  # noqa: E402
import pairs_store  # noqa: E402
import schedule_store  # noqa: E402

NEW_SCHEMA = (PROJECT / "schema.sql").read_text(encoding="utf-8")
MIGRATIONS = {n: (PROJECT / "migrations" / n).read_text(encoding="utf-8")
              for n in ("001_collection_admission.sql", "002_dashboard_users.sql", "003_pair_changes.sql",
                        "004_snapshot_images.sql", "005_run_scope.sql",
                        "006_snapshot_marketplace_key.sql", "007_pair_changes_edit.sql",
                        "008_asin_registry.sql", "009_snapshot_rating_reviews.sql")}


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
    q("DROP SCHEMA IF EXISTS bsr_radar CASCADE;")
    q("CREATE SCHEMA bsr_radar;")
    q(schema_sql)
    if migrations:
        for sql in MIGRATIONS.values():
            q(sql)
    if schedule:
        q("INSERT INTO bsr_radar.schedule (id, hour, minute) VALUES (1, %s, %s);", schedule)


def reset_old() -> None:
    """Схема как на боевой до миграции 001: текущая схема без колонок owner_key и claimed_at."""
    reset(migrations=False)
    q("ALTER TABLE bsr_radar.collection_runs DROP COLUMN owner_key, DROP COLUMN claimed_at;")


def insert_run(step, status, started, finished=None, owner_key=None, claimed=None, source="github_actions", new_columns=True, scope="all"):
    if new_columns:
        q("INSERT INTO bsr_radar.collection_runs (source, step, status, started_at, finished_at, owner_key, claimed_at, scope) "
          "VALUES (%s, %s, %s, %s, %s, %s, %s, %s);", (source, step, status, started, finished, owner_key, claimed, scope))
    else:
        q("INSERT INTO bsr_radar.collection_runs (source, step, status, started_at, finished_at) "
          "VALUES (%s, %s, %s, %s, %s);", (source, step, status, started, finished))


def count(where: str = "TRUE") -> int:
    return q(f"SELECT count(*) FROM bsr_radar.collection_runs WHERE {where};")[0][0]


def gh(run, attempt=1, force=False, scope="all"):
    return db_runs.Invocation("github_actions", f"github:owner/repo:{run}:{attempt}", force, scope)


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
    q("DELETE FROM bsr_radar.schedule;")
    ov = schedule_store.load_overview(connect, kyiv(2026, 9, 21, 8, 0))
    check("пустая база: расписания нет, сегодня не собирали, запусков нет", ov.schedule is None and not ov.collected_today and ov.runs == [])
    schedule_store.save_schedule(connect, 9, 0, True, **editor)
    check("включение записывает время", schedule_store.load_overview(connect, kyiv(2026, 9, 21, 8, 0)).schedule == schedule_store.Schedule(9, 0))
    schedule_store.save_schedule(connect, 10, 30, True, **editor)
    check("повторное сохранение обновляет ту же единственную строку", q("SELECT count(*), min(hour), min(minute) FROM bsr_radar.schedule;")[0] == (1, 10, 30))
    schedule_store.save_schedule(connect, 10, 30, False, **editor)
    check("выключение удаляет строку", schedule_store.load_overview(connect, kyiv(2026, 9, 21, 8, 0)).schedule is None)
    schedule_store.save_schedule(connect, 9, 0, False, **editor)
    check("выключение без строки не падает", True)

    q("INSERT INTO bsr_radar.collection_runs (source, step, status, started_at, finished_at) VALUES "
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
        ("CHECK: email только строчными", "INSERT INTO bsr_radar.dashboard_users (email, role) VALUES (%s, %s);", ("UPPER@EXAMPLE.COM", "editor")),
        ("CHECK: роль только admin/editor", "INSERT INTO bsr_radar.dashboard_users (email, role) VALUES (%s, %s);", ("x@example.com", "superuser")),
        ("CHECK: в расписании допустима только строка id=1", "INSERT INTO bsr_radar.schedule (id, hour, minute) VALUES (2, 9, 0);", ()),
    ):
        try:
            q(stmt, params)
            check(label, False)
        except psycopg2.errors.CheckViolation:
            check(label, True)


def dashboard_sql(function_name: str, extra: str = "") -> str:
    """SQL из dashboard_db.py (первый аргумент pd.read_sql), чтобы проверять именно тот запрос, что в дашборде.

    Запросы стали f-строками: необязательные столбцы (фото, рейтинг) подставляются по тому, что
    реально есть в базе. Подставляем вместо этих вставок то, что передали в extra, — иначе
    проверялся бы не тот запрос, который выполняет дашборд."""
    import ast

    tree = ast.parse((PROJECT / "dashboard_db.py").read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == function_name:
            for call in ast.walk(node):
                if isinstance(call, ast.Call) and getattr(call.func, "attr", "") == "read_sql":
                    argument = call.args[0]
                    if isinstance(argument, ast.Constant):
                        return argument.value
                    if isinstance(argument, ast.JoinedStr):
                        return "".join(
                            part.value if isinstance(part, ast.Constant) else extra
                            for part in argument.values
                        )
    raise LookupError(function_name)


def section_pairs() -> None:
    print("\n== пары ASIN ==")
    ours, mine = "B0OURASIN1", dict(actor_role=access.ROLE_EDITOR, actor="Аня")

    def seed():
        q("INSERT INTO bsr_radar.competitor_pairs (marketplace, our_asin, our_product, comp_asin, competitor_name, active) VALUES "
          "('US', %s, 'Our product', 'B0COMPAAA1', 'Comp A1', TRUE),"
          "('US', %s, 'Our product', 'B0COMPAAA2', '', FALSE),"
          "('CA', 'B0OTHERAA1', 'Other', 'B0COMPBBB1', 'Comp B1', TRUE);", (ours, ours))

    # Сначала как сейчас на боевой: таблицы журнала ещё нет — изменения всё равно обязаны применяться.
    reset(migrations=False)
    q(MIGRATIONS["001_collection_admission.sql"])
    q(MIGRATIONS["002_dashboard_users.sql"])
    seed()
    check("без таблицы журнала journal_exists = ложь", pairs_store.journal_exists(connect) is False)
    early = pairs_store.apply_plan(connect, pairs_store.make_plan(connect, ours, None, "B0COMPAAA2 B0COMPAAA3"), **mine)
    check("без журнала пары добавляются и возвращаются", early == {"add": 1, "enable": 1}, str(early))
    check("без журнала пары отключаются", pairs_store.set_pairs_active(connect, [("US", ours, "B0COMPAAA3")], False, **mine) == 1
          and q("SELECT count(*) FROM bsr_radar.competitor_pairs WHERE active;")[0][0] == 3)
    q(MIGRATIONS["003_pair_changes.sql"])
    check("после миграции журнал подключается сам", pairs_store.journal_exists(connect) is True)

    reset()
    seed()

    plan = pairs_store.make_plan(connect, ours, None, "B0COMPAAA1 B0COMPAAA2 B0COMPAAA3 https://www.amazon.com/dp/B0COMPAAA4 junk")
    check("план на настоящей базе: новые, возвращаемые и уже активные определены верно",
          plan.market == "US" and plan.our_product == "Our product" and sorted(plan.to_add) == ["B0COMPAAA3", "B0COMPAAA4"]
          and plan.to_enable == ["B0COMPAAA2"] and plan.already == ["B0COMPAAA1"] and plan.invalid == ["junk"], str(plan.errors))
    check("«в каждый сбор добавится ASIN»: считаются только ASIN, которых ещё нет среди активных", plan.new_asins == 3)
    check("план ничего не записал", q("SELECT count(*) FROM bsr_radar.competitor_pairs;")[0][0] == 3
          and q("SELECT count(*) FROM bsr_radar.pair_changes;")[0][0] == 0)

    result = pairs_store.apply_plan(connect, plan, **mine)
    check("применение: 2 добавлено, 1 возвращено", result == {"add": 2, "enable": 1}, str(result))
    log = q("SELECT action, actor, marketplace, our_asin FROM bsr_radar.pair_changes ORDER BY id;")
    check("журнал записан в той же транзакции: кто, что, где", sorted(a for a, *_ in log) == ["add", "add", "enable"]
          and {row[1] for row in log} == {"Аня"} and {row[2] for row in log} == {"US"})
    check("новые пары активны и получили название нашего товара",
          q("SELECT count(*) FROM bsr_radar.competitor_pairs WHERE active AND our_product = 'Our product' AND our_asin = %s;", (ours,))[0][0] == 4)
    again = pairs_store.apply_plan(connect, pairs_store.make_plan(connect, ours, None, "B0COMPAAA3 B0COMPAAA2"), **mine)
    check("повторное добавление ничего не меняет и не пишет в журнал", again == {"add": 0, "enable": 0}
          and q("SELECT count(*) FROM bsr_radar.pair_changes;")[0][0] == 3)

    keys = [("US", ours, "B0COMPAAA3"), ("US", ours, "B0COMPAAA4")]
    check("отключение двух пар", pairs_store.set_pairs_active(connect, keys, False, **mine) == 2)
    check("повторное отключение ничего не меняет", pairs_store.set_pairs_active(connect, keys, False, **mine) == 0)
    check("отключённые пары остались в таблице (история цела)", q("SELECT count(*) FROM bsr_radar.competitor_pairs WHERE NOT active;")[0][0] == 2)
    plan2 = pairs_store.make_plan(connect, ours, None, "B0COMPAAA3")
    check("отключённую пару можно вернуть добавлением: это «возврат», а не «новая»", plan2.to_enable == ["B0COMPAAA3"] and not plan2.to_add)
    pairs_store.apply_plan(connect, plan2, **mine)
    check("возврат записан в журнал как enable, не add",
          q("SELECT action FROM bsr_radar.pair_changes ORDER BY id DESC LIMIT 1;")[0][0] == "enable")
    check("последние изменения читаются, новые сверху", pairs_store.recent_changes(connect, 3)[0]["action"] == "enable")

    fresh = "B0COMPNEW1"
    race_plan = pairs_store.make_plan(connect, ours, None, fresh)
    out = race(lambda _: pairs_store.apply_plan(connect, race_plan, **mine), list(range(12)))
    total = sum(r["add"] + r["enable"] for kind, r in out if kind == "ok")
    check("12 одновременных добавлений одной пары: в базе одна строка, в журнале одна запись",
          not [e for kind, e in out if kind == "err"] and total == 1
          and q("SELECT count(*) FROM bsr_radar.competitor_pairs WHERE comp_asin = %s;", (fresh,))[0][0] == 1
          and q("SELECT count(*) FROM bsr_radar.pair_changes WHERE comp_asin = %s;", (fresh,))[0][0] == 1)

    try:
        q("INSERT INTO bsr_radar.pair_changes (actor, action, marketplace, our_asin, comp_asin) VALUES ('x', 'delete', 'US', 'a', 'b');")
        check("CHECK: в журнале допустимы только add/enable/disable", False)
    except psycopg2.errors.CheckViolation:
        check("CHECK: в журнале допустимы только add/enable/disable", True)

    # Запросы дашборда — ровно те, что лежат в dashboard_db.py.
    q("INSERT INTO bsr_radar.snapshots (snapshot_date, marketplace, our_asin, our_product, comp_asin, competitor_name, our_bsr, comp_bsr) VALUES "
      "('2026-09-20', 'US', %s, 'Our title', 'B0COMPAAA1', 'Scraped A1', 100, 200),"
      "('2026-09-21', 'US', %s, 'Our title', 'B0COMPAAA1', 'Scraped A1', 101, 201),"
      "('2026-09-21', 'US', %s, 'Our title', 'B0COMPAAA2', 'Scraped A2', 102, 202),"
      "('2026-09-21', 'US', %s, 'Our title', 'B0COMPAAA4', 'Scraped A4', 103, 203);", (ours, ours, ours, ours))
    current = q(dashboard_sql("load_current"))
    got = sorted((row[3], row[8]) for row in current)
    check("«Текущее состояние»: только активные пары и только последний снимок каждой",
          got == [(ours, "B0COMPAAA1"), (ours, "B0COMPAAA2")] and {row[0].isoformat() for row in current} == {"2026-09-21"}, str(got))

    # Необязательные столбцы дашборд подставляет по тому, что есть в базе, — передаём их явно.
    photos = ", s.our_image_url, s.comp_image_url"
    with_photos = dashboard_sql("load_current", photos)
    check("запрос «Текущего состояния» в дашборде читает фото (миграция 004)",
          "our_image_url" in with_photos and "comp_image_url" in with_photos)
    q("UPDATE bsr_radar.snapshots SET our_image_url = 'https://x/our.jpg', comp_image_url = 'https://x/comp.jpg' "
      "WHERE comp_asin = 'B0COMPAAA1' AND snapshot_date = '2026-09-21';")
    photo_row = next(row for row in q(with_photos) if row[8] == "B0COMPAAA1")
    check("фото читается из последнего снимка пары", photo_row[-2:] == ("https://x/our.jpg", "https://x/comp.jpg"))

    ratings = ", s.our_rating, s.our_reviews_count, s.comp_rating, s.comp_reviews_count"
    q("UPDATE bsr_radar.snapshots SET our_rating = 4.6, our_reviews_count = 0 "
      "WHERE comp_asin = 'B0COMPAAA1' AND snapshot_date = '2026-09-21';")
    rated = next(row for row in q(dashboard_sql("load_current", ratings)) if row[8] == "B0COMPAAA1")
    check("рейтинг и отзывы доходят до «Текущего состояния», ноль остаётся нулём",
          float(rated[-4]) == 4.6 and rated[-3] == 0, str(rated[-4:]))
    listing = {row[3]: row for row in q(dashboard_sql("load_competitor_pairs"))}
    check("список пар: пустое название подставляется из последнего снимка, заданное остаётся",
          listing["B0COMPAAA2"][4] == "Scraped A2" and listing["B0COMPAAA1"][4] == "Comp A1" and listing["B0COMPAAA1"][2] == "Our product")
    check("список пар: пара без снимков не пропадает и не даёт пустых значений NULL", listing["B0COMPNEW1"][4] == "" and listing["B0COMPNEW1"][5] is True)
    check("список пар содержит и активные, и отключённые", {row[5] for row in listing.values()} == {True, False})

    reset_old()
    q(MIGRATIONS["003_pair_changes.sql"])
    q(MIGRATIONS["003_pair_changes.sql"])
    check("миграция 003 на «старой» схеме создаёт журнал и индекс и безопасна при повторе",
          q("SELECT count(*) FROM pg_indexes WHERE schemaname = 'bsr_radar' AND indexname IN ('pair_changes_at_idx', 'snapshots_pair_date_idx');")[0][0] == 2
          and q("SELECT to_regclass('bsr_radar.pair_changes') IS NOT NULL;")[0][0])

    q("ALTER TABLE bsr_radar.snapshots DROP COLUMN our_image_url, DROP COLUMN comp_image_url;")
    q(MIGRATIONS["004_snapshot_images.sql"])
    q(MIGRATIONS["004_snapshot_images.sql"])
    check("миграция 004 на «старой» схеме (без фото) добавляет обе колонки и безопасна при повторе",
          q("SELECT count(*) FROM information_schema.columns WHERE table_schema = 'bsr_radar' "
            "AND table_name = 'snapshots' AND column_name IN ('our_image_url', 'comp_image_url');")[0][0] == 2)

    q("ALTER TABLE bsr_radar.collection_runs DROP CONSTRAINT collection_runs_scope_check;")
    q("ALTER TABLE bsr_radar.collection_runs DROP COLUMN scope;")
    q(MIGRATIONS["005_run_scope.sql"])
    q(MIGRATIONS["005_run_scope.sql"])
    check("миграция 005 на «старой» схеме (без scope) добавляет колонку со значением 'all' у старых строк и безопасна при повторе",
          q("SELECT count(*) FROM bsr_radar.collection_runs WHERE scope <> 'all';")[0][0] == 0
          and q("SELECT count(*) FROM information_schema.columns WHERE table_schema = 'bsr_radar' "
                "AND table_name = 'collection_runs' AND column_name = 'scope';")[0][0] == 1)
    try:
        q("INSERT INTO bsr_radar.collection_runs (source, step, status, scope) VALUES ('x', 'parser', 'done', 'bogus');")
        check("CHECK: scope принимает только all/ours/competitors", False)
    except psycopg2.errors.CheckViolation:
        check("CHECK: scope принимает только all/ours/competitors", True)


def section_run_control() -> None:
    print("\n== предпросмотр допуска (кнопка «Собрать сейчас») ==")
    import run_control

    counter = iter(range(9000, 9999))

    def agree(label: str) -> None:
        before = count()
        preview = run_control.admission_preview(connect, datetime.now(schedule_store.TZ))
        check(f"предпросмотр ничего не записывает: {label}", count() == before)
        decision = db_runs.admit_parser_run(gh(next(counter)))
        check(f"предпросмотр совпадает с настоящей проверкой: {label}", (preview is None) == decision.should_run,
              f"предпросмотр={preview!r}; проверка={decision.reason!r}")

    def hours_ago(hours: int):
        return q(f"SELECT now() - interval '{hours} hours';")[0][0]

    reset()
    agree("свободно (расписание 00:00)")
    agree("после этого идёт сбор (running)")
    reset()
    insert_run("parser", "done", hours_ago(0), hours_ago(0), owner_key="github:owner/repo:1:1")
    agree("сегодня уже был успешный сбор")
    reset()
    for _ in range(3):
        insert_run("parser", "error", hours_ago(0), hours_ago(0), owner_key="github:owner/repo:1:1")
    agree("исчерпан лимит попыток")
    reset(schedule=None)
    agree("расписания нет (автосбор выключен)")
    kyiv_now = q("SELECT now() AT TIME ZONE 'Europe/Kyiv';")[0][0]
    if (kyiv_now.hour, kyiv_now.minute) < (23, 58):
        reset(schedule=(23, 59))
        agree("время сбора не наступило")
    reset()
    insert_run("sync", "running", hours_ago(72), owner_key="github:owner/repo:1:1")
    agree("старая зависшая запись")

    print("\n== частичный сбор: у каждой области сбора свой дневной лимит ==")

    def agree_scope(label: str, scope: str) -> None:
        before = count()
        preview = run_control.admission_preview(connect, datetime.now(schedule_store.TZ), scope=scope)
        check(f"предпросмотр ({scope}) ничего не записывает: {label}", count() == before)
        decision = db_runs.admit_parser_run(gh(next(counter), scope=scope))
        check(f"предпросмотр ({scope}) совпадает с настоящей проверкой: {label}",
              (preview is None) == decision.should_run, f"предпросмотр={preview!r}; проверка={decision.reason!r}")

    reset()
    insert_run("parser", "done", hours_ago(0), hours_ago(0), owner_key="github:owner/repo:1:1", scope="all")
    check("успешный сбор «всё» не блокирует область «наши»",
          run_control.admission_preview(connect, datetime.now(schedule_store.TZ), scope="ours") is None)
    check("успешный сбор «всё» не блокирует область «конкуренты»",
          run_control.admission_preview(connect, datetime.now(schedule_store.TZ), scope="competitors") is None)
    agree_scope("но саму область «всё» блокирует", "all")

    reset()
    insert_run("parser", "done", hours_ago(0), hours_ago(0), owner_key="github:owner/repo:1:1", scope="ours")
    agree_scope("успешный сбор «наши» блокирует именно «наши»", "ours")
    check("но не блокирует «конкуренты»",
          run_control.admission_preview(connect, datetime.now(schedule_store.TZ), scope="competitors") is None)
    check("и не блокирует «всё»",
          run_control.admission_preview(connect, datetime.now(schedule_store.TZ), scope="all") is None)

    reset()
    insert_run("parser", "running", hours_ago(0), owner_key="github:owner/repo:1:1", scope="ours")
    check("незавершённый частичный сбор («наши») блокирует и «конкуренты» — общий на все области",
          run_control.admission_preview(connect, datetime.now(schedule_store.TZ), scope="competitors") is not None)
    check("и «всё» тоже заблокировано",
          run_control.admission_preview(connect, datetime.now(schedule_store.TZ), scope="all") is not None)

    reset()
    for _ in range(3):
        insert_run("parser", "error", hours_ago(0), hours_ago(0), owner_key="github:owner/repo:1:1", scope="competitors")
    agree_scope("лимит попыток «конкуренты» исчерпан именно у «конкуренты»", "competitors")
    check("«наши» при этом свободны", run_control.admission_preview(connect, datetime.now(schedule_store.TZ), scope="ours") is None)


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
        # Первый вызов списывает трёхдневную running как зависшую и сам проходит (создавая
        # СВОЮ, уже свежую running); второй вызов блокируется именно этой свежей записью,
        # а не старой — force её тоже не снимает, это отдельная (не дневная) защита.
        d, d_force = db_runs.admit_parser_run(gh(601)), db_runs.admit_parser_run(gh(602, force=True))
        check(f"running шага {step} трёхдневной давности списывается, допуск проходит, но следующий вызов блокирует уже свежая running",
              d.should_run and not d_force.should_run, d_force.reason)

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

    reset()
    recent_start = q("SELECT now() - interval '10 minutes';")[0][0]
    insert_run("parser", "running", recent_start, None, owner_key="github:owner/repo:1201:1")
    d = db_runs.admit_parser_run(gh(1202))
    check("свежая (10 минут) running всё ещё блокирует — не списывается раньше срока",
          not d.should_run and "незавершённая" in d.reason and count("status = 'running'") == 1)

    reset()
    old_start = q(f"SELECT now() - interval '{db_runs.STALE_RUNNING_MINUTES + 1} minutes';")[0][0]
    insert_run("parser", "running", old_start, None, owner_key="github:owner/repo:1203:1")
    d = db_runs.admit_parser_run(gh(1204))
    check("running за порогом списывается автоматически и допуск проходит",
          d.should_run and count("status = 'error'") == 1 and count("status = 'running'") == 1)

    reset_old()
    q(MIGRATIONS["002_dashboard_users.sql"])
    try:
        db_runs.admit_parser_run(gh(1001))
        check("новый код на немигрированной базе отказывает, а не работает без защиты", False)
    except db_runs.RunStoreError:
        check("новый код на немигрированной базе отказывает, а не работает без защиты", count() == 0)
    insert_run("parser", "done", q("SELECT now() - interval '9 days';")[0][0], q("SELECT now() - interval '9 days' + interval '10 minutes';")[0][0], new_columns=False)
    insert_run("sync", "running", q("SELECT now() - interval '2 days';")[0][0], None, new_columns=False)
    snapshot = "SELECT id, source, step, status, started_at, finished_at, error FROM bsr_radar.collection_runs ORDER BY id;"
    before = q(snapshot)
    q(MIGRATIONS["001_collection_admission.sql"])
    q(MIGRATIONS["001_collection_admission.sql"])
    check("миграция 001 не меняет существующие строки и безопасна при повторе", before == q(snapshot))
    d = db_runs.admit_parser_run(gh(1002))
    check("старая (2 дня) зависшая running теперь списывается автоматически и не блокирует", d.should_run)
    check("списанная попытка получила статус error, а не осталась висеть running", count("status = 'error'") == 1)

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


def section_rating_reviews() -> None:
    """Миграция 009: рейтинг и отзывы. NULL — «нет данных», 0 отзывов — настоящий ноль."""
    print("\n== рейтинг и отзывы (миграция 009) ==")

    def columns() -> set:
        return {row[0] for row in q(
            "SELECT column_name FROM information_schema.columns WHERE table_schema='bsr_radar' "
            "AND table_name='snapshots';")}

    # Старая схема: снимки есть, новых столбцов нет — ровно как на боевой базе до миграции.
    reset(migrations=False)
    q("ALTER TABLE bsr_radar.snapshots DROP COLUMN our_rating, DROP COLUMN our_reviews_count, "
      "DROP COLUMN comp_rating, DROP COLUMN comp_reviews_count;")
    q("""
        INSERT INTO bsr_radar.snapshots (snapshot_date, marketplace, our_asin, comp_asin, our_bsr)
        VALUES ('2026-09-01', 'UK', 'B0OURASIN1', 'B0COMPAAA1', 1234);
    """)
    check("до миграции столбцов рейтинга нет", "our_rating" not in columns())

    before = q("SELECT snapshot_date, marketplace, our_asin, comp_asin, our_bsr FROM bsr_radar.snapshots ORDER BY id;")
    q(MIGRATIONS["009_snapshot_rating_reviews.sql"])
    q(MIGRATIONS["009_snapshot_rating_reviews.sql"])
    after = q("SELECT snapshot_date, marketplace, our_asin, comp_asin, our_bsr FROM bsr_radar.snapshots ORDER BY id;")
    check("миграция 009 не трогает существующие строки и безопасна при повторе", before == after)
    check("столбцы появились", {"our_rating", "our_reviews_count", "comp_rating", "comp_reviews_count"} <= columns())

    old = q("SELECT our_rating, our_reviews_count FROM bsr_radar.snapshots WHERE snapshot_date='2026-09-01';")[0]
    check("старая история осталась пустой, а не нулевой", old == (None, None), str(old))

    # Ноль отзывов — настоящее значение и должен отличаться от отсутствия данных.
    q("""
        INSERT INTO bsr_radar.snapshots
            (snapshot_date, marketplace, our_asin, comp_asin, our_rating, our_reviews_count)
        VALUES ('2026-09-24', 'UK', 'B0OURASIN1', 'B0COMPAAA1', 4.6, 0);
    """)
    fresh = q("SELECT our_rating, our_reviews_count FROM bsr_radar.snapshots WHERE snapshot_date='2026-09-24';")[0]
    check("ноль отзывов хранится как 0 и отличается от NULL",
          fresh[1] == 0 and fresh[1] is not None and float(fresh[0]) == 4.6, str(fresh))

    for bad, label in ((5.4, "рейтинг больше 5"), (-0.1, "отрицательный рейтинг")):
        try:
            q("INSERT INTO bsr_radar.snapshots (snapshot_date, marketplace, our_asin, comp_asin, our_rating) "
              "VALUES ('2026-09-25', 'UK', 'B0OURASIN1', 'B0COMPAAA1', %s);", (bad,))
            check(f"база не принимает {label}", False)
        except Exception:  # noqa: BLE001
            check(f"база не принимает {label}", True)

    try:
        q("INSERT INTO bsr_radar.snapshots (snapshot_date, marketplace, our_asin, comp_asin, our_reviews_count) "
          "VALUES ('2026-09-26', 'UK', 'B0OURASIN1', 'B0COMPAAA1', -1);")
        check("база не принимает отрицательное число отзывов", False)
    except Exception:  # noqa: BLE001
        check("база не принимает отрицательное число отзывов", True)


def section_asin_registry() -> None:
    """Справочник ASIN (миграция 008): ASIN существует сам по себе, без пары."""
    print("\n== справочник ASIN (миграция 008) ==")

    # Схема без справочника: код обязан это распознать, а не упасть.
    reset(migrations=False)
    q("DROP TABLE IF EXISTS bsr_radar.asins;")
    check("без миграции справочник честно считается отсутствующим",
          asins_store.registry_exists(connect) is False)

    # Перенос из пар: справочник должен сразу отражать то, что уже заведено.
    q("""
        INSERT INTO bsr_radar.competitor_pairs (marketplace, our_asin, our_product, comp_asin, competitor_name, active)
        VALUES ('US', 'B0OURASIN1', 'Наш товар', 'B0COMPAAA1', 'Конкурент A', TRUE),
               ('US', 'B0OURASIN1', 'Наш товар', 'B0COMPBBB2', 'Конкурент B', FALSE),
               ('DE', 'B0OURASIN1', '',          'B0COMPCCC3', 'Конкурент C', TRUE);
    """)
    q(MIGRATIONS["008_asin_registry.sql"])
    q(MIGRATIONS["008_asin_registry.sql"])
    rows = q("SELECT marketplace, asin, name, kind, active FROM bsr_radar.asins ORDER BY marketplace, asin;")
    check("миграция переносит ASIN из пар и безопасна при повторе", len(rows) == 5, str(len(rows)))
    by_key = {(row[0], row[1]): row for row in rows}
    check("наш ASIN помечен как наш", by_key[("US", "B0OURASIN1")][3] == "ours")
    check("конкурент помечен как конкурент", by_key[("US", "B0COMPAAA1")][3] == "competitor")
    check("подпись подтянулась из пары", by_key[("US", "B0OURASIN1")][2] == "Наш товар")
    check("ASIN только из отключённой пары перенесён неактивным",
          by_key[("US", "B0COMPBBB2")][4] is False)
    check("тот же ASIN на другом рынке — отдельная строка", ("DE", "B0OURASIN1") in by_key)

    # ASIN сам по себе, без всякой пары, — ради этого справочник и заводился.
    added = asins_store.add_asins(connect, "US", "B0NEWNEW01 B0NEWNEW02", "competitor",
                                  actor_role=access.ROLE_EDITOR, actor="Проверка")
    # Без LIKE: знак процента psycopg2 принял бы за подстановку параметра.
    check("ASIN добавляется без пары", added["added"] == 2 and
          q("SELECT count(*) FROM bsr_radar.asins WHERE asin IN ('B0NEWNEW01', 'B0NEWNEW02');")[0][0] == 2)
    check("пары при этом не появились",
          q("SELECT count(*) FROM bsr_radar.competitor_pairs;")[0][0] == 3)

    again = asins_store.add_asins(connect, "US", "B0NEWNEW01", "competitor",
                                  actor_role=access.ROLE_EDITOR, actor="Проверка")
    check("повторное добавление не плодит дубли",
          again["added"] == 0 and q("SELECT count(*) FROM bsr_radar.asins WHERE asin = 'B0NEWNEW01';")[0][0] == 1)

    removed = asins_store.set_active(connect, [("US", "B0NEWNEW01")], False,
                                     actor_role=access.ROLE_EDITOR, actor="Проверка")
    state = q("SELECT active FROM bsr_radar.asins WHERE marketplace='US' AND asin='B0NEWNEW01';")[0][0]
    check("убранный ASIN не удаляется, а становится неактивным", removed == 1 and state is False)

    back = asins_store.add_asins(connect, "US", "B0NEWNEW01", "competitor",
                                 actor_role=access.ROLE_EDITOR, actor="Проверка")
    check("добавление убранного ASIN возвращает его", back["restored"] == 1 and
          q("SELECT active FROM bsr_radar.asins WHERE asin='B0NEWNEW01';")[0][0] is True)

    asins_store.rename(connect, ("US", "B0NEWNEW02"), "Новая подпись",
                       actor_role=access.ROLE_EDITOR, actor="Проверка")
    check("подпись меняется, ключ остаётся",
          q("SELECT name, asin FROM bsr_radar.asins WHERE asin='B0NEWNEW02';")[0] == ("Новая подпись", "B0NEWNEW02"))

    try:
        asins_store.add_asins(connect, "US", "B0NEWNEW03", "competitor", actor_role=None, actor="Чужой")
        check("зритель не может править справочник", False)
    except access.AccessDenied:
        check("зритель не может править справочник",
              q("SELECT count(*) FROM bsr_radar.asins WHERE asin='B0NEWNEW03';")[0][0] == 0)

    check("справочник не влияет на то, что собирается: пары не тронуты",
          q("SELECT count(*) FROM bsr_radar.competitor_pairs WHERE active;")[0][0] == 2)


def section_pair_editing() -> None:
    """Правка названий уже заведённой пары (миграция 007 разрешает действие 'edit' в журнале)."""
    print("\n== правка названий пары ==")
    reset()
    q("""
        INSERT INTO bsr_radar.competitor_pairs (marketplace, our_asin, our_product, comp_asin, competitor_name, active)
        VALUES ('US', 'B0OURASIN1', 'Старое наше', 'B0COMPAAA1', 'Старый конкурент', TRUE);
    """)
    key = ("US", "B0OURASIN1", "B0COMPAAA1")

    changed = pairs_store.edit_pair_names(connect, key, "Новое наше", "Новый конкурент",
                                          actor_role=access.ROLE_EDITOR, actor="Проверка")
    names = q("SELECT our_product, competitor_name FROM bsr_radar.competitor_pairs;")[0]
    check("правка меняет обе подписи", changed == 1 and names == ("Новое наше", "Новый конкурент"), str(names))

    again = pairs_store.edit_pair_names(connect, key, "Новое наше", "Новый конкурент",
                                        actor_role=access.ROLE_EDITOR, actor="Проверка")
    check("повторная правка теми же значениями ничего не меняет", again == 0)

    actions = q("SELECT action, actor FROM bsr_radar.pair_changes ORDER BY id;")
    check("в журнал попала ровно одна запись 'edit'",
          actions == [("edit", "Проверка")], str(actions))

    key_after = q("SELECT marketplace, our_asin, comp_asin, active FROM bsr_radar.competitor_pairs;")[0]
    check("ключ пары и признак активности не тронуты", key_after == ("US", "B0OURASIN1", "B0COMPAAA1", True))

    try:
        pairs_store.edit_pair_names(connect, key, "Х", "Х", actor_role=None, actor="Чужой")
        check("зритель не может править", False)
    except access.AccessDenied:
        check("зритель не может править", q("SELECT our_product FROM bsr_radar.competitor_pairs;")[0][0] == "Новое наше")

    # Миграция 007 на «старом» журнале: без неё действие 'edit' нарушило бы ограничение.
    # Записи 'edit' сначала убираем — иначе старое ограничение просто не создастся.
    q("DELETE FROM bsr_radar.pair_changes WHERE action = 'edit';")
    q("ALTER TABLE bsr_radar.pair_changes DROP CONSTRAINT IF EXISTS pair_changes_action_check;")
    q("ALTER TABLE bsr_radar.pair_changes ADD CONSTRAINT pair_changes_action_check "
      "CHECK (action IN ('add', 'enable', 'disable'));")
    try:
        pairs_store.edit_pair_names(connect, key, "До миграции", "До миграции",
                                    actor_role=access.ROLE_EDITOR, actor="Проверка")
        check("на старом журнале правка отклоняется, а не пишется мимо ограничения", False)
    except Exception:  # noqa: BLE001
        check("на старом журнале правка отклоняется, а не пишется мимо ограничения",
              q("SELECT our_product FROM bsr_radar.competitor_pairs;")[0][0] == "Новое наше")

    q(MIGRATIONS["007_pair_changes_edit.sql"])
    q(MIGRATIONS["007_pair_changes_edit.sql"])
    after = pairs_store.edit_pair_names(connect, key, "После миграции", "После миграции",
                                        actor_role=access.ROLE_EDITOR, actor="Проверка")
    check("после миграции 007 правка проходит (повтор миграции безопасен)",
          after == 1 and q("SELECT our_product FROM bsr_radar.competitor_pairs;")[0][0] == "После миграции")

    # Журнала может не быть вовсе: на боевой базе миграция 003 не применялась.
    q("DROP TABLE bsr_radar.pair_changes;")
    without = pairs_store.edit_pair_names(connect, key, "Без журнала", "Без журнала",
                                          actor_role=access.ROLE_EDITOR, actor="Проверка")
    check("без журнала правка всё равно применяется",
          without == 1 and q("SELECT our_product FROM bsr_radar.competitor_pairs;")[0][0] == "Без журнала")


def section_snapshot_marketplace_key() -> None:
    """Миграция 006: страна в ключе снимков. На боевой базе 25 пар ASIN отслеживаются сразу
    на нескольких рынках и до этой миграции схлопывались в одну строку за день."""
    print("\n== страна в ключе снимков (миграция 006) ==")

    def snapshot_key() -> str:
        rows = q("""
            SELECT pg_get_constraintdef(oid) FROM pg_constraint
            WHERE conrelid = 'bsr_radar.snapshots'::regclass AND contype = 'u';
        """)
        return rows[0][0] if rows else ""

    def insert_snapshot(market, price, day="2026-09-23", our="B0OURASIN1", comp="B0COMPAAA1", target=None):
        q(f"""
            INSERT INTO bsr_radar.snapshots (snapshot_date, marketplace, our_asin, comp_asin, our_price)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT {target} DO UPDATE SET marketplace = EXCLUDED.marketplace, our_price = EXCLUDED.our_price;
        """, (day, market, our, comp, price))

    def snapshot_rows():
        return q("SELECT marketplace, our_price FROM bsr_radar.snapshots ORDER BY marketplace;")

    # Старая схема (ключ без страны) — воспроизводим сам баг, чтобы было видно, что чиним.
    reset(migrations=False)
    q("ALTER TABLE bsr_radar.snapshots DROP CONSTRAINT snapshots_day_market_pair_key;")
    q("ALTER TABLE bsr_radar.snapshots ADD CONSTRAINT snapshots_snapshot_date_our_asin_comp_asin_key "
      "UNIQUE (snapshot_date, our_asin, comp_asin);")
    for market, price in (("ES", 19.99), ("FR", 21.50), ("IT", 23.00)):
        insert_snapshot(market, price, target="(snapshot_date, our_asin, comp_asin)")
    check("старый ключ: три рынка схлопываются в одну строку (сам баг)", len(snapshot_rows()) == 1, str(snapshot_rows()))

    # Миграция на «боевых» данных: строки должны пережить расширение ключа.
    before = q("SELECT id, snapshot_date, marketplace, our_asin, comp_asin, our_price FROM bsr_radar.snapshots ORDER BY id;")
    q(MIGRATIONS["006_snapshot_marketplace_key.sql"])
    q(MIGRATIONS["006_snapshot_marketplace_key.sql"])
    after = q("SELECT id, snapshot_date, marketplace, our_asin, comp_asin, our_price FROM bsr_radar.snapshots ORDER BY id;")
    check("миграция 006 не меняет существующие строки и безопасна при повторе", before == after)
    check("после миграции ключ включает страну", "marketplace" in snapshot_key(), snapshot_key())

    # После миграции те же три рынка живут как три отдельные строки.
    target = "(snapshot_date, marketplace, our_asin, comp_asin)"
    for market, price in (("ES", 19.99), ("FR", 21.50), ("IT", 23.00)):
        insert_snapshot(market, price, target=target)
    rows = snapshot_rows()
    check("новый ключ: три рынка — три строки, данные не затирают друг друга",
          len(rows) == 3 and [r[0] for r in rows] == ["ES", "FR", "IT"], str(rows))

    # Повторный сбор того же дня по-прежнему обновляет строку, а не плодит дубли.
    insert_snapshot("FR", 25.00, target=target)
    rows = snapshot_rows()
    check("повторный сбор обновляет строку своего рынка и только её",
          len(rows) == 3 and dict(rows)["FR"] == 25 and dict(rows)["ES"] == Decimal("19.99"), str(rows))

    # Разные пары на одном рынке не сливаются.
    insert_snapshot("FR", 30.00, comp="B0COMPBBB2", target=target)
    check("разные пары на одном рынке остаются разными строками",
          len(q("SELECT 1 FROM bsr_radar.snapshots;")) == 4)


try:
    section_schedule_and_users()
    section_pairs()
    section_run_control()
    section_admission()
    section_snapshot_marketplace_key()
    section_pair_editing()
    section_asin_registry()
    section_rating_reviews()
finally:
    failed = [name for name, ok in checks if not ok]
    print(f"\nитого: {len(checks) - len(failed)} из {len(checks)} проверок прошли")
    for name in failed:
        print("  ПРОВАЛ:", name)
    server.cleanup()
    shutil.rmtree(WORK, ignore_errors=True)
sys.exit(1 if failed else 0)
