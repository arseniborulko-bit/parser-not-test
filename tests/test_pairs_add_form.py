"""Форма «Добавить пару»: живой счётчик распознанных ссылок под каждым полем — опечатка видна
сразу, не дожидаясь расчёта всего плана (владелец, 25.09.2026: «мне нравится 2» из списка
предложенных улучшений)."""

import pairs_ui


def test_empty_field_shows_the_format_hint(monkeypatch):
    shown = []
    monkeypatch.setattr(pairs_ui.st, "caption", lambda msg: shown.append(msg))
    pairs_ui._render_recognized_caption("   ", "подсказка про формат")
    assert shown == ["подсказка про формат"]


def test_valid_links_are_counted(monkeypatch):
    shown = []
    monkeypatch.setattr(pairs_ui.st, "caption", lambda msg: shown.append(msg))
    pairs_ui._render_recognized_caption(
        "https://www.amazon.com/dp/B0AAAAAAAA\nhttps://www.amazon.de/dp/B0BBBBBBBB", "hint",
    )
    assert shown == ["Распознано: 2."]


def test_invalid_entries_are_reported_separately(monkeypatch):
    shown = []
    monkeypatch.setattr(pairs_ui.st, "caption", lambda msg: shown.append(msg))
    # один настоящий, один голый ASIN (без ссылки — не распознаётся, require_link=True)
    pairs_ui._render_recognized_caption("https://www.amazon.com/dp/B0AAAAAAAA B0BBBBBBBB", "hint")
    assert shown == ["Распознано: 1 · не распознано: 1."]


def test_repeats_are_reported(monkeypatch):
    shown = []
    monkeypatch.setattr(pairs_ui.st, "caption", lambda msg: shown.append(msg))
    same = "https://www.amazon.com/dp/B0AAAAAAAA"
    pairs_ui._render_recognized_caption(f"{same} {same}", "hint")
    assert shown == ["Распознано: 1 · повторов: 1."]
