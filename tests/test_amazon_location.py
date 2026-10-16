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


class TextResponse:
    def __init__(self, status, text):
        self.status_code, self.text = status, text


class TextSession:
    def __init__(self, response):
        self.response, self.calls = response, []

    def get(self, url, params, timeout):
        self.calls.append((url, params, timeout))
        return self.response


def test_scrape_page_uses_the_generic_endpoint_and_hides_the_token(caplog):
    ok = TextSession(TextResponse(200, "<html>ok</html>"))
    assert scraping.scrape_page("https://www.amazon.com/gp/bestsellers/fashion/1", token="T", session=ok) == "<html>ok</html>"
    ((url, params, timeout),) = ok.calls
    assert url == "https://api.scrapingdog.com/scrape" and timeout >= 45
    assert params == {"api_key": "T", "url": "https://www.amazon.com/gp/bestsellers/fashion/1", "dynamic": "false"}
    bad = TextSession(TextResponse(410, "timed out for key T"))
    assert scraping.scrape_page("https://x", token="T", session=bad) is None
    assert "timed out for key ***" in caplog.text and "key T" not in caplog.text
