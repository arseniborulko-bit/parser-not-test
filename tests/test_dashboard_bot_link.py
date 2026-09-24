"""Ссылка на Telegram-бота в шапке дашборда.

Имя бота публичное и в коде лежать может; токен — нет, и рядом с этой ссылкой его быть не должно.
"""

import pytest

from test_dashboard_access import dash, run  # noqa: F401

import dashboard_db as dash_module


def header(at) -> str:
    return " ".join(block.value for block in at.markdown)


def test_the_header_links_to_the_bot(dash):  # noqa: F811
    text = header(run())
    assert "https://t.me/BSR_Competitors_Trackerbot" in text
    assert "@BSR_Competitors_Trackerbot" in text


def test_the_link_opens_in_a_new_tab_without_handing_over_the_page(dash):  # noqa: F811
    link = next(block.value for block in run().markdown if "t.me/" in block.value)
    assert 'target="_blank"' in link
    assert 'rel="noopener"' in link


def test_a_replacement_bot_can_be_set_by_secret_without_touching_the_code(dash, monkeypatch):  # noqa: F811
    monkeypatch.setattr(dash, "_secret", lambda name: "@OtherBot" if name == "TELEGRAM_BOT_USERNAME" else "")
    assert dash._bot_username() == "OtherBot", "лишняя @ в секрете не должна попадать в адрес"


def test_no_link_is_drawn_when_there_is_no_bot(dash, monkeypatch):  # noqa: F811
    monkeypatch.setattr(dash, "_bot_username", lambda: "")
    assert "t.me/" not in header(run())


def test_the_bot_token_is_not_anywhere_in_the_dashboard_source():
    """Имя бота — публичное, токен — нет: он живёт только в секретах и в .env."""
    with open(dash_module.__file__, encoding="utf-8") as handle:
        source = handle.read()
    assert "TELEGRAM_BOT_TOKEN" not in source
    # токены Telegram выглядят как 1234567890:AA... — такого в исходнике быть не должно
    import re

    assert not re.search(r"\b\d{8,}:[A-Za-z0-9_-]{30,}", source)
