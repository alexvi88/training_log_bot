"""POST /v1/ai/conversations/workout — «Обсудить с тренером» под разбором.

Что держат эти тесты:

- новый разговор начинается с разбора: реплика «Обсудим тренировку …» и
  ответ тренера — сам комментарий, прошлый разговор уехал в архив;
- следующий вопрос уходит модели с карточкой тренировки и разбором в
  истории — иначе «а насколько сбросить?» тренер не понял бы;
- модель при этом не зовётся;
- чужая тренировка — 404, без комментария — 409.
"""

import httpx
import pytest

import ai_trainer
import api_v1


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=api_v1.build_app()), base_url="http://test")


async def _linked_client(fresh_db, telegram_id=111, lang="ru"):
    await fresh_db.get_or_create_user(telegram_id=telegram_id, username="tester")
    await fresh_db.set_user_lang(telegram_id, lang)
    code = await fresh_db.issue_oauth_link_code(telegram_id, ttl_seconds=600, digits=8)
    client = _client()
    resp = await client.post("/auth/link", json={"code": code})
    assert resp.status_code == 200, resp.text
    client.headers["Authorization"] = f"Bearer {resp.json()['token']}"
    return client


async def _workout(client) -> int:
    exercise_id = (await client.post("/exercises", json={"name": "Жим лёжа"})).json()["id"]
    workout_id = (await client.post("/workouts/active")).json()["id"]
    resp = await client.post(
        f"/workouts/{workout_id}/sets", json={"exercise_id": exercise_id, "weight": 80, "reps": 5}
    )
    assert resp.status_code in (200, 201), resp.text
    return workout_id


@pytest.mark.asyncio
async def test_discuss_starts_conversation_with_the_comment(fresh_db, monkeypatch):
    seen: list[list] = []

    async def fake_ask(user_id, question, history, on_wire=None, **kwargs):
        seen.append(history)
        answer = f"ответ на: {question}"
        if on_wire is not None:
            await on_wire(history + [{"role": "user", "content": question}, {"role": "assistant", "content": answer}])
        return answer

    monkeypatch.setattr(ai_trainer, "is_configured", lambda: True)
    monkeypatch.setattr(ai_trainer, "ask", fake_ask)
    client = await _linked_client(fresh_db)
    await client.patch("/settings", json={"ai_consent": True})
    await client.post("/ai/ask", json={"question": "старый вопрос"})
    workout_id = await _workout(client)
    await fresh_db.set_workout_ai_comment(workout_id, "Жим зашёл ровно.")
    calls_before = len(seen)

    resp = await client.post("/ai/conversations/workout", json={"workout_id": workout_id})
    assert resp.status_code == 200, resp.text
    assert resp.json()["conversation_id"] == 2

    messages = (await client.get("/ai/history")).json()["messages"]
    texts = [m["text"] for m in messages]
    assert texts[0].startswith("Обсудим тренировку ")
    assert texts[1] == "Жим зашёл ровно."
    # Старый разговор цел — в архиве.
    listing = (await client.get("/ai/conversations")).json()
    assert [c["title"] for c in listing["conversations"]][-1] == "старый вопрос"
    assert len(seen) == calls_before, "обсуждение не должно звать модель"

    await client.post("/ai/ask", json={"question": "а насколько сбросить?"})
    history = seen[-1]
    assert [m["role"] for m in history] == ["user", "assistant"]
    assert "Жим лёжа" in history[0]["content"], "модель не видит подходов тренировки"
    assert history[1]["content"] == "Жим зашёл ровно."


@pytest.mark.asyncio
async def test_discuss_without_comment_is_409(fresh_db):
    client = await _linked_client(fresh_db)
    workout_id = await _workout(client)
    resp = await client.post("/ai/conversations/workout", json={"workout_id": workout_id})
    assert resp.status_code == 409
    assert resp.json()["error"] == "no_ai_comment"


@pytest.mark.asyncio
async def test_discuss_foreign_workout_is_404(fresh_db):
    owner = await _linked_client(fresh_db, telegram_id=111)
    workout_id = await _workout(owner)
    await fresh_db.set_workout_ai_comment(workout_id, "Чужой разбор.")
    stranger = await _linked_client(fresh_db, telegram_id=222)
    resp = await stranger.post("/ai/conversations/workout", json={"workout_id": workout_id})
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_discuss_question_follows_account_language(fresh_db):
    client = await _linked_client(fresh_db, lang="en")
    workout_id = await _workout(client)
    await fresh_db.set_workout_ai_comment(workout_id, "Solid bench.")
    assert (await client.post("/ai/conversations/workout", json={"workout_id": workout_id})).status_code == 200
    first = (await client.get("/ai/history")).json()["messages"][0]["text"]
    assert first.startswith("Let's talk about my ")
    assert first.endswith(" workout")
