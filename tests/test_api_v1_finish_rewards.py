"""Итоги завершённой тренировки в ответе POST /v1/workouts/{id}/finish.

По образцу tests/test_api_v1_account.py: httpx поверх ASGI-приложения без
сокета, `client_factory` и `_linked_client` заведены локально — в чужой
тестовый файл не лезем, его может в это же время править другой агент.
"""

import re

import httpx
import pytest

import api_v1


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
    token = resp.json()["token"]
    client.headers["Authorization"] = f"Bearer {token}"
    return client


# Кириллица в ответе — единственный надёжный признак «сервер отдал русский»,
# не зависящий от конкретных формулировок в locales/: сверять с точной строкой
# значило бы ронять тест на каждой правке текста.
_CYRILLIC = re.compile("[а-яёА-ЯЁ]")


def _texts(rewards: dict) -> list[str]:
    values = [rewards["tonnage"], rewards["tonnage_equivalent"], rewards["milestone"]]
    if rewards["rank_promotion"]:
        values.append(rewards["rank_promotion"]["name"])
    for badge in rewards["new_achievements"]:
        values += [badge["name"], badge["description"]]
    return [v for v in values if v]


async def _log_sets(client, workout_id: int, exercise_id: int, count: int):
    for _ in range(count):
        resp = await client.post(
            f"/workouts/{workout_id}/sets",
            json={"exercise_id": exercise_id, "weight": 80, "reps": 5},
        )
        assert resp.status_code in (200, 201), resp.text


@pytest.mark.asyncio
async def test_finish_returns_rewards_matching_what_was_logged(fresh_db, client_factory):
    """Числа в `rewards` — про эту самую тренировку, а не про историю вообще:
    приложение рисует ими карточку итога сразу после кнопки «Завершить»."""
    client = await _linked_client(fresh_db, client_factory)
    bench = (await client.post("/exercises", json={"name": "Жим лёжа"})).json()["id"]
    squat = (await client.post("/exercises", json={"name": "Присед"})).json()["id"]
    workout_id = (await client.post("/workouts/active")).json()["id"]
    await _log_sets(client, workout_id, bench, 3)
    await _log_sets(client, workout_id, squat, 2)

    resp = await client.post(f"/workouts/{workout_id}/finish", json={})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    # Прежние поля тренировки на месте — у ответа finish уже есть потребители.
    assert body["id"] == workout_id
    assert body["status"] == "finished"
    rewards = body["rewards"]
    assert rewards["sets"] == 5
    assert rewards["exercises"] == 2
    assert rewards["tonnage"]
    assert isinstance(rewards["new_achievements"], list)


@pytest.mark.asyncio
async def test_first_workout_ever_reports_new_achievements(fresh_db, client_factory):
    """Тот же путь, что присваивает значки (tests/test_api_v1.py::
    test_finishing_through_the_api_awards_achievements), теперь ещё и
    рассказывает о них: значок, о котором не сказали, человек не заметит."""
    client = await _linked_client(fresh_db, client_factory)
    exercise_id = (await client.post("/exercises", json={"name": "Жим лёжа"})).json()["id"]
    workout_id = (await client.post("/workouts/active")).json()["id"]
    await _log_sets(client, workout_id, exercise_id, 1)

    rewards = (await client.post(f"/workouts/{workout_id}/finish", json={})).json()["rewards"]
    assert rewards["new_achievements"], "первая тренировка не дала ни одного значка"
    for badge in rewards["new_achievements"]:
        assert badge["code"] and badge["name"] and badge["description"]
    # Первая в жизни тренировка — сама по себе милестоун.
    assert rewards["milestone"]


@pytest.mark.asyncio
async def test_backfilled_workout_has_no_milestone(fresh_db, client_factory):
    """Занесение задним числом вносится не по порядку, и «N-я тренировка» по
    нему считала бы не то, что человек подумает, — бот молчит там же."""
    client = await _linked_client(fresh_db, client_factory)
    exercise_id = (await client.post("/exercises", json={"name": "Жим лёжа"})).json()["id"]
    workout_id = (
        await client.post("/workouts/backfill", json={"date": "2024-01-02"})
    ).json()["id"]
    await _log_sets(client, workout_id, exercise_id, 2)

    rewards = (await client.post(f"/workouts/{workout_id}/finish", json={})).json()["rewards"]
    assert rewards["milestone"] is None
    assert rewards["sets"] == 2


@pytest.mark.asyncio
async def test_rewards_speak_the_users_language(fresh_db, client_factory):
    """Без i18n.use_lang язык ответа был бы тем, который первым дёрнул модуль
    в этом процессе (CLAUDE.md), и англоязычный атлет получил бы русский итог."""
    client = await _linked_client(fresh_db, client_factory)
    await client.patch("/settings", json={"lang": "en"})
    exercise_id = (await client.post("/exercises", json={"name": "Bench press"})).json()["id"]
    workout_id = (await client.post("/workouts/active")).json()["id"]
    await _log_sets(client, workout_id, exercise_id, 8)

    rewards = (await client.post(f"/workouts/{workout_id}/finish", json={})).json()["rewards"]
    texts = _texts(rewards)
    assert texts, "в итогах не оказалось ни одной строки — проверять нечего"
    for text in texts:
        assert not _CYRILLIC.search(text), text


@pytest.mark.asyncio
async def test_rewards_texts_carry_no_telegram_markup(fresh_db, client_factory):
    """Часть формулировок в locales/ несёт <b> — оформление карточки бота.
    Приложение рисует текст само и показало бы тег буквами."""
    client = await _linked_client(fresh_db, client_factory)
    exercise_id = (await client.post("/exercises", json={"name": "Жим лёжа"})).json()["id"]
    workout_id = (await client.post("/workouts/active")).json()["id"]
    await _log_sets(client, workout_id, exercise_id, 4)

    rewards = (await client.post(f"/workouts/{workout_id}/finish", json={})).json()["rewards"]
    texts = _texts(rewards)
    assert texts, "в итогах не оказалось ни одной строки — проверять нечего"
    for text in texts:
        assert "<" not in text and "&" not in text, text


# ---------- комментарий AI-тренера ----------


@pytest.mark.asyncio
async def test_ai_comment_requires_auth(fresh_db, client_factory):
    client = client_factory()
    resp = await client.get("/workouts/1/ai-comment")
    assert resp.status_code == 401
    assert resp.json()["error"] == "unauthorized"


@pytest.mark.asyncio
async def test_ai_comment_of_someone_elses_workout_is_not_found(fresh_db, client_factory):
    """404, а не 403: чужая тренировка для этого токена не существует вовсе —
    иначе ответ сам подтверждал бы, что такой id у кого-то есть."""
    owner = await _linked_client(fresh_db, client_factory, telegram_id=111)
    stranger = await _linked_client(fresh_db, client_factory, telegram_id=222)
    workout_id = (await owner.post("/workouts/active")).json()["id"]

    resp = await stranger.get(f"/workouts/{workout_id}/ai-comment")
    assert resp.status_code == 404
    assert resp.json()["error"] == "not_found"


@pytest.mark.asyncio
async def test_ai_comment_is_null_until_it_is_ready(fresh_db, client_factory):
    """Сразу после финиша комментария ещё нет — приложение опросит позже."""
    client = await _linked_client(fresh_db, client_factory)
    exercise_id = (await client.post("/exercises", json={"name": "Жим лёжа"})).json()["id"]
    workout_id = (await client.post("/workouts/active")).json()["id"]
    await _log_sets(client, workout_id, exercise_id, 1)
    await client.post(f"/workouts/{workout_id}/finish", json={})

    resp = await client.get(f"/workouts/{workout_id}/ai-comment")
    assert resp.status_code == 200
    assert resp.json() == {"comment": None}

    await fresh_db.set_workout_ai_comment(workout_id, "Хорошая работа.")
    resp = await client.get(f"/workouts/{workout_id}/ai-comment")
    assert resp.json() == {"comment": "Хорошая работа."}
