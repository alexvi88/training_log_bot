"""«Чем заменить упражнение»: `/<язык>/exercises/<адрес>/alternatives` для
каждой страницы техники, у которой замен не меньше `seo_catalog.MIN_SWAPS`.

Замены — те же, что атлет видит в боте по кнопке «Альтернативы»
(`exercise_alternatives.FAMILIES`), добранные упражнениями той же мышцы и
того же движения (`seo_catalog.swaps`). Что меняется у каждой замены —
только из данных каталога: снаряд, мишень и группа (правило объёма бота),
базовое или изолирующее (паттерн движения бота), одна сторона за раз, чем
растёт нагрузка (вид прогрессии бота) и порог входа там, где он сверен
руками (`seo_library.EX_FACTS`). Медицинских советов страница не даёт: боль
— повод к врачу, а не к списку замен.
"""

from __future__ import annotations

import seed_data
import seo_catalog
import seo_pages as sp

Page = sp.Page


def identity_of(url: str) -> str:
    return sp._identity_for_media_slug(sp._EXERCISE_BY_URL[url])


def has_page(url: str) -> bool:
    return len(seo_catalog.swaps(identity_of(url))) >= seo_catalog.MIN_SWAPS


def pages() -> list[Page]:
    return [Page("exercise_alt", url) for url, _s in sp.EXERCISES if has_page(url)]


def page_h1(page: Page, lang: str) -> str:
    return sp._t(lang)("seo.swap.h1", name=seed_data.localized_exercise_name(identity_of(page.arg), lang))


def _muscle_label(identity: str, lang: str) -> str:
    target = seo_catalog.target_of(identity)
    if target:
        return seed_data.localized_muscle_target_name(target, lang)
    return seed_data.localized_muscle_group_name(seo_catalog.group_of(identity), lang)


def _lc(text: str, lang: str) -> str:
    import seo_library

    return seo_library._second(text, lang)


def differences(source: str, alt: str, lang: str) -> list[str]:
    """Что меняется при замене `source` на `alt` — строками, только из данных."""
    t = sp._t(lang)
    out = []
    eq_s, eq_a = seo_catalog.equipment_of(source), seo_catalog.equipment_of(alt)
    if eq_a in seo_catalog.EQUIPMENT:
        if eq_a == eq_s:
            out.append(t("seo.swap.d.equip_same", equip=_lc(t(f"seo.me.eq.{eq_a}"), lang)))
        elif eq_s in seo_catalog.EQUIPMENT:
            out.append(t("seo.swap.d.equip_other", equip=_lc(t(f"seo.me.eq.{eq_a}"), lang), was=_lc(t(f"seo.me.eq.{eq_s}"), lang)))
        else:
            out.append(t("seo.swap.d.equip_same", equip=_lc(t(f"seo.me.eq.{eq_a}"), lang)))
    m_s, m_a = _muscle_label(source, lang), _muscle_label(alt, lang)
    g_s, g_a = seo_catalog.group_of(source), seo_catalog.group_of(alt)
    if m_s == m_a:
        out.append(t("seo.swap.d.muscle_same", muscle=_lc(m_a, lang)))
    elif g_s == g_a:
        out.append(t("seo.swap.d.muscle_shift", muscle=_lc(m_a, lang)))
    else:
        out.append(t("seo.swap.d.muscle_group", muscle=_lc(m_a, lang)))
    p_s, p_a = seo_catalog.pattern_of(source), seo_catalog.pattern_of(alt)
    if bool(p_s) != bool(p_a):
        out.append(t("seo.swap.d.compound" if p_a else "seo.swap.d.isolation"))
    l_s, l_a = seo_catalog.level_of(source), seo_catalog.level_of(alt)
    if l_s and l_a:
        key = "easier" if l_a < l_s else "harder" if l_a > l_s else "level_same"
        out.append(t(f"seo.swap.d.{key}", a=l_a, b=l_s))
    if seo_catalog.unilateral(alt) and not seo_catalog.unilateral(source):
        out.append(t("seo.swap.d.unilateral"))
    kind_s, kind_a = seed_data.progression_kind(source), seed_data.progression_kind(alt)
    if kind_a != kind_s:
        out.append(t(f"seo.swap.d.progress.{kind_a}"))
    return out


def _alt_item(source: str, alt: str, n: int, lang: str) -> str:
    import exercise_media
    import seo_library

    t = sp._t(lang)
    name = seed_data.localized_exercise_name(alt, lang)
    url = seo_catalog.page_url(alt)
    src_url = seo_catalog.page_url(source)
    slug = exercise_media.EXERCISE_IMAGE_SLUGS[alt]
    pic = sp.exercise_picture(slug, 2, name, sizes="(max-width: 600px) 40vw, 200px", widths=seo_catalog.card_widths(alt), width=360, height=270)
    title = f'<a href="{sp.url_for(Page("exercise", url), lang)}">{sp._e(name)}</a>' if url else sp._e(name)
    bullets = "".join(f"<li>{sp._e(x)}</li>" for x in differences(source, alt, lang))
    links = []
    if url:
        links.append((sp.url_for(Page("exercise", url), lang), t("seo.swap.link.technique")))
        pair = next(((a, b) for a, b in seo_library.PAIRS if {a, b} == {url, src_url}), None)
        if pair:
            links.append((sp.url_for(Page("pair", seo_library.pair_slug(*pair)), lang), t("seo.swap.link.compare")))
        if has_page(url):
            links.append((sp.url_for(Page("exercise_alt", url), lang), t("seo.swap.link.its_swaps")))
    link_html = "".join(f'<a href="{h}">{sp._e(label)} →</a>' for h, label in links)
    return (f'<li class="swap reveal" id="alt-{n}"><div class=swap-pic>{pic}</div><div class=swap-body>'
            f"<div class=alt-head><span class=alt-n>{n}</span><h2>{title}</h2></div>"
            f"<ul class=swap-diff>{bullets}</ul>"
            + (f"<p class=swap-links>{link_html}</p>" if link_html else "") + "</div></li>")


def _reasons(source: str, alts: list[str], lang: str) -> str:
    """Почему меняют: занят снаряд, нет снаряда, движение не заходит — с
    заменами из списка под каждую причину."""
    t = sp._t(lang)
    eq_s = seo_catalog.equipment_of(source)
    by_equip: dict[str, list[str]] = {}
    for alt in alts:
        eq = seo_catalog.equipment_of(alt)
        if eq in seo_catalog.EQUIPMENT and eq != eq_s:
            by_equip.setdefault(eq, []).append(seed_data.localized_exercise_name(alt, lang))
    items = []
    if by_equip:
        groups = "; ".join(t("seo.swap.why.equip_list", equip=t(f"seo.me.eq.{eq}"), names=", ".join(names))
                           for eq, names in by_equip.items())
        key = "busy" if eq_s in ("machine", "barbell") else "other"
        items.append(f"<li><b>{sp._e(t(f'seo.swap.why.{key}.h'))}</b> {sp._e(t(f'seo.swap.why.{key}', list=groups))}</li>")
    same = [seed_data.localized_exercise_name(a, lang) for a in alts if seo_catalog.equipment_of(a) == eq_s]
    if same:
        items.append(f"<li><b>{sp._e(t('seo.swap.why.angle.h'))}</b> {sp._e(t('seo.swap.why.angle', list=', '.join(same)))}</li>")
    items.append(f"<li><b>{sp._e(t('seo.swap.why.pain.h'))}</b> {sp._e(t('seo.swap.why.pain'))}</li>")
    return f"<ul class=reasons>{''.join(items)}</ul>"


# Порядок групп «по снаряду»: в поиске чаще всего спрашивают замену «на
# тренажёре» (machine alternative) и «дома» (at home) — они первыми.
_EQUIP_ORDER = ("machine", "dumbbell", "barbell", "bodyweight")


def _by_equipment(alts: list[str]) -> list[tuple[str, list[tuple[int, str]]]]:
    """(снаряд, [(номер в списке, замена)]) — только снаряды из каталога."""
    groups: dict[str, list[tuple[int, str]]] = {}
    for i, alt in enumerate(alts):
        eq = seo_catalog.equipment_of(alt)
        if eq in _EQUIP_ORDER:
            groups.setdefault(eq, []).append((i + 1, alt))
    return [(eq, groups[eq]) for eq in _EQUIP_ORDER if eq in groups]


def _equipment_block(name: str, alts: list[str], lang: str) -> str:
    """Замены по снаряду, ссылками на пункты списка ниже. Запросы вида
    «machine chest press alternative», «… alternative machine», «чем
    заменить тягу верхнего блока дома» — человек ищет замену под снаряд,
    который у него есть, и находит её здесь одной строкой."""
    t = sp._t(lang)
    rows = "".join(
        f"<li><b>{sp._e(t(f'seo.swap.eq.{eq}'))}</b> "
        + ", ".join(f'<a href="#alt-{n}">{sp._e(seed_data.localized_exercise_name(a, lang))}</a>' for n, a in items)
        + "</li>"
        for eq, items in _by_equipment(alts)
    )
    if not rows:
        return ""
    return (f"<section class='prose reveal sub'><h2>{sp._e(t('seo.swap.eq.h', name=name))}</h2>"
            f"<ul class=reasons>{rows}</ul></section>")


def _equipment_phrase(alts: list[str], lang: str) -> str:
    """«machine, dumbbell & bodyweight» — для заголовка в выдаче."""
    t = sp._t(lang)
    words = [t(f"seo.swap.eq.word.{eq}") for eq, _items in _by_equipment(alts)]
    if not words:
        return ""
    if len(words) == 1:
        return words[0]
    return t("seo.swap.eq.join", head=", ".join(words[:-1]), last=words[-1])


def _render(lang: str, page: Page, request) -> str:
    import seo_library
    import seo_plans

    t = sp._t(lang)
    url = page.arg
    source = identity_of(url)
    name = seed_data.localized_exercise_name(source, lang)
    alts = seo_catalog.swaps(source)
    names = [seed_data.localized_exercise_name(a, lang) for a in alts]
    params = sp._article_params(lang)
    params.update(exercises=sp._e(sp._strip_emoji(t("btn.exercises"))), swap=sp._e(sp._strip_emoji(t("btn.swap"))))
    group = seo_catalog.group_of(source)
    muscle_pages = [m for m, g, tg in seo_library.MUSCLES if g == group and (tg is None or tg == seo_catalog.target_of(source))]
    related = [(sp.url_for(Page("exercise", url), lang), t("seo.swap.back", name=name))]
    related += [(sp.url_for(Page("muscle", m), lang), t("seo.muscle.h1", name=seo_library.muscle_name(m, lang))) for m in muscle_pages]
    eq = seo_catalog.equipment_of(source)
    for m in muscle_pages:
        if (m, eq) in seo_plans.equip_pages():
            related.append((sp.url_for(Page("muscle_equip", m, eq), lang), t(f"seo.me.{eq}.h1", muscle=seo_plans.muscle_word(m, lang))))
    phrase = _equipment_phrase(alts, lang)
    title = (t("seo.swap.title_equip", name=name, n=len(alts), equip=phrase) if phrase
             else t("seo.swap.title", name=name, n=len(alts)))
    description = t("seo.swap.description", name=name, n=len(alts), list=", ".join(names[:3]))
    body = (
        f"<span class=eyebrow>{sp._e(sp._group_for(source, lang))}</span>"
        f"<h1>{sp._e(page_h1(page, lang))}</h1>"
        f"<p class=lead>{sp._e(t('seo.swap.lead', name=_lc(name, lang), n=len(alts)))}</p>"
        + _equipment_block(name, alts, lang)
        + f"<section class='prose reveal sub'><h2>{sp._e(t('seo.swap.why.h'))}</h2>{_reasons(source, alts, lang)}</section>"
        + f"<ol class=swaps>{''.join(_alt_item(source, a, i + 1, lang) for i, a in enumerate(alts))}</ol>"
        + f"<p class=muted>{sp._e(t('seo.swap.data_note'))}</p>"
        + f"<section class='prose reveal sub'><h2>{sp._e(t('seo.swap.diary.h'))}</h2><p>{t('seo.swap.diary_html', **params)}</p></section>"
        + sp._cta(lang, page.block, sp._e(t("seo.swap.cta")), request)
        + f"<section class='reveal sub'><h2>{sp._e(t('seo.lib.related'))}</h2><ul class=links>"
        + "".join(f'<li><a href="{h}">{sp._e(label)}</a></li>' for h, label in related) + "</ul></section>"
        + f"<p class=muted>{t('seo.exercise.credit_html')}</p>"
    )
    ld = sp._webpage_ld(lang, page, request, title, description, extra={
        "about": {"@type": "Thing", "name": name},
        "mainEntity": {"@type": "ItemList", "itemListElement": [
            {"@type": "ListItem", "position": i + 1, "name": n} for i, n in enumerate(names)]}})
    return sp._layout(lang, page, request, title=title, description=description, body=body, wide=True, ld=[ld])


def aside_link(url: str, lang: str) -> list[tuple[str, str]]:
    """Ссылка со страницы техники — если у неё есть страница замен."""
    if not has_page(url):
        return []
    name = seed_data.localized_exercise_name(identity_of(url), lang)
    return [(sp.url_for(Page("exercise_alt", url), lang), sp._t(lang)("seo.swap.h1", name=name))]


def teaser(url: str, lang: str) -> str:
    """Блок на странице техники: первые замены и ссылка на весь список."""
    if not has_page(url):
        return ""
    t = sp._t(lang)
    alts = seo_catalog.swaps(identity_of(url))[:3]
    names = ", ".join(seed_data.localized_exercise_name(a, lang) for a in alts)
    return (f"<section class=reveal><h2>{sp._e(t('seo.swap.teaser.h'))}</h2>"
            f"<p>{sp._e(t('seo.swap.teaser', list=names))} "
            f'<a href="{sp.url_for(Page("exercise_alt", url), lang)}">{sp._e(t("seo.swap.teaser.link", n=len(seo_catalog.swaps(identity_of(url)))))} →</a></p></section>')


RENDERERS = {"exercise_alt": _render}
