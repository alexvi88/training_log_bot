"""«Сколько должен жать мужчина весом 80 кг»: нормативы жима, приседа и
становой для одного веса тела — `/<язык>/strength/<упражнение>/<пол>/<вес>kg`.

Зачем отдельные страницы, когда есть общая таблица (`strength_lift`): так
ищут — с весом в запросе («how much should I bench at 180 lbs»), и короткий
ответ с цифрой для этого веса нужен в заголовке и первой строке, а не в
двадцатой строке таблицы. Цифры — та же шкала, что у таблицы и калькулятора
(`seo_tools.level_weight`, DOTS), и медиана соревнующихся из того же файла
OpenPowerlifting, что у калькулятора перцентилей (`seo_tools.percentile_data`).
Придуманных цифр нет: для упражнений без открытых данных страниц нет.

Вес в адресе — килограммы. Английская страница называет и фунты (округлённо
до целого), потому что так ищут в США; адрес у пары один — hreflang требует
одной страницы на оба языка.
"""

from __future__ import annotations

from typing import Optional

import config
import seo_pages as sp
import seo_tools

Page = sp.Page

# Вес тела, кг: где ищут чаще всего, шагом 10. Шире — уже за краем, где шкала
# держится (DOTS_BW_LIMITS), и там помогает общая таблица.
BODYWEIGHTS = {"m": (60, 70, 80, 90, 100, 110, 120), "f": (50, 60, 70, 80, 90)}
# Упражнения: адрес страницы → ключ шкалы. Сумма троеборья по весу не ищется.
LIFTS = (("bench-press", "bench"), ("squat", "squat"), ("deadlift", "deadlift"))
_KEY_BY_URL = dict(LIFTS)
_SEX_BY_URL = {"men": "m", "women": "f"}
_URL_BY_SEX = {v: k for k, v in _SEX_BY_URL.items()}
# Медиана и верхние 10% — точки таблицы перцентилей (quantiles в файле).
_MEDIAN_Q, _TOP_Q = 50, 90


def arg2(sex: str, bw: int) -> str:
    return f"{_URL_BY_SEX[sex]}/{bw}kg"


def pages() -> list[Page]:
    return [Page("strength_bw", url, arg2(sex, bw)) for url, _k in LIFTS for sex in ("m", "f") for bw in BODYWEIGHTS[sex]]


def parts(page: Page) -> tuple[str, str, int]:
    """(ключ упражнения, пол m|f, вес тела в кг)."""
    sex_url, bw = page.arg2.split("/")
    return _KEY_BY_URL[page.arg], _SEX_BY_URL[sex_url], int(bw.removesuffix("kg"))


def lb_of(bw_kg: float) -> int:
    return int(round(bw_kg * config.LB_PER_KG))


def bw_label(lang: str, bw: int) -> str:
    """Вес тела в тексте: «80 кг», на английском — «176 lb (80 kg)»: в США
    ищут в фунтах."""
    t = sp._t(lang)
    if lang == "en":
        return f"{lb_of(bw)}\u00a0{t('seo.tool.unit_lb')} ({bw}\u00a0{t('seo.tool.unit_kg')})"
    return f"{bw}\u00a0{t('seo.tool.unit_kg')}"


def page_h1(page: Page, lang: str) -> str:
    key, sex, bw = parts(page)
    return sp._t(lang)(f"seo.sbw.{key}.{sex}.h1", w=bw_label(lang, bw))


def levels(key: str, sex: str, bw: int) -> list[tuple[str, float]]:
    """(уровень, разовый максимум в кг, округлён до 2.5) — как в общей таблице."""
    return [(name, seo_tools.round_to(seo_tools.level_weight(th, key, bw, sex), 2.5)) for name, th in seo_tools.LEVELS]


def competitors(key: str, sex: str, bw: int) -> Optional[tuple[str, int, float, float]]:
    """(весовая категория, атлетов в клетке, медиана, порог верхних 10%) или None."""
    data = seo_tools.percentile_data()
    cls = seo_tools.weight_class(sex, bw)
    cell = data["table"].get(sex, {}).get(cls, {}).get(key)
    if not cell:
        return None
    qs = data["quantiles"]
    return cls, cell["n"], cell["q"][qs.index(_MEDIAN_Q)], cell["q"][qs.index(_TOP_Q)]


def _weight(lang: str, kg: float) -> str:
    """Вес текстом: «100 кг», на английском — «100 kg (220 lb)»."""
    t = sp._t(lang)
    text = f"{sp._num(kg)} {t('seo.tool.unit_kg')}"
    if lang == "en":
        text += f" ({sp._num(seo_tools.round_to(kg * config.LB_PER_KG, 5.0))} {t('seo.tool.unit_lb')})"
    return text


def _lead(lang: str, key: str, sex: str, bw: int) -> str:
    t = sp._t(lang)
    by_name = dict(levels(key, sex, bw))
    return t(f"seo.sbw.{key}.{sex}.lead", w=bw_label(lang, bw), mid=_weight(lang, by_name["intermediate"]),
             beg=_weight(lang, by_name["beginner"]), adv=_weight(lang, by_name["advanced"]))


def _table(lang: str, key: str, sex: str, bw: int) -> str:
    t = sp._t(lang)
    kg_label, lb_label = t("seo.tool.unit_kg"), t("seo.tool.unit_lb")
    head = f"<th>{sp._e(t('seo.sbw.col_level'))}</th><th class=num>{sp._e(t('seo.sbw.col_kg'))}</th>"
    if lang == "en":
        head += f"<th class=num>{sp._e(t('seo.sbw.col_lb'))}</th>"
    head += f"<th class=num>{sp._e(t('seo.sbw.col_ratio'))}</th>"
    rows = []
    for name, kg in levels(key, sex, bw):
        cells = f"<th scope=row>{sp._e(t(f'seo.strength.level.{name}'))}</th><td class=num>{sp._w(kg, kg_label)}</td>"
        if lang == "en":
            cells += f"<td class=num>{sp._w(seo_tools.round_to(kg * config.LB_PER_KG, 5.0), lb_label)}</td>"
        cells += f"<td class=num>{sp._num(round(kg / bw, 2))}×</td>"
        rows.append(f"<tr>{cells}</tr>")
    return (f"<div class='table-card scroll-x sticky-first'><table><thead><tr>{head}</tr></thead>"
            f"<tbody>{''.join(rows)}</tbody></table></div>")


def _competitors_html(lang: str, key: str, sex: str, bw: int) -> str:
    found = competitors(key, sex, bw)
    if found is None:
        return ""
    t = sp._t(lang)
    cls, n, median, top = found
    plus = cls.endswith("+")
    text = t("seo.sbw.pct_plus" if plus else "seo.sbw.pct", cls=cls.rstrip("+"), n=n,
             lift=t(f"seo.sbw.{key}.of"), median=_weight(lang, median), top=_weight(lang, top))
    pct_href = sp.url_for(Page("tool", "strength-percentile"), lang)
    return (f"<section class='prose reveal sub'><h2>{sp._e(t('seo.sbw.pct.h'))}</h2><p>{sp._e(text)}</p>"
            f"<p>{t('seo.sbw.pct_link_html', href=pct_href)}</p></section>")


def _links(lang: str, page: Page, key: str, sex: str, bw: int) -> str:
    t = sp._t(lang)
    same = [(sp.url_for(Page("strength_bw", page.arg, arg2(sex, w)), lang), t("seo.sbw.link_bw", w=bw_label(lang, w)))
            for w in BODYWEIGHTS[sex] if w != bw]
    other = [(sp.url_for(Page("strength_bw", url, page.arg2), lang), sp._strip_emoji(page_h1(Page("strength_bw", url, page.arg2), lang)))
             for url, k in LIFTS if k != key]
    lift_page = Page("strength_lift", page.arg, _URL_BY_SEX[sex])
    other.append((sp.url_for(lift_page, lang), sp._strip_emoji(sp.page_h1(lift_page, lang))))
    ex_url = sp._LIFT_BY_URL[page.arg][1]
    if ex_url:
        other.append((sp.url_for(Page("exercise", ex_url), lang),
                      t("seo.strength.technique", lift=t(f"seo.strength.lift.{key}"))))

    def ul(items: list[tuple[str, str]]) -> str:
        return "<ul class=links>" + "".join(f'<li><a href="{h}">{sp._e(label)}</a></li>' for h, label in items) + "</ul>"

    return (f"<section class='reveal sub'><h2>{sp._e(t('seo.sbw.other_bw.h'))}</h2>{ul(same)}</section>"
            f"<section class='reveal sub'><h2>{sp._e(t('seo.lib.related'))}</h2>{ul(other)}</section>")


def _render(lang: str, page: Page, request) -> str:
    t = sp._t(lang)
    key, sex, bw = parts(page)
    h1 = page_h1(page, lang)
    lead = _lead(lang, key, sex, bw)
    by_name = dict(levels(key, sex, bw))
    title = t("seo.sbw.title", h1=h1)
    description = t(f"seo.sbw.{key}.{sex}.description", w=bw_label(lang, bw), mid=_weight(lang, by_name["intermediate"]),
                    beg=_weight(lang, by_name["beginner"]), elite=_weight(lang, by_name["elite"]))
    form, result, script = sp._strength_tool(lang, key, sex, bw=bw, lift_kg=by_name["intermediate"])
    body = (
        f"<span class=eyebrow>{sp._e(t('seo.eyebrow.strength'))}</span>"
        f"<h1>{sp._e(h1)}</h1><p class=lead>{sp._e(lead)}</p>"
        + f"<section class=reveal><h2>{sp._e(t('seo.sbw.table.h', w=bw_label(lang, bw)))}</h2>{_table(lang, key, sex, bw)}"
        f"<p class=muted>{sp._e(t('seo.sbw.table_note'))}</p></section>"
        + _competitors_html(lang, key, sex, bw)
        + f"<section class=reveal><h2>{sp._e(t('seo.strength.tool.h'))}</h2><div class=tool>{form}{result}</div></section>"
        + sp._strength_method(lang)
        + sp._cta(lang, page.block, sp._e(t("seo.strength.cta")), request)
        + _links(lang, page, key, sex, bw)
        + sp._calc_script(lang, script)
    )
    ld = [sp._webpage_ld(lang, page, request, title, description)]
    return sp._layout(lang, page, request, title=title, description=description, body=body, wide=True, ld=ld)


def lift_page_links(lang: str, lift_url: str, sex: str) -> str:
    """Блок на общей таблице: ссылки на страницы отдельных весов."""
    if lift_url not in _KEY_BY_URL:
        return ""
    t = sp._t(lang)
    items = "".join(
        f'<li><a href="{sp.url_for(Page("strength_bw", lift_url, arg2(sex, w)), lang)}">'
        f"{sp._e(t('seo.sbw.link_bw', w=bw_label(lang, w)))}</a></li>" for w in BODYWEIGHTS[sex])
    return f"<section class='reveal sub'><h2>{sp._e(t('seo.sbw.by_bw.h'))}</h2><ul class=links>{items}</ul></section>"


RENDERERS = {"strength_bw": _render}
