import os
import html
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from config import (
    BATCH_SIZE,
    BROKEN_ASINS_SHEET_URL,
    DOMAIN,
    FALLBACK_ASINS,
    MAX_CONCURRENT_WORKERS,
    TELEGRAM_BOT_TOKEN,
    TELEGRAM_CHAT_ID,
    get_scrapingdog_token,
    resolve_asins_from_environment,
)
from scraping import fetch_products_concurrent
from sheets import _amazon_domain, build_asin_domain_map, connect_sheet, load_asins_from_config, refresh_current_matrix
from subscribers import get_active_subscriber_ids
from telegram import broadcast_telegram_message, broadcast_telegram_report
from utils import ProductData, average_price, clean_number, extract_asin_from_text, find_key_file, logger


def format_product_telegram_message(product: ProductData) -> str:
    """
    Формирует уведомление Telegram о собранном товаре.

    Сейчас НЕ вызывается в основном потоке run_parser() — раньше отправлялась
    по каждому товару отдельно, что при сотнях ASIN превращалось в сотни
    сообщений за прогон без полезного сигнала. Оставлена как готовый формат
    на случай, если понадобится точечное уведомление (например, для будущего
    алерта по большим изменениям цены/BSR).
    """
    title_short = (product.title[:80] + "…") if len(product.title) > 80 else product.title
    return (
        f"📦 <b>{product.asin}</b>\n"
        f"{title_short}\n"
        f"Цена: {product.price or '—'}\n"
        f"Рейтинг: {product.stars or '—'} ⭐ ({product.reviews or 0} отзывов)\n"
        f"BSR: {product.bsr or '—'}\n"
        f"Наличие: {product.stock_status or '—'}"
    )


def _asin_link(asin: Optional[str], marketplace: Optional[str]) -> str:
    """Кликабельная HTML-ссылка на товар для Telegram (parse_mode=HTML)."""
    if not asin:
        return "—"
    domain = _amazon_domain(marketplace or "US")
    return f'<a href="https://www.amazon.{domain}/dp/{asin}">{html.escape(asin)}</a>'


def _fmt_bsr(value) -> str:
    return f"{int(value):,}".replace(",", " ")


def _fmt_price(value) -> str:
    return f"${value:.2f}"


def _fmt_rating(value) -> str:
    return f"{value:.1f}"


def _fmt_reviews(value) -> str:
    return f"{int(value)}"


def _bullet_best(label: str, our_value, comp_entries, fmt, better: str) -> Optional[str]:
    """
    Строка-буллет вида "Рейтинг: ваш X / лучший Y (Конкурент)".
    comp_entries — список (значение, подпись конкурента) уже отфильтрованных на None.
    better — "max" (рейтинг/отзывы) или "min" (BSR, меньше=лучше).
    🟢 ставится ТОЛЬКО когда есть и наше значение, и значение хотя бы одного конкурента —
    то есть реально было с чем сравнить. Красного индикатора больше нет по запросу —
    зелёный просто отмечает "по этому пункту данные собраны и сопоставимы".
    """
    has_our = our_value is not None
    has_comp = bool(comp_entries)
    if not has_our and not has_comp:
        return None

    our_str = fmt(our_value) if has_our else "нет данных"

    if has_comp:
        best_value, best_label = (max if better == "max" else min)(comp_entries, key=lambda pair: pair[0])
        comp_str = fmt(best_value)
        if best_label:
            comp_str += f" ({html.escape(str(best_label))})"
    else:
        comp_str = "нет данных"

    marker = " 🟢" if (has_our and has_comp) else ""
    return f"• {label}: ваш {our_str} / лучший {comp_str}{marker}"


def _bullet_price(our_price, comp_prices) -> Optional[str]:
    """Отдельная строка для цены — сравнение со СРЕДНЕЙ ценой конкурентов, не с лучшей."""
    has_our = our_price is not None
    has_comp = bool(comp_prices)
    if not has_our and not has_comp:
        return None

    our_str = _fmt_price(our_price) if has_our else "нет данных"
    comp_str = _fmt_price(sum(comp_prices) / len(comp_prices)) if has_comp else "нет данных"
    marker = " 🟢" if (has_our and has_comp) else ""
    return f"• Цена: ваша {our_str} / средняя {comp_str}{marker}"


def build_grouped_telegram_report(records: List[Dict[str, Any]]) -> List[str]:
    """
    Группирует записи Current по нашему товару (our_asin) — один блок = один наш
    ASIN + все его конкуренты из Competitors по этой же паре. Внутри блока —
    Рейтинг/BSR/Отзывы/Цена, каждая строка отмечена 🟢 только если реально было
    с чем сравнить (наше значение + хотя бы одно значение конкурента). Без
    красного индикатора и без Купонов/Prime — этих данных в таблице физически нет.
    ASIN — кликабельные ссылки на карточку товара на Amazon.
    """
    groups: "Dict[str, Dict[str, Any]]" = {}
    order: List[str] = []

    for record in records:
        # report_extra (рейтинг/отзывы/бренд) специально хранится ОТДЕЛЬНО от
        # values в sheets.py — чтобы эти поля не улетали как безымянные колонки
        # в History. Здесь, для построения отчёта, просто объединяем их.
        values = {**record.get("values", {}), **record.get("report_extra", {})}
        our_asin = extract_asin_from_text(values.get("our_asin"))
        if not our_asin:
            continue

        group = groups.get(our_asin)
        if group is None:
            group = {
                "marketplace": values.get("marketplace") or "US",
                "our_title": "",
                "our_bsr": None,
                "our_price": None,
                "our_stars": None,
                "our_reviews": None,
                "competitors": [],
            }
            groups[our_asin] = group
            order.append(our_asin)

        if not group["our_title"] and values.get("our_product"):
            group["our_title"] = str(values["our_product"]).strip()
        for field, key in (
            ("our_bsr", "our_bsr"), ("our_price", "our_price"),
            ("our_stars", "our_stars"), ("our_reviews", "our_reviews"),
        ):
            if group[key] is None:
                num = clean_number(values.get(field))
                if num is not None:
                    group[key] = num

        comp_asin = extract_asin_from_text(values.get("comp_asin"))
        if comp_asin:
            comp_label = values.get("comp_brand") or values.get("competitor") or comp_asin
            comp_label = str(comp_label).strip()
            if len(comp_label) > 30:
                comp_label = comp_label[:29] + "…"
            group["competitors"].append({
                "label": comp_label,
                "bsr": clean_number(values.get("comp_bsr")),
                "price": clean_number(values.get("comp_price")),
                "stars": clean_number(values.get("comp_stars")),
                "reviews": clean_number(values.get("comp_reviews")),
            })

    lines: List[str] = []
    for our_asin in order:
        group = groups[our_asin]
        comps = group["competitors"]

        title = html.escape(group["our_title"] or our_asin)
        link = _asin_link(our_asin, group["marketplace"])
        lines.append(f"📌 <b>{title}</b> ({link}) — конкурентов: {len(comps)}")

        bullet = _bullet_best(
            "Рейтинг", group["our_stars"],
            [(c["stars"], c["label"]) for c in comps if c["stars"] is not None],
            _fmt_rating, "max",
        )
        if bullet:
            lines.append(bullet)

        bullet = _bullet_best(
            "BSR", group["our_bsr"],
            [(c["bsr"], c["label"]) for c in comps if c["bsr"] is not None],
            _fmt_bsr, "min",
        )
        if bullet:
            lines.append(bullet)

        bullet = _bullet_best(
            "Отзывы", group["our_reviews"],
            [(c["reviews"], c["label"]) for c in comps if c["reviews"] is not None],
            _fmt_reviews, "max",
        )
        if bullet:
            lines.append(bullet)

        bullet = _bullet_price(
            group["our_price"],
            [c["price"] for c in comps if c["price"] is not None],
        )
        if bullet:
            lines.append(bullet)

        lines.append("")  # пустая строка-разделитель между блоками

    if lines and lines[-1] == "":
        lines.pop()

    return lines


def run_parser(progress_callback: Optional[Callable[[float, str], None]] = None) -> Dict[str, Any]:
    # --- Флаг для тестового запуска ---
    IS_TEST_RUN = False  # Установите True, если нужно ограничить прогон для теста

    base_dir = Path(__file__).parent
    key_file = find_key_file(base_dir)

    sheet = None
    asins = resolve_asins_from_environment(FALLBACK_ASINS)
    asin_domains: Dict[str, str] = {}
    logs: List[str] = []

    if key_file:
        try:
            sheet = connect_sheet(key_file)
            asins = resolve_asins_from_environment(load_asins_from_config(sheet.spreadsheet, FALLBACK_ASINS))
            logger.info(f"Google Sheets подключен. Получено ASIN для проверки: {len(asins)}")
            # Карта ASIN -> домен amazon (com/ca/co.uk/...) по маркетплейсу из Competitors.
            # Нужна, чтобы каждый ASIN запрашивался у ScrapingDog на СВОЁМ маркетплейсе,
            # а не всегда на amazon.com (иначе ASIN с amazon.ca/co.uk будет ошибочно
            # помечаться как "не найден", хотя реально существует на своём домене).
            try:
                asin_domains = build_asin_domain_map(sheet.spreadsheet)
                logger.info(f"Построена карта ASIN->маркетплейс для {len(asin_domains)} ASIN.")
            except Exception as exc:
                logger.warning(f"Не удалось построить карту ASIN->маркетплейс, использую домен по умолчанию: {exc}")
        except Exception as exc:
            message = f"Warning: Google Sheets недоступен. Запись в таблицу отключена: {exc}"
            logs.append(message)
            logger.warning(message)
        if os.environ.get("TEST_ASIN") or os.environ.get("SINGLE_ASIN") or os.environ.get("ASIN"):
            logger.info("Режим теста: используется один/указанный ASIN из переменной окружения.")
    else:
        message = "Warning: Google Sheets key file не найден. Запись в таблицу отключена."
        logs.append(message)
        logger.warning(message)
        logger.info("Используется список ASIN из конфигурации/переменных окружения.")

    if IS_TEST_RUN:
        asins = asins[:10]  # Ограничение для теста, например, первые 10
        logger.info(f"--- РЕЖИМ ТЕСТИРОВАНИЯ: используется только {len(asins)} ASIN ---")

    # --- Подписчики Telegram ---
    #
    # Рассылка теперь идёт не в один фиксированный TELEGRAM_CHAT_ID, а всем,
    # кто подписался через бота (написал ему что-нибудь, например /start).
    # Новые сообщения Telegram (/start) обрабатывает только постоянный
    # telegram_subscriber_bot.py. Не вызываем getUpdates здесь: два
    # одновременных читателя могли получить одно и то же обновление и
    # отправить приветствие дважды.
    #
    # TELEGRAM_CHAT_ID из .env (если задан) тоже остаётся получателем — для
    # обратной совместимости с текущей настройкой, даже если через бота ещё
    # никто не подписывался.
    telegram_recipients: List[str] = []
    if TELEGRAM_BOT_TOKEN and sheet:
        try:
            telegram_recipients = get_active_subscriber_ids(sheet.spreadsheet)
        except Exception as exc:
            message = f"Не удалось получить список подписчиков Telegram: {exc}"
            logs.append(message)
            logger.warning(message)
    if TELEGRAM_CHAT_ID and TELEGRAM_CHAT_ID not in telegram_recipients:
        telegram_recipients.append(TELEGRAM_CHAT_ID)

    telegram_enabled = bool(TELEGRAM_BOT_TOKEN) and bool(telegram_recipients)
    if not telegram_enabled:
        message = "Telegram не настроен (нет TELEGRAM_BOT_TOKEN) или нет ни одного подписчика — уведомления отключены."
        logs.append(message)
        logger.info(message)
    else:
        logger.info(f"Отчёты уйдут {len(telegram_recipients)} получателям в Telegram.")
    # Стартовое сообщение убрано намеренно — теперь за весь прогон уходит
    # ОДИН отчёт (broadcast_telegram_report, при необходимости — несколько
    # частей подряд, если данные не влезают в лимит Telegram) в конце run_parser().

    token = get_scrapingdog_token()

    batch_size = BATCH_SIZE
    asin_chunks = [asins[i : i + batch_size] for i in range(0, len(asins), batch_size)]
    total_asins = len(asins) or 1

    all_collected_products: List[ProductData] = []
    all_products_by_asin: Dict[str, ProductData] = {}
    all_failed_asins: List[str] = []

    processed_count = 0

    for chunk_idx, chunk in enumerate(asin_chunks, start=1):
        logger.info(f"--- Пакет {chunk_idx}/{len(asin_chunks)}: обработка {len(chunk)} ASIN ---")

        chunk_collected, chunk_by_asin, chunk_failed = fetch_products_concurrent(
            asins=chunk,
            domain=DOMAIN,
            token=token,
            max_workers=MAX_CONCURRENT_WORKERS,
            progress_callback=progress_callback,
            asin_domains=asin_domains,
        )

        all_collected_products.extend(chunk_collected)
        all_products_by_asin.update(chunk_by_asin)
        all_failed_asins.extend(chunk_failed)

        processed_count += len(chunk)
        if progress_callback:
            progress_callback(
                processed_count / total_asins,
                f"Обработано {processed_count}/{total_asins} ASIN (пакет {chunk_idx}/{len(asin_chunks)})",
            )

        # Уведомления в Telegram по каждому товару отдельно убраны намеренно:
        # при сотнях ASIN за прогон это превращалось в сотни сообщений в чате
        # и не несло полезного сигнала. Вместо этого — только старт (см. выше)
        # и одна итоговая сводка в конце run_parser(), где уже перечислены
        # ВСЕ упавшие ASIN за прогон целиком (см. summary_lines ниже).
        for failed_asin in chunk_failed:
            message = f"⚠️ Не удалось получить данные по ASIN {failed_asin}"
            logs.append(message)

        # Промежуточная запись в Google Sheets после каждой пачки, чтобы не терять
        # прогресс, если парсер прервётся до конца списка ASIN.
        #
        # ВАЖНО: в "Current" всегда пишем полный накопленный с начала прогона список
        # (dict_products_so_far/all_failed_asins) — там должна быть самая свежая
        # информация по каждой паре. А вот в "History" на каждом промежуточном
        # сохранении должна попадать только ДЕЛЬТА — ASIN именно этого батча
        # (history_asins=set(chunk)). Если передавать туда весь накопленный список
        # (как было раньше), то на каждом следующем батче в History будут заново
        # переписываться уже ранее записанные пары — и после N батчей одна и та же
        # запись оказывается продублирована N раз.
        if sheet:
            try:
                dict_products_so_far = {
                    asin: (p.to_dict() if isinstance(p, ProductData) else p)
                    for asin, p in all_products_by_asin.items()
                }
                refresh_current_matrix(
                    sheet,
                    dict_products_so_far,
                    datetime.now().strftime("%Y-%m-%d"),
                    all_failed_asins,
                    history_asins=set(chunk),
                )
                logger.info(
                    f"📊 Промежуточное сохранение в Google Sheets после пакета {chunk_idx}/{len(asin_chunks)} "
                    f"({processed_count}/{total_asins} ASIN обработано)."
                )
            except Exception as e:
                message = f"Ошибка промежуточного сохранения в Google Sheets (пакет {chunk_idx}): {e}"
                logs.append(message)
                logger.error(message, exc_info=True)

    raw_collected_products = [
        (p.to_dict() if isinstance(p, ProductData) else p) for p in all_collected_products
    ]

    avg_price = average_price(raw_collected_products)
    logger.info(f"Средняя цена по всем собранным товарам ({len(all_collected_products)} шт): {avg_price}")

    # Финальная синхронизация листа "Current" после завершения парсинга.
    #
    # ВАЖНО: history_asins=set() — пустой набор. Это намеренно: каждый батч уже
    # записал свою дельту в History в цикле выше, поэтому здесь мы НЕ хотим повторно
    # добавлять туда те же самые пары ещё раз (раньше именно этот финальный вызов
    # дублировал в History весь список, обработанный последним батчем). Лист
    # "Current" при этом всё равно полностью пересобирается из накопленных данных —
    # это безопасно, так как "Current" каждый раз перезаписывается (.clear()), а не
    # аппендится.
    refresh_result: Dict[str, Any] = {"records": []}
    if sheet:
        try:
            dict_products = {
                asin: (p.to_dict() if isinstance(p, ProductData) else p)
                for asin, p in all_products_by_asin.items()
            }
            refresh_result = refresh_current_matrix(
                sheet,
                dict_products,
                datetime.now().strftime("%Y-%m-%d"),
                all_failed_asins,
                history_asins=set(),
            )
            records_count = len(refresh_result.get("records", []))
            logger.info(f"📊 Google Sheets успешно обновлён. Всего записано/обновлено {records_count} строк.")
        except Exception as e:
            message = f"Критическая ошибка при финальном обновлении Google Sheets: {e}"
            logs.append(message)
            logger.error(message, exc_info=True)

    # --- Отчёт в Telegram: группировка по нашему товару (our_asin) ---
    #
    # Источник — refresh_result["records"]: те же самые пары "наш товар vs
    # конкурент", что реально записаны в лист Current. Один блок отчёта =
    # один наш ASIN + все его конкуренты из Competitors по этой же паре
    # (Рейтинг/BSR/Отзывы/Цена). 🟢 ставится только там, где реально было
    # с чем сравнить (наше значение + хотя бы одно значение конкурента) —
    # без красного индикатора и без Купонов/Prime (этих данных в таблице
    # нет). ASIN — кликабельные ссылки на карточку товара на Amazon.
    # broadcast_telegram_report сама делит на несколько сообщений, если отчёт не
    # влезает в лимит Telegram — режет только между строками.
    records = refresh_result.get("records", [])
    report_lines = build_grouped_telegram_report(records)

    header = (
        f"📊 <b>Отчёт по товарам</b> — обработано {len(all_collected_products)}/{len(asins)}, "
        f"не удалось {len(all_failed_asins)}, средняя цена {avg_price}"
    )

    sent_parts = (
        broadcast_telegram_report(report_lines, telegram_recipients, header=header)
        if (report_lines and telegram_recipients) else 0
    )

    # --- Отдельно: компактный список именно БИТЫХ ASIN ---
    #
    # Это не то же самое, что "Not Found" в отдельном поле (BSR/цена могут не
    # найтись у Amazon и это нормально) — здесь только те ASIN, которые вообще
    # не удалось получить в этом прогоне (сетевая ошибка/ошибка API), они же
    # помечены "@" и подсвечены жёлтым в самой таблице. Отправляется отдельным
    # сообщением следом за основным отчётом, чтобы сразу было видно, что
    # реально требует внимания, а не искать "@" по всей длинной таблице.
    if all_failed_asins and telegram_recipients:
        failed_lines = [f"🟡 @ <code>{html.escape(str(asin))}</code>" for asin in all_failed_asins]
        failed_header = (
            f"🛠 <b>Надо заменить битые ASIN — ошибка «Собачка» ({len(all_failed_asins)}):</b>\n"
            f'📄 <a href="{BROKEN_ASINS_SHEET_URL}">Открыть таблицу с конкурентами</a>'
        )
        broadcast_telegram_report(failed_lines, telegram_recipients, header=failed_header)

    if sent_parts == 0 and telegram_recipients:
        # Fallback: либо Google Sheets недоступен (нет records), либо отправка
        # не удалась — короткое сообщение с итогом, чтобы не остаться совсем
        # без уведомления о результате прогона.
        broadcast_telegram_message(
            "✅ <b>Парсер завершил работу</b>\n"
            f"Обработано: {len(all_collected_products)}/{len(asins)}\n"
            f"Средняя цена: {avg_price}\n"
            f"Не удалось получить: {len(all_failed_asins)}",
            telegram_recipients,
        )

    return {
        "products": raw_collected_products,
        "failed_asins": all_failed_asins,
        "logs": logs,
        "avg_price": avg_price,
        "total": len(asins),
        "processed": len(all_collected_products),
        "telegram_enabled": telegram_enabled,
        "telegram_recipients": len(telegram_recipients),
    }



def main():
    run_parser()


if __name__ == "__main__":
    main()
