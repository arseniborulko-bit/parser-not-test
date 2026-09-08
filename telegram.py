from typing import Iterable, Optional
import requests

from config import REQUEST_TIMEOUT, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, TELEGRAM_ENABLED
from utils import logger


def send_telegram_message(text: str, token: Optional[str] = None, chat_id: Optional[str] = None) -> bool:
    """
    Отправляет сообщение в Telegram-чат через Bot API.
    Если TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID не заданы — ничего не делает.
    """
    bot_token = token or TELEGRAM_BOT_TOKEN
    chat_identifier = chat_id or TELEGRAM_CHAT_ID

    if not (bot_token and chat_identifier):
        return False

    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    payload_text = text[:4000]

    try:
        response = requests.post(
            url,
            data={
                "chat_id": chat_identifier,
                "text": payload_text,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            },
            timeout=REQUEST_TIMEOUT,
        )
        if response.status_code != 200:
            logger.warning(f"Telegram API вернул статус {response.status_code}: {response.text[:200]}")
            return False
        return True
    except requests.RequestException as e:
        safe_error = str(e).replace(bot_token, "***") if bot_token else str(e)
        logger.error(f"Telegram API ошибка отправки — {safe_error}")
        return False


def send_telegram_report(
    lines,
    header: str = "📊 Полный отчёт по товарам",
    token: Optional[str] = None,
    chat_id: Optional[str] = None,
    safe_limit: int = 3800,
) -> int:
    """
    Отправляет длинный список строк (например, все товары с ценой/BSR/рейтингом)
    одним или несколькими сообщениями в Telegram.

    В отличие от send_telegram_message(), которая просто ОБРЕЗАЕТ текст на
    4000 символах (payload_text = text[:4000]) — здесь список строк заранее
    делится на несколько сообщений, каждое в пределах safe_limit (с запасом
    от реального лимита Telegram в 4096 символов), так что при 500+ товарах
    данные не теряются, а приходят несколькими сообщениями подряд с меткой
    "(часть N/M)".

    Возвращает количество реально отправленных (успешных) сообщений.
    """
    if not lines:
        return 0

    # Резервируем место под заголовок + метку части ("... (часть 12/12)\n"),
    # чтобы даже с самой длинной меткой не вылезти за safe_limit.
    header_reserve = len(header) + 30

    chunks = []
    current_chunk = []
    current_length = 0
    for line in lines:
        line_length = len(line) + 1  # +1 за перевод строки
        if current_chunk and current_length + line_length + header_reserve > safe_limit:
            chunks.append(current_chunk)
            current_chunk = []
            current_length = 0
        current_chunk.append(line)
        current_length += line_length
    if current_chunk:
        chunks.append(current_chunk)

    total_parts = len(chunks)
    sent_count = 0
    for idx, chunk_lines in enumerate(chunks, start=1):
        part_label = f" (часть {idx}/{total_parts})" if total_parts > 1 else ""
        message_text = f"{header}{part_label}\n" + "\n".join(chunk_lines)
        if send_telegram_message(message_text, token=token, chat_id=chat_id):
            sent_count += 1
        else:
            logger.warning(f"Не удалось отправить часть {idx}/{total_parts} полного отчёта в Telegram.")

    return sent_count


def broadcast_telegram_message(text: str, chat_ids: Iterable[str], token: Optional[str] = None) -> int:
    """
    Отправляет ОДНО И ТО ЖЕ сообщение каждому chat_id из списка (список
    подписчиков из subscribers.get_active_subscriber_ids()).
    Возвращает количество успешных отправок.
    """
    sent = 0
    for chat_id in chat_ids:
        if send_telegram_message(text, token=token, chat_id=chat_id):
            sent += 1
    return sent


def broadcast_telegram_report(
    lines,
    chat_ids: Iterable[str],
    header: str = "📊 Полный отчёт по товарам",
    token: Optional[str] = None,
    safe_limit: int = 3800,
) -> int:
    """
    То же самое, что send_telegram_report(), но рассылает каждому подписчику
    из списка chat_ids отдельно (у каждого — свои части, если отчёт длинный).
    Возвращает суммарное количество успешно отправленных ЧАСТЕЙ по всем
    получателям вместе (не количество получателей).
    """
    total_sent = 0
    for chat_id in chat_ids:
        total_sent += send_telegram_report(lines, header=header, token=token, chat_id=chat_id, safe_limit=safe_limit)
    return total_sent