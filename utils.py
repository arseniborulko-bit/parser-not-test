import logging
import re
from dataclasses import dataclass, asdict
from typing import Any, Dict, List, Optional, Union

from config import ASIN_PATTERN


def setup_logger(name: str = "amazon_parser", level: int = logging.INFO) -> logging.Logger:
    """Создаёт и настраивает логгер с единым форматом вывода."""
    logger = logging.getLogger(name)
    if not logger.handlers:
        handler = logging.StreamHandler()
        formatter = logging.Formatter(
            "[%(asctime)s] [%(levelname)s] [%(name)s] %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
        handler.setFormatter(formatter)
        logger.addHandler(handler)
        logger.setLevel(level)
    return logger


logger = setup_logger()


@dataclass
class ProductData:
    asin: str = ""
    title: str = ""
    stars: Union[float, str] = ""
    reviews: Union[int, str] = ""
    price: Union[float, str] = ""
    bsr: str = ""
    category: str = ""
    brand: str = ""
    stock_status: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def find_key_file(base_dir) -> Optional[str]:
    """Ищет файл ключа Google service account в рабочей папке."""
    for candidate in __import__("config").KEY_FILE_CANDIDATES:
        p = base_dir / candidate
        if p.exists():
            return str(p)
    return None


def normalize_header(values: List[str]) -> List[str]:
    return [v.strip().lower() for v in values]


PRICE_NUMBER_PATTERN = re.compile(r"\d[\d.,]*\d|\d")


def extract_price(raw: Any) -> Optional[float]:
    """
    Достаёт ТОЛЬКО денежную сумму из цены, даже если строка — это целая фраза
    вроде '99,95 € mit 7 Prozent Einsparungen' (реальный кейс с amazon.de:
    цена + текст скидки в одном поле). clean_number() в такой строке видит
    ВСЕ цифры сразу ("99,95" + "7" из "7 Prozent") и слепо клеит их в
    "99,957" → по правилу тысяч это превращается в 99957 — то есть в дикое
    число, которое реально ловили в проде (95176, 402217, 1190811 и т.п.).

    Здесь вместо этого сначала регуляркой вынимается ТОЛЬКО первое число в
    строке (цена всегда идёт первой, до текста скидки/сохранений), и только
    оно передаётся в clean_number — весь "довесок" после цены игнорируется.
    """
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        return clean_number(raw)
    text = str(raw).strip()
    if not text:
        return None
    match = PRICE_NUMBER_PATTERN.search(text)
    if not match:
        return None
    return clean_number(match.group(0))


def clean_number(value: Any) -> Optional[float]:
    """
    Безопасно приводит значение к float.
    Возвращает None, если значение отсутствует или некорректно.

    Понимает как американский формат ("$19.99", "1,234.56" — запятая это
    разделитель тысяч, точка — десятичный), так и европейский ("49,99 €",
    "1.234,56" — на амазонах DE/FR/ES/IT всё наоборот: точка — тысячи,
    запятая — десятичный разделитель). Раньше код всегда убирал запятые
    как разделитель тысяч ("49,99" → "4999"), что било реальную цену
    в 100 раз на всех европейских маркетплейсах.
    """
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        cleaned = re.sub(r"[^\d.,]", "", value)
        if not cleaned:
            return None

        has_comma = "," in cleaned
        has_dot = "." in cleaned

        if has_comma and has_dot:
            # Оба разделителя есть — тот, что встречается последним, десятичный.
            if cleaned.rfind(",") > cleaned.rfind("."):
                cleaned = cleaned.replace(".", "").replace(",", ".")
            else:
                cleaned = cleaned.replace(",", "")
        elif has_comma:
            # Только запятая. Если после последней запятой ровно 1-2 цифры —
            # это десятичный разделитель (европейский формат: "49,99").
            # Иначе — разделитель тысяч (например "1,234").
            parts = cleaned.split(",")
            last_group = parts[-1]
            if len(last_group) in (1, 2):
                integer_part = "".join(parts[:-1])
                cleaned = f"{integer_part}.{last_group}" if integer_part else last_group
            else:
                cleaned = cleaned.replace(",", "")
        elif has_dot and cleaned.count(".") == 1:
            # Только точка, один раз. "49.99" (2 цифры после точки) — почти
            # наверняка десятичная цена, не трогаем. "1.234" (ровно 3 цифры
            # после точки) — почти наверняка европейский разделитель тысяч
            # (частый случай для count'ов отзывов вида "1.234 Bewertungen"
            # на DE/AT), а не цена в 49.999 доллара — такое на Amazon не
            # встречается. Убираем точку в этом случае.
            after_dot = cleaned.split(".")[-1]
            if len(after_dot) == 3:
                cleaned = cleaned.replace(".", "")
        # else: несколько точек без запятых, либо чистое число — не трогаем.

        if not cleaned:
            return None
        try:
            return float(cleaned)
        except ValueError:
            return None
    return None


def extract_asin_from_text(value: Optional[str]) -> Optional[str]:
    """Достаёт ASIN как из чистого текста, так и из ссылки на товар."""
    if not value:
        return None
    value = str(value).strip()
    if not value:
        return None

    match = ASIN_PATTERN.search(value)
    if match:
        return match.group(1).upper()

    for pattern in (
        r"(?:dp|asin)/([A-Z0-9]{10})",
        r"[?&]asin=([A-Z0-9]{10})",
    ):
        match = re.search(pattern, value, re.IGNORECASE)
        if match:
            return match.group(1).upper()
    return None


def normalize_asin_input(value: Any) -> str:
    """Нормализует ASIN из ссылки/текста и поднимает ValueError при невалидном значении."""
    if value is None:
        raise ValueError("ASIN не может быть пустым")

    normalized = str(value).strip()
    if not normalized:
        raise ValueError("ASIN не может быть пустым")

    asin = extract_asin_from_text(normalized)
    if not asin:
        raise ValueError("Не удалось определить ASIN из ссылки или текста")
    return asin


# Ключ "Best Sellers Rank" в product_information берётся ScrapingDog'ом
# ПРЯМО из HTML-таблицы "Product Information" на странице Amazon — то есть
# его текст на языке конкретного маркетплейса, а не фиксированный английский
# идентификатор. Раньше код искал только "Best Sellers Rank" (EN: US/UK/CA/AU)
# и "Amazon BestsellerRang" (одна попытка под DE) — для FR/ES/IT ключ называется
# иначе, и код молча возвращал "Not Found", хотя ранг реально есть в ответе API.
# Здесь — по подстроке в НАЗВАНИИ ключа, без учёта регистра, под все маркетплейсы,
# которые поддерживает _amazon_domain() в sheets.py (US/CA/UK/DE/FR/ES/IT/MX/JP/AU).
BSR_KEY_FRAGMENTS = (
    "best sellers rank",                     # EN — US, CA, UK, AU
    "bestsellerrang",                        # DE (без дефиса — как приходило раньше)
    "bestseller-rang",                       # DE (с дефисом — как реально на странице)
    "clasificación en los más vendidos",     # ES
    "clasificacion en los mas vendidos",     # ES без диакритики
    "classement des meilleures ventes",      # FR
    "posizione nella classifica bestseller", # IT
    "clasificación en los más vendidos de amazon",  # ES (полный вариант)
    "ranking de mais vendidos",              # на случай MX/BR-локализации
    "ランキング",                              # JP (частая часть фразы на странице)
)


def _parse_bsr_number(raw: Any) -> str:
    """Достаёт число ранга из строки/числа вида '#1.234 в Category' и т.п."""
    if isinstance(raw, str):
        match = re.search(r"(\d[\d.,\s]*\d|\d)", raw)
        if match:
            digits_only = re.sub(r"[.,\s]", "", match.group(1))
            return digits_only or "Not Found"
        return "Not Found"
    if isinstance(raw, (int, float)):
        return str(int(raw))
    return "Not Found"


def extract_bsr(best_sellers_rank: Any) -> str:
    """
    Извлекает числовой BSR.

    Принимает либо готовое значение (обратная совместимость — как раньше:
    строка/число), либо весь словарь product_information — в этом случае сам
    ищет нужный ключ по всем известным локализованным названиям (см.
    BSR_KEY_FRAGMENTS) вместо жёстко заданных 1-2 английских/немецких ключей.
    Если ScrapingDog не вернул BSR вообще — возвращает 'Not Found', чтобы
    отличать это от полностью упавшего ASIN (там пишется '@').
    """
    if isinstance(best_sellers_rank, dict):
        for key, value in best_sellers_rank.items():
            key_norm = str(key).strip().casefold()
            if any(fragment in key_norm for fragment in BSR_KEY_FRAGMENTS):
                result = _parse_bsr_number(value)
                if result != "Not Found":
                    return result
        return "Not Found"

    return _parse_bsr_number(best_sellers_rank)


def extract_category(data: dict) -> str:
    value = (
        data.get("category")
        or data.get("categories")
        or data.get("product_category")
        or data.get("product_categories")
    )
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, list):
        items = []
        for item in value:
            if isinstance(item, str) and item.strip():
                items.append(item.strip())
            elif isinstance(item, dict) and item.get("name"):
                items.append(str(item["name"]).strip())
        return " > ".join(items)
    return ""


def average_price(products: List[Any]) -> float:
    """
    Средняя цена по списку товаров.
    products — список чисел ИЛИ список словарей/ProductData с ключом/атрибутом 'price'.
    """
    prices = []
    for product in products:
        if hasattr(product, "price"):
            raw = product.price
        elif isinstance(product, dict):
            raw = product.get("price")
        else:
            raw = product
        num = clean_number(raw)
        if num is not None:
            prices.append(num)
    return round(sum(prices) / len(prices), 2) if prices else 0.0


def build_parent_formula(asin: str, domain: str) -> str:
    domain_name = "www.amazon.com" if domain == "com" else f"www.amazon.{domain}"
    return f'=HYPERLINK("https://{domain_name}/dp/{asin}", "{asin}")'