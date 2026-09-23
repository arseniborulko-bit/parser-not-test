"""Оформление: светлая тема закреплена, вкладки и неактивные кнопки видны в любой теме браузера."""

import re
import tomllib
from pathlib import Path

from test_dashboard_access import dash, run  # noqa: F401

ROOT = Path(__file__).resolve().parent.parent


def style_of(at) -> str:
    return " ".join(m.value for m in at.markdown if "<style>" in m.value)


def test_the_light_theme_is_locked_because_the_page_itself_is_drawn_light():
    config = tomllib.loads((ROOT / ".streamlit" / "config.toml").read_text(encoding="utf-8"))
    assert config["theme"]["base"] == "light"
    assert config["client"]["toolbarMode"] == "minimal"


def test_tabs_are_styled_by_role_which_newer_streamlit_versions_still_have(dash):  # noqa: F811
    css = style_of(run())
    assert re.search(r'\[role="tablist"\]\s*\{[^}]*gap', css)
    assert re.search(r'\[role="tab"\][^{}]*\{[^}]*background: #121826 !important', css)
    assert re.search(r'\[role="tab"\]\[aria-selected="true"\]\s*\{[^}]*background: #168ed0 !important', css)


def test_disabled_buttons_inside_tabs_look_disabled(dash):  # noqa: F811
    assert re.search(r'stTabs"\] button:disabled\s*\{[^}]*opacity: \.45 !important', style_of(run()))


def test_the_fork_github_toolbar_is_hidden(dash):  # noqa: F811
    assert re.search(r'\[data-testid="stToolbar"\]\s*\{[^}]*display: none !important', style_of(run()))


def test_tab_styling_does_not_leak_onto_every_button_inside_a_tab(dash):  # noqa: F811
    """Оформление вкладки доставалось и кнопке-подсказке «?» внутри формы: она выглядела
    тёмным пустым квадратом. Красим только сами заголовки вкладок ([role="tab"])."""
    css = style_of(run())
    painting = [rule for rule in css.split("}") if "#121826 !important" in rule]
    assert painting, "правило, красящее заголовки вкладок, пропало"
    for rule in painting:
        selector = rule.split("{")[0]
        assert 'role="tab"' in selector
        assert not re.search(r'stTabs"\]\s+button\s*(,|\{|$)', selector), (
            f"оформление вкладки красит все кнопки внутри вкладки: {selector.strip()}"
        )
