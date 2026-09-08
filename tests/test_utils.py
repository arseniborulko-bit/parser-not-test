import importlib


def test_normalize_asin_input_extracts_from_url_and_plain_text():
    utils = importlib.import_module("utils")

    assert utils.normalize_asin_input("B0B917WMSF") == "B0B917WMSF"
    assert utils.normalize_asin_input("https://www.amazon.com/dp/B0CZ767JDG") == "B0CZ767JDG"


def test_normalize_asin_input_rejects_invalid_value():
    utils = importlib.import_module("utils")

    try:
        utils.normalize_asin_input("not-an-asin")
    except ValueError as exc:
        assert "ASIN" in str(exc)
    else:
        raise AssertionError("Expected ValueError for invalid ASIN input")
