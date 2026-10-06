"""Продавцы на наших карточках (offers.py, check_offers.py): без сети, без настоящей базы."""

import offers
import check_offers
from test_access import FakeDb

SAMPLE = {  # формат из документации ScrapingDog Amazon Offers API
    "asin": "B0BZXDFGSJ", "offers_count": 3,
    "offers": [
        {"price": {"symbol": "$", "value": 38.97, "currency": "USD", "raw": "$38.97"},
         "seller": {"name": "Merino Store", "id": "AOURS123"},
         "condition": {"is_new": True, "title": "New"},
         "delivery": {"fulfilled_by_amazon": True}, "buybox_winner": True},
        {"price": {"value": 43.95, "currency": "USD"},
         "seller": {"name": "Orva Stores", "id": "A2NEM58BFPMEIL"},
         "condition": {"is_new": True}, "delivery": {"fulfilled_by_amazon": False}, "buybox_winner": False},
        "мусор",
        {"price": "битая", "seller": None},
    ],
}
OURS = offers.OurSellers.from_environment({"OUR_SELLER_IDS": "aours123", "OUR_SELLER_NAMES": ""})


def test_parse_offers_reads_seller_price_fba_and_buybox_and_skips_junk():
    parsed = offers.parse_offers(SAMPLE)
    assert parsed[0] == offers.Offer("AOURS123", "Merino Store", 38.97, "USD", True, True, True)
    assert parsed[1].seller_id == "A2NEM58BFPMEIL" and parsed[1].buybox is False and parsed[1].fba is False
    assert parsed[2] == offers.Offer("", "", None, "", None, None, False)
    assert len(parsed) == 3
    assert offers.parse_offers({}) == [] and offers.parse_offers(None) == []


def test_our_seller_is_matched_by_id_or_by_name_case_insensitively():
    by_name = offers.OurSellers.from_environment({"OUR_SELLER_NAMES": " merino   STORE ; Other"})
    offer = offers.parse_offers(SAMPLE)[0]
    assert by_name.is_ours(offer) and OURS.is_ours(offer)
    assert not OURS.is_ours(offers.parse_offers(SAMPLE)[1])
    assert not offers.OurSellers.from_environment({}).known


def test_a_foreign_seller_is_reported_and_the_buybox_is_still_ours():
    check = offers.make_check("US", "B0BZXDFGSJ", "Футболка", offers.parse_offers(SAMPLE), OURS)
    assert [o.seller_name for o in check.foreign] == ["Orva Stores", ""]
    assert not check.buybox_lost
    lines = offers.alert_lines([check], {"US": "com"})
    assert lines[0].startswith("<b>US</b>") and "amazon.com/dp/B0BZXDFGSJ" in lines[0]
    assert "🚨 Orva Stores — 43.95 USD" in lines[1]
    assert not any("Buy Box" in line for line in lines)


def test_a_lost_buybox_is_reported():
    data = {"offers": [
        {"price": {"value": 30.0, "currency": "EUR"}, "seller": {"name": "Hijacker", "id": "AX"},
         "delivery": {"fulfilled_by_amazon": True}, "buybox_winner": True},
        {"price": {"value": 35.0, "currency": "EUR"}, "seller": {"name": "Merino Store", "id": "AOURS123"},
         "buybox_winner": False},
    ]}
    check = offers.make_check("DE", "B0X", "<Kid & Co>", offers.parse_offers(data), OURS)
    assert check.buybox_lost
    lines = offers.alert_lines([check], {"DE": "de"})
    assert "&lt;Kid &amp; Co&gt;" in lines[0] and "amazon.de/dp/B0X" in lines[0]
    assert "⚠️ Buy Box у «Hijacker» — 30.00 EUR" in lines[1]
    assert "🚨 Hijacker — 30.00 EUR · FBA" in lines[2]


def test_without_knowing_our_seller_nothing_is_alarming():
    check = offers.make_check("US", "B0BZXDFGSJ", "", offers.parse_offers(SAMPLE), offers.OurSellers.from_environment({}))
    assert check.foreign == [] and not check.buybox_lost
    assert offers.alert_lines([check], {}) == []


def test_only_our_seller_means_no_alert():
    data = {"offers": [SAMPLE["offers"][0]]}
    check = offers.make_check("US", "B0BZXDFGSJ", "", offers.parse_offers(data), OURS)
    assert offers.alert_lines([check], {"US": "com"}) == []


def test_fetch_offers_hides_the_token_and_returns_none_on_errors():
    class Response:
        def __init__(self, status, body):
            self.status_code, self._body, self.text = status, body, f"error for key SECRET {status}"

        def json(self):
            if self._body is None:
                raise ValueError
            return self._body

    class Session:
        def __init__(self, response):
            self.response, self.calls = response, []

        def get(self, url, params, timeout):
            self.calls.append((url, params))
            return self.response

    ok = Session(Response(200, SAMPLE))
    assert offers.fetch_offers("B0BZXDFGSJ", "de", "de", "SECRET", session=ok) == SAMPLE
    url, params = ok.calls[0]
    assert url == "https://api.scrapingdog.com/amazon/offers"
    assert params == {"api_key": "SECRET", "asin": "B0BZXDFGSJ", "domain": "de", "country": "de"}
    assert offers.fetch_offers("B0", "com", "us", "SECRET", session=Session(Response(500, {}))) is None
    assert offers.fetch_offers("B0", "com", "us", "SECRET", session=Session(Response(200, None))) is None


def test_save_checks_writes_one_row_per_offer_and_a_row_for_an_empty_listing():
    db = FakeDb()
    full = offers.make_check("US", "B0A", "A", offers.parse_offers(SAMPLE), OURS)
    empty = offers.make_check("UK", "B0B", "B", [], OURS)
    assert offers.save_checks(db.connect, [full, empty]) == 4
    rows = [params for _, params in db.executed]
    assert rows[0][3:] == ("AOURS123", "Merino Store", 38.97, "USD", True, True, True, True)
    assert rows[1][-1] is False  # чужой
    assert rows[3] == ("UK", "B0B", "B", None, None, None, None, None, None, False, None)
    assert db.commits == 1  # весь прогон — одна транзакция


def test_our_targets_are_unique_per_country_and_asin():
    pairs = [
        {"marketplace": "US", "our_asin": "B0A", "our_product": "A", "comp_asin": "C1"},
        {"marketplace": "US", "our_asin": "B0A", "our_product": "A", "comp_asin": "C2"},
        {"marketplace": "UK", "our_asin": "B0A", "our_product": "A uk", "comp_asin": "C3"},
        {"marketplace": "US", "our_asin": "", "our_product": "", "comp_asin": "C4"},
    ]
    assert check_offers.our_targets(pairs) == [("US", "B0A", "A"), ("UK", "B0A", "A uk")]


def test_collect_retries_only_the_failed_asins_once():
    calls = []
    answers = {"B0A": [SAMPLE], "B0B": [None, {"offers": []}], "B0C": [None, None]}

    def fetch(asin, domain, country):
        calls.append((asin, domain, country))
        return answers[asin].pop(0)

    targets = [("US", "B0A", "A"), ("DE", "B0B", "B"), ("US", "B0C", "C")]
    checks, missing = check_offers.collect(
        targets, fetch, {"US": "com", "DE": "de"}.get, {"com": "us", "de": "de"}.get, OURS)
    assert [c.asin for c in checks] == ["B0A", "B0B"]
    assert missing == [("US", "B0C", "C")]
    assert sorted(calls) == sorted([("B0A", "com", "us"), ("B0B", "de", "de"), ("B0C", "com", "us"),
                                    ("B0B", "de", "de"), ("B0C", "com", "us")])
