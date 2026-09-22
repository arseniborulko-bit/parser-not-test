"""В таблицах дашборда пропущенное значение (NaN/None — например, не распознанная цена) должно быть
пустой ячейкой, а не видимым текстом "None"/"nan" — в этой версии Streamlit пустая строка рендерится
пустой ячейкой, а None/NaN — нет. Числовые столбцы форматируются в строку (иначе смесь float и ""
в одном столбце валит сериализацию в Arrow — см. _NUMERIC_LABELS). Без настоящей базы."""

import numpy as np
import pandas as pd

import dashboard_db


def test_missing_numeric_values_become_empty_strings_not_the_word_none():
    data = pd.DataFrame([{"our_asin": "B0X", "comp_asin": "B0Y", "marketplace": "US", "our_price": np.nan, "our_bsr": 100}])
    result, _ = dashboard_db._present_table(data)
    assert result["Цена наша"].tolist() == [""]
    assert result["BSR наш"].tolist() == ["100"]


def test_missing_text_values_become_empty_strings_too():
    data = pd.DataFrame([{"our_asin": "B0X", "comp_asin": "B0Y", "marketplace": "US", "comp_stock": None}])
    result, _ = dashboard_db._present_table(data)
    assert result["Наличие"].tolist() == [""]


def test_a_row_with_no_missing_values_is_unaffected():
    data = pd.DataFrame([{"our_asin": "B0X", "comp_asin": "B0Y", "marketplace": "US", "our_price": 12.5, "comp_stock": "In Stock"}])
    result, _ = dashboard_db._present_table(data)
    assert result["Цена наша"].tolist() == ["12.5"]
    assert result["Наличие"].tolist() == ["In Stock"]


def test_a_whole_number_price_does_not_get_a_trailing_dot_zero():
    data = pd.DataFrame([{"our_asin": "B0X", "comp_asin": "B0Y", "marketplace": "US", "our_price": 20.0}])
    result, _ = dashboard_db._present_table(data)
    assert result["Цена наша"].tolist() == ["20"]


def test_a_numeric_column_never_mixes_float_and_string_so_streamlit_can_serialize_it(monkeypatch):
    """Ловит регрессию: смешанный object-столбец (числа + "") падал в Arrow при показе таблицы."""
    import pyarrow as pa

    data = pd.DataFrame([
        {"our_asin": "B0X", "comp_asin": "B0Y", "marketplace": "US", "our_price": 12.5, "our_bsr": 100},
        {"our_asin": "B0Z", "comp_asin": "B0W", "marketplace": "US", "our_price": np.nan, "our_bsr": np.nan},
    ])
    result, _ = dashboard_db._present_table(data)
    pa.Table.from_pandas(result, preserve_index=False)  # не должно бросать ArrowTypeError/ArrowInvalid
