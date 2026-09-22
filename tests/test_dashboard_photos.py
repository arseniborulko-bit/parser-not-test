"""«Текущее состояние» показывает фото товара и конкурента; «История» — нет. Без настоящей базы."""

import pandas as pd

import dashboard_db

ROW = {
    "snapshot_date": "2026-09-22", "marketplace": "US", "our_asin": "B0OUR00001", "our_product": "Our",
    "comp_asin": "B0COMP0001", "competitor_name": "Comp",
    "our_image_url": "https://m.media-amazon.com/images/I/our.jpg", "comp_image_url": "",
}


def test_photo_columns_are_added_first_with_an_image_column_config():
    result, config = dashboard_db._present_table(pd.DataFrame([ROW]), with_images=True)
    assert list(result.columns)[:2] == ["Фото наш", "Фото конкурента"]
    assert result["Фото наш"].tolist() == ["https://m.media-amazon.com/images/I/our.jpg"]
    assert isinstance(config["Фото наш"], object) and isinstance(config["Фото конкурента"], object)


def test_a_missing_photo_stays_an_empty_string_not_the_text_none():
    """В этой версии Streamlit ImageColumn рисует Python None как видимый текст "None"; пустая строка — пустая ячейка."""
    result, _ = dashboard_db._present_table(pd.DataFrame([ROW]), with_images=True)
    assert result["Фото конкурента"].tolist() == [""]
    assert result["Фото конкурента"].isna().sum() == 0


def test_the_raw_url_column_does_not_also_appear_as_plain_text():
    result, _ = dashboard_db._present_table(pd.DataFrame([ROW]), with_images=True)
    assert "our_image_url" not in result.columns and "comp_image_url" not in result.columns


def test_the_history_table_never_gets_photo_columns_even_if_present_in_the_data():
    result, config = dashboard_db._present_table(pd.DataFrame([ROW]))
    assert "Фото наш" not in result.columns and "Фото конкурента" not in result.columns
    assert "Фото наш" not in config


def test_missing_image_columns_do_not_break_the_table():
    row = {k: v for k, v in ROW.items() if "image" not in k}
    result, config = dashboard_db._present_table(pd.DataFrame([row]), with_images=True)
    assert "Фото наш" not in result.columns and "Фото наш" not in config
