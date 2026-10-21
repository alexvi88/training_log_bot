"""Сайт, третья часть: разделы-библиотеки, шаблон дневника, новые
калькуляторы, карточки результата, живые цифры, 301 на свой домен, gzip и
ETag/304 (seo_library, seo_calcs, seo_template, seo_http, site_numbers).

Страницы — через то же приложение, что поднимает прод (mcp_server.build_app),
как и в tests/test_seo_pages.py.
"""

import csv
import datetime as dt
import gzip as gzip_mod
import io
import json
import re
import shutil
import subprocess
import xml.etree.ElementTree as ET
import zipfile

import httpx
import pytest

import config
import formatting
import i18n
import mcp_server
import seo_calcs
import seo_library
import seo_pages
import seo_static
import seo_template
import seo_tools
import site_numbers
from handlers import csv_import

HOST = "https://training-log.example.com"
STORE = "https://apps.apple.com/app/apple-store/id6815211995?pt=129483617&mt=8"
UA_IPHONE = "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 Version/18.0 Mobile/15E148 Safari/604.1"
UA_ANDROID = "Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 Chrome/130.0 Mobile Safari/537.36"
_CYRILLIC = re.compile(r"[А-Яа-яЁё]")


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
    site_numbers.reset()
    yield
    site_numbers.reset()


async def _get_many(paths, headers=None, base=HOST):
    transport = httpx.ASGITransport(app=mcp_server.build_app())
    async with httpx.AsyncClient(transport=transport, base_url=base) as client:
        return {p: await client.get(p, headers=headers or {}) for p in paths}


async def _get(path, headers=None, base=HOST):
    return (await _get_many([path], headers, base))[path]


def _pages(kind):
    return [p for p in seo_pages.pages() if p.kind == kind]


# ---------- шаблон дневника: импорт без вопросов ----------


def _xlsx_sheet_as_rows(data: bytes, sheet: int) -> list[list[str]]:
    """Лист xlsx → строки, как их сохранит «Сохранить как CSV»: число —
    числом, дата из колонки A — ГГГГ-ММ-ДД (формат ячейки в шаблоне)."""
    ns = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        root = ET.fromstring(zf.read(f"xl/worksheets/sheet{sheet}.xml"))
    rows = []
    for row in root.findall(".//m:row", ns):
        cells: dict[int, str] = {}
        for c in row.findall("m:c", ns):
            col = ord(c.get("r")[0]) - ord("A")
            if c.get("t") == "inlineStr":
                cells[col] = c.find("m:is/m:t", ns).text or ""
            elif c.find("m:v", ns) is not None:
                value = c.find("m:v", ns).text
                if col == 0:
                    value = (dt.date(1899, 12, 30) + dt.timedelta(days=int(value))).isoformat()
                cells[col] = value
        if cells:
            rows.append(cells)
    width = max((max(c) + 1 for c in rows), default=0)  # «Сохранить как CSV» добивает до ширины шапки
    return [[c.get(i, "") for i in range(width)] for c in rows]


def _parse(rows: list[list[str]], account_unit: str = "kg"):
    buf = io.StringIO()
    csv.writer(buf).writerows(rows)
    headers, data_rows, has_header = csv_import._read_table(buf.getvalue())
    mapping = csv_import._auto_detect(headers)
    workouts = csv_import._build_workout_groups(
        data_rows, mapping, today=dt.date(2026, 10, 16),
        weight_factor=csv_import._weight_factor(headers, mapping, account_unit), account_unit=account_unit,
    )
    return headers, mapping, workouts


@pytest.mark.parametrize("lang", ["ru", "en"])
async def test_template_example_imports_without_a_single_question(fresh_db, lang):
    row = await fresh_db.get_or_create_user(telegram_id=4242, username="tpl")
    user_id = row["telegram_id"]
    # csv-пример и лист «Пример» из xlsx — одни и те же строки.
    csv_rows = list(csv.reader(io.StringIO(seo_template.example_csv(lang).decode("utf-8-sig"))))
    xlsx_rows = _xlsx_sheet_as_rows(seo_template.xlsx(lang), 2)
    assert csv_rows == xlsx_rows
    for rows in (csv_rows, xlsx_rows):
        headers, mapping, workouts = _parse(rows)
        # Все колонки узнаются сами — ни одного шага ручного маппинга.
        assert set(mapping) >= {"date", "exercise", "weight", "reps", "round", "rpe", "title", "exercise_note"}
        assert csv_import._file_weight_unit(headers, mapping) == "kg"
        assert len(workouts) == 2
        assert [w["title"] for w in workouts] == [i18n.t_in(lang, "seo.template.example.workout_a"),
                                                  i18n.t_in(lang, "seo.template.example.workout_b")]
        sets = sum(len(e["sets"]) for w in workouts for e in w["entries"])
        assert sets == len(seo_template.example_rows(lang))
        names = [e["name"] for w in workouts for e in w["entries"]]
        matches = await csv_import.match_exercise_names(user_id, names)
        # Имена из каталога сводятся с шаблоном точно — «какое это упражнение?» не будет.
        assert all(not m.needs_choice and m.template_name for m in matches.values()), matches
        assert any(e["note"] for w in workouts for e in w["entries"])


@pytest.mark.parametrize("lang", ["ru", "en"])
def test_template_files_are_deterministic_and_well_formed(lang):
    first = seo_template.xlsx.__wrapped__(lang)
    assert first == seo_template.xlsx.__wrapped__(lang)
    with zipfile.ZipFile(io.BytesIO(first)) as zf:
        names = set(zf.namelist())
        assert {"[Content_Types].xml", "xl/workbook.xml", "xl/styles.xml", "xl/worksheets/sheet3.xml"} <= names
        for name in names:
            ET.fromstring(zf.read(name))  # каждый XML разбирается
        workbook = zf.read("xl/workbook.xml").decode()
        sheet1 = zf.read("xl/worksheets/sheet1.xml").decode()
    for key in ("log", "example", "catalog"):
        assert i18n.t_in(lang, f"seo.template.sheet.{key}") in workbook
    # Выпадающий список упражнений не запрещает своё название.
    assert 'type="list"' in sheet1 and 'showErrorMessage="0"' in sheet1
    blank = seo_template.blank_csv(lang).decode("utf-8-sig").strip()
    assert blank == ",".join(seo_template.headers(lang))
    catalog = _xlsx_sheet_as_rows(first, 3)
    assert len(catalog) > 200


async def test_template_page_and_downloads():
    paths = ["/ru/templates/workout-log", "/en/templates/workout-log"] + [
        f"/{lang}/templates/{name}" for lang in ("ru", "en") for name in seo_template.FILES]
    responses = await _get_many(paths)
    for lang in ("ru", "en"):
        page = responses[f"/{lang}/templates/workout-log"].text
        for name in seo_template.FILES:
            assert f'href="/{lang}/templates/{name}" download' in page
            resp = responses[f"/{lang}/templates/{name}"]
            assert resp.status_code == 200
            assert resp.headers["content-disposition"] == f'attachment; filename="{name}"'
        assert responses[f"/{lang}/templates/workout-log.xlsx"].headers["content-type"].startswith(
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
        assert "FAQPage" in page and "DigitalDocument" in page
    assert "Загрузи заполненный шаблон — я разберу историю." in responses["/ru/templates/workout-log"].text


# ---------- разделы-библиотеки ----------


async def test_library_pages_link_what_they_promise():
    paths = [seo_pages.url_for(p, lang) for kind in ("pair", "muscle", "term", "guide") for p in _pages(kind)
             for lang in ("ru", "en")]
    responses = await _get_many(paths)
    for page in _pages("pair"):
        a, b = seo_library.pair_exercises(page.arg)
        for lang in ("ru", "en"):
            html = responses[seo_pages.url_for(page, lang)].text
            for url in (a, b):
                assert f'href="/{lang}/exercises/{url}"' in html
                assert f"ex/{seo_pages._EXERCISE_BY_URL[url]}_1-720.webp" in html
            assert "Article" in html
    for page in _pages("muscle"):
        for lang in ("ru", "en"):
            html = responses[seo_pages.url_for(page, lang)].text
            for url, _slug in seo_library.muscle_exercises(page.arg):
                assert f'href="/{lang}/exercises/{url}"' in html
    for page in _pages("term"):
        for lang in ("ru", "en"):
            html = responses[seo_pages.url_for(page, lang)].text
            assert "DefinedTerm" in html
            # Термин ведёт дальше — на калькулятор, гайд или другой термин.
            assert re.search(rf'href="/{lang}/(tools|guides|glossary|programs|muscles)/', html)
    for page in _pages("guide"):
        for lang in ("ru", "en"):
            html = responses[seo_pages.url_for(page, lang)].text
            for _label, url in seo_library.GUIDE_SOURCES[page.arg]:
                assert f'href="{url}"' in html
            assert '"@type":"Article"' in html and '"wordCount"' in html


def test_guides_are_long_enough_and_glossary_complete():
    assert len(seo_library.GUIDES) == 24 and len(seo_library.GLOSSARY) == 21 and len(seo_library.PAIRS) == 54
    assert len(seo_library.MUSCLES) == 12
    for slug in seo_library.GUIDES[:10]:
        for lang in ("ru", "en"):
            words = len(seo_library.guide_text(slug, lang).split())
            assert 800 <= words <= 1600, (slug, lang, words)
    # Четвёртая волна — понятия и программы: восемь разделов, русский текст
    # короче английского на те же мысли (падежи вместо предлогов и артиклей).
    for slug in seo_library.GUIDES[10:]:
        assert seo_library.GUIDE_SECTIONS[slug] == 8
        for lang, low in (("ru", 650), ("en", 750)):
            words = len(seo_library.guide_text(slug, lang).split())
            assert low <= words <= 1600, (slug, lang, words)


def test_library_texts_are_unique_per_page():
    for lang in ("ru", "en"):
        params = seo_library.link_params(lang)
        seen: dict[str, set[str]] = {}
        for slug in (seo_library.pair_slug(a, b) for a, b in seo_library.PAIRS):
            for part in ("lead", "diff_html", "both_html", "description"):
                text = i18n.t_in(lang, f"seo.pair.{slug}.{part}", **params)
                assert text not in seen.setdefault(part, set()), (slug, part)
                seen[part].add(text)
        for url, _g, _t in seo_library.MUSCLES:
            for part in ("lead", "about_html", "tips", "title"):
                text = i18n.t_in(lang, f"seo.mus.{url}.{part}", **params)
                assert text not in seen.setdefault(f"mus.{part}", set()), (url, part)
                seen[f"mus.{part}"].add(text)
                if part == "tips":
                    assert len(text.split("\n")) >= 3


def test_muscle_programs_count_sets_with_the_bot_rule():
    # Становая — половина спине и по четверти бицепсу бедра и ягодицам (seed_data.volume_split).
    back = dict((k, v) for k, v, _d in seo_library.muscle_programs("back"))
    hams = dict((k, v) for k, v, _d in seo_library.muscle_programs("hamstrings"))
    # Сила 5×5: тяга в наклоне 5×5 (спина, 5) + становая 1×5 → спине 0.5, бицепсу бедра 0.25.
    assert back["strength5x5"] == pytest.approx(5.5)
    assert hams["strength5x5"] == pytest.approx(0.25)
    assert seo_library._sets_of("3×8–12") == 3 and seo_library._sets_of("3×30–60 сек") == 3
    assert seo_library._sets_of(None) is None


async def test_nav_is_six_hubs_and_crumbs_go_through_them():
    resp = await _get("/ru/compare/bench-press-vs-dumbbell-bench-press")
    nav = re.search(r"<nav class=head-nav[^>]*>(.*?)</nav>", resp.text).group(1)
    assert re.findall(r'href="(/ru[^"]*)"', nav) == ["/ru/guides", "/ru/exercises", "/ru/muscles", "/ru/programs",
                                                     "/ru/compare", "/ru/tools"]
    assert 'href="/ru/compare" aria-current=page' in nav
    crumbs = re.search(r"<ol class=crumbs>(.*?)</ol>", resp.text).group(1)
    assert 'href="/ru/compare"' in crumbs


# ---------- JSON-LD, даты, справка для AI ----------


def _ld(html):
    return [json.loads(raw) for raw in re.findall(r'<script type="application/ld\+json">(.*?)</script>', html, re.S)]


async def test_every_page_has_updated_date_and_date_modified():
    responses = await _get_many([seo_pages.url_for(p, "en") for p in seo_pages.pages()])
    for path, resp in responses.items():
        assert f'<time datetime="{seo_pages.SITE_UPDATED}">' in resp.text, path
        creative = [b for b in _ld(resp.text) if b["@type"] not in ("BreadcrumbList", "FAQPage", "Organization",
                                                                      "WebSite", "DefinedTermSet")]
        assert creative and all(b.get("dateModified") == seo_pages.SITE_UPDATED for b in creative), path


async def test_organization_and_app_say_who_builds_it(monkeypatch):
    monkeypatch.setattr(config, "APP_STORE_URL", STORE)
    responses = await _get_many(["/en", "/ru/about"])
    for path in ("/en", "/ru/about"):
        org = next(b for b in _ld(responses[path].text) if b["@type"] == "Organization")
        assert org["founder"] == {"@type": "Person", "name": "Aleksandr Vilman"}
        assert STORE in org["sameAs"] and any(u.startswith("https://t.me/") for u in org["sameAs"])
    app = next(b for b in _ld(responses["/en"].text) if b["@type"] == "SoftwareApplication")
    assert app["author"]["name"] == "Aleksandr Vilman" and app["publisher"]["@id"].endswith("/#org")


async def test_llms_full_lists_every_page_in_both_languages():
    resp = await _get("/llms-full.txt")
    assert resp.status_code == 200 and resp.headers["content-type"].startswith("text/markdown")
    for page in seo_pages.pages():
        for lang in ("ru", "en"):
            assert f"({HOST}{seo_pages.url_for(page, lang)})" in resp.text
    assert "Aleksandr Vilman" in resp.text
    assert f"{HOST}/llms-full.txt" in (await _get("/llms.txt")).text


async def test_sitemap_has_lastmod_and_image_entries():
    root = ET.fromstring((await _get("/sitemap.xml")).text)
    ns = {"s": "http://www.sitemaps.org/schemas/sitemap/0.9", "i": "http://www.google.com/schemas/sitemap-image/1.1"}
    urls = {u.find("s:loc", ns).text: u for u in root.findall("s:url", ns)}
    assert all(u.find("s:lastmod", ns).text == seo_pages.SITE_UPDATED for u in urls.values())
    squat = urls[f"{HOST}/en/exercises/back-squat"]
    images = [i.find("i:loc", ns).text for i in squat.findall("i:image", ns)]
    assert images == [f"{HOST}/v1/media/exercises/barbell_squat_1.jpg", f"{HOST}/v1/media/exercises/barbell_squat_2.jpg"]
    assert urls[f"{HOST}/ru"].findall("i:image", ns)


# ---------- единицы, эмодзи ----------


def _visible(html: str) -> str:
    html = re.sub(r"<script.*?</script>|<style.*?</style>", "", html, flags=re.S)
    return re.sub(r"<[^>]+>", "", html)


async def test_site_puts_a_no_break_space_between_number_and_unit():
    paths = ["/ru", "/en", "/ru/tools/1rm", "/en/strength/bench-press/men", "/ru/guides/progressive-overload",
             "/en/guides/estimate-1rm-safely", "/ru/templates/workout-log", "/ru/glossary/tonnage"]
    responses = await _get_many(paths)
    for path, resp in responses.items():
        text = _visible(resp.text)
        assert not re.search(r"\d(?:кг|kg|lb)\b", text), (path, re.findall(r".{20}\d(?:кг|kg|lb)\b", text)[:3])
        assert not re.search(r"\d (?:кг|kg|lb)\b", text), path


async def test_bot_button_emoji_stay_out_of_site_copy():
    resp = await _get("/ru/import/hevy")
    assert "🔧" not in resp.text and "📥" not in resp.text


# ---------- калькуляторы: тоннаж, таймер, цель, перцентиль ----------


@pytest.mark.skipif(shutil.which("node") is None, reason="node не установлен")
@pytest.mark.parametrize("lang", ["ru", "en"])
def test_tonnage_comparison_is_the_bots_one(lang):
    totals = [(100, "kg"), (900, "kg"), (1920, "kg"), (5200, "kg"), (12500, "kg"), (61000, "kg"), (8000, "lb")]
    script = (seo_tools.calc_js() + seo_calcs.tonnage_prelude(lang) + seo_calcs.TONNAGE_JS
              + f"console.log(JSON.stringify({json.dumps(totals)}.map(function(c){{return equiv(c[0],c[1]);}})));")
    got = json.loads(subprocess.run(["node", "-e", script], capture_output=True, text=True, check=True).stdout)
    with i18n.use_lang(lang):
        for (total, unit), text in zip(totals, got, strict=True):
            expected = formatting.format_tonnage_equivalent(total, seed=0, unit=unit)
            expected = re.sub(r"\s*[^\w\s.,!?—-]+$", "", expected or "").strip()
            assert text == expected, (total, unit)


@pytest.mark.skipif(shutil.which("node") is None, reason="node не установлен")
def test_goal_and_percentile_scripts_match_python():
    goals = [(60.0, 100.0, "beginner", 2), (100.0, 120.0, "intermediate", 3), (180.0, 200.0, "advanced", 1),
             (90.0, 90.0, "beginner", 2), (50.0, 300.0, "advanced", 1)]
    pcts = [(120.0, "m", 82.0, "bench"), (500.0, "m", 92.0, "total"), (60.0, "f", 60.0, "bench"), (5.0, "m", 70.0, "squat"),
            (400.0, "m", 150.0, "deadlift"), (140.0, "f", 90.0, "deadlift")]
    data = seo_tools.percentile_data()
    script = seo_tools.calc_js() + f"""
var P = {json.dumps(data)};
console.log(JSON.stringify({{
  goal: {json.dumps(goals)}.map(function (c) {{ return [goalWeeks(c[0], c[1], c[2], c[3], true), goalWeeks(c[0], c[1], c[2], c[3], false)]; }}),
  pct: {json.dumps(pcts)}.map(function (c) {{ return percentile(P, c[0], c[1], c[2], c[3]); }})
}}));
"""
    got = json.loads(subprocess.run(["node", "-e", script], capture_output=True, text=True, check=True).stdout)
    for (cur, target, level, freq), (fast, slow) in zip(goals, got["goal"], strict=True):
        assert fast == seo_tools.goal_weeks(cur, target, level, freq, True)
        assert slow == seo_tools.goal_weeks(cur, target, level, freq, False)
    for case, value in zip(pcts, got["pct"], strict=True):
        expected = seo_tools.percentile(*case)
        assert value[0] == pytest.approx(expected[0]) and value[1] == expected[1] and value[2] == expected[2]


def test_goal_forecast_is_honest_and_bounded():
    # Быстрый край раньше медленного, цель уже взята — 0 недель, недостижимое за 3 года — None.
    assert seo_tools.goal_weeks(60, 100, "beginner", 2, True) < seo_tools.goal_weeks(60, 100, "beginner", 2, False)
    assert seo_tools.goal_weeks(100, 90, "beginner", 2, True) == 0
    assert seo_tools.goal_weeks(50, 300, "advanced", 1, True) is None
    # Темп не обгоняет двойную прогрессию бота: повтор за тренировку ≈ 1/30 веса.
    curve = seo_tools.goal_curve(100, "beginner", 1, True, 10)
    assert all(b / a - 1 <= 1 / 30 + 1e-12 for a, b in zip(curve, curve[1:], strict=False))
    # Чаще тренируешь движение — раньше дойдёшь.
    assert seo_tools.goal_weeks(80, 100, "intermediate", 3, True) <= seo_tools.goal_weeks(80, 100, "intermediate", 1, True)


def test_percentile_table_is_documented_and_monotonic():
    data = seo_tools.percentile_data()
    assert data["source_url"] == "https://gitlab.com/openpowerlifting/opl-data" and data["meets"] >= 100
    for sex, classes in data["table"].items():
        assert set(classes) == set(data["classes"][sex])
        for lifts in classes.values():
            assert set(lifts) == {"squat", "bench", "deadlift", "total"}
            for cell in lifts.values():
                assert cell["n"] >= 30 and cell["q"] == sorted(cell["q"])
    assert seo_tools.weight_class("m", 83) == "83" and seo_tools.weight_class("m", 83.1) == "93"
    assert seo_tools.weight_class("f", 120) == "84+"
    low, mid, high = (seo_tools.percentile(w, "m", 82, "bench")[0] for w in (40, 120, 400))
    assert low == 0 and 0 < mid < 100 and high == 100


async def test_new_tools_render_and_link_from_the_hub():
    paths = ["/ru/tools", "/en/tools"] + [f"/{lang}/tools/{t}" for lang in ("ru", "en")
                                         for t in ("tonnage", "rest-timer", "goal", "strength-percentile")]
    responses = await _get_many(paths)
    for lang in ("ru", "en"):
        hub = responses[f"/{lang}/tools"].text
        for tool in ("tonnage", "rest-timer", "goal", "strength-percentile"):
            assert f'href="/{lang}/tools/{tool}"' in hub
            assert "WebApplication" in responses[f"/{lang}/tools/{tool}"].text
    timer = responses["/en/tools/rest-timer"].text
    for s in seo_calcs.REST_PRESETS:
        assert f'value="{s}"' in timer
    assert "wakeLock" in timer and "navigator.vibrate" in timer
    assert "OpenPowerlifting" in responses["/en/tools/strength-percentile"].text


async def test_result_cards_are_pngs_and_reject_bad_params():
    good = ["/og/card/ru/goal.png?w=100&u=kg&lift=bench&m1=3&y1=2027&m2=6&y2=2027",
            "/og/card/en/pct.png?p=68&cls=83&sex=m&lift=total",
            "/og/card/en/rm1.png?v=116.7&u=kg", "/og/card/ru/tonnage.png?v=5200&u=kg",
            "/og/card/en/dots.png?v=345", "/og/card/ru/level.png?l=2&lift=squat"]
    bad = ["/og/card/ru/goal.png?w=abc&u=kg&lift=bench&m1=3&y1=2027&m2=6&y2=2027",
           "/og/card/ru/goal.png?w=100&u=kg&lift=bench&m1=13&y1=2027&m2=6&y2=2027",
           "/og/card/en/pct.png?p=150&cls=83&sex=m&lift=total", "/og/card/en/pct.png?p=50&cls=999&sex=m&lift=total",
           "/og/card/xx/rm1.png?v=1&u=kg", "/og/card/en/evil.png", "/og/card/en/rm1.png?v=1&u=stone"]
    responses = await _get_many(good + bad)
    for path in good:
        assert responses[path].status_code == 200, path
        assert responses[path].content[:8] == b"\x89PNG\r\n\x1a\n"
    for path in bad:
        assert responses[path].status_code in (400, 404), path
    assert seo_calcs.goal_card_text("ru", "100", "kg", "bench", 3, 2027, 6, 2027) == \
        "Моя цель — 100 кг в упражнении «жим лёжа». Прогноз: март 2027 — июнь 2027"
    assert seo_calcs.pct_card_text("en", 68, "83", "m", "total") == \
        "I'm stronger than 68% of competitive powerlifters at 83 kg — powerlifting total"


@pytest.mark.skipif(shutil.which("node") is None, reason="node не установлен")
async def test_goal_months_have_no_year_suffix():
    """«ноябрь 2026», а не «ноябрь 2026 г.»: русский Intl дописывает «г.»,
    поэтому месяцы — из каталога, как на карточке для сторис."""
    responses = await _get_many(["/ru/tools/goal", "/en/tools/goal"])
    for lang, want in (("ru", "ноябрь\u00a02026"), ("en", "November\u00a02026")):
        html = responses[f"/{lang}/tools/goal"].text
        assert "toLocaleDateString" not in html and "DateTimeFormat" not in html
        mn = re.search(r"var MN=(\[.*?\]);(function month\(d\)\{.*?\})", html)
        script = f"var MN={mn.group(1)};{mn.group(2)};console.log(JSON.stringify(month(new Date(2026, 10, 3))))"
        got = json.loads(subprocess.run(["node", "-e", script], capture_output=True, text=True, check=True).stdout)
        assert got == want and "г." not in got
    text = seo_calcs.goal_card_text("ru", "100", "kg", "bench", 11, 2026, 12, 2026)
    assert "ноябрь 2026 — декабрь 2026" in text and "г." not in text


def test_no_site_page_formats_dates_with_intl():
    import pathlib

    for name in ("seo_pages.py", "seo_calcs.py", "seo_library.py", "seo_tools.py", "seo_template.py", "seo_design.py"):
        source = (pathlib.Path(__file__).resolve().parent.parent / name).read_text(encoding="utf-8")
        assert "toLocaleDateString" not in source and "DateTimeFormat" not in source, name


# Оговорки «не нашёл в описании» вместо прямого «нет». Ссылка на справку
# там, где факт есть («в справке Hevy сказано…»), — не оговорка.
_HEDGES = re.compile(r"не нашёл|не нашли|не заявлен|в описании (?:приложения )?нет|[Nn]ot found|not listed|"
                     r"[Dd]idn't find|(?:aren't|isn't|not) in the description")


async def test_competitor_no_cells_say_no_plainly():
    """Где у конкурента функции нет — в клетке «Нет», без «в описании не
    нашёл»; источники — списком внизу страницы, как были."""
    paths = [f"/{lang}/vs/{c}" for lang in ("ru", "en") for c in seo_pages.COMPETITORS]
    paths += [f"/{lang}/alternatives/{c}" for lang in ("ru", "en") for c in seo_pages.ALTERNATIVES]
    responses = await _get_many(paths)
    for c in seo_pages.COMPETITORS:
        for row, mark in seo_pages._VS_MARKS[c].items():
            for lang in ("ru", "en"):
                cell = i18n.t_in(lang, f"seo.vs.{c}.{row}_html")
                if mark == "no" and row not in ("price", "free"):
                    assert cell.split(",")[0] in ("Нет", "No"), (c, row, lang, cell)
                if mark != "yes":
                    assert not _HEDGES.search(cell), (c, row, lang, cell)
    for path, resp in responses.items():
        table = re.search(r"<table>.*?</table>", resp.text, re.S)
        if table:
            assert not _HEDGES.search(table.group(0)), path
        if "/vs/" in path:
            assert re.search(r"<section><h2>[^<]+</h2><ul class=muted><li><a href=\"https://", resp.text), path
    for lang in ("ru", "en"):
        assert not _HEDGES.search(i18n.t_in(lang, "seo.vs.mark.no"))
        assert not _HEDGES.search(i18n.t_in(lang, "seo.alt.app.strong.what_html"))
        # Своё «до 20 секунд» — из того же потолка, что держат бот и /v1.
        assert str(config.MAX_VIDEO_SECONDS) in responses[f"/{lang}/vs/hevy"].text.split("<table>", 1)[1].split("</table>", 1)[0]


async def test_log_cta_follows_the_device(monkeypatch):
    monkeypatch.setattr(config, "APP_STORE_URL", STORE)
    iphone = await _get("/go/diary?f=rm1_log", headers={"User-Agent": UA_IPHONE, "Referer": f"{HOST}/ru/tools/1rm"})
    android = await _get("/go/diary?f=rm1_log", headers={"User-Agent": UA_ANDROID, "Referer": f"{HOST}/en/tools/1rm"})
    assert iphone.status_code == 302 and iphone.headers["location"].startswith("https://apps.apple.com/")
    assert "ct=site_rm1_log" in iphone.headers["location"]
    assert android.headers["location"].endswith("start=src_seo_rm1_log")


# ---------- живые цифры недели ----------


async def test_basement_board_shows_only_real_numbers(fresh_db, monkeypatch):
    def body(html: str) -> str:
        return html[html.index("<body"):]

    site_numbers.reset()
    empty = body((await _get("/ru")).text)
    assert "basement-floor" in empty and "class=board " not in empty and "board-n" not in empty
    user = await fresh_db.get_or_create_user(telegram_id=777, username="lifter")
    uid = user["telegram_id"]
    wid = await fresh_db.create_workout(uid)
    ex = await fresh_db.create_exercise(uid, "Жим штанги лёжа", None)
    block = await fresh_db.get_or_create_single_block_for_exercise(wid, ex)
    await fresh_db.add_set(block, ex, 1, 0, 100, 5)
    await fresh_db.finish_workout(wid)
    site_numbers.reset()
    site_numbers.reset()
    html = body((await _get("/ru")).text)
    site_numbers.reset()
    assert re.search(r"<span class=board-n>1</span><small>", html), html[html.find("board"):][:400]


# ---------- 301 на свой домен, gzip, ETag ----------


_NEVER_REDIRECTED = [
    "/v1/health", "/v1/auth/apple", "/mcp", "/mcp/", "/mcp/sse", "/.well-known/oauth-authorization-server",
    "/.well-known/apple-app-site-association", "/authorize", "/token", "/register", "/revoke", "/connect",
    "/privacy", "/terms", "/support", "/coach/invite/abc", "/coach/abc", "/embed/ru/1rm", "/go/bot?f=home",
    "/go/appstore?f=home", "/og/ru/home.png", "/og/coach.jpg", "/site/icons/favicon-32.png", "/site/coach.jpg",
    "/favicon.ico", "/apple-touch-icon.png", "/site.webmanifest", "/media/x.jpg", "/v1/media/exercises/pullups_1.jpg",
]
_REDIRECTED = ["/", "/ru", "/en", "/ru/tools/1rm", "/en/guides/rest-between-sets?utm_source=x", "/sitemap.xml",
               "/robots.txt", "/llms.txt", "/llms-full.txt", "/ru/nope"]


@pytest.mark.parametrize("host", ["training-log-bot.fly.dev", "www.athletediary.app"])
async def test_site_paths_on_other_hosts_redirect_permanently(monkeypatch, host):
    monkeypatch.setattr(config, "SITE_BASE_URL", "https://athletediary.app")
    monkeypatch.setattr(config, "MCP_PUBLIC_URL", "https://training-log-bot.fly.dev")
    transport = httpx.ASGITransport(app=mcp_server.build_app())
    async with httpx.AsyncClient(transport=transport, base_url=f"https://{host}") as client:
        for path in _REDIRECTED:
            resp = await client.get(path)
            assert resp.status_code == 301, (host, path)
            assert resp.headers["location"] == "https://athletediary.app" + path
            assert (await client.head(path)).status_code == 301
        for path in _NEVER_REDIRECTED:
            resp = await client.get(path)
            assert resp.status_code != 301, (host, path)
            assert not resp.headers.get("location", "").startswith("https://athletediary.app"), (host, path)
            post = await client.post(path, data={})
            assert post.status_code != 301, (host, path)
        # Даже адрес страницы — не GET — не перенаправляется.
        assert (await client.post("/ru", data={})).status_code != 301


async def test_own_domain_and_unset_site_url_never_redirect(monkeypatch):
    monkeypatch.setattr(config, "MCP_PUBLIC_URL", "https://training-log-bot.fly.dev")
    for site, host in (("https://athletediary.app", "athletediary.app"), ("", "training-log-bot.fly.dev")):
        monkeypatch.setattr(config, "SITE_BASE_URL", site)
        transport = httpx.ASGITransport(app=mcp_server.build_app())
        async with httpx.AsyncClient(transport=transport, base_url=f"https://{host}") as client:
            for path in ("/ru", "/sitemap.xml", "/en/tools"):
                assert (await client.get(path)).status_code == 200, (site, path)


async def test_site_responses_are_gzipped_and_api_is_not():
    responses = await _get_many(["/en/guides/rest-between-sets", "/sitemap.xml", "/llms-full.txt", "/v1/health",
                                 "/site/icons/favicon-32.png"], headers={"Accept-Encoding": "gzip"})
    page = responses["/en/guides/rest-between-sets"]
    assert page.headers["content-encoding"] == "gzip" and "Accept-Encoding" in page.headers["vary"]
    assert "<html lang=en>" in page.text  # httpx распаковал сам
    assert responses["/sitemap.xml"].headers["content-encoding"] == "gzip"
    assert "content-encoding" not in responses["/v1/health"].headers
    assert "content-encoding" not in responses["/site/icons/favicon-32.png"].headers
    raw = gzip_mod.compress(b"x")  # модуль на месте — проверка, что импорт не сломан
    assert raw


async def test_etag_and_not_modified_for_pages_and_static():
    for path in ("/en/glossary/rpe", "/site/coach.jpg", "/og/en/home.png", "/sitemap.xml"):
        first = await _get(path)
        etag = first.headers["etag"]
        again = await _get(path, headers={"If-None-Match": etag})
        assert again.status_code == 304, path
        assert again.content == b"" and again.headers["etag"] == etag
        other = await _get(path, headers={"If-None-Match": 'W/"nope"'})
        assert other.status_code == 200 and other.content == first.content


async def test_removed_and_unknown_pages_keep_their_status():
    responses = await _get_many(["/ru/no-such-page", "/en/guides/nope", "/en/compare/a-vs-b", "/ru/muscles/tail"])
    for path, resp in responses.items():
        assert resp.status_code == 404, path
        assert "noindex" in resp.text


async def test_images_have_sizes_and_lazy_loading():
    responses = await _get_many(["/ru", "/en/exercises", "/en/compare/pull-up-vs-chin-up", "/ru/muscles/chest"])
    for path, resp in responses.items():
        for tag in re.findall(r"<img [^>]*>", resp.text):
            assert "width=" in tag and "height=" in tag, (path, tag)
        lazy = [t for t in re.findall(r"<img [^>]*>", resp.text) if "loading=lazy" in t]
        assert lazy, path


async def test_english_pages_of_phase_three_have_no_cyrillic():
    kinds = ("muscle", "pair", "term", "guide", "alternatives", "template", "about", "muscles", "compare", "glossary",
             "guides")
    paths = [seo_pages.url_for(p, "en") for p in seo_pages.pages() if p.kind in kinds]
    paths += [f"/en/tools/{t}" for t in ("tonnage", "rest-timer", "goal", "strength-percentile")]
    responses = await _get_many(paths)
    for path, resp in responses.items():
        assert not _CYRILLIC.search(resp.text), (path, _CYRILLIC.findall(resp.text)[:5])


def test_og_wrap_keeps_number_and_unit_together():
    """«1.9 т» в карточке не разрывается: перенос только по обычным пробелам."""
    from PIL import Image, ImageDraw

    import seo_static

    draw = ImageDraw.Draw(Image.new("RGB", (10, 10)))
    font = seo_static._font(64)
    text = "Поднял за тренировку 1.9 т"
    width = int(draw.textlength("Поднял за тренировку 1.9", font=font)) + 2
    lines = seo_static._wrap(draw, text, font, width)
    assert "т" not in [line.strip() for line in lines]
    assert any("1.9 т" in line for line in lines)
