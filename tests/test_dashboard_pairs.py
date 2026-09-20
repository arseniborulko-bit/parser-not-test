"""Вкладка «Пары конкурентов» целиком (Streamlit AppTest): без настоящей базы и без настоящей записи."""

import pandas as pd
import pytest

import access
import pairs_store
from test_dashboard_access import TEAM_PASSWORD, dash, logged_in, run, sign_in, unlock  # noqa: F401

OUR = "B0OURASIN1"
OTHER = "B0OURASIN2"


def pair_rows(active_count=3, inactive_count=1, our=OUR, market="US"):
    rows = [
        {"marketplace": market, "our_asin": our, "our_product": "Our product", "comp_asin": f"B0COMP{i:04d}",
         "competitor_name": f"Competitor {i}", "active": True}
        for i in range(active_count)
    ]
    rows += [
        {"marketplace": market, "our_asin": our, "our_product": "Our product", "comp_asin": f"B0OFF{i:05d}",
         "competitor_name": f"Disabled {i}", "active": False}
        for i in range(inactive_count)
    ]
    return pd.DataFrame(rows)


@pytest.fixture
def pairs_env(monkeypatch, dash):  # noqa: F811
    monkeypatch.setenv("TEAM_PASSWORD", TEAM_PASSWORD)
    frame = pair_rows()
    monkeypatch.setattr(dash, "load_competitor_pairs", lambda: frame)
    calls = {"plan": [], "apply": [], "toggle": []}
    plan = pairs_store.Plan(market="US", our_asin=OUR, our_product="Our product", to_add=["B0NEWPAIR1", "B0NEWPAIR2"],
                            to_enable=["B0OFF00000"], already=["B0COMP0000"], invalid=["junk"], new_asins=2, active_now=3)

    def fake_plan(connect, our_text, market_choice, competitors_text, *, max_active=1500):
        calls["plan"].append((our_text, market_choice, competitors_text, max_active))
        return calls.get("plan_result", plan)

    def fake_apply(connect, plan_arg, *, actor_role, actor):
        calls["apply"].append((plan_arg, actor_role, actor))
        return {"add": len(plan_arg.to_add), "enable": len(plan_arg.to_enable)}

    def fake_toggle(connect, keys, active, *, actor_role, actor):
        calls["toggle"].append((list(keys), active, actor_role, actor))
        return len(keys)

    monkeypatch.setattr(pairs_store, "make_plan", fake_plan)
    monkeypatch.setattr(pairs_store, "apply_plan", fake_apply)
    monkeypatch.setattr(pairs_store, "set_pairs_active", fake_toggle)
    monkeypatch.setattr(pairs_store, "recent_changes", lambda connect, limit=30: [])
    monkeypatch.setattr(pairs_store, "journal_exists", lambda connect: True)
    calls["default_plan"] = plan
    return calls


def pairs_table(at):
    return [d.value for d in at.dataframe if "comp_asin" in d.value.columns and "active" in d.value.columns][0]


def metrics(at):
    return {m.label: m.value for m in at.metric}


def open_management(at):
    return unlock(at)


def test_guest_sees_summary_and_table_but_no_management(pairs_env):
    at = run()
    assert not at.exception
    assert metrics(at)["Активных пар"] == "3" and metrics(at)["Отключено"] == "1"
    assert set(pairs_table(at)["active"]) == {True}
    assert any("Чтобы добавлять и убирать пары" in c.value for c in at.caption)
    assert not [t for t in at.text_input if t.key == "pairs_our"]
    assert not [b for b in at.button if b.key == "pairs_add"]


def test_status_filter_shows_disabled_or_all_pairs(pairs_env):
    at = run()
    at.radio(key="pairs_status").set_value("Отключённые")
    at.run(timeout=30)
    assert set(pairs_table(at)["active"]) == {False}
    at.radio(key="pairs_status").set_value("Все")
    at.run(timeout=30)
    assert len(pairs_table(at)) == 4


def test_search_filters_the_pairs_table(pairs_env):
    at = run()
    at.text_input(key="pairs_search").input("competitor 1")
    at.run(timeout=30)
    assert list(pairs_table(at)["comp_asin"]) == ["B0COMP0001"]


def test_management_sections_appear_after_unlock(pairs_env):
    at = open_management(run())
    assert not at.exception
    labels = [e.label for e in at.expander]
    for expected in ("➕ Добавить конкурентов", "🗑 Убрать пары", "↩ Вернуть отключённые", "📜 Журнал изменений"):
        assert expected in labels
    assert [t for t in at.text_input if t.key == "pairs_our"]


def test_preview_shows_counts_cost_and_problems_before_anything_is_written(pairs_env):
    at = open_management(run())
    at.text_input(key="pairs_our").input(OUR)
    at.text_area(key="pairs_comps").input("B0NEWPAIR1 B0NEWPAIR2 B0OFF00000 B0COMP0000 junk")
    at.run(timeout=30)
    assert not at.exception
    assert pairs_env["plan"][-1][:3] == (OUR, None, "B0NEWPAIR1 B0NEWPAIR2 B0OFF00000 B0COMP0000 junk")
    shown = metrics(at)
    assert (shown["Новых"], shown["Вернутся"], shown["Уже есть"], shown["Не подошло"]) == ("2", "1", "1", "1")
    assert any("В каждом сборе добавится ASIN: 2" in c.value for c in at.caption)
    assert any("Не распознано" in w.value for w in at.warning)
    assert pairs_env["apply"] == []
    button = [b for b in at.button if b.key == "pairs_add"][0]
    assert button.label == "Добавить пар: 3" and not button.disabled


def test_chosen_marketplace_is_passed_to_the_plan(pairs_env):
    at = open_management(run())
    at.text_input(key="pairs_our").input(OUR)
    at.selectbox(key="pairs_market").select("CA")
    at.text_area(key="pairs_comps").input("B0NEWPAIR1")
    at.run(timeout=30)
    assert pairs_env["plan"][-1][1] == "CA"


def test_confirming_the_preview_writes_with_editor_role_and_the_actor(pairs_env):
    at = open_management(run())
    at.text_input(key="pairs_our").input(OUR)
    at.text_area(key="pairs_comps").input("B0NEWPAIR1 B0NEWPAIR2")
    at.run(timeout=30)
    [b for b in at.button if b.key == "pairs_add"][0].click()
    at.run(timeout=30)
    assert not at.exception
    (applied, role, actor), = pairs_env["apply"]
    assert role == access.ROLE_EDITOR and actor == "Аня" and applied.to_add == ["B0NEWPAIR1", "B0NEWPAIR2"]
    assert any("Готово: добавлено 2, возвращено 1" in s.value for s in at.success)
    assert at.text_area(key="pairs_comps").value == ""


def test_a_plan_with_errors_cannot_be_applied(pairs_env):
    pairs_env["plan_result"] = pairs_store.Plan(market="US", our_asin=OUR, to_add=["B0NEWPAIR1"], errors=["Активных пар станет больше лимита"])
    at = open_management(run())
    at.text_input(key="pairs_our").input(OUR)
    at.text_area(key="pairs_comps").input("B0NEWPAIR1")
    at.run(timeout=30)
    assert any("больше лимита" in e.value for e in at.error)
    assert [b for b in at.button if b.key == "pairs_add"][0].disabled
    assert pairs_env["apply"] == []


def test_store_failure_while_applying_is_shown_not_raised(monkeypatch, pairs_env):
    def broken(connect, plan, *, actor_role, actor):
        raise pairs_store.PairsStoreError("Операция с парами не подтверждена (OperationalError).")

    monkeypatch.setattr(pairs_store, "apply_plan", broken)
    at = open_management(run())
    at.text_input(key="pairs_our").input(OUR)
    at.text_area(key="pairs_comps").input("B0NEWPAIR1")
    at.run(timeout=30)
    [b for b in at.button if b.key == "pairs_add"][0].click()
    at.run(timeout=30)
    assert not at.exception
    assert any("не подтверждена" in e.value for e in at.error)


def test_select_all_then_disable_sends_every_active_pair_of_that_product(pairs_env):
    at = open_management(run())
    at.checkbox(key=f"pairs_remove_all_{OUR}").check()
    at.run(timeout=30)
    button = [b for b in at.button if b.key == "pairs_remove_btn"][0]
    assert button.label == "Отключить отмеченные (3)"
    button.click()
    at.run(timeout=30)
    assert not at.exception
    (keys, active, role, actor), = pairs_env["toggle"]
    assert active is False and role == access.ROLE_EDITOR and actor == "Аня"
    assert sorted(keys) == [("US", OUR, "B0COMP0000"), ("US", OUR, "B0COMP0001"), ("US", OUR, "B0COMP0002")]
    assert any("Готово: отключено 3" in s.value for s in at.success)


def test_nothing_is_disabled_until_something_is_ticked(pairs_env):
    at = open_management(run())
    button = [b for b in at.button if b.key == "pairs_remove_btn"][0]
    assert button.label == "Отключить отмеченные (0)" and button.disabled


def test_disabling_many_pairs_needs_a_typed_confirmation(monkeypatch, dash, pairs_env):  # noqa: F811
    big = pair_rows(active_count=30, inactive_count=0)
    monkeypatch.setattr(dash, "load_competitor_pairs", lambda: big)
    at = open_management(run())
    at.checkbox(key=f"pairs_remove_all_{OUR}").check()
    at.run(timeout=30)
    assert [b for b in at.button if b.key == "pairs_remove_btn"][0].disabled
    at.text_input(key="pairs_remove_confirm").input("нет")
    at.run(timeout=30)
    assert [b for b in at.button if b.key == "pairs_remove_btn"][0].disabled
    at.text_input(key="pairs_remove_confirm").input("убрать")
    at.run(timeout=30)
    button = [b for b in at.button if b.key == "pairs_remove_btn"][0]
    assert not button.disabled and button.label == "Отключить отмеченные (30)"
    button.click()
    at.run(timeout=30)
    assert len(pairs_env["toggle"][0][0]) == 30


def test_a_missing_journal_table_is_explained_and_does_not_break_the_page(monkeypatch, pairs_env):
    monkeypatch.setattr(pairs_store, "journal_exists", lambda connect: False)

    def must_not_be_read(connect, limit=30):
        raise AssertionError("журнал читать нельзя, пока таблицы нет")

    monkeypatch.setattr(pairs_store, "recent_changes", must_not_be_read)
    at = open_management(run())
    assert not at.exception and not at.error
    assert any("Журнал изменений ещё не подключён" in c.value for c in at.caption)


def test_change_log_is_shown_in_kyiv_time(monkeypatch, pairs_env):
    from datetime import datetime, timezone

    changes = [{"at": datetime(2026, 9, 21, 6, 30, tzinfo=timezone.utc), "actor": "Борис", "action": "disable",
                "marketplace": "US", "our_asin": OUR, "comp_asin": "B0COMP0000"}]
    monkeypatch.setattr(pairs_store, "recent_changes", lambda connect, limit=30: changes)
    at = open_management(run())
    log = [d.value for d in at.dataframe if "Кто" in d.value.columns][0]
    assert list(log["Когда (Киев)"]) == ["21.09 09:30"] and list(log["Кто"]) == ["Борис"] and list(log["Что"]) == ["отключена"]


def test_open_management_shows_the_pairs_manager_to_everyone_and_writes_as_the_team(monkeypatch, pairs_env):
    monkeypatch.delenv("TEAM_PASSWORD")
    at = run()
    assert not at.exception
    assert [t for t in at.text_input if t.key == "pairs_our"]
    assert not any("Чтобы добавлять и убирать пары" in c.value for c in at.caption)
    at.text_input(key="actor_name").input("Борис")
    at.run(timeout=30)
    at.text_input(key="pairs_our").input(OUR)
    at.text_area(key="pairs_comps").input("B0NEWPAIR1 B0NEWPAIR2")
    at.run(timeout=30)
    [b for b in at.button if b.key == "pairs_add"][0].click()
    at.run(timeout=30)
    (applied, role, actor), = pairs_env["apply"]
    assert role == access.ROLE_EDITOR and actor == "Борис"


def test_the_pairs_manager_stays_hidden_while_a_password_is_required(pairs_env):
    at = run()
    assert not [t for t in at.text_input if t.key == "pairs_our"]
    assert any("Чтобы добавлять и убирать пары" in c.value for c in at.caption)


def test_management_is_available_to_a_google_editor_without_the_team_password(monkeypatch, dash, pairs_env):  # noqa: F811
    monkeypatch.delenv("TEAM_PASSWORD", raising=False)
    monkeypatch.setenv("ADMIN_EMAILS", "boss@x.com")
    monkeypatch.setattr(access, "list_users", lambda connect: [])
    sign_in(monkeypatch, dash, logged_in("boss@x.com"))
    at = run()
    assert not at.exception
    assert [t for t in at.text_input if t.key == "pairs_our"]


def test_the_current_state_query_only_counts_active_pairs():
    import inspect

    import dashboard_db

    source = inspect.getsource(dashboard_db.load_current.__wrapped__ if hasattr(dashboard_db.load_current, "__wrapped__") else dashboard_db.load_current)
    assert "JOIN parser_not_test.competitor_pairs" in source and "p.active" in source
