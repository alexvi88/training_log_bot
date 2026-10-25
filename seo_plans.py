"""Публичный сайт, четвёртая часть: сплиты, тренировки «две группы за день»
и упражнения на мышцу одним снарядом.

- `/splits`, `/splits/<n>-day` (2–6), `/splits/phul`,
  `/splits/full-body-vs-split` — сколько дней в неделю и как их разложить.
  Варианты — готовые программы бота (`seed_data.WORKOUT_PROGRAMS`), а где
  такой нет — шаблон из упражнений каталога (`TEMPLATES`), подписанный как
  шаблон: в боте его нет, его собирают сами или просят тренера.
  У каждого варианта — неделя по дням и подходы на мышцу за неделю,
  посчитанные тем же правилом, что страницы мышц (`seed_data.volume_split`:
  становая — пополам спине и задней цепи ног).
- `/workouts/<a>-and-<b>` — день двух групп: из программы бота, где такой
  день есть («Грудь + трицепс» в «Сплите на 3 дня»), иначе шаблон.
- `/muscles/<мышца>/<снаряд>` — упражнения каталога на мышцу одним
  снарядом (seo_catalog.equipment_of) и пример тренировки из них. Страница
  есть, только если таких упражнений не меньше `MIN_EQUIP_EXERCISES`:
  меньше — тонкая, без неё лучше.

Тексты — `seo.split.*`, `seo.tpl.*`, `seo.wkt.*`, `seo.me.*` в каталоге.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Optional

import seed_data
import seo_catalog
import seo_pages as sp

Page = sp.Page

# ---------- шаблоны ----------

# Шаблоны (в боте таких программ нет) — идентичности каталога, seo_catalog.SITE_TEMPLATES.
TEMPLATES = seo_catalog.SITE_TEMPLATES
# Верх/низ + жим/тяга/ноги: дни целиком из двух программ бота.
_COMPOSED = {"ulppl": (("upperlower", 0), ("upperlower", 1), ("ppl", 0), ("ppl", 1), ("ppl", 2))}

# Источник варианта: ("program", ключ программы бота) или ("template", ключ шаблона).
Source = tuple[str, str]
# Неделя: 7 клеток пн…вс, в клетке — номер дня программы или None (отдых).
# Несколько недель — чередование (5×5: A-B-A, потом B-A-B).
Schedule = tuple[tuple[Optional[int], ...], ...]

_X = None
SPLITS: dict[str, tuple[tuple[Source, Schedule], ...]] = {
    "2-day": ((("program", "fullbody2"), ((0, _X, _X, 1, _X, _X, _X),)),
              (("program", "upperlower"), ((0, _X, _X, 1, _X, _X, _X),))),
    "3-day": ((("program", "fullbody3"), ((0, _X, 1, _X, 2, _X, _X),)),
              (("program", "strength5x5"), ((0, _X, 1, _X, 0, _X, _X), (1, _X, 0, _X, 1, _X, _X))),
              (("program", "split3"), ((0, _X, 1, _X, 2, _X, _X),)),
              (("program", "ppl"), ((0, _X, 1, _X, 2, _X, _X),)),
              (("program", "glutes3"), ((0, _X, 1, _X, 2, _X, _X),))),
    "4-day": ((("program", "upperlower"), ((0, 1, _X, 0, 1, _X, _X),)),
              (("template", "phul"), ((0, 1, _X, 2, 3, _X, _X),))),
    "5-day": ((("template", "bro5"), ((0, 1, 2, 3, 4, _X, _X),)),
              (("template", "ulppl"), ((0, 1, _X, 2, 3, 4, _X),))),
    "6-day": ((("program", "ppl"), ((0, 1, 2, 0, 1, 2, _X),)),),
    "phul": ((("template", "phul"), ((0, 1, _X, 2, 3, _X, _X),)),),
}
SPLIT_DAYS = {"2-day": 2, "3-day": 3, "4-day": 4, "5-day": 5, "6-day": 6, "phul": 4}
# Сравнение «всё тело или сплит»: три подхода к неделе на 3–6 дней.
COMPARE = ((("program", "fullbody3"), ((0, _X, 1, _X, 2, _X, _X),)),
           (("program", "upperlower"), ((0, 1, _X, 0, 1, _X, _X),)),
           (("program", "ppl"), ((0, 1, 2, 0, 1, 2, _X),)),
           (("program", "split3"), ((0, _X, 1, _X, 2, _X, _X),)))
SPLIT_PAGES = ("2-day", "3-day", "4-day", "5-day", "6-day", "phul", "full-body-vs-split")

# День «две группы»: (источник, номер дня) — программа бота, где он есть, или шаблон.
WORKOUTS: dict[str, tuple[Source, int]] = {
    "chest-and-triceps": (("program", "split3"), 0),
    "back-and-biceps": (("program", "split3"), 1),
    "shoulders-and-arms": (("template", "arms"), 0),
}

# Мышцы в таблице объёма — те же адреса, что /muscles/<мышца>.
VOLUME_MUSCLES = ("chest", "back", "shoulders", "side-delts", "rear-delts", "biceps", "triceps", "quads",
                  "hamstrings", "glutes", "calves", "abs")

# Мышца × снаряд: мышцы — без дельт по отдельности (их упражнения уже в «Плечах»).
EQUIP_MUSCLES = ("chest", "back", "shoulders", "biceps", "triceps", "abs", "quads", "hamstrings", "glutes", "calves")
MIN_EQUIP_EXERCISES = 4
MAX_EQUIP_EXERCISES = 12
# Основная доля подхода: становая с половиной спине — да, четверть бицепсу бедра — нет.
_MAIN_SHARE = 0.5


# ---------- данные ----------


def days_of(source: Source) -> list[list[tuple[str, str]]]:
    kind, key = source
    if kind == "program":
        return [list(exs) for _name, exs in seed_data.PROGRAM_BY_KEY[key]["days"]]
    if key in _COMPOSED:
        return [list(seed_data.PROGRAM_BY_KEY[p]["days"][i][1]) for p, i in _COMPOSED[key]]
    return [list(d) for d in TEMPLATES[key]]


def day_name(source: Source, index: int, lang: str) -> str:
    kind, key = source
    if kind == "program":
        return seed_data.localized_program_day_name(key, index, lang)
    if key in _COMPOSED:
        prog, i = _COMPOSED[key][index]
        return seed_data.localized_program_day_name(prog, i, lang)
    return sp._t(lang)(f"seo.tpl.{key}.d{index + 1}")


def source_name(source: Source, lang: str) -> str:
    kind, key = source
    if kind == "program":
        return sp._strip_emoji(seed_data.localized_program_name(key, lang))
    return sp._t(lang)(f"seo.tpl.{key}.name")


def _sets(scheme: str) -> int:
    import seo_library

    return seo_library._sets_of(scheme) or 0


def _muscle(url: str) -> tuple[str, Optional[str]]:
    import seo_library

    return seo_library._MUSCLE_BY_URL[url]


def session_volume(day: list[tuple[str, str]]) -> dict[str, float]:
    """Подходы на мышцу за одну тренировку (правилом бота)."""
    out = {}
    for url in VOLUME_MUSCLES:
        group, target = _muscle(url)
        out[url] = sum(_sets(scheme) * seo_catalog.share(identity, group, target) for identity, scheme in day)
    return out


def weekly_volume(source: Source, schedule: Schedule) -> dict[str, float]:
    """Подходы на мышцу за неделю: сумма по дням недели, среднее по неделям чередования."""
    days = days_of(source)
    per_day = [session_volume(d) for d in days]
    out = {url: 0.0 for url in VOLUME_MUSCLES}
    for week in schedule:
        for cell in week:
            if cell is None:
                continue
            for url, sets in per_day[cell].items():
                out[url] += sets / len(schedule)
    return out


def weekly_frequency(source: Source, schedule: Schedule, url: str) -> float:
    """Сколько раз в неделю мышца получает хотя бы один основной подход."""
    group, target = _muscle(url)
    days = days_of(source)
    hit = [any(seo_catalog.share(i, group, target) >= _MAIN_SHARE for i, _s in d) for d in days]
    total = sum(1 for week in schedule for cell in week if cell is not None and hit[cell])
    return total / len(schedule)


def sessions_per_week(schedule: Schedule) -> int:
    return max(sum(1 for c in week if c is not None) for week in schedule)


def sets_per_session(source: Source) -> float:
    days = days_of(source)
    return sum(_sets(s) for d in days for _i, s in d) / len(days)


def program_schedules(key: str) -> list[tuple[str, Schedule]]:
    """(страница дней, неделя) для программы бота — во всех страницах сплитов, где она есть."""
    out = []
    for page, variants in SPLITS.items():
        if page == "phul":
            continue
        for source, schedule in variants:
            if source == ("program", key):
                out.append((page, schedule))
    return out


@lru_cache(maxsize=None)
def equip_exercises(muscle: str, equip: str) -> tuple[str, ...]:
    """Упражнения каталога с кадрами на мышцу этим снарядом — по популярности."""
    group, target = _muscle(muscle)
    out = []
    for _g, name in seed_data.EXERCISE_TEMPLATES:
        if not seo_catalog.shown(name) or seo_catalog.equipment_of(name) != equip:
            continue
        if seo_catalog.share(name, group, target) >= _MAIN_SHARE:
            out.append(name)
    out.sort(key=lambda n: (seo_catalog.page_url(n) is None, seed_data.catalog_popularity(n)))
    return tuple(out[:MAX_EQUIP_EXERCISES])


@lru_cache(maxsize=None)
def equip_pages() -> tuple[tuple[str, str], ...]:
    return tuple((m, e) for m in EQUIP_MUSCLES for e in seo_catalog.EQUIPMENT
            if len(equip_exercises(m, e)) >= MIN_EQUIP_EXERCISES)


def sample_session(names: tuple[str, ...]) -> list[tuple[str, str]]:
    """Пример тренировки из списка: до двух базовых первыми, потом изоляция;
    четыре упражнения. Схемы — по виду прогрессии бота (повторы у веса тела,
    секунды у удержаний)."""
    compound = [n for n in names if seo_catalog.pattern_of(n)]
    isolation = [n for n in names if not seo_catalog.pattern_of(n)]
    take = max(2, 4 - len(isolation))
    picked = (compound[:take] + isolation)[:4]
    if len(picked) < 4:
        picked += [n for n in compound[take:]][: 4 - len(picked)]
    picked.sort(key=lambda n: not seo_catalog.pattern_of(n))
    out = []
    for name in picked:
        kind = seed_data.progression_kind(name)
        if kind == "timed":
            scheme = seo_catalog.TIMED_SCHEME
        elif kind in ("bodyweight", "no_load"):
            scheme = "3×8–15" if seo_catalog.pattern_of(name) else "3×12–20"
        elif seo_catalog.pattern_of(name):
            scheme = "3×6–10"
        else:
            scheme = "3×10–15"
        out.append((name, scheme))
    return out


def shown_identities() -> list[str]:
    """Всё, что эти страницы показывают карточкой с кадром."""
    names: list[str] = []
    for m, e in equip_pages():
        names += list(equip_exercises(m, e))
    for key in TEMPLATES:
        for day in TEMPLATES[key]:
            names += [i for i, _s in day]
    return list(dict.fromkeys(names))


def pages() -> list[Page]:
    out = [Page("splits")] + [Page("split", s) for s in SPLIT_PAGES]
    out += [Page("workout", w) for w in WORKOUTS]
    out += [Page("muscle_equip", m, e) for m, e in equip_pages()]
    return out


# ---------- заголовки ----------


def muscle_word(url: str, lang: str) -> str:
    """«грудь» / «chest» — имя мышцы для «упражнения на …»."""
    return sp._t(lang)(f"seo.musacc.{url}")


def page_h1(page: Page, lang: str) -> str:
    t = sp._t(lang)
    if page.kind == "splits":
        return t("seo.splits.h1")
    if page.kind == "split":
        return t(f"seo.split.{page.arg}.h1")
    if page.kind == "workout":
        return t(f"seo.wkt.{page.arg}.h1")
    if page.kind == "muscle_equip":
        return t(f"seo.me.{page.arg2}.h1", muscle=muscle_word(page.arg, lang))
    raise ValueError(page.kind)


# ---------- разметка ----------


def _ex_link(identity: str, lang: str) -> str:
    name = sp._e(seed_data.localized_exercise_name(identity, lang))
    url = seo_catalog.page_url(identity)
    if url:
        return f'<a class=ex-link href="{sp.url_for(Page("exercise", url), lang)}">{name}</a>'
    return name


def day_cards(source: Source, lang: str, only: Optional[int] = None) -> str:
    t = sp._t(lang)
    cards = []
    for i, day in enumerate(days_of(source)):
        if only is not None and i != only:
            continue
        rows = "".join(f"<li><span>{_ex_link(identity, lang)}</span><span>{sp._e(seed_data.localized_target(scheme, lang) or '')}</span></li>"
                       for identity, scheme in day)
        label = "" if only is not None else f"<small>{sp._e(t('seo.program.day', n=i + 1))}</small>"
        cards.append(f'<div class="day reveal"><h3>{label}{sp._e(day_name(source, i, lang))}</h3><ul>{rows}</ul></div>')
    return f"<div class=days>{''.join(cards)}</div>"


def schedule_table(source: Source, schedule: Schedule, lang: str) -> str:
    """Неделя по дням: строки — пн…вс, столбцы — недели чередования."""
    t = sp._t(lang)
    head = "".join(f"<th>{sp._e(t('seo.split.week_n', n=w + 1))}</th>" for w in range(len(schedule))) if len(schedule) > 1 else (
        f"<th>{sp._e(t('seo.split.col.workout'))}</th>")
    rows = []
    for d in range(7):
        cells = []
        for week in schedule:
            cell = week[d]
            cells.append(f"<td>{sp._e(day_name(source, cell, lang))}</td>" if cell is not None
                         else f"<td class=muted>{sp._e(t('seo.split.rest'))}</td>")
        rows.append(f"<tr><th scope=row>{sp._e(t(f'seo.wd.{d + 1}'))}</th>{''.join(cells)}</tr>")
    return (f"<div class='table-card week'><table><thead><tr><th>{sp._e(t('seo.split.col.day'))}</th>{head}</tr></thead>"
            f"<tbody>{''.join(rows)}</tbody></table></div>")


def _fmt_sets(value: float) -> str:
    return sp._num(round(value, 1)) if value else "0"


def volume_table(columns: list[tuple[str, dict[str, float]]], lang: str) -> str:
    """Подходы на мышцу за неделю: строки — мышцы, столбцы — варианты."""
    import seo_library

    t = sp._t(lang)
    head = "".join(f"<th class=num>{sp._e(label)}</th>" for label, _v in columns)
    rows = "".join(
        f'<tr><th scope=row><a href="{sp.url_for(Page("muscle", url), lang)}">{sp._e(seo_library.muscle_name(url, lang))}</a></th>'
        + "".join(f"<td class=num>{_fmt_sets(vol[url])}</td>" for _l, vol in columns) + "</tr>"
        for url in VOLUME_MUSCLES)
    return (f"<div class='table-card scroll-x sticky-first'><table><thead><tr><th>{sp._e(t('seo.split.col.muscle'))}</th>{head}</tr></thead>"
            f"<tbody>{rows}</tbody></table></div>")


def _volume_note(lang: str) -> str:
    import seo_library

    return f"<p class=muted>{sp._t(lang)('seo.split.volume_note_html', **seo_library.link_params(lang))}</p>"


def _variant(lang: str, source: Source, schedule: Schedule, n: int, show_days: bool) -> str:
    """Один вариант недели: что это, неделя, объём, дни (у шаблона) или ссылка на программу."""
    import seo_library

    t = sp._t(lang)
    kind, key = source
    name = source_name(source, lang)
    if kind == "program":
        about = sp._e(seed_data.localized_program_description(key, lang))
        badge = f"<em class=badge>{sp._e(t('seo.split.badge.program'))}</em>"
        link = (f'<p><a href="{sp.url_for(Page("program", key), lang)}">'
                f"{sp._e(t('seo.split.open_program', name=name))} →</a></p>")
    else:
        about = t(f"seo.tpl.{key}.about_html", **seo_library.link_params(lang))
        badge = f"<em class='badge tpl'>{sp._e(t('seo.split.badge.template'))}</em>"
        link = ""
    days_html = day_cards(source, lang) if show_days or kind == "template" else ""
    volume = weekly_volume(source, schedule)
    return (
        f"<section class='variant reveal'><h2><span class=alt-n>{n}</span>{sp._e(name)} {badge}</h2>"
        f"<p>{about}</p>"
        f"<div class=variant-grid><div><h3>{sp._e(t('seo.split.week.h'))}</h3>{schedule_table(source, schedule, lang)}</div>"
        f"<div><h3>{sp._e(t('seo.split.volume.h'))}</h3>{volume_table([(t('seo.split.col.sets_week'), volume)], lang)}</div></div>"
        f"{days_html}{link}</section>"
    )


def _cta_text(lang: str, sources: list[Source]) -> str:
    """Призыв под сплитом: с шаблоном — «собери сам или попроси тренера», только
    программы бота — «возьми готовую»."""
    key = "seo.split.cta_html" if any(kind == "template" for kind, _k in sources) else "seo.split.cta_prog_html"
    return sp._t(lang)(key, **sp._article_params(lang))


def _split_ld(lang: str, page: Page, request, title: str, description: str, sources: list[Source]) -> dict:
    return sp._webpage_ld(lang, page, request, title, description, extra={
        "mainEntity": {"@type": "ItemList", "itemListElement": [
            {"@type": "ListItem", "position": i + 1, "item": {"@type": "ExercisePlan", "name": source_name(s, lang),
                                                              "activityFrequency": sp._t(lang)("seo.split.freq", n=SPLIT_DAYS.get(page.arg, 0))}}
            for i, s in enumerate(sources)]},
    })


def _render_split(lang: str, page: Page, request) -> str:
    import seo_library

    t = sp._t(lang)
    slug = page.arg
    params = seo_library.link_params(lang)
    if slug == "full-body-vs-split":
        return _render_compare(lang, page, request)
    variants = SPLITS[slug]
    body_variants = "".join(_variant(lang, src, sch, i + 1, show_days=False) for i, (src, sch) in enumerate(variants))
    others = [s for s in SPLIT_PAGES if s != slug]
    title, description = t(f"seo.split.{slug}.title"), t(f"seo.split.{slug}.description")
    body = (
        f"<span class=eyebrow>{sp._e(t('seo.eyebrow.split'))}</span>"
        f"<h1>{sp._e(page_h1(page, lang))}</h1><p class=lead>{sp._e(t(f'seo.split.{slug}.lead'))}</p>"
        + seo_library._updated_line(lang, page)
        + f"<section class='prose reveal sub'><h2>{sp._e(t('seo.split.who.h'))}</h2>{t(f'seo.split.{slug}.who_html', **params)}</section>"
        + body_variants
        + _volume_note(lang)
        + f"<section class='prose reveal sub'><h2>{sp._e(t('seo.split.choose.h'))}</h2>{t(f'seo.split.{slug}.choose_html', **params)}</section>"
        + f"<section class='prose reveal sub'><h2>{sp._e(t('seo.split.progress.h'))}</h2>{t('seo.split.progress_html', **params)}</section>"
        + sp._cta(lang, page.block, _cta_text(lang, [s for s, _w in variants]), request)
        + f"<h2 class=group-title>{sp._e(t('seo.split.more.h'))}</h2>"
        + sp._cards([(sp.url_for(Page("split", s), lang), t(f"seo.split.{s}.h1"), t(f"seo.split.{s}.card")) for s in others])
    )
    ld = _split_ld(lang, page, request, title, description, [s for s, _w in variants])
    return sp._layout(lang, page, request, title=title, description=description, body=body, wide=True, ld=[ld])


def _render_compare(lang: str, page: Page, request) -> str:
    """«Всё тело или сплит»: четыре недели рядом — дни, частота, подходы."""
    import seo_library

    t = sp._t(lang)
    params = seo_library.link_params(lang)
    cols = [(source_name(src, lang), weekly_volume(src, sch)) for src, sch in COMPARE]
    rows = []
    for row in ("days", "session", "freq_chest", "freq_quads"):
        cells = []
        for src, sch in COMPARE:
            if row == "days":
                val = str(sessions_per_week(sch))
            elif row == "session":
                val = sp._num(round(sets_per_session(src)))
            else:
                val = sp._num(round(weekly_frequency(src, sch, row.split("_")[1]), 1))
            cells.append(f"<td class=num>{val}</td>")
        rows.append(f"<tr><th scope=row>{sp._e(t(f'seo.split.cmp.row.{row}'))}</th>{''.join(cells)}</tr>")
    head = "".join(f'<th class=num><a href="{sp.url_for(Page("program", key), lang)}">{sp._e(source_name((k, key), lang))}</a></th>'
                   for (k, key), _s in COMPARE)
    facts = (f"<div class='table-card scroll-x sticky-first'><table><thead><tr><th></th>{head}</tr></thead>"
             f"<tbody>{''.join(rows)}</tbody></table></div>")
    title, description = t("seo.split.full-body-vs-split.title"), t("seo.split.full-body-vs-split.description")
    slug = "full-body-vs-split"
    body = (
        f"<span class=eyebrow>{sp._e(t('seo.eyebrow.compare'))}</span>"
        f"<h1>{sp._e(page_h1(page, lang))}</h1><p class=lead>{sp._e(t(f'seo.split.{slug}.lead'))}</p>"
        + seo_library._updated_line(lang, page)
        + f"<section class='reveal sub'><h2>{sp._e(t('seo.split.cmp.facts.h'))}</h2>{facts}</section>"
        + f"<section class='reveal sub'><h2>{sp._e(t('seo.split.volume.h'))}</h2>{volume_table(cols, lang)}{_volume_note(lang)}</section>"
        + f"<section class='prose reveal sub'><h2>{sp._e(t('seo.split.who.h'))}</h2>{t(f'seo.split.{slug}.who_html', **params)}</section>"
        + f"<section class='prose reveal sub'><h2>{sp._e(t('seo.split.choose.h'))}</h2>{t(f'seo.split.{slug}.choose_html', **params)}</section>"
        + sp._cta(lang, page.block, _cta_text(lang, [s for s, _w in COMPARE]), request)
        + f"<section class='reveal sub'><h2>{sp._e(t('seo.lib.related'))}</h2><ul class=links>"
        + "".join(f'<li><a href="{sp.url_for(Page("split", s), lang)}">{sp._e(t(f"seo.split.{s}.h1"))}</a></li>'
                  for s in SPLIT_PAGES if s != slug)
        + f'<li><a href="{params["guide_training_frequency"]}">{sp._e(t("seo.guide.training-frequency.h1"))}</a></li></ul></section>'
    )
    base = sp.base_url(request)
    article = {
        "@context": "https://schema.org", "@type": "Article", "headline": title[:110], "description": description,
        "inLanguage": sp.hreflang(lang), "mainEntityOfPage": base + sp.url_for(page, lang),
        "author": {"@id": base + sp.ORG_ID, "@type": "Organization", "name": t("seo.brand")},
        "publisher": {"@id": base + sp.ORG_ID, "@type": "Organization", "name": t("seo.brand")},
        "about": [{"@type": "ExercisePlan", "name": source_name(s, lang)} for s, _w in COMPARE],
    }
    return sp._layout(lang, page, request, title=title, description=description, body=body, wide=True, ld=[article])


def _render_splits(lang: str, page: Page, request) -> str:
    import seo_library

    t = sp._t(lang)
    cards = [(sp.url_for(Page("split", s), lang), t(f"seo.split.{s}.h1"), t(f"seo.split.{s}.card")) for s in SPLIT_PAGES]
    workouts = [(sp.url_for(Page("workout", w), lang), t(f"seo.wkt.{w}.h1"), t(f"seo.wkt.{w}.card")) for w in WORKOUTS]
    title, description = t("seo.splits.title"), t("seo.splits.description")
    body = (
        f"<span class=eyebrow>{sp._e(t('seo.eyebrow.library'))}</span>"
        f"<h1>{sp._e(t('seo.splits.h1'))}</h1><p class=lead>{sp._e(t('seo.splits.lead'))}</p>"
        + sp._cards(cards)
        + f"<h2 class=group-title>{sp._e(t('seo.splits.workouts.h'))}</h2>" + sp._cards(workouts)
        + f"<section class='prose reveal'><h2>{sp._e(t('seo.splits.how.h'))}</h2>{t('seo.splits.how_html', **seo_library.link_params(lang))}</section>"
        + f"<h2 class=group-title>{sp._e(t('seo.splits.programs.h'))}</h2>"
        + sp.program_cards([p["key"] for p in seed_data.WORKOUT_PROGRAMS], lang)
        + sp._cta(lang, page.block, t("seo.split.cta_html", **sp._article_params(lang)), request)
    )
    ld = sp._webpage_ld(lang, page, request, title, description, "CollectionPage", {
        "hasPart": [{"@type": "WebPage", "name": name, "url": sp.base_url(request) + href} for href, name, _m in cards],
    })
    return sp._layout(lang, page, request, title=title, description=description, body=body, wide=True, ld=[ld])


def program_week_html(key: str, lang: str) -> str:
    """Блок для страницы программы: неделя по дням и подходы на мышцу за неделю
    — по каждой странице сплитов, где программа стоит."""
    t = sp._t(lang)
    variants = program_schedules(key)
    if not variants:
        return ""
    source: Source = ("program", key)
    weeks = "".join(f"<div><h3>{sp._e(t(f'seo.split.{page}.h1'))}</h3>{schedule_table(source, schedule, lang)}</div>"
                    for page, schedule in variants)
    cols = [(t("seo.split.col.days_n", n=sessions_per_week(schedule)), weekly_volume(source, schedule)) for _p, schedule in variants]
    links = "".join(f'<li><a href="{sp.url_for(Page("split", page), lang)}">{sp._e(t(f"seo.split.{page}.h1"))}</a></li>'
                    for page, _s in variants)
    return (f"<section class=reveal><h2>{sp._e(t('seo.split.week.h'))}</h2><div class=variant-grid>{weeks}</div></section>"
            f"<section class=reveal><h2>{sp._e(t('seo.split.volume.h'))}</h2>{volume_table(cols, lang)}{_volume_note(lang)}"
            f"<ul class=links>{links}</ul></section>")


# ---------- день двух групп ----------


def _render_workout(lang: str, page: Page, request) -> str:
    import seo_library

    t = sp._t(lang)
    slug = page.arg
    params = seo_library.link_params(lang)
    source, index = WORKOUTS[slug]
    day = days_of(source)[index]
    vol = session_volume(day)
    vol_rows = "".join(
        f'<tr><th scope=row><a href="{sp.url_for(Page("muscle", u), lang)}">{sp._e(seo_library.muscle_name(u, lang))}</a></th>'
        f"<td class=num>{_fmt_sets(v)}</td></tr>" for u, v in vol.items() if v > 0)
    kind, key = source
    if kind == "program":
        origin = t("seo.wkt.from_program_html", name=sp._e(source_name(source, lang)),
                   href=sp.url_for(Page("program", key), lang), day=sp._e(day_name(source, index, lang)))
    else:
        origin = t("seo.wkt.template_html")
    grid = "".join(_card(identity, lang) for identity, _s in day)
    tips = "".join(f"<li>{sp._e(x)}</li>" for x in t(f"seo.wkt.{slug}.tips").split("\n") if x.strip())
    others = [w for w in WORKOUTS if w != slug]
    title, description = t(f"seo.wkt.{slug}.title"), t(f"seo.wkt.{slug}.description")
    body = (
        f"<span class=eyebrow>{sp._e(t('seo.eyebrow.workout'))}</span>"
        f"<h1>{sp._e(page_h1(page, lang))}</h1><p class=lead>{sp._e(t(f'seo.wkt.{slug}.lead'))}</p>"
        + seo_library._updated_line(lang, page)
        + f"<section class=reveal><h2>{sp._e(t('seo.wkt.session.h'))}</h2>{day_cards(source, lang, only=index)}"
        f"<p class=muted>{origin}</p></section>"
        + f"<section class=reveal><h2>{sp._e(t('seo.wkt.exercises.h'))}</h2><div class='grid ex-grid'>{grid}</div></section>"
        + f"<section class='prose reveal sub'><h2>{sp._e(t('seo.wkt.why.h'))}</h2>{t(f'seo.wkt.{slug}.why_html', **params)}</section>"
        + f"<section class='reveal sub'><h2>{sp._e(t('seo.wkt.volume.h'))}</h2><div class=table-card><table><thead><tr>"
        f"<th>{sp._e(t('seo.split.col.muscle'))}</th><th class=num>{sp._e(t('seo.wkt.col.sets'))}</th></tr></thead>"
        f"<tbody>{vol_rows}</tbody></table></div>{_volume_note(lang)}</section>"
        + f"<section class='mistakes reveal'><h2>{sp._e(t('seo.wkt.tips.h'))}</h2><ul>{tips}</ul></section>"
        + f"<section class='prose reveal sub'><h2>{sp._e(t('seo.wkt.week.h'))}</h2>{t(f'seo.wkt.{slug}.week_html', **params)}</section>"
        + sp._cta(lang, page.block, _cta_text(lang, [source]), request)
        + f"<h2 class=group-title>{sp._e(t('seo.wkt.more.h'))}</h2>"
        + sp._cards([(sp.url_for(Page("workout", w), lang), t(f"seo.wkt.{w}.h1"), t(f"seo.wkt.{w}.card")) for w in others]
                    + [(sp.url_for(Page("splits"), lang), t("seo.splits.h1"), t("seo.splits.card"))])
    )
    base = sp.base_url(request)
    plan = sp._webpage_ld(lang, page, request, title, description, extra={
        "mainEntity": {"@type": "ExercisePlan", "name": sp._strip_emoji(page_h1(page, lang)),
                       "exerciseType": [seed_data.localized_exercise_name(i, lang) for i, _s in day],
                       "url": base + sp.url_for(page, lang)}})
    return sp._layout(lang, page, request, title=title, description=description, body=body, wide=True, ld=[plan])


# ---------- мышца × снаряд ----------


def _card(identity: str, lang: str) -> str:
    """Карточка упражнения каталога: кадр, имя, снаряд; ссылка — на страницу
    техники, если она есть, иначе карточка без ссылки (своей страницы нет)."""
    import exercise_media

    slug = exercise_media.EXERCISE_IMAGE_SLUGS[identity]
    name = seed_data.localized_exercise_name(identity, lang)
    pic = sp.exercise_picture(slug, 2, name, sizes="(max-width: 600px) 50vw, 280px", widths=seo_catalog.card_widths(identity), width=360, height=270)
    equip = seo_catalog.equipment_of(identity)
    meta = f"<span>{sp._e(sp._t(lang)(f'seo.me.eq.{equip}'))}</span>" if equip in seo_catalog.EQUIPMENT else ""
    url = seo_catalog.page_url(identity)
    if url:
        return f'<a class="card thumb-card reveal" href="{sp.url_for(Page("exercise", url), lang)}">{pic}<b>{sp._e(name)}</b>{meta}</a>'
    return f'<div class="card thumb-card static reveal">{pic}<b>{sp._e(name)}</b>{meta}</div>'


def _render_muscle_equip(lang: str, page: Page, request) -> str:
    import seo_library

    t = sp._t(lang)
    muscle, equip = page.arg, page.arg2
    params = seo_library.link_params(lang)
    names = equip_exercises(muscle, equip)
    word = muscle_word(muscle, lang)
    shown = [seed_data.localized_exercise_name(n, lang) for n in names]
    session = sample_session(names)
    rows = "".join(f"<li><span>{_ex_link(i, lang)}</span><span>{sp._e(seed_data.localized_target(s, lang) or '')}</span></li>"
                   for i, s in session)
    others = [e for e in seo_catalog.EQUIPMENT if e != equip and (muscle, e) in equip_pages()]
    links = [(sp.url_for(Page("muscle_equip", muscle, e), lang), t(f"seo.me.{e}.h1", muscle=word)) for e in others]
    links.append((sp.url_for(Page("muscle", muscle), lang), t("seo.muscle.h1", name=seo_library.muscle_name(muscle, lang))))
    swap_links = [(sp.url_for(Page("exercise_alt", seo_catalog.page_url(n)), lang),
                   t("seo.swap.h1", name=seed_data.localized_exercise_name(n, lang)))
                  for n in names if seo_catalog.page_url(n) and Page("exercise_alt", seo_catalog.page_url(n)) in _swap_pages()][:4]
    title = t(f"seo.me.{equip}.title", muscle=word, n=len(names))
    description = t(f"seo.me.{equip}.description", muscle=word, n=len(names), first=", ".join(shown[:3]))
    body = (
        f"<span class=eyebrow>{sp._e(seo_library.muscle_name(muscle, lang))} · {sp._e(t(f'seo.me.eq.{equip}'))}</span>"
        f"<h1>{sp._e(page_h1(page, lang))}</h1>"
        f"<p class=lead>{sp._e(t('seo.me.lead', n=len(names), muscle=word))}</p>"
        + f"<section class=reveal><h2>{sp._e(t('seo.me.list.h', n=len(names)))}</h2><div class='grid ex-grid'>"
        + "".join(_card(n, lang) for n in names) + "</div></section>"
        + f"<section class=reveal><h2>{sp._e(t('seo.me.session.h'))}</h2><div class=days><div class='day reveal'>"
        f"<h3><small>{sp._e(t('seo.split.badge.template'))}</small>{sp._e(t('seo.me.session.name', muscle=word))}</h3><ul>{rows}</ul></div></div>"
        f"<p class=muted>{t('seo.me.session.note_html', **params)}</p></section>"
        + f"<section class='prose reveal sub'><h2>{sp._e(t(f'seo.me.{equip}.why.h'))}</h2>{t(f'seo.me.{equip}.why_html', muscle=word, **params)}</section>"
        + sp._cta(lang, page.block, sp._e(t("seo.muscle.cta", name=seo_library.muscle_name(muscle, lang))), request)
        + f"<section class='reveal sub'><h2>{sp._e(t('seo.lib.related'))}</h2><ul class=links>"
        + "".join(f'<li><a href="{h}">{sp._e(label)}</a></li>' for h, label in links + swap_links) + "</ul></section>"
    )
    ld = sp._webpage_ld(lang, page, request, title, description, "CollectionPage", extra={
        "mainEntity": {"@type": "ItemList", "itemListElement": [
            {"@type": "ListItem", "position": i + 1, "name": n} for i, n in enumerate(shown)]}})
    return sp._layout(lang, page, request, title=title, description=description, body=body, wide=True, ld=[ld])


def _swap_pages() -> set:
    import seo_swaps

    return {Page("exercise_alt", u) for u, _s in sp.EXERCISES if seo_swaps.has_page(u)}


def equip_links(muscle: str, lang: str) -> str:
    """Ссылки «той же мышце — гантелями / штангой…» для страницы мышцы."""
    t = sp._t(lang)
    items = [(sp.url_for(Page("muscle_equip", muscle, e), lang), t(f"seo.me.{e}.h1", muscle=muscle_word(muscle, lang)))
             for m, e in equip_pages() if m == muscle]
    if not items:
        return ""
    return (f"<section class='reveal sub'><h2>{sp._e(t('seo.me.by_equipment.h'))}</h2><ul class=links>"
            + "".join(f'<li><a href="{h}">{sp._e(label)}</a></li>' for h, label in items) + "</ul></section>")


RENDERERS = {"splits": _render_splits, "split": _render_split, "workout": _render_workout,
             "muscle_equip": _render_muscle_equip}
