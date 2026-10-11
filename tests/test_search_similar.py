"""Опечатка в поиске упражнения — «Похоже, ты про:», а не «ничего не нашёл».

«присидания» вместо «приседания» раньше давали пустую выдачу, и человек тут же
заводил «новое» упражнение — дубль каталожного приседа. Обычный поиск пуст —
похожие (db.similar_exercise_matches) показываются как предложение, а
«Создать» остаётся ниже, последним выходом. То же в /v1 (поле `similar`) и
в подборе бота.
"""

import httpx
import pytest

import api_v1
import db
import i18n
import search_terms
from handlers import workout
from tests.test_workout_picker import _make_message, _make_state

# ---------- search_terms.typo_score ----------

def test_typo_score_matches_one_typo_and_word_forms():
    assert search_terms.typo_score("присидания", "Присед со штангой") > 0
    assert search_terms.typo_score("присидания", "Приседания без веса") > 0
    assert search_terms.typo_score("жим лажа", "Жим штанги лёжа") > 0
    assert search_terms.typo_score("sqaut", "Barbell Squat") > 0
    # ё и регистр — не опечатка.
    assert search_terms.typo_score("ЖИМ ЛЁЖА", "жим лежа") == 1.0


def test_typo_score_rejects_other_words():
    # Три буквы — только точное начало слова: «жир» не «жим».
    assert search_terms.typo_score("жир", "Жим лёжа") == 0
    # Первая буква другая — другое слово, даже если остальное сошлось.
    assert search_terms.typo_score("йога", "Жим ногами") == 0
    assert search_terms.typo_score("банан", "Жим лёжа") == 0
    # Каждое значимое слово обязано найтись: «Ларсена» в «Жиме лёжа» нет.
    assert search_terms.typo_score("жим ларсена", "Жим лёжа") == 0
    # Пустой запрос и одни предлоги ничего не предлагают.
    assert search_terms.typo_score("", "Жим лёжа") == 0
    assert search_terms.typo_score("со", "Присед со штангой") == 0


# ---------- db ----------

async def test_typo_offers_catalog_squat(fresh_db, user_id):
    own, templates, similar = await db.search_with_similar(user_id, "присидания")
    assert similar is True
    assert own == []
    names = [t["display_name"] for t in templates]
    assert "Присед со штангой" in names
    assert len(names) <= db.SIMILAR_LIMIT


async def test_exact_search_is_not_marked_similar(fresh_db, user_id):
    own, templates, similar = await db.search_with_similar(user_id, "приседания")
    assert similar is False
    assert templates


async def test_nothing_similar_stays_empty(fresh_db, user_id):
    own, templates, similar = await db.search_with_similar(user_id, "zxqvnonexistent")
    assert (own, templates, similar) == ([], [], False)


async def test_own_exercise_with_typo_comes_first(fresh_db, user_id):
    group_id = await db.create_muscle_group(user_id, "Спина")
    ex_id = await db.create_exercise(user_id, "Тяга Пендлея", group_id)
    own, templates, similar = await db.search_with_similar(user_id, "тяга пендлэя")
    assert similar is True
    assert [r["id"] for r in own] == [ex_id]


async def test_owned_template_identity_is_not_offered_twice(fresh_db, user_id):
    """Форк каталожного приседа уже свой — похожий шаблон с той же
    идентичностью второй строкой не идёт, только своё."""
    tpl = (await db.search_exercise_templates(user_id, "Присед со штангой", limit=1))[0]
    ex_id = await db.fork_exercise_from_template(user_id, tpl["id"])
    own, templates, similar = await db.search_with_similar(user_id, "присидания со штангой")
    assert similar is True
    assert ex_id in [r["id"] for r in own]
    assert tpl["id"] not in [t["id"] for t in templates]


# ---------- /v1 ----------

@pytest.fixture
def client_factory():
    def _make():
        transport = httpx.ASGITransport(app=api_v1.build_app())
        return httpx.AsyncClient(transport=transport, base_url="http://test")

    return _make


async def _linked_client(fresh_db, client_factory, telegram_id=111):
    await fresh_db.get_or_create_user(telegram_id=telegram_id, username="tester")
    code = await fresh_db.issue_oauth_link_code(telegram_id, ttl_seconds=600, digits=8)
    client = client_factory()
    resp = await client.post("/auth/link", json={"code": code})
    assert resp.status_code == 200, resp.text
    client.headers["Authorization"] = f"Bearer {resp.json()['token']}"
    return client


async def test_v1_templates_return_similar_on_typo(fresh_db, client_factory):
    client = await _linked_client(fresh_db, client_factory)
    resp = await client.get("/exercise-templates", params={"query": "присидания", "limit": 200})
    assert resp.status_code == 200
    body = resp.json()
    assert body and all(item["similar"] is True for item in body)
    assert "Присед со штангой" in [item["name"] for item in body]
    # Своих нет — свою половину отдаёт пустой.
    resp = await client.get("/exercises", params={"query": "присидания"})
    assert resp.status_code == 200
    assert resp.json() == []


async def test_v1_exact_search_has_no_similar_field(fresh_db, client_factory):
    client = await _linked_client(fresh_db, client_factory)
    resp = await client.get("/exercise-templates", params={"query": "приседания"})
    assert resp.status_code == 200
    assert resp.json()
    assert all("similar" not in item for item in resp.json())


async def test_v1_own_similar_and_no_catalog_noise(fresh_db, client_factory):
    client = await _linked_client(fresh_db, client_factory)
    group_id = await fresh_db.create_muscle_group(111, "Спина")
    ex_id = await fresh_db.create_exercise(111, "Тяга Пендлея", group_id)
    resp = await client.get("/exercises", params={"query": "тяга пендлэя"})
    assert resp.status_code == 200
    assert [(item["id"], item["similar"]) for item in resp.json()] == [(ex_id, True)]


async def test_v1_similar_names_follow_athlete_language(fresh_db, client_factory):
    client = await _linked_client(fresh_db, client_factory)
    await fresh_db.set_user_lang(111, "en")
    resp = await client.get("/exercise-templates", params={"query": "sqaut", "limit": 200})
    assert resp.status_code == 200
    body = resp.json()
    assert body and all(item["similar"] is True for item in body)
    for item in body:
        assert not any("а" <= ch.lower() <= "я" for ch in item["name"]), item["name"]


async def test_v1_exact_hit_in_other_half_blocks_similar(fresh_db, client_factory):
    """Свой нашёлся точно — каталог не подмешивает похожие: иначе рядом с
    найденным встали бы «ты про это?» на совсем другие движения."""
    client = await _linked_client(fresh_db, client_factory)
    group_id = await fresh_db.create_muscle_group(111, "Спина")
    await fresh_db.create_exercise(111, "Присидания у стенки", group_id)
    resp = await client.get("/exercise-templates", params={"query": "присидания"})
    assert resp.status_code == 200
    assert resp.json() == []


# ---------- бот: подбор в живой тренировке ----------

async def test_bot_picker_offers_similar_above_create(fresh_db, user_id):
    state = await _make_state(user_id)
    message = _make_message(user_id, "присидания")

    await workout.pick_exercise_search(message, state)

    kwargs = message.bot.send_message.await_args.kwargs
    assert "Похоже, ты про:" in kwargs["text"]
    assert "Ничего не нашлось" not in kwargs["text"]
    rows = kwargs["reply_markup"].inline_keyboard
    datas = [b.callback_data for row in rows for b in row]
    create_at = datas.index("pick:newquery")
    template_at = next(i for i, d in enumerate(datas) if d.startswith("pick:tpl"))
    # Похожие — выше, «Создать «присидания»» — под ними.
    assert template_at < create_at
    create = next(b for row in rows for b in row if b.callback_data == "pick:newquery")
    assert "присидания" in create.text


async def test_bot_picker_speaks_english_for_similar(fresh_db, user_id):
    await db.set_user_lang(user_id, "en")
    state = await _make_state(user_id)
    message = _make_message(user_id, "sqaut")

    # Язык ставит middleware по users.lang; в обход апдейта — руками.
    with i18n.use_lang("en"):
        await workout.pick_exercise_search(message, state)

    text = message.bot.send_message.await_args.kwargs["text"]
    assert "Did you mean:" in text
