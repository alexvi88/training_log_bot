"""Пол атлета: users.gender — 'male' | 'female' | NULL (не выбран).

Пишет его только сам человек (кнопки бота, PATCH /v1/settings) через
db.set_user_gender. Тренер знает пол, только когда он выбран, и никогда не
угадывает: без выбора в запрос не уходит ни строки о поле.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from luna_fakes import as_luna

import account_deletion
import ai_trainer
import api_v1
import i18n
import keyboards


def _client():
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=api_v1.build_app()), base_url="http://test")


async def _linked_client(fresh_db, telegram_id=111, lang="ru"):
    await fresh_db.get_or_create_user(telegram_id=telegram_id, username="tester", language_code=lang)
    code = await fresh_db.issue_oauth_link_code(telegram_id, ttl_seconds=600, digits=8)
    client = _client()
    resp = await client.post("/auth/link", json={"code": code})
    assert resp.status_code == 200, resp.text
    client.headers["Authorization"] = f"Bearer {resp.json()['token']}"
    return client


def _fake_model(content="ок"):
    message = SimpleNamespace(content=content, tool_calls=None)
    response = SimpleNamespace(choices=[SimpleNamespace(message=message)])
    return SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=AsyncMock(return_value=response)))
    )


# ---------- база ----------


async def test_new_user_has_no_gender(fresh_db, user_id):
    assert (await fresh_db.get_user(user_id))["gender"] is None


async def test_set_and_clear_gender(fresh_db, user_id):
    await fresh_db.set_user_gender(user_id, "female")
    assert (await fresh_db.get_user(user_id))["gender"] == "female"
    await fresh_db.set_user_gender(user_id, "male")
    assert (await fresh_db.get_user(user_id))["gender"] == "male"
    await fresh_db.set_user_gender(user_id, None)
    assert (await fresh_db.get_user(user_id))["gender"] is None


@pytest.mark.parametrize("bad", ["", "Female", "other", "m"])
async def test_set_gender_rejects_anything_else(fresh_db, user_id, bad):
    await fresh_db.set_user_gender(user_id, "male")
    with pytest.raises(ValueError):
        await fresh_db.set_user_gender(user_id, bad)
    assert (await fresh_db.get_user(user_id))["gender"] == "male"


async def test_account_deletion_takes_gender_with_the_users_row(fresh_db, user_id):
    await fresh_db.set_user_gender(user_id, "female")
    left = await account_deletion.delete_account(user_id)
    assert not left
    assert await fresh_db.get_user(user_id) is None


# ---------- REST ----------


async def test_settings_expose_null_gender_by_default(fresh_db):
    client = await _linked_client(fresh_db)
    body = (await client.get("/settings")).json()
    assert "gender" in body and body["gender"] is None


async def test_patch_sets_and_clears_gender(fresh_db):
    client = await _linked_client(fresh_db)
    resp = await client.patch("/settings", json={"gender": "female"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["gender"] == "female"
    assert (await client.get("/settings")).json()["gender"] == "female"

    # Другое поле — пол не трогает.
    resp = await client.patch("/settings", json={"unit": "kg"})
    assert resp.json()["gender"] == "female"

    resp = await client.patch("/settings", json={"gender": None})
    assert resp.status_code == 200
    assert resp.json()["gender"] is None
    assert (await fresh_db.get_user(111))["gender"] is None


@pytest.mark.parametrize("bad", ["other", "Male", "", 1, True, ["male"]])
async def test_patch_rejects_invalid_gender_and_writes_nothing(fresh_db, bad):
    client = await _linked_client(fresh_db)
    await fresh_db.set_user_gender(111, "male")
    resp = await client.patch("/settings", json={"gender": bad, "e1rm_formula": "brzycki"})
    assert resp.status_code == 400
    body = resp.json()
    assert body["error"] == "invalid_gender"
    assert body["message"] == i18n.t_in("ru", "api.error.invalid_gender")
    user = await fresh_db.get_user(111)
    assert user["gender"] == "male"
    assert user["e1rm_formula"] == "epley"


async def test_invalid_gender_error_speaks_english_to_english_athlete(fresh_db):
    client = await _linked_client(fresh_db, lang="en")
    await fresh_db.set_user_lang(111, "en")
    resp = await client.patch("/settings", json={"gender": "x"})
    assert resp.status_code == 400
    assert resp.json()["message"] == i18n.t_in("en", "api.error.invalid_gender")


async def test_profile_shows_gender_and_clear_keeps_it(fresh_db):
    """«Что тренер про тебя знает» в приложении: пол — та же настройка, видна
    и тут, а «Очистить» память тренера её не стирает."""
    client = await _linked_client(fresh_db)
    assert (await client.get("/profile")).json()["gender"] is None
    await client.patch("/settings", json={"gender": "male"})
    assert (await client.get("/profile")).json()["gender"] == "male"
    resp = await client.delete("/profile")
    assert resp.json()["gender"] == "male"


async def test_app_gender_survives_linking_telegram(fresh_db):
    app = await fresh_db.create_app_only_user(language_code="en", tz_offset=0)
    await fresh_db.set_user_gender(app["telegram_id"], "female")
    await fresh_db.get_or_create_user(777, "tg", "ru")
    assert await fresh_db.link_telegram_to_app_account(app["telegram_id"], 777, "tg") == "ok"
    assert (await fresh_db.get_user(777))["gender"] == "female"


# ---------- тренер ----------


async def test_gender_line_only_when_set(fresh_db, user_id):
    assert ai_trainer._gender_line(await fresh_db.get_user(user_id)) == ""
    await fresh_db.set_user_gender(user_id, "female")
    assert ai_trainer._gender_line(await fresh_db.get_user(user_id)) == "Пол атлета: женский."
    await fresh_db.set_user_gender(user_id, "male")
    assert ai_trainer._gender_line(await fresh_db.get_user(user_id)) == "Пол атлета: мужской."


async def test_overview_profile_has_gender_only_when_set(fresh_db, user_id):
    overview = await ai_trainer._training_overview(user_id)
    assert "gender" not in overview["profile"]
    await fresh_db.set_user_gender(user_id, "female")
    overview = await ai_trainer._training_overview(user_id)
    assert overview["profile"]["gender"] == "female"


async def test_question_carries_gender_when_set(fresh_db, user_id, monkeypatch):
    await fresh_db.set_user_gender(user_id, "female")
    client = _fake_model()
    monkeypatch.setattr(ai_trainer, "_get_client", lambda: as_luna(client))

    await ai_trainer._ask_plain(user_id, "Сколько белка мне есть?", history=[])

    messages = client.chat.completions.create.await_args_list[-1].kwargs["messages"]
    today = await ai_trainer._user_today(user_id)
    assert messages[-1]["content"] == (
        f"Сколько белка мне есть?\n\nСегодня {today.isoformat()}.\n{ai_trainer._CLIENT_LINE_TELEGRAM}\nПол атлета: женский."
    )
    # Шапка от пола не зависит — общий кэшируемый префикс у всех один.
    assert messages[0]["content"] == ai_trainer._system_prompt()


async def test_question_stays_neutral_when_gender_not_set(fresh_db, user_id, monkeypatch):
    client = _fake_model()
    monkeypatch.setattr(ai_trainer, "_get_client", lambda: as_luna(client))

    await ai_trainer._ask_plain(user_id, "Сколько белка мне есть?", history=[])

    messages = client.chat.completions.create.await_args_list[-1].kwargs["messages"]
    assert all("Пол атлета:" not in str(m.get("content")) for m in messages[1:])


async def test_workout_comment_carries_gender_only_when_set(fresh_db, user_id, monkeypatch):
    workout_id = await fresh_db.create_workout(user_id)
    await fresh_db.finish_workout(workout_id)

    client = _fake_model("Хорошо.")
    monkeypatch.setattr(ai_trainer, "_get_client", lambda: as_luna(client))
    await ai_trainer.comment_on_workout(user_id, workout_id)
    user_msg = client.chat.completions.create.await_args.kwargs["messages"][-1]["content"]
    assert "Пол атлета" not in user_msg

    await fresh_db.set_user_gender(user_id, "male")
    client = _fake_model("Хорошо.")
    monkeypatch.setattr(ai_trainer, "_get_client", lambda: as_luna(client))
    await ai_trainer.comment_on_workout(user_id, workout_id)
    user_msg = client.chat.completions.create.await_args.kwargs["messages"][-1]["content"]
    assert user_msg.endswith("Пол атлета: мужской.")


def test_prompt_forbids_guessing_gender():
    prompt = ai_trainer.SYSTEM_PROMPT
    assert "ПОЛ АТЛЕТА" in prompt
    assert "НИКОГДА не угадывай его по имени" in prompt
    # Пол — не память тренера: инструмент записи его не знает.
    tool = next(t for t in ai_trainer.TOOLS if t["function"]["name"] == "save_athlete_profile")
    assert "gender" not in str(tool["function"]["parameters"])
    for side_prompt in (ai_trainer.WORKOUT_COMMENT_SYSTEM_PROMPT, ai_trainer.WEEKLY_DIGEST_SYSTEM_PROMPT):
        assert "не угадывай пол" in side_prompt


# ---------- бот ----------


@pytest.mark.parametrize("lang", ["ru", "en"])
def test_gender_picker_labels_in_both_languages(lang):
    with i18n.use_lang(lang):
        kb = keyboards.gender_keyboard("female", "menu")
    rows = [[(b.text, b.callback_data) for b in row] for row in kb.inline_keyboard]
    assert rows[0][0] == (i18n.t_in(lang, "settings.gender.value.male"), "settings:gset:male:menu")
    assert rows[1][0] == (
        "✓ " + i18n.t_in(lang, "settings.gender.value.female"), "settings:gset:female:menu"
    )
    assert rows[2][0] == (i18n.t_in(lang, "settings.gender.value.none"), "settings:gset:none:menu")
    assert rows[3][0][1] == "settings:menu"


def test_settings_button_shows_current_gender():
    with i18n.use_lang("ru"):
        assert keyboards.gender_button_text(None) == "Пол: Не выбран"
        assert keyboards.gender_button_text("male") == "Пол: Мужской"
    with i18n.use_lang("en"):
        assert keyboards.gender_button_text("female") == "Gender: Female"


async def test_bot_buttons_write_through_the_db(fresh_db, user_id, monkeypatch):
    from aiogram.fsm.context import FSMContext
    from aiogram.fsm.storage.base import StorageKey
    from aiogram.fsm.storage.memory import MemoryStorage

    import ui
    from handlers import settings

    seen = {}

    async def fake_edit(callback, text, **kwargs):
        seen["text"] = text
        seen["kb"] = kwargs.get("reply_markup")

    monkeypatch.setattr(ui, "safe_edit", fake_edit)
    state = FSMContext(storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=user_id, user_id=user_id))

    def cb(data):
        return SimpleNamespace(
            data=data, from_user=SimpleNamespace(id=user_id), answer=AsyncMock(), message=None
        )

    await settings.settings_gender_set(cb("settings:gset:female:profile"), state)
    assert (await fresh_db.get_user(user_id))["gender"] == "female"
    # Вернулись на «Что я про тебя знаю», и пол там виден.
    assert "<b>Пол:</b> Женский" in seen["text"]

    await settings.settings_gender_set(cb("settings:gset:none:menu"), state)
    assert (await fresh_db.get_user(user_id))["gender"] is None

    await settings.settings_gender_set(cb("settings:gset:bogus:menu"), state)
    assert (await fresh_db.get_user(user_id))["gender"] is None
