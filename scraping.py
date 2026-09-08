import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Callable, Dict, List, Optional, Tuple

import requests
from urllib3.util import Retry
from requests.adapters import HTTPAdapter

from config import DOMAIN, MAX_CONCURRENT_WORKERS, REQUEST_TIMEOUT, get_scrapingdog_token
from utils import ProductData, clean_number, extract_bsr, extract_category, extract_price, logger


# domain (TLD amazon.<domain>) -> country-параметр ScrapingDog.
# ВАЖНО: без явного "country" ScrapingDog по умолчанию использует "us" даже
# при domain="de"/"fr"/... — то есть просит страницу amazon.de, но "глазами"
# покупателя из США. Amazon в таком рассинхроне часто прячет цену за выбором
# адреса доставки, и в ответе API целиком отсутствует поле "price" (не пустое,
# а именно отсутствует) — это подтвердилось на реальном сыром JSON для DE.
DOMAIN_TO_COUNTRY = {
    "com": "us",
    "ca": "ca",
    "co.uk": "gb",
    "de": "de",
    "fr": "fr",
    "es": "es",
    "it": "it",
    "com.mx": "mx",
    "co.jp": "jp",
    "com.au": "au",
}


def _get_http_session() -> requests.Session:
    """
    Создаёт сессию requests БЕЗ автоматических повторов на уровне транспорта.
    Повторные попытки — и, соответственно, повторное списание токенов ScrapingDog —
    здесь намеренно не делаются: 1 ASIN = максимум 1 фактический запрос к API.
    """
    session = requests.Session()
    retries = Retry(total=0)
    adapter = HTTPAdapter(max_retries=retries, pool_connections=10, pool_maxsize=20)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


def normalize_stock_status(value: Optional[str]) -> str:
    """
    Приводит статусы Amazon к значениям, используемым в матрице.

    Раньше проверялись только английские фразы, поэтому для DE/FR/ES/IT
    (где availability_status приходит на языке страницы) функция всегда
    возвращала "" — не "Not Found", а просто пусто, но по факту это то же
    самое молчаливое отсутствие данных, только не заметное в диагностике.
    Ниже — те же 3 категории, но с фразами на всех поддерживаемых языках.
    """
    text = str(value or "").strip().casefold()
    if not text:
        return ""

    out_of_stock_markers = (
        "out of stock", "currently unavailable", "unavailable",  # EN
        "derzeit nicht verfügbar", "nicht auf lager", "nicht verfügbar",  # DE
        "actuellement indisponible", "rupture de stock", "indisponible",  # FR
        "actualmente no disponible", "no disponible", "agotado",  # ES
        "attualmente non disponibile", "non disponibile", "esaurito",  # IT
    )
    low_stock_markers = (
        "order soon", "low stock", "only ", "few left", "limited stock",  # EN
        "nur noch", "bald bestellen", "geringer bestand",  # DE
        "commandez vite", "il ne reste plus que", "stock limité",  # FR
        "pedido pronto", "quedan pocas unidades", "solo queda",  # ES
        "ordina presto", "ne restano solo", "scorte limitate",  # IT
    )
    in_stock_markers = (
        "in stock", "available",  # EN
        "auf lager", "verfügbar",  # DE
        "en stock", "disponible",  # FR/ES
        "disponibile",  # IT
    )

    if any(marker in text for marker in out_of_stock_markers):
        return "Out of Stock"
    if any(marker in text for marker in low_stock_markers):
        return "Low Stock (<5)"
    if any(marker in text for marker in in_stock_markers):
        return "In Stock"
    return ""


def fetch_product(asin: str, domain: str = DOMAIN, token: Optional[str] = None, session: Optional[requests.Session] = None) -> Optional[dict]:
    """
    Делает ровно один запрос к ScrapingDog на один ASIN (без повторов),
    чтобы расход токенов был предсказуемым: 1 ASIN = 1 токен.
    Возвращает распарсенный JSON или None при ошибке.
    """
    token = token or get_scrapingdog_token()
    country = DOMAIN_TO_COUNTRY.get(str(domain).strip().lower(), "us")
    params = {"api_key": token, "asin": asin, "domain": domain, "country": country}
    req_session = session or _get_http_session()

    try:
        response = req_session.get(
            "https://api.scrapingdog.com/amazon/product",
            params=params,
            timeout=REQUEST_TIMEOUT,
        )
    except requests.RequestException as e:
        safe_error = str(e).replace(token, "***") if token else str(e)
        logger.warning(f"[{asin}] Ошибка сети: {safe_error}")
        return None

    if response.status_code == 200:
        try:
            return response.json()
        except ValueError:
            logger.error(f"[{asin}] Сервер вернул невалидный JSON.")
            return None

    logger.error(f"[{asin}] Ошибка {response.status_code}: {response.text[:200]}")
    return None


def parse_product(data: dict, asin: str = "") -> ProductData:
    """Достаёт нужные поля из ответа API и возвращает структуру ProductData."""
    product_info = data.get("product_information", {}) or {}

    title = data.get("title") or ""
    stars = clean_number(data.get("average_rating"))
    reviews = clean_number(data.get("total_reviews"))

    raw_price_field = (
        data.get("price")
        or data.get("product_price")
        or data.get("discounted_price")
        or data.get("previous_price")
    )
    # extract_price (не clean_number!) — берёт только первое число в строке,
    # игнорируя текст скидки/сохранений, который ScrapingDog иногда
    # приклеивает к цене в одном поле (см. комментарий в utils.py).
    price = extract_price(raw_price_field)

    # Защитный фильтр: цена в этой нише (термобельё/одежда из мерино) физически
    # не бывает в десятки/сотни тысяч $. Такие значения — почти наверняка баг
    # склейки полей (например, к цене приклеивается кусок соседнего поля —
    # рейтинга, скидки, кол-ва отзывов), а не реальная цена товара. Раньше это
    # число просто записывалось в таблицу и портило среднюю цену по прогону.
    # Пока не нашли точную причину — считаем такую цену недостоверной и не
    # затираем её на "Not Found" молча: логируем сырое значение, чтобы можно
    # было прицельно разобрать конкретный ASIN через debug_price_fields.py.
    PRICE_SANITY_MAX = 3000  # реальные цены в этой категории товаров < $3000
    if price is not None and (price > PRICE_SANITY_MAX or price <= 0):
        logger.warning(
            f"[{asin}] Похоже на битую цену: сырое значение {raw_price_field!r} "
            f"распарсилось в {price} — отбрасываю, ставлю 'Not Found'. "
            f"Разобрать: python debug_price_fields.py {asin} --domain <домен>"
        )
        price = None
    elif price is not None:
        # Валюта не бывает с 3-4 знаками после запятой (76.491, 67.0015 и т.п.
        # видели в реальных логах) — округляем до цента, чтобы такие "хвосты"
        # не расползались дальше по таблице и расчётам.
        price = round(price, 2)

    # Передаём весь product_information, а не 1-2 захардкоженных ключа:
    # extract_bsr сама ищет нужное поле по всем локализованным названиям
    # (US/UK/CA/AU/DE/FR/ES/IT/JP), см. BSR_KEY_FRAGMENTS в utils.py.
    bsr = extract_bsr(product_info)
    category = extract_category(data)
    stock_status = (
        data.get("availability")
        or data.get("availability_status")
        or product_info.get("Availability")
        or product_info.get("availability")
        or ""
    )

    # ScrapingDog ответил успешно, но конкретное поле в ответе отсутствует —
    # пишем "Not Found", а не пустую строку. Это отличается от случая, когда
    # весь ASIN не удалось получить вообще (тогда в таблице ставится "@" и
    # ASIN подсвечивается жёлтым — см. sheets.py / mark_failed_asins_in_competitors).
    return ProductData(
        asin=asin or data.get("asin", ""),
        title=title if title else "Not Found",
        stars=stars if stars is not None else "Not Found",
        reviews=int(reviews) if reviews is not None else "Not Found",
        price=price if price is not None else "Not Found",
        bsr=bsr,
        category=category,
        brand=data.get("brand") or data.get("manufacturer") or "",
        stock_status=normalize_stock_status(stock_status),
    )


def fetch_products_concurrent(
    asins: List[str],
    domain: str = DOMAIN,
    token: Optional[str] = None,
    max_workers: int = MAX_CONCURRENT_WORKERS,
    progress_callback: Optional[Callable[[float, str], None]] = None,
    asin_domains: Optional[Dict[str, str]] = None,
) -> Tuple[List[ProductData], Dict[str, ProductData], List[str]]:
    """
    Параллельный сбор данных о товарах с использованием пула потоков.
    Возвращает (список собранных ProductData, словарь по asin, список необработанных asin).

    asin_domains — опциональная карта {ASIN: домен amazon}. Если для конкретного ASIN
    в ней есть запись — запрос уходит именно на этот домен (например, "ca" для
    канадского товара), а не на общий domain по умолчанию. Это важно: ASIN валиден
    только на своём маркетплейсе, и запрос "чужого" ASIN на amazon.com часто
    возвращает ошибку/не тот товар, даже если на своём домене (amazon.ca, amazon.co.uk
    и т.д.) он существует.
    """
    token = token or get_scrapingdog_token()
    session = _get_http_session()
    collected: List[ProductData] = []
    products_by_asin: Dict[str, ProductData] = {}
    failed_asins: List[str] = []

    total = len(asins) or 1
    completed_count = 0

    def task(asin: str) -> Tuple[str, Optional[dict]]:
        asin_domain = (asin_domains or {}).get(asin, domain)
        data = fetch_product(asin, domain=asin_domain, token=token, session=session)
        return asin, data

    with ThreadPoolExecutor(max_workers=min(max_workers, len(asins) or 1)) as executor:
        futures = {executor.submit(task, asin): asin for asin in asins}
        for future in as_completed(futures):
            asin = futures[future]
            completed_count += 1
            if progress_callback:
                progress_callback(completed_count / total, f"Обработан {asin} ({completed_count}/{total})")
            
            try:
                _, data = future.result()
                if data is None:
                    failed_asins.append(asin)
                else:
                    product = parse_product(data, asin=asin)
                    collected.append(product)
                    products_by_asin[asin] = product
                    logger.info(f"Успешно обработан [{asin}]: {product.title[:50]}... | Цена: {product.price} | BSR: {product.bsr}")
            except Exception as exc:
                logger.error(f"Непредвиденная ошибка при обработке {asin}: {exc}")
                failed_asins.append(asin)

    return collected, products_by_asin, failed_asins