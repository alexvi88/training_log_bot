"""«Гайды» сторис — guide_stories.py и GET /v1/guides (api_v1_guides.py).

Разбор статьи на слайды (без разметки, длина, обрезка по предложению,
«Коротко» пунктами), витрина (плато, новичок, никто), кнопки, полки и ряд
«Меню», форма ответа (все ключи всегда, iOS-модели строгие) и кадры —
только существующие рисунки тренера.
"""

import datetime as dt
import os
import re

import httpx
import pytest

import api_v1
import db
import exercise_media
import guide_stories as G
import i18n
import product_metrics
import seo_library

LANGS = ("ru", "en", "es", "pt", "pt-PT", "de", "fr", "tr")
CYRILLIC = re.compile(r"[А-Яа-яЁё]")
SLIDE_KEYS = {"kind", "title", "text", "image", "chip", "items", "action"}
GUIDE_KEYS = {"slug", "title", "card", "cover", "featured", "reason", "url", "share_url", "slides"}
ACTION_KEYS = {"kind", "label", "exercise_id", "question"}


# ---------- разбор текста ----------


def test_clip_strips_html_and_cuts_on_a_sentence_boundary():
    html = ("<p>Первое предложение про <b>жим</b>. Второе — со <a href='/x'>ссылкой</a>.</p>"
            "<p>Третье. Четвёртое не влезет.</p>")
    assert G.clip(html) == "Первое предложение про жим. Второе — со ссылкой. Третье."


def test_clip_respects_the_length_limit_by_whole_sentences():
    long = "Слово " * 60
    html = f"<p>Короткое начало. {long.strip()}. Хвост.</p>"
    assert G.clip(html) == "Короткое начало."


def test_clip_never_ends_on_a_lead_in_to_a_list():
    html = "<p>Сначала скажу главное. Ответь на три вопроса:</p><ul><li>" + "Очень длинный пункт " * 20 + "</li></ul>"
    assert G.clip(html) == "Сначала скажу главное."


def test_clip_cuts_one_overlong_sentence_after_a_whole_word():
    html = "<p>" + "долгое слово " * 40 + "конец.</p>"
    text = G.clip(html)
    assert len(text) <= G.MAX_CHARS and text.endswith("…")
    assert text[:-1].split()[-1] == "слово"


def test_sentences_keep_abbreviations_and_ordinals_together():
    assert G.sentences("Pelland et al. (2024) zählten Sätze. Danach kam mehr.") == [
        "Pelland et al. (2024) zählten Sätze.", "Danach kam mehr."]
    assert G.sentences("z. B. Kniebeugen. Ende.") == ["z. B. Kniebeugen.", "Ende."]
    assert G.sentences("Cuma 3. gün: şınav. Bitti.") == ["Cuma 3. gün: şınav.", "Bitti."]


@pytest.mark.parametrize("lang", LANGS)
def test_every_guide_parses_into_clean_slides(lang):
    for slug in seo_library.GUIDES:
        g = G.static_guide(slug, lang)
        assert set(g) == GUIDE_KEYS
        slides = g["slides"]
        assert slides[0]["kind"] == "cover" and slides[-1]["kind"] == "takeaways"
        assert 5 <= len(slides) <= 8, (slug, len(slides))
        assert all(s["kind"] == "section" for s in slides[1:-1])
        for s in slides:
            assert set(s) == SLIDE_KEYS
            for text in (s["title"], s["text"], *s["items"]):
                assert "<" not in text and ">" not in text and "&nbsp;" not in text, (slug, text)
                assert "{" not in text, (slug, text)
            assert len(s["text"]) <= G.MAX_CHARS, (slug, s["text"])
            # «…» — только после целого слова, никогда посреди.
            if s["text"].endswith("…"):
                assert s["text"][-2] != " " and s["text"][-2].isalpha() or s["text"][-2] in ".,»)"
        assert all(s["text"] for s in slides[:-1]), slug
        assert slides[-1]["text"] == "" and 1 <= len(slides[-1]["items"]) <= G.MAX_TAKEAWAYS
        if lang != "ru":
            assert not CYRILLIC.search(str(g)), (lang, slug)


def test_takeaways_are_the_article_lines():
    g = G.static_guide("break-a-plateau", "ru")
    lines = i18n.t_in("ru", "seo.guide.break-a-plateau.takeaways").split("\n")
    assert g["slides"][-1]["items"] == lines[: G.MAX_TAKEAWAYS]
    assert g["slides"][-1]["title"] == i18n.t_in("ru", "seo.guide.takeaways.h")


def test_sections_skip_the_product_pitch_and_cap_at_six():
    assert G.section_numbers("break-a-plateau") == [1, 2, 3, 4, 5, 6]  # s7 «Как я помогаю»
    assert G.section_numbers("common-gym-mistakes") == [1, 2, 3, 4, 5, 6]
    assert G.section_numbers("first-week-in-the-gym") == [1, 2, 3, 4, 5, 6]  # без примера и FAQ
    for slug in seo_library.GUIDES:
        assert all(i <= seo_library.GUIDE_SECTIONS[slug] for i in G.SKIP_SECTIONS.get(slug, ()))


# ---------- каталог: порядок, кадры, кнопки, полки ----------


def test_order_covers_every_guide_once_nutrition_last():
    assert sorted(G.ORDER) == sorted(seo_library.GUIDES)
    tail = G.ORDER[-len(seo_library.NUTRITION_GUIDES):]
    assert set(tail) == set(seo_library.NUTRITION_GUIDES)
    assert G.ORDER.index("switch-from-hevy-or-strong") == len(G.ORDER) - len(tail) - 1


def test_every_frame_is_an_existing_coach_drawing():
    assert set(G.FRAMES) == set(seo_library.GUIDES)
    for slug, slugs in G.FRAMES.items():
        assert 2 <= len(slugs) <= 3, slug
        for s in slugs:
            assert s in exercise_media.FRAME_REVISION, s
            for n in (1, 2):
                assert os.path.isfile(os.path.join(exercise_media.MEDIA_DIR, exercise_media.frame_name(s, n)))
        g = G.static_guide(slug, "en")
        assert g["cover"] == g["slides"][0]["image"]
        # Обложка — своя картинка гайда (media/guides), если она нарисована;
        # остальные кадры — рисунки тренера из упражнений.
        cover_ok = ("/media/exercises/", "/media/guides/")
        assert g["slides"][0]["image"].startswith(cover_ok)
        assert all(s["image"].startswith("/media/exercises/") for s in g["slides"][1:])
        assert len({s["image"] for s in g["slides"]}) >= 2


@pytest.mark.parametrize("lang", LANGS)
def test_every_action_has_a_label_and_a_question_where_needed(lang):
    assert set(G.ACTIONS) == set(seo_library.GUIDES)
    for slug, kind in G.ACTIONS.items():
        assert kind in G.ACTION_KINDS
        action = G.action_for(slug, lang)
        assert set(action) == ACTION_KEYS and action["kind"] == kind and action["label"]
        assert action["exercise_id"] is None
        if kind == "coach_question":
            assert action["question"] and action["question"].endswith("?")
        else:
            assert action["question"] is None
    pinned = G.action_for("break-a-plateau", lang, exercise=(7, "Bench"))
    assert pinned == {"kind": "exercise_progress", "label": pinned["label"], "exercise_id": 7, "question": None}
    assert "Bench" in pinned["label"]


def test_action_map_follows_the_guide_topic():
    assert G.ACTIONS["rest-between-sets"] == "rest_timer"
    assert G.ACTIONS["film-your-set"] == "video"
    assert G.ACTIONS["switch-from-hevy-or-strong"] == "import"
    assert G.ACTIONS["choose-a-program"] == "programs"
    assert G.ACTIONS["progressive-overload"] == "progress"
    assert all(G.ACTIONS[g] == "coach_question" for g in seo_library.NUTRITION_GUIDES)
    assert i18n.t_in("ru", "guides.question.protein-per-day") == "Сколько белка мне нужно с моим весом?"


def test_shelves_and_menu():
    assert [k for k, _ in G.SHELVES] == ["start", "growth", "training", "programs", "nutrition"]
    on_shelves = {s for _, slugs in G.SHELVES for s in slugs}
    assert on_shelves <= set(seo_library.GUIDES)
    shelves = G.shelves("en", "break-a-plateau")
    growth = next(s for s in shelves if s["key"] == "growth")
    assert growth["slugs"][0] == "break-a-plateau" and growth["title"] == "Growth"
    assert len(growth["slugs"]) == len(set(growth["slugs"]))
    assert G.menu("break-a-plateau", False) == [
        "break-a-plateau", "progressive-overload", "rest-between-sets", "protein-per-day"]
    assert G.menu("first-week-in-the-gym", True) == [
        "first-week-in-the-gym", "beginner-gym-program", "common-gym-mistakes", "protein-per-day"]
    assert G.menu(None, False) == ["progressive-overload", "rest-between-sets", "sets-per-muscle-per-week",
                                   "protein-per-day"]
    for menu in (G.menu(None, True), G.menu("break-a-plateau", True)):
        assert len(menu) == len(set(menu)) <= G.MENU_SIZE


def test_url_points_at_the_site_page_in_the_athletes_language():
    assert G.guide_url("creatine", "pt-PT").endswith("/pt-pt/guides/creatine")
    assert G.guide_url("creatine", "ru").startswith("https://")


def test_guide_events_are_accepted_without_user_text():
    clean = product_metrics.clean_props
    assert clean("guide_open", {"slug": "break-a-plateau", "source": "menu", "text": "x"}) == {
        "slug": "break-a-plateau", "source": "menu"}
    assert clean("guide_finish", {"slug": "cutting-without-losing-muscle", "slides_seen": 7}) == {
        "slug": "cutting-without-losing-muscle", "slides_seen": 7}
    assert clean("guide_action", {"slug": "creatine", "action": "coach_question"}) == {
        "slug": "creatine", "action": "coach_question"}
    assert clean("guide_action", {"slug": "Привет мир", "action": "free text"}) == {}
    for slug in seo_library.GUIDES:
        assert clean("guide_open", {"slug": slug})["slug"] == slug


# ---------- личное: витрина и чипы ----------


async def _exercise(user_id: int, name: str) -> int:
    group_id = (await db.list_muscle_groups(None, global_only=True))[0]["id"]
    return await db.create_exercise(user_id, name, group_id)


async def _workout(user_id: int, ex_id: int, days_ago: int, weight: float, reps: int = 5,
                   source: str = "manual") -> None:
    day = dt.datetime.now() - dt.timedelta(days=days_ago)
    wid = await db.create_finished_workout(user_id, day.isoformat(), (day + dt.timedelta(hours=1)).isoformat(),
                                           source=source)
    block_id = await db.create_block(wid, "single")
    await db.add_block_exercise(block_id, ex_id, 0)
    for _ in range(3):
        await db.append_set(block_id, ex_id, 0, weight, reps)


def _featured(guides):
    return [g for g in guides if g["featured"]]


@pytest.mark.asyncio
async def test_newbie_gets_the_first_week_guide(fresh_db, user_id):
    await fresh_db.set_user_lang(user_id, "en")
    user = await db.get_user(user_id)
    resp = await G.build_response(user_id, user)
    guides = resp["guides"]
    assert [g["slug"] for g in _featured(guides)] == ["first-week-in-the-gym"]
    assert guides[0]["slug"] == "first-week-in-the-gym"
    assert guides[0]["reason"] == i18n.t_in("en", "guides.newbie.reason")
    assert resp["menu"][0] == "first-week-in-the-gym"
    start = next(s for s in resp["shelves"] if s["key"] == "start")
    assert start["slugs"][0] == "first-week-in-the-gym"


@pytest.mark.asyncio
async def test_imported_history_keeps_the_newbie_guide_but_not_the_claim(fresh_db, user_id):
    ex = await _exercise(user_id, "Bench Press")
    for i in range(5):
        await _workout(user_id, ex, 100 + i * 3, 60 + i * 5, source="import")
    guides = await G.build(user_id, await db.get_user(user_id))
    assert guides[0]["slug"] == "first-week-in-the-gym" and guides[0]["featured"]
    assert guides[0]["reason"] is None


@pytest.mark.asyncio
async def test_growing_athlete_has_no_featured_guide(fresh_db, user_id):
    ex = await _exercise(user_id, "Bench Press")
    for i, days_ago in enumerate((35, 28, 21, 14, 7)):
        await _workout(user_id, ex, days_ago, 80 + i * 5)
    resp = await G.build_response(user_id, await db.get_user(user_id))
    assert not _featured(resp["guides"])
    assert [g["slug"] for g in resp["guides"]] == list(G.ORDER)
    assert all(g["reason"] is None for g in resp["guides"])
    assert resp["menu"] == G.menu(None, False)


@pytest.mark.asyncio
async def test_stalled_lift_features_the_plateau_guide(fresh_db, user_id):
    await fresh_db.set_user_lang(user_id, "ru")
    ex = await _exercise(user_id, "Жим лёжа")
    for days_ago in (42, 35, 28, 21, 14, 7, 3):
        await _workout(user_id, ex, days_ago, 80, reps=5)
    resp = await G.build_response(user_id, await db.get_user(user_id))
    guides = resp["guides"]
    assert [g["slug"] for g in _featured(guides)] == ["break-a-plateau"]
    plateau = guides[0]
    assert plateau["slug"] == "break-a-plateau"
    assert plateau["reason"].startswith("Жим лёжа не растёт 5 недель")
    assert plateau["slides"][1]["chip"] == "Жим лёжа · e1RM 93кг — 5 недель без роста"
    action = plateau["slides"][-1]["action"]
    assert action == {"kind": "exercise_progress", "label": "Открыть прогресс: Жим лёжа",
                      "exercise_id": ex, "question": None}
    assert resp["menu"] == ["break-a-plateau", "progressive-overload", "rest-between-sets", "protein-per-day"]
    # Чип объёма — подходы за последние 7 дней по группам.
    volume = next(g for g in guides if g["slug"] == "sets-per-muscle-per-week")
    assert volume["slides"][1]["chip"].startswith("Подходов за 7 дней: ")


@pytest.mark.asyncio
async def test_short_stall_is_not_a_plateau(fresh_db, user_id):
    ex = await _exercise(user_id, "Bench Press")
    for days_ago in (20, 17, 14, 12):
        await _workout(user_id, ex, days_ago, 80)
    assert await G.stalled_lift(user_id, await db.get_user(user_id)) is None


# ---------- ручка ----------


@pytest.fixture
def client_factory():
    def _make():
        transport = httpx.ASGITransport(app=api_v1.build_app())
        return httpx.AsyncClient(transport=transport, base_url="http://test")

    return _make


@pytest.mark.asyncio
async def test_guides_route_requires_auth(fresh_db, client_factory):
    resp = await client_factory().get("/guides")
    assert resp.status_code == 401


@pytest.mark.asyncio
@pytest.mark.parametrize("lang", ["en", "de"])
async def test_guides_route_returns_the_full_contract(fresh_db, client_factory, lang):
    await fresh_db.get_or_create_user(telegram_id=111, username="tester")
    await fresh_db.set_user_lang(111, lang)
    code = await fresh_db.issue_oauth_link_code(111, ttl_seconds=600, digits=8)
    client = client_factory()
    token = (await client.post("/auth/link", json={"code": code})).json()["token"]
    client.headers["Authorization"] = f"Bearer {token}"
    resp = await client.get("/guides")
    assert resp.status_code == 200
    body = resp.json()
    assert set(body) == {"guides", "menu", "shelves"}
    assert len(body["guides"]) == len(seo_library.GUIDES)
    assert sum(g["featured"] for g in body["guides"]) <= 1
    for g in body["guides"]:
        assert set(g) == GUIDE_KEYS
        assert g["url"].endswith(f"/{lang}/guides/{g['slug']}")
        # Ссылка «поделиться» — без языка: уходит другу, язык решит его браузер.
        assert g["share_url"].startswith("https://") and g["share_url"].endswith(f"/g/{g['slug']}")
        for s in g["slides"]:
            assert set(s) == SLIDE_KEYS
            assert isinstance(s["text"], str) and isinstance(s["items"], list)
        action = g["slides"][-1]["action"]
        assert set(action) == ACTION_KEYS
    assert all(set(s) == {"key", "title", "slugs"} for s in body["shelves"])
    assert not CYRILLIC.search(resp.text)


def test_cover_is_the_guide_drawing_when_the_file_exists(tmp_path, monkeypatch):
    (tmp_path / "creatine.jpg").write_bytes(b"\xff\xd8jpg")
    monkeypatch.setattr(G, "GUIDE_COVER_DIR", str(tmp_path))
    assert G.cover_path("creatine") == "/media/guides/creatine.jpg"
    assert G.cover_path("whey-protein") is None


def test_guide_cover_route_serves_the_file_and_refuses_traversal(tmp_path, monkeypatch):
    import api_v1_media
    from api_v1_common import ApiError

    (tmp_path / "creatine.jpg").write_bytes(b"\xff\xd8jpg")
    resp = api_v1_media._serve_file(str(tmp_path), "creatine.jpg")
    assert resp.path.endswith("creatine.jpg")
    for name in ("../guide_stories.py", "missing.jpg"):
        try:
            api_v1_media._serve_file(str(tmp_path), name)
        except ApiError as e:
            assert e.status_code == 404
        else:
            raise AssertionError(name)
