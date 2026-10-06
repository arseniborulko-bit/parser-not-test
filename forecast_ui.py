"""Вкладка «Прогноз»: аналитика группы товаров — история, сравнение и прогноз BSR.

BSR — место в категории: чем меньше, тем лучше. «Хуже порога» значит BSR больше порога внимания.
Товар — пара (страна, ASIN): один ASIN в разных парах считается один раз, в разных странах — отдельно.

Прогноз — тот же расчёт, что был на вкладке раньше (не модель): типичное изменение BSR в день — медиана
дневных изменений за последние TREND_DAYS дней — продлевается до общей целевой даты «дата расчёта +
горизонт». Меньше MIN_POINTS замеров — прогноз не строится, причина показывается. Вероятностей, точности и
доверительных интервалов этот расчёт не даёт, поэтому их нет. Для группы прогноз не складывается: у
товаров BSR отличается в сотни раз, обоснованного способа агрегировать прогнозы нет.

Расчёты — функции без Streamlit (prepare, analyze, …), их проверяют тесты; render() рисует вкладку.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from html import escape
from typing import Iterable, Optional

import altair as alt
import numpy as np
import pandas as pd
import streamlit as st

TREND_DAYS = 14
# Меньше трёх замеров — это не тренд, а две точки и совпадение. Такой ASIN в прогноз не берём.
MIN_POINTS = 3
HORIZONS = (7, 14, 30)
PERIODS = {"7 дней": 7, "30 дней": 30, "90 дней": 90, "Свой диапазон": None}
LEVELS = {"Все": None, "Наши": "наш", "Конкуренты": "конкурент"}
ALL_MARKETS = "Все страны"
DEFAULT_THRESHOLD = 100_000
# BSR шумит на десятки процентов за день: «падает» — только если вырос больше чем на столько за период.
FALL_TOLERANCE_PCT = 10
COMPARE_MAX = 5
SMALL_PAGE = 12
ROWS_PAGE = 25
NAME_LENGTH = 38
# Streamlit с use_container_width считает высоту вместе с осями и подписями — запас под них плюс шаг на строку.
AXIS_ROOM = 70
ROW_STEP = 30
# Фиксированный порядок цветов: цвет закреплён за позицией товара в группе, а не за его рангом.
PALETTE = ["#1f6feb", "#e8590c", "#2f9e44", "#9c36b5", "#c2255c"]

RISK_NOW, RISK_FORECAST, RISK_FALLING, RISK_NONE, RISK_NO_DATA = 0, 1, 2, 3, 4
RISK_LABELS = {RISK_NOW: "Хуже порога", RISK_FORECAST: "Прогноз хуже порога", RISK_FALLING: "BSR растёт",
               RISK_NONE: "—", RISK_NO_DATA: "Нет данных"}


def _key(market: str, asin: str) -> str:
    return f"{market}|{asin}"


def short(name: str, limit: int = NAME_LENGTH) -> str:
    name = " ".join(str(name or "").split())
    return name if len(name) <= limit else name[: limit - 1] + "…"


# --- Данные ------------------------------------------------------------------------------------

def prepare(data: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    """(каталог, дневные замеры) из снимков пар.

    Каталог: key, market, asin, name, kind («наш» — если ASIN хоть в одной паре стоит нашим), ours —
    наши ASIN, с которыми конкурент в паре, rivals — конкуренты нашего товара.
    Замеры: key, date, bsr, reviews, rating — одно значение на товар и день; пусто остаётся пустым, не нулём.
    """
    empty = (pd.DataFrame(columns=["key", "market", "asin", "name", "kind", "ours", "rivals"]),
             pd.DataFrame(columns=["key", "date", "bsr", "reviews", "rating"]))
    if data.empty or "snapshot_date" not in data:
        return empty
    frame = data.sort_values("snapshot_date")
    market = frame["marketplace"].astype(str) if "marketplace" in frame else pd.Series("", index=frame.index)
    parts = []
    for asin_col, name_col, bsr_col, reviews_col, rating_col, kind in (
        ("our_asin", "our_product", "our_bsr", "our_reviews_count", "our_rating", "наш"),
        ("comp_asin", "competitor_name", "comp_bsr", "comp_reviews_count", "comp_rating", "конкурент"),
    ):
        if asin_col not in frame or bsr_col not in frame:
            continue
        parts.append(pd.DataFrame({
            "market": market, "asin": frame[asin_col].astype(str).str.strip(),
            "name": frame[name_col] if name_col in frame else "",
            "date": pd.to_datetime(frame["snapshot_date"], errors="coerce").dt.normalize(),
            "bsr": pd.to_numeric(frame[bsr_col], errors="coerce"),
            "reviews": pd.to_numeric(frame[reviews_col], errors="coerce") if reviews_col in frame else np.nan,
            "rating": pd.to_numeric(frame[rating_col], errors="coerce") if rating_col in frame else np.nan,
            "kind": kind,
        }))
    if not parts:
        return empty
    rows = pd.concat(parts, ignore_index=True)
    rows = rows[rows["asin"].ne("") & rows["asin"].ne("nan") & rows["date"].notna()]
    if rows.empty:
        return empty
    rows["key"] = rows["market"] + "|" + rows["asin"]

    our_keys = set(rows.loc[rows["kind"] == "наш", "key"])
    names = rows.dropna(subset=["name"])
    names = names[names["name"].astype(str).str.strip().ne("")].groupby("key")["name"].last()
    pairs = frame.assign(_m=market)
    rivals: dict[str, list[str]] = {}
    ours: dict[str, list[str]] = {}
    if "our_asin" in pairs and "comp_asin" in pairs:
        for m, our, comp in zip(pairs["_m"], pairs["our_asin"].astype(str), pairs["comp_asin"].astype(str)):
            if our.strip() and comp.strip() and comp != "nan" and our != "nan":
                rivals.setdefault(_key(m, our.strip()), [])
                if _key(m, comp.strip()) not in rivals[_key(m, our.strip())]:
                    rivals[_key(m, our.strip())].append(_key(m, comp.strip()))
                ours.setdefault(_key(m, comp.strip()), [])
                if _key(m, our.strip()) not in ours[_key(m, comp.strip())]:
                    ours[_key(m, comp.strip())].append(_key(m, our.strip()))
    keys = rows.drop_duplicates("key")[["key", "market", "asin"]].reset_index(drop=True)
    catalog = keys.assign(
        name=[str(names.get(k, "")) for k in keys["key"]],
        kind=["наш" if k in our_keys else "конкурент" for k in keys["key"]],
        ours=[ours.get(k, []) if k not in our_keys else [] for k in keys["key"]],
        rivals=[rivals.get(k, []) for k in keys["key"]],
    ).sort_values(["kind", "market", "name", "asin"], ascending=[False, True, True, True], ignore_index=True)
    # Один товар на один день — одно значение: он повторяется во всех своих парах.
    last = lambda s: s.dropna().iloc[-1] if s.notna().any() else np.nan  # noqa: E731
    daily = rows.groupby(["key", "date"], as_index=False).agg(bsr=("bsr", last), reviews=("reviews", last),
                                                              rating=("rating", last))
    return catalog, daily


def trend(days: pd.Series, values: pd.Series) -> float:
    """Типичное изменение BSR в день — медиана дневных изменений, а не прямая по всем точкам.

    На боевых данных BSR скачет на сотни тысяч за сутки, и метод наименьших квадратов давал
    наклоны вроде −180000 в день: проекция улетала в минус и упиралась в ноль почти у всех.
    Медиана не даёт одному выбросу задать тренд. Дни делим на реальный разрыв, поэтому
    пропущенный день не удваивает скорость.
    """
    gaps = days.diff().dt.days.astype(float)
    changes = values.astype(float).diff()
    daily = (changes / gaps).replace([np.inf, -np.inf], np.nan).dropna()
    return float(daily.median()) if not daily.empty else 0.0


@dataclass(frozen=True)
class Settings:
    start: date
    end: date
    horizon: int
    threshold: float

    @property
    def target(self) -> date:
        """Общая целевая дата прогноза для всех товаров — от даты расчёта, а не от последнего замера товара."""
        return self.end + timedelta(days=self.horizon)


def analyze(catalog: pd.DataFrame, daily: pd.DataFrame, keys: Iterable[str], settings: Settings) -> pd.DataFrame:
    """Строка на товар группы: текущий BSR, изменение за период (по реально использованным датам),
    прогноз на общую целевую дату и признаки проблем. Ошибка одного товара не ломает остальных."""
    keys = list(dict.fromkeys(keys))
    info = catalog.set_index("key")
    by_key = {k: g for k, g in daily[daily["key"].isin(keys)].groupby("key")}
    end, start = pd.Timestamp(settings.end), pd.Timestamp(settings.start)
    rows = []
    for k in keys:
        if k not in info.index:
            continue
        row = {"key": k, "market": info.at[k, "market"], "asin": info.at[k, "asin"], "name": info.at[k, "name"],
               "kind": info.at[k, "kind"], "current": np.nan, "last_date": pd.NaT, "start_value": np.nan,
               "start_date": pd.NaT, "change_pct": np.nan, "reviews": np.nan, "reviews_added": np.nan,
               "rating": np.nan, "rating_change": np.nan, "forecast": np.nan,
               "forecast_change_pct": np.nan, "forecast_note": ""}
        try:
            series = by_key.get(k, pd.DataFrame(columns=["date", "bsr", "reviews", "rating"]))
            series = series[series["date"] <= end].sort_values("date")
            # Рейтинг и отзывы — последнее известное и изменение за период по реальным замерам.
            for column, now_key, delta_key in (("reviews", "reviews", "reviews_added"),
                                               ("rating", "rating", "rating_change")):
                known = series.dropna(subset=[column])
                if not known.empty:
                    row[now_key] = float(known[column].iloc[-1])
                    period = known[known["date"] >= start]
                    if len(period) >= 2:
                        row[delta_key] = float(period[column].iloc[-1] - period[column].iloc[0])
            valid = series.dropna(subset=["bsr"])
            if not valid.empty:
                row["current"], row["last_date"] = float(valid["bsr"].iloc[-1]), valid["date"].iloc[-1]
                in_period = valid[valid["date"] >= start]
                if len(in_period) >= 2:
                    row["start_value"], row["start_date"] = float(in_period["bsr"].iloc[0]), in_period["date"].iloc[0]
                    row["change_pct"] = (row["current"] - row["start_value"]) / row["start_value"] * 100
                recent = valid[valid["date"] > row["last_date"] - pd.Timedelta(days=TREND_DAYS)]
                if len(recent) < MIN_POINTS:
                    row["forecast_note"] = f"мало замеров за {TREND_DAYS} дн.: {len(recent)} из {MIN_POINTS}"
                else:
                    slope = trend(recent["date"], recent["bsr"])
                    steps = (pd.Timestamp(settings.target) - row["last_date"]).days
                    projected = row["current"] + slope * steps
                    if projected < 1:
                        # BSR не бывает меньше 1: тренд слишком крутой для такого горизонта, цифра была бы выдумкой.
                        row["forecast_note"] = "тренд уводит BSR ниже 1 — на этот горизонт прогноз не строим"
                    else:
                        row["forecast"] = projected
                        row["forecast_change_pct"] = (projected - row["current"]) / row["current"] * 100
            else:
                row["forecast_note"] = "нет замеров BSR"
        except Exception as exc:  # один битый товар не должен ронять группу
            row["forecast_note"] = f"ошибка расчёта ({type(exc).__name__})"
        rows.append(row)
    table = pd.DataFrame(rows)
    if table.empty:
        return table
    table["below"] = table["current"] > settings.threshold
    table["falling"] = table["change_pct"] > FALL_TOLERANCE_PCT
    table["forecast_risk"] = ~table["below"] & (table["forecast"] > settings.threshold)
    table["risk"] = np.select(
        [table["current"].isna(), table["below"], table["forecast_risk"], table["falling"]],
        [RISK_NO_DATA, RISK_NOW, RISK_FORECAST, RISK_FALLING], default=RISK_NONE,
    )
    table["label"] = [f"{short(n) or a} · {m} {a}" for n, m, a in zip(table["name"], table["market"], table["asin"])]
    return table


def problem_keys(table: pd.DataFrame) -> list[str]:
    """«Проблемные»: хуже порога сейчас, BSR вырос за период или прогноз уходит за порог."""
    if table.empty:
        return []
    return list(table.loc[table["risk"].isin([RISK_NOW, RISK_FORECAST, RISK_FALLING]), "key"])


def group_metrics(table: pd.DataFrame) -> dict:
    with_data = table.dropna(subset=["current"]) if not table.empty else table
    total = len(table)
    below = int(table["below"].sum()) if total else 0
    return {
        "median": float(with_data["current"].median()) if not with_data.empty else None,
        "with_data": len(with_data), "total": total,
        "below": below, "below_share": round(100 * below / len(with_data)) if len(with_data) else 0,
        "new_risk": int(table["forecast_risk"].sum()) if total else 0,
    }


def series_frame(daily: pd.DataFrame, table: pd.DataFrame, settings: Settings, relative: bool = False) -> pd.DataFrame:
    """Точки графика: факт (с разрывами на пропущенных днях — отдельные отрезки, а не сплошная линия)
    и прогноз — от последнего замера до общей целевой даты. relative — изменение от первого замера периода, %."""
    if table.empty:
        return pd.DataFrame(columns=["key", "label", "date", "value", "kind", "segment"])
    labels = dict(zip(table["key"], table["label"]))
    start, end = pd.Timestamp(settings.start), pd.Timestamp(settings.end)
    fact = daily[daily["key"].isin(labels) & daily["date"].between(start, end)].dropna(subset=["bsr"])
    parts = []
    for k, group in fact.groupby("key"):
        group = group.sort_values("date")
        base = float(group["bsr"].iloc[0])
        values = (group["bsr"] - base) / base * 100 if relative else group["bsr"]
        gaps = group["date"].diff().dt.days.fillna(1).gt(1).cumsum()
        parts.append(pd.DataFrame({"key": k, "label": labels[k], "date": group["date"], "value": values.values,
                                   "kind": "Факт", "segment": [f"{k}#{g}" for g in gaps]}))
        row = table.set_index("key").loc[k]
        if pd.notna(row["forecast"]) and pd.notna(row["last_date"]):
            last, projected = float(row["current"]), float(row["forecast"])
            if relative:
                last, projected = (last - base) / base * 100, (projected - base) / base * 100
            parts.append(pd.DataFrame({"key": k, "label": labels[k], "date": [row["last_date"], pd.Timestamp(settings.target)],
                                       "value": [last, projected], "kind": "Прогноз", "segment": f"{k}#forecast"}))
    return pd.concat(parts, ignore_index=True) if parts else pd.DataFrame(
        columns=["key", "label", "date", "value", "kind", "segment"])


def portfolio_frame(daily: pd.DataFrame, keys: list[str], settings: Settings) -> pd.DataFrame:
    """Медиана BSR группы по дням и сколько товаров группы имели замер в этот день (покрытие)."""
    start, end = pd.Timestamp(settings.start), pd.Timestamp(settings.end)
    rows = daily[daily["key"].isin(keys) & daily["date"].between(start, end)].dropna(subset=["bsr"])
    if rows.empty:
        return pd.DataFrame(columns=["date", "median", "count"])
    return rows.groupby("date").agg(median=("bsr", "median"), count=("bsr", "size")).reset_index()


def distribution(table: pd.DataFrame, threshold: float) -> pd.DataFrame:
    """Сколько товаров в каждом диапазоне BSR относительно порога и сколько без данных."""
    half = threshold / 2
    bins = [
        (f"Лучше {half:,.0f}".replace(",", " "), lambda v: v <= half),
        (f"{half:,.0f} – {threshold:,.0f}".replace(",", " "), lambda v: half < v <= threshold),
        (f"Хуже порога {threshold:,.0f}".replace(",", " "), lambda v: v > threshold),
    ]
    total = len(table)
    current = table["current"] if total else pd.Series(dtype=float)
    rows = [{"bucket": name, "count": int(current.dropna().map(test).sum())} for name, test in bins]
    rows.append({"bucket": "Нет данных", "count": int(current.isna().sum())})
    out = pd.DataFrame(rows)
    out["share"] = (out["count"] / total * 100).round().astype(int) if total else 0
    return out


SORTS = {
    "Сначала риски": (["risk", "change_pct"], [True, False]),
    "BSR сейчас": (["current"], [False]),
    "Изменение за период": (["change_pct"], [False]),
    "Прогноз": (["forecast"], [False]),
    "Название": (["name", "asin"], [True, True]),
}
TABLE_FILTERS = {
    "Все": lambda t: t,
    "Хуже порога": lambda t: t[t["risk"] == RISK_NOW],
    "BSR растёт": lambda t: t[t["falling"]],
    "Прогноз хуже порога": lambda t: t[t["forecast_risk"]],
    "Нет данных": lambda t: t[t["current"].isna()],
}


def sort_table(table: pd.DataFrame, how: str) -> pd.DataFrame:
    columns, ascending = SORTS[how]
    return table.sort_values(columns, ascending=ascending, na_position="last", kind="stable", ignore_index=True)


def search(catalog: pd.DataFrame, text: str) -> pd.DataFrame:
    text = " ".join(str(text or "").lower().split())
    if not text:
        return catalog
    hay = (catalog["name"].astype(str) + " " + catalog["asin"].astype(str)).str.lower()
    return catalog[hay.str.contains(text, regex=False)]


def table_view(table: pd.DataFrame, settings: Settings) -> pd.DataFrame:
    """Таблица «Товары выбранной группы» в том виде, в каком её видят и выгружают в CSV."""
    def fmt_pct(value):
        return f"{value:+.0f}%" if pd.notna(value) else ""

    def fmt_date(value):
        return value.strftime("%d.%m.%Y") if pd.notna(value) else ""

    return pd.DataFrame({
        "Товар": [f"{n or '—'} ({a})" for n, a in zip(table["name"], table["asin"])],
        "Страна": table["market"], "Тип": table["kind"],
        "BSR сейчас": table["current"].round(0),
        "Изменение за период": [
            f"{fmt_pct(c)} ({fmt_date(s)} → {fmt_date(l)})" if pd.notna(c) else "мало замеров"
            for c, s, l in zip(table["change_pct"], table["start_date"], table["last_date"])],
        "Рейтинг": [f"{v:.1f} ★" if pd.notna(v) else "" for v in table["rating"]],
        "Рейтинг за период": [f"{v:+.1f}" if pd.notna(v) else "" for v in table["rating_change"]],
        "Отзывов": pd.to_numeric(table["reviews"], errors="coerce"),
        "Новых отзывов": [f"{v:+.0f}" if pd.notna(v) else "" for v in table["reviews_added"]],
        f"Прогноз на {settings.target:%d.%m}": table["forecast"].round(0),
        "Прогнозное изменение": [fmt_pct(v) if pd.notna(v) else (note or "нет прогноза")
                                 for v, note in zip(table["forecast_change_pct"], table["forecast_note"])],
        "Текущая проблема": ["Хуже порога" if b else ("BSR растёт" if f else "") for b, f in zip(table["below"], table["falling"])],
        "Прогнозный риск": ["Уйдёт за порог" if r else "" for r in table["forecast_risk"]],
        "Последний замер": [fmt_date(v) for v in table["last_date"]],
    })


# --- Графики -----------------------------------------------------------------------------------

def _bsr_axis(title: str = "BSR (выше на графике — лучше)") -> alt.Y:
    return alt.Y("value:Q", title=title, scale=alt.Scale(type="log", reverse=True))


def history_chart(points: pd.DataFrame, settings: Settings, relative: bool, labels: list[str]) -> alt.LayerChart:
    """История (сплошная) и прогноз (пунктир) на одном графике, до COMPARE_MAX товаров: порог, граница
    «факт | прогноз», значения всех видимых товаров на дату при наведении, скрытие серий кликом по легенде."""
    colors = alt.Scale(domain=labels, range=PALETTE[: len(labels)])
    legend = alt.selection_point(fields=["label"], bind="legend", name="legend")
    nearest = alt.selection_point(nearest=True, on="pointerover", fields=["date"], empty=False, name="hover",
                                  clear="pointerout")
    y = alt.Y("value:Q", title="Изменение BSR от начала периода, % (выше — лучше)",
              scale=alt.Scale(reverse=True)) if relative else _bsr_axis()
    base = alt.Chart(points).encode(x=alt.X("date:T", title=None, axis=alt.Axis(format="%d.%m", labelAngle=0)), y=y)
    lines = base.mark_line(strokeWidth=2).encode(
        color=alt.Color("label:N", scale=colors, legend=alt.Legend(title=None, orient="bottom", columns=1, labelLimit=420)),
        strokeDash=alt.StrokeDash("kind:N", scale=alt.Scale(domain=["Факт", "Прогноз"], range=[[1, 0], [6, 4]]),
                                  legend=alt.Legend(title=None, orient="top")),
        detail="segment:N",
        opacity=alt.condition(legend, alt.value(1), alt.value(0.1)),
    ).add_params(legend)
    dots = base.mark_point(size=60, filled=True).encode(
        color=alt.Color("label:N", scale=colors, legend=None),
        opacity=alt.condition(nearest, alt.value(1), alt.value(0)),
    )
    fmt = "+.0f" if relative else ",.0f"
    ids = {label: f"s{i}" for i, label in enumerate(labels)}
    hover = alt.Chart(points.assign(sid=points["label"].map(ids))).transform_pivot(
        "sid", value="value", groupby=["date"], op="mean"  # в день последнего замера факт и начало прогноза совпадают
    ).mark_rule(color="#94a3b8").encode(
        x="date:T", opacity=alt.condition(nearest, alt.value(0.7), alt.value(0)),
        tooltip=[alt.Tooltip("date:T", title="Дата", format="%d.%m.%Y")]
        + [alt.Tooltip(f"{sid}:Q", title=short(label, 30), format=fmt) for label, sid in ids.items()],
    ).add_params(nearest)
    boundary = alt.Chart(pd.DataFrame({"date": [pd.Timestamp(settings.end)], "text": ["факт | прогноз"]}))
    layers = [lines, dots, hover,
              boundary.mark_rule(color="#64748b", strokeDash=[2, 3]).encode(x="date:T"),
              boundary.mark_text(align="left", dx=4, dy=-6, color="#64748b", fontSize=11).encode(
                  x="date:T", y=alt.value(8), text="text:N")]
    if not relative:
        rule = alt.Chart(pd.DataFrame({"value": [settings.threshold]}))
        layers += [rule.mark_rule(color="#c62828", strokeDash=[5, 4]).encode(y="value:Q"),
                   rule.mark_text(align="left", x=4, dy=-6, color="#c62828", fontSize=11).encode(
                       y="value:Q", text=alt.value(f"порог {settings.threshold:,.0f}".replace(",", " ")))]
    return alt.layer(*layers).properties(height=380)


def small_multiples(points: pd.DataFrame, settings: Settings, relative: bool) -> alt.FacetChart:
    """Больше COMPARE_MAX товаров — маленькие графики с общими осями вместо клубка линий."""
    y = alt.Y("value:Q", title=None, scale=alt.Scale(reverse=True)) if relative else alt.Y(
        "value:Q", title=None, scale=alt.Scale(type="log", reverse=True))
    base = alt.Chart().encode(x=alt.X("date:T", title=None, axis=alt.Axis(format="%d.%m", labelAngle=0)), y=y)
    lines = base.mark_line(strokeWidth=1.6).encode(
        strokeDash=alt.StrokeDash("kind:N", scale=alt.Scale(domain=["Факт", "Прогноз"], range=[[1, 0], [5, 3]]),
                                  legend=alt.Legend(title=None, orient="top")),
        color=alt.condition("datum.kind == 'Прогноз'", alt.value("#e8590c"), alt.value("#1f6feb")),
        detail="segment:N",
        tooltip=[alt.Tooltip("date:T", title="Дата", format="%d.%m.%Y"), alt.Tooltip("kind:N", title=" "),
                 alt.Tooltip("value:Q", title="%" if relative else "BSR", format="+.0f" if relative else ",.0f")],
    )
    layers = [lines, alt.Chart().mark_rule(color="#94a3b8", strokeDash=[2, 3]).encode(
        x=alt.datum(alt.DateTime(year=settings.end.year, month=settings.end.month, date=settings.end.day)))]
    if not relative:
        layers.append(alt.Chart().mark_rule(color="#c62828", strokeDash=[5, 4]).encode(y=alt.datum(settings.threshold)))
    return alt.layer(*layers, data=points).properties(width=230, height=130).facet(
        facet=alt.Facet("label:N", title=None, header=alt.Header(labelLimit=230, labelFontSize=11)), columns=3,
    ).resolve_scale(y="shared")


def dumbbell_chart(table: pd.DataFrame, settings: Settings, order: list[str]) -> alt.LayerChart:
    """«Сейчас → прогноз»: круг — текущий BSR, ромб — прогноз, линия между ними, вертикаль — порог."""
    data = table.assign(now=table["current"], future=table["forecast"],
                        now_text=table["current"].map(lambda v: f"{v:,.0f}".replace(",", " ") if pd.notna(v) else ""),
                        future_text=table["forecast"].map(lambda v: f"{v:,.0f}".replace(",", " ") if pd.notna(v) else ""),
                        note=table["forecast_note"].replace("", "—"))
    pick = alt.selection_point(fields=["key"], name="pick")
    y = alt.Y("label:N", sort=order, title=None, axis=alt.Axis(labelLimit=320))
    x_scale = alt.Scale(type="log")
    tooltip = [alt.Tooltip("label:N", title="Товар"), alt.Tooltip("now:Q", title="BSR сейчас", format=",.0f"),
               alt.Tooltip("future:Q", title=f"Прогноз на {settings.target:%d.%m}", format=",.0f"),
               alt.Tooltip("note:N", title="Почему нет прогноза"),
               alt.Tooltip("rating:Q", title="Рейтинг", format=".1f"),
               alt.Tooltip("reviews:Q", title="Отзывов", format=",.0f")]
    base = alt.Chart(data).encode(y=y, tooltip=tooltip)
    link = base.mark_rule(color="#94a3b8", strokeWidth=2).encode(
        x=alt.X("now:Q", scale=x_scale, title="BSR (левее — лучше)"), x2="future:Q")
    now = base.mark_point(shape="circle", size=110, filled=True, color="#1f6feb").encode(
        x=alt.X("now:Q", scale=x_scale), opacity=alt.condition(pick, alt.value(1), alt.value(0.85))).add_params(pick)
    future = base.mark_point(shape="diamond", size=120, filled=True, color="#e8590c").encode(
        x=alt.X("future:Q", scale=x_scale))
    now_text = base.mark_text(dy=-11, fontSize=10, color="#1f6feb").encode(x=alt.X("now:Q", scale=x_scale), text="now_text:N")
    future_text = base.mark_text(dy=11, fontSize=10, color="#e8590c").encode(
        x=alt.X("future:Q", scale=x_scale), text="future_text:N")
    rule = alt.Chart(pd.DataFrame({"t": [settings.threshold]})).mark_rule(color="#c62828", strokeDash=[5, 4]).encode(
        x=alt.X("t:Q", scale=x_scale))
    return alt.layer(link, now, future, now_text, future_text, rule).properties(height=AXIS_ROOM + 38 * len(data))


def change_chart(table: pd.DataFrame) -> alt.LayerChart:
    """«Кто теряет позиции»: изменение BSR за период в %, от самого сильного ухудшения к улучшению."""
    data = table.dropna(subset=["change_pct"]).sort_values("change_pct", ascending=False)
    data = data.assign(
        sign=np.where(data["change_pct"] > 0, "Хуже (BSR вырос)", "Лучше (BSR снизился)"),
        text=data["change_pct"].map(lambda v: f"{v:+.0f}%"),
        dates=[f"{s:%d.%m} → {l:%d.%m}" for s, l in zip(data["start_date"], data["last_date"])],
    )
    pick = alt.selection_point(fields=["key"], name="pick")
    base = alt.Chart(data).encode(
        y=alt.Y("label:N", sort=list(data["label"]), title=None, axis=alt.Axis(labelLimit=320)),
        x=alt.X("change_pct:Q", title="Изменение BSR за период, % (вправо — хуже)"),
        tooltip=[alt.Tooltip("label:N", title="Товар"), alt.Tooltip("text:N", title="Изменение"),
                 alt.Tooltip("dates:N", title="Даты замеров")],
    )
    bars = base.mark_bar().encode(
        color=alt.Color("sign:N", scale=alt.Scale(domain=["Хуже (BSR вырос)", "Лучше (BSR снизился)"],
                                                  range=["#c62828", "#2e7d32"]),
                        legend=alt.Legend(title=None, orient="top"))).add_params(pick)
    labels = base.mark_text(align="left", dx=4, fontSize=10, color="#334155").encode(text="text:N")
    zero = alt.Chart(pd.DataFrame({"x": [0]})).mark_rule(color="#64748b").encode(x="x:Q")
    return alt.layer(bars, labels, zero).properties(height=AXIS_ROOM + 20 + ROW_STEP * len(data))


def portfolio_chart(frame: pd.DataFrame, total: int) -> alt.Chart:
    data = frame.assign(coverage=frame["count"].map(lambda c: f"{c} из {total}"))
    return alt.Chart(data).mark_line(point=True, strokeWidth=2, color="#1f6feb").encode(
        x=alt.X("date:T", title=None, axis=alt.Axis(format="%d.%m", labelAngle=0)),
        y=alt.Y("median:Q", title="Медиана BSR группы (выше — лучше)", scale=alt.Scale(type="log", reverse=True)),
        tooltip=[alt.Tooltip("date:T", title="Дата", format="%d.%m.%Y"),
                 alt.Tooltip("median:Q", title="Медиана BSR", format=",.0f"),
                 alt.Tooltip("coverage:N", title="Товаров с замером")],
    ).properties(height=300)


def distribution_chart(dist: pd.DataFrame) -> alt.LayerChart:
    data = dist.assign(text=[f"{c} · {s}%" for c, s in zip(dist["count"], dist["share"])])
    base = alt.Chart(data).encode(
        y=alt.Y("bucket:N", sort=list(data["bucket"]), title=None, axis=alt.Axis(labelLimit=260)),
        x=alt.X("count:Q", title="Товаров", axis=alt.Axis(tickMinStep=1, format="d")),
        tooltip=[alt.Tooltip("bucket:N", title="Диапазон"), alt.Tooltip("count:Q", title="Товаров"),
                 alt.Tooltip("share:Q", title="Доля, %")],
    )
    return alt.layer(base.mark_bar(color="#1f6feb"),
                     base.mark_text(align="left", dx=4, color="#334155").encode(text="text:N")).properties(
        height=AXIS_ROOM + ROW_STEP * len(data))


# --- Интерфейс ---------------------------------------------------------------------------------

_STATE = "fc_selected"      # выбранная группа: множество ключей «страна|ASIN»
_PRODUCT = "fc_product"     # открытый подробный анализ одного товара
_EDITOR = "fc_editor_version"


def _selected() -> set:
    return st.session_state.setdefault(_STATE, set())


def _set_selected(keys: Iterable[str]) -> None:
    st.session_state[_STATE] = set(keys)
    st.session_state[_EDITOR] = st.session_state.get(_EDITOR, 0) + 1


def _open_product(key: str) -> None:
    st.session_state[_PRODUCT] = key


def _apply_editor(editor_key: str, shown: list[str]) -> None:
    """Галочки в списке товаров → общий выбор. Применяются к товарам, которые были на экране, поэтому
    смена поиска уже выбранное не снимает."""
    edits = st.session_state.get(editor_key, {}).get("edited_rows", {})
    chosen = set(_selected())
    for index, change in edits.items():
        if "✓" in change and int(index) < len(shown):
            (chosen.add if change["✓"] else chosen.discard)(shown[int(index)])
    _set_selected(chosen)


def _page(items: list, key: str, size: int) -> list:
    pages = max(1, -(-len(items) // size))
    if pages == 1:
        return items
    page = st.number_input(f"Страница (из {pages})", 1, pages, 1, key=key)
    return items[(page - 1) * size: page * size]


def _chart_pick(event) -> Optional[str]:
    try:
        picked = event["selection"].get("pick") or []
        return picked[0].get("key") if picked else None
    except (AttributeError, KeyError, TypeError, IndexError):
        return None


def render(data: pd.DataFrame) -> None:
    catalog, daily = _prepared(data)
    if catalog.empty or daily["bsr"].notna().sum() == 0:
        st.info("Нет данных для прогноза.")
        return
    first_day, last_day = daily["date"].min().date(), daily["date"].max().date()

    # Фильтры: страна, уровень, период, горизонт, порог — в одну строку, на узком экране — столбиком.
    c1, c2, c3, c4, c5 = st.columns([1.1, 1.1, 1.4, 0.9, 1.1])
    markets = sorted(catalog["market"].unique())
    market = c1.selectbox("Страна", [ALL_MARKETS] + markets, key="fc_market")
    level = c2.radio("Уровень", list(LEVELS), horizontal=True, key="fc_level")
    period = c3.radio("Период", list(PERIODS), index=1, horizontal=True, key="fc_period")
    horizon = c4.selectbox("Прогноз на", HORIZONS, index=1, format_func=lambda d: f"{d} дн.", key="fc_horizon")
    threshold = c5.number_input("Порог внимания, BSR", min_value=1, value=DEFAULT_THRESHOLD, step=10_000,
                                key="fc_threshold", help="BSR больше порога — «хуже порога». Чем меньше BSR, тем лучше.")
    if PERIODS[period] is None:
        chosen = st.date_input("Диапазон", (max(first_day, last_day - timedelta(days=30)), last_day),
                               min_value=first_day, max_value=last_day, key="fc_range", format="DD.MM.YYYY")
        start, end = (chosen if isinstance(chosen, (tuple, list)) and len(chosen) == 2 else (first_day, last_day))
    else:
        start, end = last_day - timedelta(days=PERIODS[period] - 1), last_day
    settings = Settings(start=start, end=end, horizon=horizon, threshold=float(threshold))

    available = catalog
    if market != ALL_MARKETS:
        available = available[available["market"] == market]
    if LEVELS[level]:
        available = available[available["kind"] == LEVELS[level]]
    available_keys = list(available["key"])
    selected = _selected()
    group = [k for k in available_keys if k in selected]
    hidden = len(selected) - len(group)

    _render_selector(available, daily, settings, group)
    group = [k for k in available_keys if k in _selected()]
    st.caption(
        f"Выбрано: {len(group)} из {len(available_keys)} · {market} · {level.lower()} · "
        f"{settings.start:%d.%m.%Y} – {settings.end:%d.%m.%Y} · прогноз на {settings.target:%d.%m.%Y} · "
        f"порог {settings.threshold:,.0f}".replace(",", " ")
        + (f" · ещё {hidden} выбранных не подходят под страну/уровень и не показаны" if hidden > 0 else "")
    )
    if not group:
        st.info("Выберите товары: отметьте галочками в списке выше, «Выбрать все по фильтрам» или «Выбрать проблемные».")
        return

    table = analyze(catalog, daily, group, settings)
    focus = st.session_state.get(_PRODUCT)
    if focus and focus in set(catalog["key"]):
        _render_product(focus, catalog, daily, settings)
        return

    metrics = group_metrics(table)
    m1, m2, m3 = st.columns(3)
    m1.metric("Медиана BSR группы", f"{metrics['median']:,.0f}".replace(",", " ") if metrics["median"] else "—",
              help=f"Медиана текущего BSR по {metrics['with_data']} из {metrics['total']} товаров с замерами.")
    m1.caption(f"медиана · {metrics['with_data']} из {metrics['total']} с данными")
    m2.metric("Уже хуже порога", f"{metrics['below']} · {metrics['below_share']}%")
    m3.metric("Новый риск в прогнозе", metrics["new_risk"],
              help="Сейчас в пределах порога, но по прогнозу на целевую дату уйдут за него.")
    st.caption(f"Прогноз: медиана дневных изменений BSR за последние {TREND_DAYS} дн., продлённая до "
               f"{settings.target:%d.%m.%Y}; расчёт на {settings.end:%d.%m.%Y}; нужно ≥ {MIN_POINTS} замеров. "
               "Это тренд, а не модель: сезон и акции он не учитывает.")

    mode = st.radio("Режим", ["Сравнение", "Портфель", "Товар"], horizontal=True, key="fc_mode")
    if mode == "Товар":
        ordered = sort_table(table, "Сначала риски")
        labels = dict(zip(ordered["key"], ordered["label"]))
        chosen = st.selectbox("Товар группы", list(ordered["key"]), format_func=labels.get, key="fc_product_pick")
        _render_product(chosen, catalog, daily, settings, with_back=False)
        return
    if mode == "Сравнение":
        _render_comparison(table, daily, settings)
    else:
        _render_portfolio(table, daily, group, settings)

    left, right = st.columns(2)
    with left:
        st.markdown('<p class="section-title">Сейчас → прогноз</p>', unsafe_allow_html=True)
        how = st.selectbox("Сортировка", list(SORTS), key="fc_dumbbell_sort")
        ordered = sort_table(table, how)
        shown = _page(list(ordered["key"]), "fc_dumbbell_page", ROWS_PAGE)
        part = ordered[ordered["key"].isin(shown)]
        event = st.altair_chart(dumbbell_chart(part, settings, list(part["label"])), use_container_width=True,
                                on_select="rerun", key="fc_dumbbell")
        if (picked := _chart_pick(event)):
            _open_product(picked)
            st.rerun()
        missing = part[part["forecast"].isna()]
        if not missing.empty:
            st.caption("Без прогноза: " + "; ".join(f"{short(n or a, 24)} — {note}" for n, a, note in
                                                       zip(missing["name"], missing["asin"], missing["forecast_note"])))
    with right:
        st.markdown('<p class="section-title">Кто теряет позиции</p>', unsafe_allow_html=True)
        changes = table.dropna(subset=["change_pct"]).sort_values("change_pct", ascending=False)
        if changes.empty:
            st.info("Изменение не посчитать: в периоде у товаров меньше двух замеров.")
        else:
            shown = _page(list(changes["key"]), "fc_change_page", ROWS_PAGE)
            event = st.altair_chart(change_chart(changes[changes["key"].isin(shown)]), use_container_width=True,
                                    on_select="rerun", key="fc_change")
            if (picked := _chart_pick(event)):
                _open_product(picked)
                st.rerun()

    _render_table(table, settings)


@st.cache_data(ttl=60, show_spinner=False)
def _prepared(data: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    return prepare(data)


def _render_selector(available: pd.DataFrame, daily: pd.DataFrame, settings: Settings, group: list[str]) -> None:
    with st.expander(f"Выбор товаров — выбрано {len(group)}", expanded=not group):
        query = st.text_input("Поиск по названию или ASIN", key="fc_search", placeholder="Название или ASIN")
        found = search(available, query)
        b1, b2, b3, _ = st.columns([1.3, 1, 1.3, 2])
        if b1.button(f"Выбрать все по фильтрам ({len(found)})", key="fc_all", use_container_width=True):
            _set_selected(_selected() | set(found["key"]))
            st.rerun()
        if b2.button("Очистить", key="fc_clear", use_container_width=True):
            _set_selected(set())
            st.session_state.pop(_PRODUCT, None)
            st.rerun()
        if b3.button("Выбрать проблемные", key="fc_problems", use_container_width=True):
            problems = problem_keys(analyze(available.assign(), daily, list(available["key"]), settings))
            _set_selected(_selected() | set(problems))
            st.rerun()
        shown = list(found["key"])
        ordered_days = daily.sort_values("date")
        latest = ordered_days.dropna(subset=["bsr"]).groupby("key")["bsr"].last()
        rating = ordered_days.dropna(subset=["rating"]).groupby("key")["rating"].last()
        reviews = ordered_days.dropna(subset=["reviews"]).groupby("key")["reviews"].last()
        chosen = _selected()
        view = pd.DataFrame({
            "✓": [k in chosen for k in shown],
            "Товар": [n or "—" for n in found["name"]], "ASIN": found["asin"].values,
            "Страна": found["market"].values, "Тип": found["kind"].values,
            "BSR сейчас": [latest.get(k, np.nan) for k in shown],
            "Рейтинг": [rating.get(k, np.nan) for k in shown],
            "Отзывов": [reviews.get(k, np.nan) for k in shown],
        })
        editor_key = f"fc_editor_{st.session_state.get(_EDITOR, 0)}"
        st.data_editor(
            view, key=editor_key, hide_index=True, use_container_width=True, height=min(420, 38 + 35 * len(view)),
            disabled=["Товар", "ASIN", "Страна", "Тип", "BSR сейчас", "Рейтинг", "Отзывов"],
            column_config={"✓": st.column_config.CheckboxColumn("✓", width="small"),
                           "BSR сейчас": st.column_config.NumberColumn(format="%d"),
                           "Рейтинг": st.column_config.NumberColumn(format="%.1f ★"),
                           "Отзывов": st.column_config.NumberColumn(format="%d")},
            on_change=_apply_editor, args=(editor_key, shown),
        )
        st.caption(f"Найдено {len(found)} · выбрано всего {len(chosen)}")


def _render_comparison(table: pd.DataFrame, daily: pd.DataFrame, settings: Settings) -> None:
    st.markdown('<p class="section-title">История и прогноз</p>', unsafe_allow_html=True)
    scale = st.radio("Шкала", ["BSR", "Изменение от начала периода, %"], horizontal=True, key="fc_scale")
    relative = scale != "BSR"
    ordered = sort_table(table, "Сначала риски")
    if len(ordered) <= COMPARE_MAX:
        points = series_frame(daily, ordered, settings, relative)
        if points.empty:
            st.info("В выбранном периоде у этих товаров нет замеров BSR.")
            return
        st.altair_chart(history_chart(points, settings, relative, list(ordered["label"])), use_container_width=True)
        st.caption("Сплошная — факт, пунктир — прогноз; разрыв линии — день без замера. "
                   "Клик по легенде временно скрывает товар, не убирая его из группы.")
    else:
        st.caption(f"Товаров {len(ordered)} — больше {COMPARE_MAX}, поэтому отдельные графики с общей шкалой "
                   f"(по {SMALL_PAGE} на странице, сначала проблемные).")
        keys = _page(list(ordered["key"]), "fc_small_page", SMALL_PAGE)
        part = ordered[ordered["key"].isin(keys)]
        points = series_frame(daily, part, settings, relative)
        if not points.empty:
            st.altair_chart(small_multiples(points, settings, relative), use_container_width=False)


def _render_portfolio(table: pd.DataFrame, daily: pd.DataFrame, group: list[str], settings: Settings) -> None:
    st.markdown('<p class="section-title">Динамика группы</p>', unsafe_allow_html=True)
    frame = portfolio_frame(daily, group, settings)
    if frame.empty:
        st.info("В выбранном периоде у группы нет замеров BSR.")
    else:
        st.altair_chart(portfolio_chart(frame, len(group)), use_container_width=True)
        st.caption(f"Медиана BSR по товарам группы с замером в этот день (в подсказке — сколько из {len(group)}). "
                   "Прогноз для группы не строится: у товаров BSR отличается в сотни раз, обоснованно сложить "
                   "их прогнозы нельзя — смотрите прогноз по товарам ниже.")
    st.markdown('<p class="section-title">Распределение BSR</p>', unsafe_allow_html=True)
    st.altair_chart(distribution_chart(distribution(table, settings.threshold)), use_container_width=True)
    st.caption(f"По выбранной группе: {len(group)} товаров, текущий BSR.")
    problems = table[table["risk"].isin([RISK_NOW, RISK_FORECAST, RISK_FALLING])]
    if not problems.empty:
        st.markdown("**Проблемные в группе** (за средним не прячутся): " + ", ".join(
            f"{escape(short(n or a, 30))} — {RISK_LABELS[r][0].lower() + RISK_LABELS[r][1:]}" for n, a, r in
            zip(problems["name"], problems["asin"], problems["risk"])))


def _render_table(table: pd.DataFrame, settings: Settings) -> None:
    st.markdown('<p class="section-title">Товары выбранной группы</p>', unsafe_allow_html=True)
    f1, f2, f3 = st.columns([1.4, 1.6, 1])
    query = f1.text_input("Поиск в таблице", key="fc_table_search", placeholder="Название или ASIN")
    which = f2.radio("Показать", list(TABLE_FILTERS), horizontal=True, key="fc_table_filter")
    how = f3.selectbox("Сортировка", list(SORTS), key="fc_table_sort")
    rows = TABLE_FILTERS[which](table)
    rows = rows[rows["key"].isin(search(rows.assign(), query)["key"])] if query else rows
    rows = sort_table(rows, how)
    view = table_view(rows, settings)
    st.caption(f"Показано {len(view)} из {len(table)} в группе. Поиск и фильтр меняют только таблицу — "
               "показатели и графики считаются по всей группе. Клик по строке выделяет её.")
    event = st.dataframe(view, use_container_width=True, hide_index=True, on_select="rerun",
                         selection_mode="multi-row", key="fc_table", placeholder="—",
                         column_config={"BSR сейчас": st.column_config.NumberColumn(format="%d"),
                                        f"Прогноз на {settings.target:%d.%m}": st.column_config.NumberColumn(format="%d"),
                                        "Отзывов": st.column_config.NumberColumn(format="%d")})
    picked = [rows["key"].iloc[i] for i in getattr(getattr(event, "selection", None), "rows", []) if i < len(rows)]
    a1, a2, a3 = st.columns([1, 1.3, 1])
    if len(picked) == 1 and a1.button("Открыть товар", key="fc_open_row", use_container_width=True):
        _open_product(picked[0])
        st.rerun()
    if picked and a2.button(f"Оставить в группе только выбранные ({len(picked)})", key="fc_keep_rows",
                            use_container_width=True):
        _set_selected(picked)
        st.rerun()
    a3.download_button("⬇ CSV (показанные строки)", view.to_csv(index=False).encode("utf-8-sig"),
                       file_name="forecast_group.csv", mime="text/csv", key="fc_csv", use_container_width=True)


def _render_product(key: str, catalog: pd.DataFrame, daily: pd.DataFrame, settings: Settings,
                    with_back: bool = True) -> None:
    """Подробно один товар. Открыт кликом — кнопка возврата; группа, период, горизонт и фильтры не трогаются."""
    item = catalog.set_index("key").loc[key]
    row = analyze(catalog, daily, [key], settings).iloc[0]
    if with_back and st.button("← Вернуться к группе", key="fc_back"):
        st.session_state.pop(_PRODUCT, None)
        st.rerun()
    domain = {"US": "com", "CA": "ca", "UK": "co.uk", "DE": "de", "FR": "fr", "ES": "es", "IT": "it",
              "MX": "com.mx", "JP": "co.jp", "AU": "com.au"}.get(item["market"], "com")
    st.markdown(f'<p class="section-title">{escape(item["name"] or item["asin"])}</p>'
                f'<p class="section-note">{escape(item["market"])} · {item["kind"]} · '
                f'<a href="https://www.amazon.{domain}/dp/{escape(item["asin"])}" target="_blank">{escape(item["asin"])}</a></p>',
                unsafe_allow_html=True)
    m1, m2, m3 = st.columns(3)
    m1.metric("BSR сейчас", f"{row['current']:,.0f}".replace(",", " ") if pd.notna(row["current"]) else "нет данных",
              delta=f"{row['change_pct']:+.0f}% за период" if pd.notna(row["change_pct"]) else None, delta_color="inverse")
    m2.metric(f"Прогноз на {settings.target:%d.%m}",
              f"{row['forecast']:,.0f}".replace(",", " ") if pd.notna(row["forecast"]) else "—",
              delta=f"{row['forecast_change_pct']:+.0f}%" if pd.notna(row["forecast_change_pct"]) else None,
              delta_color="inverse", help=None if pd.notna(row["forecast"]) else f"Прогноза нет: {row['forecast_note']}")
    m3.metric("Статус", RISK_LABELS[int(row["risk"])])
    r1, r2, _ = st.columns(3)
    r1.metric("Рейтинг", f"{row['rating']:.1f} ★" if pd.notna(row["rating"]) else "нет данных",
              delta=f"{row['rating_change']:+.1f} за период" if pd.notna(row["rating_change"]) else None)
    r2.metric("Отзывов", f"{row['reviews']:,.0f}".replace(",", " ") if pd.notna(row["reviews"]) else "нет данных",
              delta=f"{row['reviews_added']:+.0f} за период" if pd.notna(row["reviews_added"]) else None)
    points = series_frame(daily, pd.DataFrame([row]), settings)
    if points.empty:
        st.info("В выбранном периоде у товара нет замеров BSR.")
    else:
        st.altair_chart(history_chart(points, settings, False, [row["label"]]), use_container_width=True)
    related = item["rivals"] if item["kind"] == "наш" else item["ours"]
    if related:
        title = "Конкуренты этого товара" if item["kind"] == "наш" else "С какими нашими товарами в паре"
        st.markdown(f'<p class="section-title">{title}</p>', unsafe_allow_html=True)
        st.dataframe(table_view(analyze(catalog, daily, related, settings), settings), use_container_width=True,
                     hide_index=True, placeholder="—")
        if st.button("Сравнить вместе с ними", key="fc_compare_related"):
            _set_selected(_selected() | {key, *related})
            st.session_state["fc_mode"] = "Сравнение"
            st.session_state.pop(_PRODUCT, None)
            st.rerun()
