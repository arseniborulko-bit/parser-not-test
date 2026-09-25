"""Список ссылок на все ASIN, которые сейчас в работе, по маркетплейсам (владелец, 25.09.2026:
сперва «чисто список ссылок», потом — редактируемый текст: стереть строку и «Пересохранить
список» отключает пару, как показано на референсе из другого продукта)."""

import pandas as pd

from test_dashboard_access import TEAM_PASSWORD, dash, run, unlock  # noqa: F401

import pairs_ui


def pair(market="US", our="B0OURASIN1", comp="B0COMPAAA1", active=True):
    return {"marketplace": market, "our_asin": our, "our_product": "Наш",
            "comp_asin": comp, "competitor_name": "Конкурент", "active": active}


PAIRS = pd.DataFrame([
    pair(),
    pair(market="US", our="B0OURASIN1", comp="B0COMPBBB2"),  # тот же наш ASIN, другой конкурент
    pair(market="UK", our="B0OURASIN3", comp="B0COMPCCC3"),
    pair(market="UK", our="B0OURASIN3", comp="B0COMPDDD4", active=False),  # отключена — не попадает
])


def test_links_are_grouped_by_market_and_deduplicated():
    by_market = pairs_ui._active_asin_links_by_market(PAIRS)
    assert set(by_market) == {"US", "UK"}
    us_asins = [asin for asin, _ in by_market["US"]]
    assert us_asins == ["B0COMPAAA1", "B0COMPBBB2", "B0OURASIN1"]  # наш ASIN один раз, не дважды


def test_disabled_pairs_do_not_contribute_asins():
    by_market = pairs_ui._active_asin_links_by_market(PAIRS)
    uk_asins = [asin for asin, _ in by_market["UK"]]
    assert uk_asins == ["B0COMPCCC3", "B0OURASIN3"]
    assert "B0COMPDDD4" not in uk_asins


def test_links_point_to_the_right_marketplace_domain():
    by_market = pairs_ui._active_asin_links_by_market(PAIRS)
    url = dict(by_market["UK"])["B0OURASIN3"]
    assert url == "https://www.amazon.co.uk/dp/B0OURASIN3"


def test_no_active_pairs_means_no_groups():
    empty = pd.DataFrame([pair(active=False)])
    assert pairs_ui._active_asin_links_by_market(empty) == {}


def test_render_shows_an_info_message_when_nothing_is_active(monkeypatch):
    shown = []
    monkeypatch.setattr(pairs_ui.st, "info", lambda msg: shown.append(msg))
    pairs_ui.render_active_asin_links(None, pd.DataFrame([pair(active=False)]), "Тест", "editor", True)
    assert shown


def test_a_viewer_without_edit_rights_sees_a_read_only_list(monkeypatch):
    class FakeExpander:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    monkeypatch.setattr(pairs_ui.st, "expander", lambda label: FakeExpander())
    written = []
    monkeypatch.setattr(pairs_ui.st, "markdown", lambda html, **kwargs: written.append(html))
    pairs_ui.render_active_asin_links(None, PAIRS, None, None, False)
    text = " ".join(written)
    assert 'href="https://www.amazon.com/dp/B0OURASIN1"' in text
    assert "B0OURASIN1" in text and "B0COMPCCC3" in text


def test_the_link_list_appears_on_the_collect_tab(dash, monkeypatch):  # noqa: F811
    monkeypatch.setenv("TEAM_PASSWORD", TEAM_PASSWORD)
    at = unlock(run())
    assert not at.exception
    text = " ".join(block.value for block in at.markdown) + " ".join(
        exp.label for exp in at.expander
    )
    assert "Ссылки на все ASIN в работе" in text


class ButtonRecorder:
    """Подменяет st.button: не «нажимается» сама, но запоминает on_click/args, чтобы можно было
    проверить, что _render_links_editor правильно посчитал, какие пары отключатся."""

    def __init__(self):
        self.calls = []

    def __call__(self, label, key=None, disabled=False, on_click=None, args=()):
        self.calls.append({"label": label, "key": key, "disabled": disabled, "on_click": on_click, "args": args})
        return False


def test_erasing_a_competitor_line_disables_only_that_pair(monkeypatch):
    items = pairs_ui._active_asin_links_by_market(PAIRS)["US"]
    # текст без строки B0COMPBBB2 — как будто владелец стёр её и собирается нажать «Пересохранить»
    kept = "\n".join(url for asin, url in items if asin != "B0COMPBBB2")
    monkeypatch.setattr(pairs_ui.st, "text_area", lambda *a, **k: kept)
    monkeypatch.setattr(pairs_ui.st, "caption", lambda *a, **k: None)
    monkeypatch.setattr(pairs_ui.st, "text_input", lambda *a, **k: "")
    recorder = ButtonRecorder()
    monkeypatch.setattr(pairs_ui.st, "button", recorder)

    pairs_ui._render_links_editor(None, PAIRS, "US", items, "Тест", "editor", "collect")

    assert len(recorder.calls) == 1
    keys = recorder.calls[0]["args"][2]
    assert keys == [("US", "B0OURASIN1", "B0COMPBBB2")]
    assert recorder.calls[0]["disabled"] is False


def test_erasing_our_asin_line_disables_all_its_pairs_in_that_country(monkeypatch):
    """Наш ASIN участвует в двух парах на US — стереть его строку значит убрать обе."""
    items = pairs_ui._active_asin_links_by_market(PAIRS)["US"]
    kept = "\n".join(url for asin, url in items if asin != "B0OURASIN1")
    monkeypatch.setattr(pairs_ui.st, "text_area", lambda *a, **k: kept)
    monkeypatch.setattr(pairs_ui.st, "caption", lambda *a, **k: None)
    monkeypatch.setattr(pairs_ui.st, "text_input", lambda *a, **k: "")
    recorder = ButtonRecorder()
    monkeypatch.setattr(pairs_ui.st, "button", recorder)

    pairs_ui._render_links_editor(None, PAIRS, "US", items, "Тест", "editor", "collect")

    keys = recorder.calls[0]["args"][2]
    assert set(keys) == {("US", "B0OURASIN1", "B0COMPAAA1"), ("US", "B0OURASIN1", "B0COMPBBB2")}


def test_leaving_the_text_untouched_disables_nothing(monkeypatch):
    items = pairs_ui._active_asin_links_by_market(PAIRS)["US"]
    monkeypatch.setattr(pairs_ui.st, "text_area", lambda *a, **k: pairs_ui._links_text(items))
    monkeypatch.setattr(pairs_ui.st, "caption", lambda *a, **k: None)
    recorder = ButtonRecorder()
    monkeypatch.setattr(pairs_ui.st, "button", recorder)

    pairs_ui._render_links_editor(None, PAIRS, "US", items, "Тест", "editor", "collect")

    call = recorder.calls[0]
    assert call["args"][2] == []
    assert call["disabled"] is True


def test_adding_a_new_line_does_not_create_anything():
    """Механика только на удаление: дописанные строки форма молча игнорирует — для добавления
    есть отдельная форма «Добавить пару»."""
    items = pairs_ui._active_asin_links_by_market(PAIRS)["US"]
    text = pairs_ui._links_text(items) + "\nhttps://www.amazon.com/dp/B0NEWNEW01"
    parsed_asins = {item.asin for item in pairs_ui.pairs_store.parse_asin_batch(text).items}
    assert "B0NEWNEW01" in parsed_asins  # текст его видит…
    # …но removed считается только как «было и пропало», добавленное новое туда не попадает
    removed = {asin for asin, _ in items} - parsed_asins
    assert removed == set()


def test_a_large_batch_needs_typed_confirmation(monkeypatch):
    many_pairs = pd.DataFrame([
        pair(market="US", our="B0OURASIN1", comp=f"B0COMP{i:04d}") for i in range(30)
    ])
    items = pairs_ui._active_asin_links_by_market(many_pairs)["US"]
    monkeypatch.setattr(pairs_ui.st, "text_area", lambda *a, **k: "")  # стёрли всё разом
    monkeypatch.setattr(pairs_ui.st, "caption", lambda *a, **k: None)
    typed_inputs = []
    monkeypatch.setattr(pairs_ui.st, "text_input", lambda *a, **k: (typed_inputs.append(1), "")[1])
    recorder = ButtonRecorder()
    monkeypatch.setattr(pairs_ui.st, "button", recorder)

    pairs_ui._render_links_editor(None, many_pairs, "US", items, "Тест", "editor", "collect")

    assert typed_inputs, "при большом отключении нужно подтверждение текстом"
    assert recorder.calls[0]["disabled"] is True  # без ввода «УБРАТЬ» кнопка недоступна


def test_the_resave_callback_uses_its_own_flash_key_not_the_pairs_grids(monkeypatch):
    """render_pairs_management уже занимает key_prefix_flash на этой же вкладке — список ссылок
    обязан использовать свой, иначе сообщение показалось бы не в том месте (тот же класс бага,
    что уже чинили для pairs_flash/collect_flash)."""
    state = {}
    monkeypatch.setattr(pairs_ui.st, "session_state", state, raising=False)
    monkeypatch.setattr(pairs_ui.pairs_store, "set_pairs_active", lambda *a, **k: 1)
    monkeypatch.setattr(pairs_ui.st.cache_data, "clear", lambda: None)
    pairs_ui._resave_links_callback(None, "US", [("US", "B0OURASIN1", "B0COMPAAA1")], "Тест", "editor",
                                    "collect_links")
    assert "collect_links_flash" in state
    assert "collect_flash" not in state
