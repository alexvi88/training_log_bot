"""Нормативы для одного веса тела: «сколько должен жать мужчина весом 80 кг»."""

import re

import seo_pages
import seo_strength_bw as sbw
import seo_tools
from seo_pages import Page


def _page(lift="bench-press", arg2="men/80kg"):
    return Page("strength_bw", lift, arg2)


def test_pages_cover_lifts_sexes_and_bodyweights_with_unique_paths():
    pages = sbw.pages()
    assert len(pages) == 3 * (7 + 5)
    paths = [p.path for p in pages]
    assert len(set(paths)) == len(paths)
    assert "/strength/bench-press/men/80kg" in paths
    assert "/strength/deadlift/women/50kg" in paths
    assert all(p in seo_pages.pages() for p in pages)


def test_short_answer_is_the_same_scale_as_the_table():
    """Цифра в первой строке — та же, что в общей таблице и калькуляторе."""
    for page in sbw.pages():
        key, sex, bw = sbw.parts(page)
        expected = seo_tools.round_to(seo_tools.level_weight(dict(seo_tools.LEVELS)["intermediate"], key, bw, sex), 2.5)
        html = seo_pages.render(page, "ru")
        lead = re.search(r"<p class=lead>(.*?)</p>", html).group(1)
        assert f"{expected:g} кг" in lead, (page, lead)


def test_english_names_pounds_and_russian_does_not():
    en = seo_pages.render(_page(), "en")
    ru = seo_pages.render(_page(), "ru")
    assert "How much should a 176 lb (80 kg) man bench?" in en
    assert "Сколько должен жать мужчина весом 80 кг" in ru
    assert " lb" not in re.search(r"<main.*</main>", ru, re.S).group(0)
    assert not re.search(r"[А-Яа-яЁё]", en)


def test_competitor_median_comes_from_the_percentile_file():
    cls, n, median, top = sbw.competitors("bench", "m", 80)
    assert cls == "83" and n > 0 and 0 < median < top
    html = seo_pages.render(_page(), "ru")
    assert f"до {cls} кг" in html and f"{median:g} кг" in html
    heavy = seo_pages.render(_page(arg2="men/120kg"), "en")
    assert "120+ kg class" in heavy or "105 kg class" in heavy or "120 kg class" in heavy


def test_lift_table_links_to_every_bodyweight_page_and_back():
    lift = seo_pages.render(Page("strength_lift", "squat", "women"), "en")
    for bw in sbw.BODYWEIGHTS["f"]:
        assert f'href="/en/strength/squat/women/{bw}kg"' in lift
    page = seo_pages.render(_page("squat", "women/60kg"), "en")
    assert 'href="/en/strength/squat/women"' in page
    assert 'href="/en/strength/bench-press/women/60kg"' in page
    assert 'href="/en/strength/squat/women/70kg"' in page


def test_calculator_is_prefilled_with_this_bodyweight():
    html = seo_pages.render(_page("deadlift", "men/100kg"), "ru")
    assert re.search(r'id=bw [^>]*value="100"', html)
