"""Индекс доставки в каждом запросе к ScrapingDog (владелец 16.10.2026): без него Amazon отдаёт страницу для
случайного адреса и BSR/цена не совпадают с тем, что видит команда. Без сети."""

import pytest

import amazon_location
import pairs_store
import scraping


def test_every_marketplace_has_a_postal_code():
    assert set(amazon_location.POSTAL_CODE_BY_DOMAIN) == set(pairs_store.DOMAIN_BY_MARKET.values())
    assert amazon_location.POSTAL_CODE_BY_DOMAIN["de"] == "10115"
    assert amazon_location.location_params("unknown") == {}


class Response:
    status_code = 200

    def json(self):
        return {"title": "x"}


class Session:
    def __init__(self):
        self.calls = []

    def get(self, url, params, timeout):
        self.calls.append((url, params))
        self.timeout = timeout
        return Response()


@pytest.mark.parametrize("domain, country, postal", [("de", "de", "10115"), ("com", "us", "10001"),
                                                      ("it", "it", "20121"), ("co.uk", "gb", "EC1A 1BB")])
def test_product_request_carries_the_postal_code(domain, country, postal):
    session = Session()
    assert scraping.fetch_product("B07WVYMY7Y", domain=domain, token="T", session=session) == {"title": "x"}
    ((url, params),) = session.calls
    assert url == "https://api.scrapingdog.com/amazon/product"
    assert params == {"api_key": "T", "asin": "B07WVYMY7Y", "domain": domain, "country": country,
                      "postal_code": postal}
    # С индексом ответ идёт до ~40 с: 15 с обрывали запрос (замер 16.10.2026).
    assert session.timeout >= 45
