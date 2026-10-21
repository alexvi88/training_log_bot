"""Сайт, четвёртая часть: замены упражнений, сплиты, тренировки «две группы»,
мышца × снаряд, новые гайды, термины, калькуляторы 5/3/1 и объёма
(seo_catalog, seo_swaps, seo_plans, seo_calcs, seo_library).

Что держит тест: страницы есть на обоих языках и не тонкие, заголовки
уникальны, ссылки внутри сайта не ведут в 404, числа объёма — правило
бота, а браузерные скрипты считают так же, как Python.
"""

import json
import re
import shutil
import subprocess
from collections import Counter, defaultdict
from urllib.parse import urlsplit

import httpx
import pytest

import config
import exercise_alternatives
import exercise_media
import i18n
import mcp_server
import seed_data
import seo_calcs
import seo_catalog
import seo_library
import seo_pages
import seo_plans
import seo_static
import seo_swaps
import seo_tools

HOST = "https://training-log.example.com"
_CYRILLIC = re.compile(r"[А-Яа-яЁё]")
NEW_KINDS = ("exercise_alt", "split", "splits", "workout", "muscle_equip", "apps")


@pytest.fixture(autouse=True)
def public_url(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "MCP_PUBLIC_URL", HOST)
    monkeypatch.setattr(config, "PUBLIC_BASE_URL", "")
    monkeypatch.setattr(config, "SITE_BASE_URL", "")
    monkeypatch.setattr(config, "APP_STORE_URL", "")
    monkeypatch.setattr(config, "GOOGLE_SITE_VERIFICATION", "")
    monkeypatch.setattr(config, "GOOGLE_SITE_VERIFICATION_FILE", "")
    monkeypatch.setattr(config, "INDEXNOW_KEY", "")
    monkeypatch.setattr(seo_static, "OG_CACHE_DIR", str(tmp_path / "og"))
    seo_static._og_memory.clear()


async def _get_many(paths):
    transport = httpx.ASGITransport(app=mcp_server.build_app())
    async with httpx.AsyncClient(transport=transport, base_url=HOST) as client:
        return {p: await client.get(p) for p in paths}


def _pages(*kinds):
    return [p for p in seo_pages.pages() if p.kind in kinds]


_RENDERED: dict = {}


def _html(page, lang):
    key = (page, lang)
    if key not in _RENDERED:
        _RENDERED[key] = seo_pages.render(page, lang)
    return _RENDERED[key]


def _main(html):
    return html.split("<main", 1)[1].split("<footer", 1)[0]


# ---------- состав ----------


def test_wave_four_page_counts():
    kinds = Counter(p.kind for p in seo_pages.pages())
    assert kinds["exercise"] == len(seo_pages.EXERCISES) >= 130
    assert 100 <= kinds["exercise_alt"] <= kinds["exercise"]
    assert kinds["split"] == len(seo_plans.SPLIT_PAGES) == 7 and kinds["splits"] == 1
    assert kinds["workout"] == 3 and kinds["apps"] == 1
    assert 20 <= kinds["muscle_equip"] <= 30
    assert kinds["guide"] == 24 and kinds["term"] == 21 and kinds["pair"] == 54
    assert {"531", "weekly-volume"} <= {p.arg for p in _pages("tool")}
    assert {("total", "men"), ("total", "women")} <= {(p.arg, p.arg2) for p in _pages("strength_lift")}
    assert {"fitbod", "jefit"} <= {p.arg for p in _pages("alternatives")}


async def test_new_pages_answer_200_on_both_languages():
    pages = _pages(*NEW_KINDS) + [p for p in _pages("tool") if p.arg in ("531", "weekly-volume")]
    paths = [seo_pages.url_for(p, lang) for p in pages for lang in seo_pages.LANGS]
    responses = await _get_many(paths)
    for path, resp in responses.items():
        assert resp.status_code == 200, path
        if path.startswith("/en/"):
            assert not _CYRILLIC.search(resp.text), (path, _CYRILLIC.findall(resp.text)[:5])


def test_titles_h1_and_descriptions_are_unique_site_wide():
    for lang in seo_pages.LANGS:
        seen: dict = defaultdict(list)
        for page in seo_pages.pages():
            html = _html(page, lang)
            title = re.search(r"<title>(.*?)</title>", html).group(1)
            desc = re.search(r'<meta name=description content="(.*?)">', html).group(1)
            h1 = re.search(r"<h1>(.*?)</h1>", html)
            for kind, value in (("title", title), ("desc", desc), ("h1", h1.group(1) if h1 else None)):
                if value:
                    seen[(kind, value)].append(page)
        dupes = {k: v for k, v in seen.items() if len(v) > 1}
        assert not dupes, list(dupes.items())[:3]


def test_json_ld_of_new_pages_parses_and_has_crumbs():
    for page in _pages(*NEW_KINDS):
        for lang in seo_pages.LANGS:
            blocks = [json.loads(raw) for raw in re.findall(r'<script type="application/ld\+json">(.*?)</script>',
                                                             _html(page, lang), re.S)]
            types = {b["@type"] for b in blocks}
            assert "BreadcrumbList" in types and len(types) >= 2, (page, types)


# ---------- ссылки ----------

_STATIC_PREFIXES = ("/go/", "/site/", "/v1/media/", "/og/", "/embed/")
_STATIC_PATHS = ("/privacy", "/terms", "/support", "/favicon.ico", "/apple-touch-icon.png", "/site.webmanifest")


def test_every_internal_link_points_to_a_real_page():
    """Ни одна ссылка внутри сайта не ведёт в 404: страница из pages(),
    файл шаблона или служебный адрес."""
    import seo_template

    known = {seo_pages.url_for(p, lang) for p in seo_pages.pages() for lang in seo_pages.LANGS}
    known |= {f"/{lang}/templates/{name}" for lang in seo_pages.LANGS for name in seo_template.FILES}
    broken = []
    for page in seo_pages.pages():
        for lang in seo_pages.LANGS:
            for href in re.findall(r'href="(/[^"]*)"', _html(page, lang)):
                path = urlsplit(href).path
                if path in known or path in _STATIC_PATHS or path.startswith(_STATIC_PREFIXES):
                    continue
                broken.append((seo_pages.url_for(page, lang), href))
    assert not broken, broken[:10]


def test_exercise_pages_link_their_alternatives_and_back():
    for page in _pages("exercise"):
        alt = seo_pages.Page("exercise_alt", page.arg)
        has = seo_swaps.has_page(page.arg)
        for lang in seo_pages.LANGS:
            linked = f'href="{seo_pages.url_for(alt, lang)}"' in _html(page, lang)
            assert linked == has, page
            if has:
                assert f'href="{seo_pages.url_for(page, lang)}"' in _html(alt, lang)


# ---------- не тонкие ----------


def test_alternatives_pages_are_not_thin_and_say_what_changes():
    for page in _pages("exercise_alt"):
        source = seo_swaps.identity_of(page.arg)
        swaps = seo_catalog.swaps(source)
        assert seo_catalog.MIN_SWAPS <= len(swaps) <= seo_catalog.MAX_SWAPS
        family = set(exercise_alternatives.alternatives_for(source))
        for alt in swaps:
            # Замена — из семейства бота или та же группа, мишень и движение.
            assert alt in family or (seo_catalog.group_of(alt) == seo_catalog.group_of(source)
                                     and seo_catalog.target_of(alt) == seo_catalog.target_of(source)), (source, alt)
            for lang in seo_pages.LANGS:
                assert len(seo_swaps.differences(source, alt, lang)) >= 2, (source, alt)
        for lang in seo_pages.LANGS:
            assert _html(page, lang).count('<li class="swap reveal">') == len(swaps)
    thin = [url for url, _s in seo_pages.EXERCISES if not seo_swaps.has_page(url)]
    assert all(len(seo_catalog.swaps(seo_swaps.identity_of(u))) < seo_catalog.MIN_SWAPS for u in thin)


def test_muscle_equipment_pages_have_enough_exercises_of_that_equipment():
    for page in _pages("muscle_equip"):
        names = seo_plans.equip_exercises(page.arg, page.arg2)
        assert len(names) >= seo_plans.MIN_EQUIP_EXERCISES
        assert all(seo_catalog.equipment_of(n) == page.arg2 for n in names)
        for lang in seo_pages.LANGS:
            html = _main(_html(page, lang))
            assert html.count("thumb-card") >= len(names)
    # Тонкие сочетания страниц не получают: средняя дельта отдельно, ягодицы на гантелях.
    assert ("glutes", "dumbbell") not in seo_plans.equip_pages()


def test_every_shown_exercise_has_known_equipment_and_a_card_image():
    shown = set(seo_catalog.card_identities())
    shown |= {seo_pages._identity_for_media_slug(s) for _u, s in seo_pages.EXERCISES}
    for name in shown:
        assert seo_catalog.equipment_of(name) in seo_catalog.EQUIPMENT + ("kettlebell",), name
        slug = exercise_media.EXERCISE_IMAGE_SLUGS[name]
        for fmt in ("avif", "webp"):
            assert seo_static.site_file(f"ex/{slug}_2-360.{fmt}"), (name, fmt)


def test_new_exercise_pages_have_facts_and_text():
    for url, slug in seo_pages.EXERCISES:
        assert url in seo_library.EX_FACTS, url
        for lang in seo_pages.LANGS:
            assert i18n.t_in(lang, f"seo.exf.{url}.muscles") != f"seo.exf.{url}.muscles", url
            for frame, width in ((1, 720), (2, 360), (2, 720)):
                assert seo_static.site_file(f"ex/{slug}_{frame}-{width}.webp"), (slug, frame, width)


# ---------- объём: правило бота ----------


def test_split_volume_is_the_bot_rule():
    # «Верх / Низ» дважды в неделю: груди 4 подхода жима за верх → 8; спине тяга 4 + блок 3 → 14.
    week = seo_plans.SPLITS["4-day"][0][1]
    vol = seo_plans.weekly_volume(("program", "upperlower"), week)
    assert vol["chest"] == pytest.approx(8) and vol["back"] == pytest.approx(14) and vol["quads"] == pytest.approx(14)
    # Одна неделя программы, где каждый день — по разу, — то же, что считает страница мышцы за цикл.
    for key in ("fullbody3", "ppl", "split3"):
        once = seo_plans.weekly_volume(("program", key), (tuple(range(len(seed_data.PROGRAM_BY_KEY[key]["days"]))) + (None,) * 4,))
        for muscle, _g, _t in seo_library.MUSCLES:
            cycle = dict((k, v) for k, v, _d in seo_library.muscle_programs(muscle)).get(key, 0.0)
            assert once[muscle] == pytest.approx(cycle), (key, muscle)
    # 5×5 чередует A-B-A и B-A-B: в среднем по полторы тренировки каждого дня.
    five = seo_plans.weekly_volume(("program", "strength5x5"), seo_plans.SPLITS["3-day"][1][1])
    assert five["chest"] == pytest.approx(7.5)


def test_weekly_volume_tool_counts_like_the_split_tables():
    for lang in seo_pages.LANGS:
        totals, bad = seo_calcs.plan_volume(seo_calcs.volume_example(lang), lang)
        assert not bad
        week = seo_plans.weekly_volume(("program", "fullbody3"), seo_plans.SPLITS["3-day"][0][1])
        for muscle, value in week.items():
            assert totals.get(muscle, 0.0) == pytest.approx(value), (lang, muscle)
    totals, bad = seo_calcs.plan_volume("Жим штанги лёжа 4×8–12\nстановая тяга 2\nНепонятное упражнение 3\n", "ru", 2)
    assert totals["chest"] == 8 and totals["back"] == 2 and totals["hamstrings"] == 1
    assert bad == ["Непонятное упражнение 3"]


@pytest.mark.skipif(shutil.which("node") is None, reason="нет node")
def test_531_and_volume_scripts_match_python():
    cases = [(117.0, 2.5, 0.9), (100.0, 2.5, 0.85), (315.0, 5.0, 0.9)]
    lines = ["Barbell Bench Press 4", "Barbell Deadlift 3x5", "pull-ups — 3", "Unknown thing 2", "", "Lat Pulldown"]
    catalog = seo_calcs.volume_catalog("en")
    script = seo_tools.calc_js() + f"var CAT={json.dumps(catalog)};" + seo_calcs.VOLUME_JS + f"""
console.log(JSON.stringify({{
  five: {json.dumps(cases)}.map(function (c) {{ return five31(c[0], c[1], c[2]); }}),
  vol: weekly({json.dumps(chr(10).join(lines))}, 2)
}}));
"""
    got = json.loads(subprocess.run(["node", "-e", script], capture_output=True, text=True, check=True).stdout)
    for (orm, step, share), js in zip(cases, got["five"], strict=True):
        py = seo_tools.five31(orm, step, share)
        assert js["tm"] == pytest.approx(py["tm"])
        assert [tuple(x) for x in js["warmup"]] == [(pytest.approx(w), r) for w, r in py["warmup"]]
        for js_week, py_week in zip(js["weeks"], py["weeks"], strict=True):
            assert [tuple(x) for x in js_week] == [(pytest.approx(p), pytest.approx(w), r) for p, w, r in py_week]
    totals, bad = seo_calcs.plan_volume("\n".join(lines), "en", 2)
    assert got["vol"][1] == bad == ["Unknown thing 2"]
    assert got["vol"][0] == pytest.approx(totals)


def test_531_weights_follow_the_published_scheme():
    plan = seo_tools.five31(100.0, 2.5)
    assert plan["tm"] == pytest.approx(90.0)
    assert [w for _p, w, _r in plan["weeks"][0]] == [57.5, 67.5, 75.0]
    assert [r for _p, _w, r in plan["weeks"][2]] == ["5", "3", "1+"]
    assert [w for w, _r in plan["warmup"]] == [35.0, 45.0, 52.5]


def test_strength_total_is_the_dots_scale_itself():
    for sex in ("m", "f"):
        for _name, threshold in seo_tools.LEVELS:
            total = seo_tools.level_weight(threshold, "total", 80.0, sex)
            assert seo_tools.level_weight(threshold, "bench", 80.0, sex) == pytest.approx(total * seo_tools.LIFT_SHARE[sex]["bench"])
            assert seo_tools.dots(total, 80.0, sex) == pytest.approx(threshold)


# ---------- язык ----------

# Латиница, которая законно стоит в русском тексте: бренды, аббревиатуры зала,
# фамилии авторов исследований и названия из каталога бота.
_LATIN_OK = {
    "Telegram", "RPE", "RIR", "EZ", "RM", "DOTS", "PR", "AMRAP", "PHUL", "PPL", "Push", "Pull", "Legs", "push", "pull",
    "legs", "V", "T", "Hevy", "Strong", "Fitbod", "JEFIT", "Boostcamp", "Pro", "PRO", "Trainer", "Elite", "CSV", "App",
    "Store", "iPhone", "Android", "Excel", "Google", "Sheets", "Apple", "Health", "Strava", "Settings", "Export", "Import",
    "Data", "Workouts", "Profile", "HevyGPT", "ChatGPT", "GPT", "AI", "Schoenfeld", "Ogborn", "Krieger", "Grgic", "Pelland",
    "Refalo", "Plotkin", "Morton", "Zourdos", "Gentil", "Soares", "Bottaro", "Roberts", "Nuckols", "Borde", "Hortobágyi",
    "Granacher", "Fragala", "Singer", "Šarić", "Fradkin", "Zazryn", "Smoliga", "Ribeiro", "Coleman", "Rogerson", "Latella",
    "Primer", "free-exercise-db", "repetitions", "in", "reserve", "as", "many", "reps", "possible", "personal", "record",
    "routines", "JavaScript", "push-pull-legs", "Push-pull-legs", "deload", "Power", "Hypertrophy", "Upper", "Lower", "A", "B", "e", "Wilks", "IPF", "GL", "lb", "kg", "USAPL", "OpenPowerlifting", "Epley", "Brzycki", "A-B-A", "B-A-B",
}


def test_russian_pages_of_wave_four_carry_no_stray_english():
    pages = _pages(*NEW_KINDS) + [p for p in _pages("guide") if p.arg in seo_library.GUIDES[10:]]
    pages += [p for p in _pages("term") if p.arg in seo_library.GLOSSARY[12:]]
    pages += [p for p in _pages("tool") if p.arg in ("531", "weekly-volume")]
    stray = Counter()
    for page in pages:
        main = _main(_html(page, "ru")).split(">", 1)[1]
        main = re.sub(r"<script.*?</script>|<ol class='muted sources'>.*?</ol>|<ul class=muted>.*?</ul>", "", main, flags=re.S)
        text = re.sub(r"<[^>]+>", " ", main)
        for word in re.findall(r"[A-Za-zÀ-ÖØ-öø-ž][A-Za-zÀ-ÖØ-öø-ž'-]*", text):
            if word.strip("-") not in _LATIN_OK:
                stray[(word, seo_pages.url_for(page, "ru"))] += 1
    assert not stray, stray.most_common(10)


async def test_sitemap_llms_and_hubs_list_wave_four():
    responses = await _get_many(["/sitemap.xml", "/llms.txt", "/llms-full.txt", "/ru/programs", "/en/compare",
                                 "/en/muscles/chest", "/ru"])
    for page in _pages(*NEW_KINDS):
        for lang in seo_pages.LANGS:
            assert f"<loc>{HOST}{seo_pages.url_for(page, lang)}</loc>" in responses["/sitemap.xml"].text
            assert f"({HOST}{seo_pages.url_for(page, lang)})" in responses["/llms-full.txt"].text
    assert f"{HOST}/en/splits" in responses["/llms.txt"].text
    assert 'href="/ru/splits"' in responses["/ru/programs"].text and 'href="/ru/splits/3-day"' in responses["/ru/programs"].text
    assert 'href="/en/compare/hevy-vs-strong"' in responses["/en/compare"].text
    assert 'href="/en/splits/full-body-vs-split"' in responses["/en/compare"].text
    assert 'href="/en/muscles/chest/dumbbell"' in responses["/en/muscles/chest"].text
    assert 'href="/ru/splits"' in responses["/ru"].text


def test_split_pages_show_week_volume_and_templates_are_labelled():
    for page in _pages("split"):
        for lang in seo_pages.LANGS:
            html = _html(page, lang)
            assert "table-card week" in html or page.arg == "full-body-vs-split"
            assert i18n.t_in(lang, "seo.split.volume.h") in html
    for key in seo_plans.TEMPLATES:
        if key == "arms":
            continue
        html = _html(seo_pages.Page("split", "5-day" if key == "bro5" else "phul"), "en")
        assert i18n.t_in("en", "seo.split.badge.template") in html
    # Шаблоны собраны только из каталога, а программы бота — ссылкой «открыть программу».
    for key, days in seo_plans.TEMPLATES.items():
        for day in days:
            for identity, scheme in day:
                assert seo_catalog.group_of(identity), identity
                assert seo_library._sets_of(scheme), (key, identity)
    for page in _pages("program"):
        assert i18n.t_in("ru", "seo.split.week.h") in _html(page, "ru")
