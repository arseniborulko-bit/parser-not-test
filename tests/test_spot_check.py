"""Точечная проверка ASIN: разбор, лимиты расхода и обработка ответов. Без настоящего ScrapingDog."""

from types import SimpleNamespace

import pytest

import spot_check
from spot_check import SpotBudget, SpotBudgetError

A, B, C = "B0ABCDEFG1", "B0ABCDEFG2", "B0ABCDEFG3"
TOKEN = "fake-scrapingdog-token"


class FakeClock:
    def __init__(self):
        self.now = 10_000.0

    def __call__(self):
        return self.now


def test_plan_uses_the_default_market_unless_a_link_or_suffix_says_otherwise():
    plan = spot_check.plan_spot_check(f"{A} {B}:DE https://www.amazon.ca/dp/{C}", "US")
    assert plan.items == [(A, "US"), (B, "DE"), (C, "CA")] and plan.errors == []


def test_plan_reports_invalid_items_and_repeats():
    plan = spot_check.plan_spot_check(f"{A} {A} junk", "US")
    assert plan.items == [(A, "US")] and plan.invalid == ["junk"] and plan.repeats == 1


@pytest.mark.parametrize("text, market, expected", [
    ("", "US", "Вставьте ASIN"),
    ("junk", "US", "Вставьте ASIN"),
    (A, "XX", "Выберите маркетплейс"),
    (" ".join(f"B0{i:08d}" for i in range(spot_check.MAX_PER_CHECK + 1)), "US", "не больше"),
])
def test_plan_errors(text, market, expected):
    plan = spot_check.plan_spot_check(text, market)
    assert plan.items == [] and expected in plan.errors[0]


def test_exactly_the_maximum_is_accepted():
    text = " ".join(f"B0{i:08d}" for i in range(spot_check.MAX_PER_CHECK))
    assert len(spot_check.plan_spot_check(text, "US").items) == spot_check.MAX_PER_CHECK


def test_budget_allows_up_to_the_hourly_limit_and_then_refuses_everything():
    budget = SpotBudget(hour=3, day=10, clock=FakeClock())
    budget.consume(2)
    assert budget.remaining() == 1
    budget.consume(1)
    with pytest.raises(SpotBudgetError, match="в час"):
        budget.consume(1)
    assert budget.remaining() == 0


def test_a_refused_request_takes_nothing():
    budget = SpotBudget(hour=3, day=10, clock=FakeClock())
    budget.consume(2)
    with pytest.raises(SpotBudgetError):
        budget.consume(2)
    assert budget.remaining() == 1


def test_the_hourly_window_slides_and_the_daily_one_holds():
    clock = FakeClock()
    budget = SpotBudget(hour=2, day=3, clock=clock)
    budget.consume(2)
    clock.now += 3601
    assert budget.remaining() == 1
    budget.consume(1)
    clock.now += 3601
    assert budget.remaining() == 0
    with pytest.raises(SpotBudgetError, match="в сутки"):
        budget.consume(1)
    clock.now += 86400
    assert budget.remaining() == 2


def fake_scraping(responses, calls=None):
    def fetch(asin, domain=None, token=None):
        if calls is not None:
            calls.append((asin, domain, token))
        result = responses[asin]
        if isinstance(result, Exception):
            raise result
        return result

    def parse(data, asin=""):
        if data == "broken":
            raise ValueError("cannot parse")
        return SimpleNamespace(title=f"Title {asin}", price=12.5, bsr="1 234", stock_status="In Stock", stars=4.5,
                               reviews=100, category="Clothing", brand="Brand")

    return lambda: SimpleNamespace(fetch_product=fetch, parse_product=parse)


def plan_of(*items):
    plan = spot_check.SpotPlan()
    plan.items = list(items)
    return plan


def test_results_come_back_in_order_with_the_fields_a_person_needs():
    calls = []
    loader = fake_scraping({A: {"ok": 1}, B: {"ok": 2}}, calls)
    rows = spot_check.run_spot_check(plan_of((A, "US"), (B, "CA")), TOKEN, SpotBudget(), loader=loader)
    assert [row["ASIN"] for row in rows] == [A, B]
    assert rows[0] == {
        "ASIN": A, "Страна": "US", "Статус": "ок", "Название": f"Title {A}", "Цена": 12.5, "BSR": "1 234",
        "Наличие": "In Stock", "Оценка": 4.5, "Отзывы": 100, "Категория": "Clothing", "Бренд": "Brand",
    }
    assert sorted(calls) == [(A, "com", TOKEN), (B, "ca", TOKEN)]


def test_failures_are_marked_per_item_without_breaking_the_rest_or_leaking_details():
    loader = fake_scraping({A: None, B: RuntimeError(f"boom {TOKEN}"), C: "broken", "B0ABCDEFG4": {"ok": 1}})
    rows = spot_check.run_spot_check(
        plan_of((A, "US"), (B, "US"), (C, "US"), ("B0ABCDEFG4", "US")), TOKEN, SpotBudget(), loader=loader,
    )
    assert [row["Статус"] for row in rows] == ["не получено", "ошибка запроса", "не удалось разобрать ответ", "ок"]
    assert TOKEN not in str(rows)


def test_every_requested_asin_costs_budget_even_when_it_fails():
    budget = SpotBudget(hour=5, day=5, clock=FakeClock())
    spot_check.run_spot_check(plan_of((A, "US"), (B, "US")), TOKEN, budget, loader=fake_scraping({A: None, B: None}))
    assert budget.remaining() == 3


def test_an_exhausted_budget_stops_before_any_request():
    calls = []
    budget = SpotBudget(hour=1, day=1, clock=FakeClock())
    with pytest.raises(SpotBudgetError):
        spot_check.run_spot_check(plan_of((A, "US"), (B, "US")), TOKEN, budget, loader=fake_scraping({A: {}, B: {}}, calls))
    assert calls == []


def test_no_token_or_a_plan_with_errors_never_reaches_the_api_or_the_budget():
    calls = []
    budget = SpotBudget(hour=5, day=5, clock=FakeClock())
    loader = fake_scraping({A: {}}, calls)
    with pytest.raises(ValueError):
        spot_check.run_spot_check(plan_of((A, "US")), None, budget, loader=loader)
    bad = spot_check.SpotPlan(errors=["нельзя"])
    with pytest.raises(ValueError, match="нельзя"):
        spot_check.run_spot_check(bad, TOKEN, budget, loader=loader)
    with pytest.raises(ValueError):
        spot_check.run_spot_check(spot_check.SpotPlan(), TOKEN, budget, loader=loader)
    assert calls == [] and budget.remaining() == 5


def test_the_default_loader_is_looked_up_at_call_time_so_tests_can_block_the_real_api(monkeypatch):
    def blocked():
        raise AssertionError("настоящий ScrapingDog вызывать нельзя")

    monkeypatch.setattr(spot_check, "_load_scraping", blocked)
    budget = SpotBudget(hour=5, day=5, clock=FakeClock())
    with pytest.raises(AssertionError, match="ScrapingDog"):
        spot_check.run_spot_check(plan_of((A, "US")), TOKEN, budget)


def test_the_real_scraping_module_is_loaded_lazily():
    import inspect

    source = inspect.getsource(spot_check)
    assert "\nimport scraping" not in source and "\nfrom scraping" not in source
