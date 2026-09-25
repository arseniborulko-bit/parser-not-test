"""Пары ASIN: разбор текста, проверка перед записью и права. Без настоящей базы."""

import logging

import pytest

import access
import pairs_store
from pairs_store import Item

A = "B0ABCDEFG1"
B = "B0ABCDEFG2"
C = "B0ABCDEFG3"
D = "B0ABCDEFG4"


class FakeDb:
    """Возвращает заранее заданные ответы по порядку выполнения запросов."""

    def __init__(self, results=None, fail_connect=None, fail_execute=None, fail_commit=None):
        self.results = list(results or [])
        self.fail_connect = fail_connect
        self.fail_execute = fail_execute
        self.fail_commit = fail_commit
        self.connects = 0
        self.executed = []
        self.commits = 0
        self.closed = 0

    def connect(self):
        self.connects += 1
        if self.fail_connect:
            raise RuntimeError(self.fail_connect)
        return FakeConn(self)


class FakeConn:
    def __init__(self, db):
        self.db = db

    def cursor(self):
        return FakeCursor(self.db)

    def commit(self):
        if self.db.fail_commit:
            raise RuntimeError(self.db.fail_commit)
        self.db.commits += 1

    def close(self):
        self.db.closed += 1


class FakeCursor:
    def __init__(self, db):
        self.db = db
        self.rowcount = -1

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=()):
        if self.db.fail_execute:
            raise RuntimeError(self.db.fail_execute)
        self.db.executed.append((sql, params))

    def fetchall(self):
        return self.db.results.pop(0)


def tokens(batch):
    return [(item.asin, item.market) for item in batch.items]


def test_plain_asins_are_split_by_any_separator_and_uppercased():
    batch = pairs_store.parse_asin_batch(f"{A.lower()}, {B};\n{C}\t{D}")
    assert tokens(batch) == [(A, None), (B, None), (C, None), (D, None)]
    assert batch.invalid == [] and batch.repeats == 0


@pytest.mark.parametrize("url, market", [
    (f"https://www.amazon.com/dp/{A}", "US"),
    (f"https://www.amazon.ca/Some-Product-Name/dp/{A}/ref=sr_1_2?psc=1", "CA"),
    (f"https://www.amazon.co.uk/gp/product/{A}", "UK"),
    (f"https://amazon.de/dp/{A}?th=1", "DE"),
    (f"https://www.amazon.fr/dp/{A}", "FR"),
    (f"https://www.amazon.es/dp/{A}", "ES"),
    (f"https://www.amazon.it/dp/{A}", "IT"),
    (f"https://www.amazon.com.mx/dp/{A}", "MX"),
    (f"https://www.amazon.co.jp/dp/{A}", "JP"),
    (f"https://www.amazon.com.au/dp/{A}", "AU"),
])
def test_links_give_asin_and_marketplace_from_the_domain(url, market):
    assert tokens(pairs_store.parse_asin_batch(url)) == [(A, market)]


def test_asin_with_market_suffix():
    assert tokens(pairs_store.parse_asin_batch(f"{A}:us {B}:DE")) == [(A, "US"), (B, "DE")]


def test_unknown_suffix_and_unsupported_domain_are_invalid():
    batch = pairs_store.parse_asin_batch(f"{A}:XX https://www.amazon.nl/dp/{B} https://www.amazon.in/dp/{C}")
    assert batch.items == []
    assert len(batch.invalid) == 3


@pytest.mark.parametrize("junk", [
    "hello", "B0SHORT", "B0ABCDEFG12", "A0ABCDEFG1", "1234567890", "xxB0ABCDEFG1", "https://example.com/dp/B0ABCDEFG1x",
])
def test_things_that_are_not_asins_are_reported_not_guessed(junk):
    batch = pairs_store.parse_asin_batch(junk)
    assert batch.items == [] and batch.invalid == [junk]


def test_repeats_inside_one_paste_are_counted_and_dropped():
    batch = pairs_store.parse_asin_batch(f"{A} {A.lower()} https://www.amazon.com/dp/{A} {B}")
    assert tokens(batch) == [(A, None), (B, None)]
    assert batch.repeats == 2


def test_surrounding_quotes_and_brackets_are_ignored():
    assert tokens(pairs_store.parse_asin_batch(f'"{A}", ({B}), <{C}>')) == [(A, None), (B, None), (C, None)]


@pytest.mark.parametrize("value", [None, 5, "", "   ", ["B0ABCDEFG1"]])
def test_empty_or_non_text_input_gives_an_empty_batch(value):
    batch = pairs_store.parse_asin_batch(value)
    assert batch.items == [] and batch.invalid == [] and batch.repeats == 0


def test_require_link_rejects_bare_asin_and_market_suffix():
    """«Добавить пару» больше не принимает голый ASIN или «ASIN:РЫНОК» — только настоящую
    ссылку (владелец, 25.09.2026: «мы добавляем только по ссылке, а не по ASIN»)."""
    batch = pairs_store.parse_asin_batch(f"{A} {B}:US", require_link=True)
    assert batch.items == []
    assert set(batch.invalid) == {A, f"{B}:US"}


def test_require_link_accepts_a_real_amazon_link():
    batch = pairs_store.parse_asin_batch(f"https://www.amazon.com/dp/{A}", require_link=True)
    assert tokens(batch) == [(A, "US")]


def test_market_tables_match_the_parsers_domain_mapping():
    import sheets

    for market, domain in pairs_store.DOMAIN_BY_MARKET.items():
        assert sheets._amazon_domain(market) == domain
    assert sheets._amazon_domain("NL") == "com"
    assert "NL" not in pairs_store.DOMAIN_BY_MARKET


def test_asin_pattern_is_the_same_as_the_projects():
    import config

    assert pairs_store._ASIN_RE.pattern == config.ASIN_PATTERN.pattern
    assert pairs_store._ASIN_RE.flags == config.ASIN_PATTERN.flags


def plan_db(known=(), active_now=100, statuses=(), collected=()):
    """Ответы make_plan по порядку: (известные строки нашего ASIN, число активных), затем (статусы, уже собираемые)."""
    results = [list(known), [(active_now,)]]
    if statuses is not None:
        results += [list(statuses), [(a,) for a in collected]]
    return FakeDb(results=results)


def link(asin: str, domain: str = "com") -> str:
    """Тестовая ссылка Amazon: make_plan (require_link=True) страну берёт только из неё."""
    return f"https://www.amazon.{domain}/dp/{asin}"


def test_plan_splits_new_returning_and_existing_pairs():
    db = plan_db(known=[("US", "Our product")], statuses=[(B, True), (C, False)], collected=[A, B])
    plan = pairs_store.make_plan(db.connect, link(A), f"{link(B)} {link(C)} {link(D)}")
    assert (plan.market, plan.our_asin, plan.our_product) == ("US", A, "Our product")
    assert (plan.to_add, plan.to_enable, plan.already) == ([D], [C], [B])
    assert plan.new_asins == 2
    assert plan.can_apply and plan.changes == 2 and plan.errors == []


def test_plan_reads_only_and_never_writes():
    db = plan_db(known=[("US", "")], statuses=[], collected=[])
    pairs_store.make_plan(db.connect, link(A), link(B))
    assert db.commits == 2
    assert all(sql.lstrip().upper().startswith("SELECT") for sql, _ in db.executed)


def test_market_always_comes_from_the_link_not_from_history():
    """Владелец, 25.09.2026: только ссылка — страна не выбирается вручную и не угадывается
    по тому, что уже есть в базе (раньше был выбор «Маркетплейс» и подстановка по истории)."""
    db = plan_db(known=[("DE", "")], statuses=[], collected=[])
    plan = pairs_store.make_plan(db.connect, link(A, "ca"), link(B, "ca"))
    assert plan.market == "CA"


def test_a_bare_asin_for_our_product_is_rejected():
    db = FakeDb()
    plan = pairs_store.make_plan(db.connect, A, link(B))
    assert not plan.can_apply and "ссылка" in plan.errors[0].lower()
    assert db.connects == 0


def test_an_asin_market_suffix_is_not_enough_a_real_link_is_required():
    db = FakeDb()
    plan = pairs_store.make_plan(db.connect, f"{A}:US", link(B))
    assert not plan.can_apply and "ссылка" in plan.errors[0].lower()


def test_new_our_asin_works_when_the_link_gives_the_market():
    db = plan_db(known=[], statuses=[], collected=[])
    plan = pairs_store.make_plan(db.connect, link(A, "ca"), f"{link(B, 'ca')} {link(C, 'ca')}")
    assert plan.market == "CA" and plan.to_add == [B, C] and plan.our_product == ""
    assert plan.new_asins == 3


@pytest.mark.parametrize("our_text", ["", "hello", f"{A} {B}", f"{A} junk"])
def test_our_product_must_be_exactly_one_link(our_text):
    db = FakeDb()
    plan = pairs_store.make_plan(db.connect, our_text, link(B))
    assert not plan.can_apply and "Наш товар" in plan.errors[0]
    assert db.connects == 0


def test_no_competitors_is_an_error_without_database_access():
    db = FakeDb()
    plan = pairs_store.make_plan(db.connect, link(A), "  ")
    assert plan.errors == ["Вставьте ссылки конкурентов."] and db.connects == 0


def test_a_bare_asin_competitor_is_rejected_not_silently_accepted():
    """Голый ASIN конкурента раньше молча считался «того же рынка, что наш товар» — теперь это
    просто нераспознанная строка, как и для нашего товара."""
    db = FakeDb()
    plan = pairs_store.make_plan(db.connect, link(A), B)
    assert plan.invalid == [B]
    assert plan.errors == ["Вставьте ссылки конкурентов."]
    assert db.connects == 0


def test_batch_limit():
    many = " ".join(link(f"B0{i:08d}") for i in range(pairs_store.MAX_BATCH + 1))
    db = FakeDb()
    plan = pairs_store.make_plan(db.connect, link(A), many)
    assert "не больше" in plan.errors[0] and db.connects == 0


def test_competitor_from_another_marketplace_or_equal_to_ours_is_rejected():
    db = plan_db(known=[("US", "")], statuses=[], collected=[])
    plan = pairs_store.make_plan(db.connect, link(A), f"{link(B, 'de')} {link(A)} {link(C)}")
    assert plan.to_add == [C]
    assert len(plan.rejected) == 2 and "DE" in plan.rejected[0] and "совпадает" in plan.rejected[1]


def test_invalid_and_repeated_items_are_reported():
    db = plan_db(known=[("US", "")], statuses=[], collected=[])
    plan = pairs_store.make_plan(db.connect, link(A), f"{link(B)} {link(B)} nonsense")
    assert plan.to_add == [B] and plan.invalid == ["nonsense"] and plan.repeats == 1


def test_active_pairs_cap_blocks_applying():
    db = plan_db(known=[("US", "")], active_now=1499, statuses=[], collected=[])
    plan = pairs_store.make_plan(db.connect, link(A), f"{link(B)} {link(C)}", max_active=1500)
    assert not plan.can_apply and "лимит" in plan.errors[0]


def test_nothing_to_change_is_not_applicable():
    db = plan_db(known=[("US", "")], statuses=[(B, True)], collected=[A, B])
    plan = pairs_store.make_plan(db.connect, link(A), link(B))
    assert plan.already == [B] and plan.changes == 0 and not plan.can_apply and plan.errors == []


@pytest.mark.parametrize("db", [
    FakeDb(fail_connect="host=secret password=hunter2"),
    FakeDb(fail_execute="password=hunter2"),
    FakeDb(results=[[], [(0,)]], fail_commit="password=hunter2"),
])
def test_driver_errors_are_wrapped_without_leaking_their_text(db):
    with pytest.raises(pairs_store.PairsStoreError) as info:
        pairs_store.make_plan(db.connect, link(A), link(B))
    assert "hunter2" not in str(info.value) and "secret" not in str(info.value)


def test_make_plans_builds_one_plan_per_our_link():
    """Владелец: «Наш товар» тоже «можно несколько» — конкуренты применяются к каждой ссылке
    отдельным планом (25.09.2026)."""
    results = (
        plan_db(known=[("US", "Our A")], statuses=[], collected=[]).results +
        plan_db(known=[("US", "Our D")], statuses=[], collected=[]).results
    )
    db = FakeDb(results=results)
    plans = pairs_store.make_plans(db.connect, f"{link(A)} {link(D)}", link(B))
    assert len(plans) == 2
    assert (plans[0].our_asin, plans[0].market, plans[0].our_product) == (A, "US", "Our A")
    assert (plans[1].our_asin, plans[1].market, plans[1].our_product) == (D, "US", "Our D")
    assert plans[0].to_add == [B] and plans[1].to_add == [B]


def test_make_plans_with_no_valid_links_returns_one_error_plan():
    db = FakeDb()
    plans = pairs_store.make_plans(db.connect, "not a link", link(B))
    assert len(plans) == 1
    assert not plans[0].can_apply and "ссылка" in plans[0].errors[0].lower()
    assert db.connects == 0


def ready_plan(**overrides):
    plan = pairs_store.Plan(market="US", our_asin=A, our_product="Our", to_add=[B, C], to_enable=[D])
    for key, value in overrides.items():
        setattr(plan, key, value)
    return plan


@pytest.mark.parametrize("role", [None, "viewer", ""])
def test_apply_requires_an_editor_and_never_touches_the_database_otherwise(role):
    db = FakeDb()
    with pytest.raises(access.AccessDenied):
        pairs_store.apply_plan(db.connect, ready_plan(), actor_role=role, actor="Аня")
    assert db.connects == 0


def test_apply_refuses_a_plan_with_errors():
    db = FakeDb()
    with pytest.raises(ValueError):
        pairs_store.apply_plan(db.connect, ready_plan(errors=["нельзя"]), actor_role=access.ROLE_EDITOR, actor="Аня")
    assert db.connects == 0


def test_journal_exists_reads_the_catalog():
    assert pairs_store.journal_exists(FakeDb(results=[[(True,)]]).connect) is True
    assert pairs_store.journal_exists(FakeDb(results=[[(False,)]]).connect) is False


def test_apply_sends_bound_arrays_in_one_transaction_and_counts_actions():
    db = FakeDb(results=[[(True,)], [("add",), ("add",), ("enable",)]])
    result = pairs_store.apply_plan(db.connect, ready_plan(), actor_role=access.ROLE_EDITOR, actor="Аня")
    assert result == {"add": 2, "enable": 1}
    sql, params = db.executed[1]
    assert params == (["US"] * 3, [A] * 3, ["Our"] * 3, [B, C, D], "Аня")
    assert A not in sql and "Аня" not in sql
    assert "pair_changes" in sql and (db.connects, db.commits, db.closed) == (2, 2, 2)


def test_without_a_journal_table_pairs_are_still_added_and_a_warning_is_logged(caplog):
    db = FakeDb(results=[[(False,)], [("add",), ("enable",)]])
    with caplog.at_level(logging.WARNING, logger="pairs_store"):
        result = pairs_store.apply_plan(db.connect, ready_plan(to_add=[B], to_enable=[C]), actor_role=access.ROLE_EDITOR, actor="Аня")
    assert result == {"add": 1, "enable": 1}
    sql, params = db.executed[1]
    assert "pair_changes" not in sql and params == (["US"] * 2, [A] * 2, ["Our"] * 2, [B, C])
    assert "не подключён" in caplog.text and "Аня" in caplog.text


def test_without_a_journal_table_toggling_still_works():
    db = FakeDb(results=[[(False,)], [(1,)]])
    assert pairs_store.set_pairs_active(db.connect, [KEY1], False, actor_role=access.ROLE_EDITOR, actor="Аня") == 1
    sql, params = db.executed[1]
    assert "pair_changes" not in sql and params == (["US"], [A], [B], False, False)


def test_apply_errors_are_wrapped_without_leaking_driver_text():
    db = FakeDb(fail_execute="password=hunter2")
    with pytest.raises(pairs_store.PairsStoreError) as info:
        pairs_store.apply_plan(db.connect, ready_plan(), actor_role=access.ROLE_EDITOR, actor="Аня")
    assert "hunter2" not in str(info.value)


def test_apply_with_nothing_to_do_makes_no_database_call():
    db = FakeDb()
    plan = pairs_store.Plan(market="US", our_asin=A)
    assert pairs_store.apply_plan(db.connect, plan, actor_role=access.ROLE_EDITOR, actor="Аня") == {"add": 0, "enable": 0}
    assert db.connects == 0


def test_apply_logs_who_changed_what(caplog):
    db = FakeDb(results=[[(True,)], [("add",)]])
    with caplog.at_level(logging.INFO, logger="pairs_store"):
        pairs_store.apply_plan(db.connect, pairs_store.Plan(market="CA", our_asin=A, to_add=[B]), actor_role=access.ROLE_EDITOR, actor="Борис")
    assert "Борис" in caplog.text and A in caplog.text and "CA" in caplog.text


KEY1 = ("US", A, B)
KEY2 = ("US", A, C)


@pytest.mark.parametrize("active, action", [(False, "disable"), (True, "enable")])
def test_toggle_passes_keys_as_arrays_and_names_the_action(active, action):
    db = FakeDb(results=[[(True,)], [(1,), (1,)]])
    changed = pairs_store.set_pairs_active(db.connect, [KEY1, KEY2, KEY1], active, actor_role=access.ROLE_EDITOR, actor="Аня")
    assert changed == 2
    sql, params = db.executed[1]
    assert params == (["US", "US"], [A, A], [B, C], active, active, "Аня", action)
    assert "pair_changes" in sql and "IS DISTINCT FROM" in sql


@pytest.mark.parametrize("role", [None, "viewer"])
def test_toggle_requires_an_editor(role):
    db = FakeDb()
    with pytest.raises(access.AccessDenied):
        pairs_store.set_pairs_active(db.connect, [KEY1], False, actor_role=role, actor="Аня")
    assert db.connects == 0


@pytest.mark.parametrize("key", [("XX", A, B), ("US", "nope", B), ("US", A, "B0SHORT"), ("US", A, B + "1")])
def test_toggle_rejects_malformed_keys_without_database_access(key):
    db = FakeDb()
    with pytest.raises(ValueError):
        pairs_store.set_pairs_active(db.connect, [KEY1, key], False, actor_role=access.ROLE_EDITOR, actor="Аня")
    assert db.connects == 0


def test_toggle_with_no_keys_is_a_no_op_and_too_many_keys_are_refused():
    db = FakeDb()
    assert pairs_store.set_pairs_active(db.connect, [], False, actor_role=access.ROLE_EDITOR, actor="Аня") == 0
    keys = [("US", A, f"B0{i:08d}") for i in range(pairs_store.MAX_TOGGLE + 1)]
    with pytest.raises(ValueError):
        pairs_store.set_pairs_active(db.connect, keys, False, actor_role=access.ROLE_EDITOR, actor="Аня")
    assert db.connects == 0


def test_recent_changes_maps_rows():
    db = FakeDb(results=[[("2026-09-21", "Аня", "add", "US", A, B)]])
    assert pairs_store.recent_changes(db.connect, 5) == [
        {"at": "2026-09-21", "actor": "Аня", "action": "add", "marketplace": "US", "our_asin": A, "comp_asin": B},
    ]
    assert db.executed[0][1] == (5,)
