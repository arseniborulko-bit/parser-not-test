"""Ссылка на фото товара из ответа ScrapingDog: main_image, запасные варианты, парсинг в ProductData."""

import scraping


def test_prefers_main_image():
    assert scraping._extract_image_url({"main_image": "https://m.media-amazon.com/images/I/a.jpg", "images": ["https://x/b.jpg"]}) == \
        "https://m.media-amazon.com/images/I/a.jpg"


def test_falls_back_to_the_image_gallery_when_main_image_is_missing():
    assert scraping._extract_image_url({"images_of_specified_asin": ["https://x/c.jpg", "https://x/d.jpg"]}) == "https://x/c.jpg"
    assert scraping._extract_image_url({"images": ["https://x/e.jpg"]}) == "https://x/e.jpg"


def test_ignores_junk_values_and_returns_empty_string():
    assert scraping._extract_image_url({}) == ""
    assert scraping._extract_image_url({"main_image": ""}) == ""
    assert scraping._extract_image_url({"main_image": "not-a-url"}) == ""
    assert scraping._extract_image_url({"images": "https://x/not-a-list.jpg"}) == ""
    assert scraping._extract_image_url({"images": [123, None, "https://x/f.jpg"]}) == "https://x/f.jpg"


def test_parse_product_fills_image_url():
    product = scraping.parse_product({"title": "T", "main_image": "https://m.media-amazon.com/images/I/a.jpg"}, asin="B0TEST0001")
    assert product.image_url == "https://m.media-amazon.com/images/I/a.jpg"


def test_parse_product_without_any_image_leaves_it_empty():
    product = scraping.parse_product({"title": "T"}, asin="B0TEST0001")
    assert product.image_url == ""
