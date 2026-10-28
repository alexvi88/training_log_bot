"""Сайт, пятая волна: питание и спортпит — восемь гайдов и калькуляторы
белка и калорий (seo_library.NUTRITION_GUIDES, seo_calcs.tool_protein /
tool_calories, математика — seo_tools).

Что держит тест: формулы совпадают с опубликованными, браузер считает так
же, как Python, страницы живут на обоих языках и сгруппированы на хабах,
каждый гайд ведёт на калькулятор и цитирует исследования, а сайт нигде не
обещает дневник еды — его в продукте нет (api_v1_food.py).
"""

import json
import re
import shutil
import subprocess

import httpx
import pytest

import config
import i18n
import mcp_server
import seo_calcs
import seo_library
import seo_pages
import seo_static
import seo_tools

HOST = "https://training-log.example.com"
NEW_TOOLS = ("protein", "calories")
_CYRILLIC = re.compile(r"[А-Яа-яЁё]")


def _cyr(text):
    """Кириллица на странице, кроме автонима «Русский» в переключателе языка
    (автонимы не переводятся — как в кнопке языка у бота)."""
    return _CYRILLIC.search(text.replace("Русский", ""))


@pytest.fixture(autouse=True)
def public_url(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "MCP_PUBLIC_URL", HOST)
    monkeypatch.setattr(config, "PUBLIC_BASE_URL", "")
    monkeypatch.setattr(config, "SITE_BASE_URL", "")
    monkeypatch.setattr(config, "APP_STORE_URL", "")
    monkeypatch.setattr(seo_static, "OG_CACHE_DIR", str(tmp_path / "og"))
    seo_static._og_memory.clear()


async def _get_many(paths):
    transport = httpx.ASGITransport(app=mcp_server.build_app())
    async with httpx.AsyncClient(transport=transport, base_url=HOST) as client:
        return {p: await client.get(p) for p in paths}


def i18n_t(lang, key):
    return seo_pages._e(i18n.t_in(lang, key))


def _paths():
    pages = [seo_pages.Page("tool", t) for t in NEW_TOOLS] + [seo_pages.Page("guide", g) for g in seo_library.NUTRITION_GUIDES]
    return [seo_pages.url_for(p, lang) for p in pages for lang in seo_pages.LANGS]


# ---------- математика ----------


def test_mifflin_st_jeor_matches_the_published_formula():
    # 10·вес + 6.25·рост − 5·возраст + 5 (мужчины) / − 161 (женщины).
    assert seo_tools.bmr_mifflin(80, 180, 30, "m") == pytest.approx(1780.0)
    assert seo_tools.bmr_mifflin(60, 165, 25, "f") == pytest.approx(1345.25)
    c = seo_tools.calorie_targets(80, 180, 30, "m", "moderate", "maintain")
    assert c["maintenance"] == pytest.approx(1780 * 1.55) and c["lo"] == c["hi"] == c["maintenance"]
    gain = seo_tools.calorie_targets(80, 180, 30, "m", "moderate", "gain")
    cut = seo_tools.calorie_targets(80, 180, 30, "m", "moderate", "cut")
    assert cut["lo"] < cut["hi"] < c["maintenance"] < gain["lo"] < gain["hi"]
    assert gain["hi"] == pytest.approx(c["maintenance"] * 1.15) and cut["lo"] == pytest.approx(c["maintenance"] * 0.8)
    assert list(seo_tools.ACTIVITY.values()) == sorted(seo_tools.ACTIVITY.values())


def test_protein_ranges_follow_the_cited_numbers():
    # Набор — Morton et al. 2018 (плато около 1.6, верх интервала 2.2); держать — ISSN 1.4–2.0.
    assert seo_tools.protein_range(80, "gain") == pytest.approx((128.0, 176.0))
    assert seo_tools.protein_range(80, "maintain") == pytest.approx((112.0, 160.0))
    lo, hi = seo_tools.protein_range(80, "cut")
    assert lo > seo_tools.protein_range(80, "maintain")[0] and hi > seo_tools.protein_range(80, "gain")[1]
    assert seo_tools.protein_per_meal(80, "gain", 4) == pytest.approx((32.0, 44.0))


@pytest.mark.skipif(shutil.which("node") is None, reason="нет node")
def test_nutrition_scripts_match_python():
    proteins = [(80.0, "gain"), (63.5, "cut"), (102.0, "maintain")]
    cals = [(80.0, 180.0, 30.0, "m", "moderate", "maintain"), (58.0, 162.0, 41.0, "f", "light", "cut"),
            (95.0, 190.5, 22.0, "m", "very", "gain"), (70.0, 175.0, 80.0, "f", "sedentary", "gain")]
    ranges = [(127.6, 175.4, 1), (2759.0, 2759.0, 10), (2344.9, 2614.0, 10)]
    script = seo_tools.calc_js() + seo_calcs.NUTRITION_JS + f"""
console.log(JSON.stringify({{
  p: {json.dumps(proteins)}.map(function (c) {{ return proteinRange(c[0], c[1]); }}),
  c: {json.dumps(cals)}.map(function (c) {{ return calorieTargets(c[0], c[1], c[2], c[3], c[4], c[5]); }}),
  r: {json.dumps(ranges)}.map(function (c) {{ return rg(c[0], c[1], 'g', c[2]); }})
}}));
"""
    got = json.loads(subprocess.run(["node", "-e", script], capture_output=True, text=True, check=True).stdout)
    for case, js in zip(proteins, got["p"], strict=True):
        assert js == pytest.approx(list(seo_tools.protein_range(*case)))
    for case, js in zip(cals, got["c"], strict=True):
        py = seo_tools.calorie_targets(*case)
        assert {k: pytest.approx(v) for k, v in py.items()} == js
    for (lo, hi, step), js in zip(ranges, got["r"], strict=True):
        assert js == seo_calcs._range(lo, hi, "g", step)
    assert got["r"][1] == "2760<span class=u> g</span>"


# ---------- страницы ----------


async def test_nutrition_pages_answer_on_both_languages_and_hubs_group_them():
    hubs = [seo_pages.url_for(seo_pages.Page(kind), lang) for kind in ("tools", "guides") for lang in seo_pages.LANGS]
    responses = await _get_many(_paths() + hubs + ["/sitemap.xml", "/llms.txt"])
    for path, resp in responses.items():
        assert resp.status_code == 200, path
        if seo_pages.lang_of_path(path) not in (None, "ru"):
            assert not _cyr(resp.text), (path, _CYRILLIC.findall(resp.text)[:5])
    for lang in seo_pages.LANGS:
        seg = seo_pages.lang_segment(lang)
        tools = responses[f"/{seg}/tools"].text
        head = i18n_t(lang, "seo.tools.nutrition.h")
        assert f"<h2 class=group-title>{head}</h2>" in tools
        nutrition = tools.split(f"<h2 class=group-title>{head}</h2>", 1)[1]
        for tool in NEW_TOOLS:
            assert f'href="/{seg}/tools/{tool}"' in nutrition
            assert "WebApplication" in responses[f"/{seg}/tools/{tool}"].text
        guides = responses[f"/{seg}/guides"].text
        head = i18n_t(lang, "seo.guides.nutrition.h")
        assert f"<h2 class=group-title>{head}</h2>" in guides
        before, after = guides.split(f"<h2 class=group-title>{head}</h2>", 1)
        for slug in seo_library.NUTRITION_GUIDES:
            assert f'href="/{seg}/guides/{slug}"' in after and f'href="/{seg}/guides/{slug}"' not in before
        assert f'href="/{seg}/guides/progressive-overload"' in before
    for path in _paths():
        # В sitemap — только языки, идущие в индекс.
        if seo_pages.lang_of_path(path) in seo_pages.INDEXED_LANGS:
            assert f"<loc>{HOST}{path}</loc>" in responses["/sitemap.xml"].text
    for tool in NEW_TOOLS:
        assert f"{HOST}/en/tools/{tool}" in responses["/llms.txt"].text


def test_every_nutrition_guide_cites_research_and_leads_to_a_calculator():
    calcs = {f"/{{lang}}/tools/{t}" for t in NEW_TOOLS}
    for slug in seo_library.NUTRITION_GUIDES:
        assert seo_library.GUIDE_SECTIONS[slug] == 8
        sources = seo_library.GUIDE_SOURCES[slug]
        assert sources and all(url.startswith("https://pubmed.ncbi.nlm.nih.gov/") for _l, url in sources), slug
        assert 2 <= len(seo_library.GUIDE_LINKS[slug]) <= 4
        for lang in seo_pages.LANGS:
            seg = seo_pages.lang_segment(lang)
            html = seo_pages.render(seo_pages.Page("guide", slug), lang)
            assert any(f'href="{c.format(lang=seg)}"' in html for c in calcs), (slug, lang)
            # Ссылки на старые страницы сайта: другие гайды, калькуляторы, приложение.
            assert len(set(re.findall(rf'href="(/{seg}/(?:guides|tools|programs|workout-log-app)[^"]*)"', html))) >= 4


async def test_site_never_promises_a_food_diary():
    # Дневника еды в продукте нет: кнопки и тексты зовут писать тренировки и вес тела.
    banned = re.compile(r"дневник (?:еды|питания)|запиш\w* (?:еду|рацион|калории)|food (?:diary|log)|"
                        r"log (?:your )?(?:food|meals|calories)|track(?:s|ing)? (?:your )?(?:food|meals|calories) for you", re.I)
    responses = await _get_many(_paths())
    for path, resp in responses.items():
        text = re.sub(r"<script.*?</script>|<[^>]+>", " ", resp.text, flags=re.S)
        assert not banned.search(text), (path, banned.findall(text)[:3])
    for tool in NEW_TOOLS:
        en = responses[f"/en/tools/{tool}"].text
        assert f'class="log-cta btn-row"><a class="btn primary" href="/go/diary?f={seo_pages._BLOCKS[f"log:{tool}"]}"' in en


def test_new_blocks_fit_the_acquisition_slug():
    for key in ("tool:protein", "tool:calories", "log:protein", "log:calories"):
        block = seo_pages._BLOCKS[key]
        assert len(seo_pages.bot_payload(block)) <= 32, block
