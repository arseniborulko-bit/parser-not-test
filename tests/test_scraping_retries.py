"""ASIN, по которым ScrapingDog не дал данных, перезапрашиваются — максимум 3 попытки на ASIN,
и только на упавшие, а не на всю пачку (владелец, 28.09.2026: «те асины по которым нету данных
нужно прогнать 3 раза»)."""

import scraping


def fake_fetch(responses):
    """responses: {asin: [результат 1-й попытки, результат 2-й, ...]} — None = сбой, dict = успех.
    Меньше элементов, чем реально попыток, — на лишние попытки отдаёт последний элемент."""
    calls = []

    def fetch(asin, domain=None, token=None, session=None):
        calls.append(asin)
        sequence = responses.get(asin, [None])
        index = calls.count(asin) - 1
        return sequence[min(index, len(sequence) - 1)]

    fetch.calls = calls
    return fetch


def test_an_asin_that_fails_every_time_is_retried_up_to_three_attempts_total(monkeypatch):
    fetch = fake_fetch({"B0FAIL0001": [None, None, None]})
    monkeypatch.setattr(scraping, "fetch_product", fetch)

    collected, by_asin, failed = scraping.fetch_products_concurrent(["B0FAIL0001"], max_workers=1)

    assert failed == ["B0FAIL0001"]
    assert collected == [] and by_asin == {}
    assert fetch.calls.count("B0FAIL0001") == 3  # 1 обычная попытка + 2 повтора, не больше


def test_an_asin_that_succeeds_on_a_retry_is_not_retried_again(monkeypatch):
    fetch = fake_fetch({"B0RETRY001": [None, {"title": "T"}]})
    monkeypatch.setattr(scraping, "fetch_product", fetch)

    collected, by_asin, failed = scraping.fetch_products_concurrent(["B0RETRY001"], max_workers=1)

    assert failed == []
    assert "B0RETRY001" in by_asin
    assert fetch.calls.count("B0RETRY001") == 2  # упал один раз, получилось со второй — третья не нужна


def test_a_successful_asin_is_never_retried_even_if_others_in_the_batch_fail(monkeypatch):
    fetch = fake_fetch({
        "B0GOOD0001": [{"title": "OK"}],
        "B0BAD00001": [None, None, None],
    })
    monkeypatch.setattr(scraping, "fetch_product", fetch)

    collected, by_asin, failed = scraping.fetch_products_concurrent(
        ["B0GOOD0001", "B0BAD00001"], max_workers=2,
    )

    assert failed == ["B0BAD00001"]
    assert "B0GOOD0001" in by_asin
    assert fetch.calls.count("B0GOOD0001") == 1  # успешный ASIN не платит за чужие повторы
    assert fetch.calls.count("B0BAD00001") == 3


def test_max_attempts_is_configurable(monkeypatch):
    fetch = fake_fetch({"B0FAIL0002": [None, None, None, None, None]})
    monkeypatch.setattr(scraping, "fetch_product", fetch)

    _, _, failed = scraping.fetch_products_concurrent(["B0FAIL0002"], max_workers=1, max_attempts=5)

    assert failed == ["B0FAIL0002"]
    assert fetch.calls.count("B0FAIL0002") == 5


def test_an_exception_during_processing_counts_as_a_failure_and_is_retried(monkeypatch):
    """Непредвиденное исключение (не сетевая ошибка внутри fetch_product, а сбой при разборе
    результата) — тоже повод для повтора, не только data is None."""
    state = {"calls": 0}

    def flaky(asin, domain=None, token=None, session=None):
        state["calls"] += 1
        if state["calls"] == 1:
            raise RuntimeError("boom")
        return {"title": "OK"}

    monkeypatch.setattr(scraping, "fetch_product", flaky)
    collected, by_asin, failed = scraping.fetch_products_concurrent(["B0FLAKY001"], max_workers=1)

    assert failed == []
    assert "B0FLAKY001" in by_asin
    assert state["calls"] == 2
