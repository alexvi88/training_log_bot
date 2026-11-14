"""Карточка прогресса под ответом тренера (progress_card).

Числа — те же, что тренер видит в отчёте: окно coach_facts.CARD_WINDOW_WEEKS,
e1RM сессии, прирост от первой сессии окна к последней. Цель — расчёт
строки «Цель» тренировки. И только на готовый вопрос «Как мой прогресс?» в
приложении.
"""

import datetime as dt

import pytest

import ai_trainer
import i18n
import progress_card
import timeutil
from tests.test_coach_facts import _group_id, _seed_curl, _session

pytestmark = pytest.mark.asyncio

TODAY = dt.date(2026, 10, 15)


@pytest.fixture(autouse=True)
def _today(monkeypatch):
    monkeypatch.setattr(timeutil, "user_today", lambda user: TODAY)


async def test_card_has_points_change_and_next_target(fresh_db, user_id):
    ex_id = await _seed_curl(fresh_db, user_id)

    card = await progress_card.card_json(user_id)

    assert card["window_weeks"] == 8
    assert card["unit"] == "kg"
    assert card["more"] == 0
    [lift] = card["lifts"]
    assert lift["exercise_id"] == ex_id
    assert lift["name"] == "Сгибание рук в блоке"
    # Окно 8 недель от 10-15 — с 08-20: сессии 08-03 и 08-09 в него не входят.
    assert [p["date"] for p in lift["points"]] == [
        "2026-08-31", "2026-09-14", "2026-09-28", "2026-10-05", "2026-10-12",
    ]
    first, last = lift["points"][0]["e1rm"], lift["points"][-1]["e1rm"]
    assert lift["e1rm"] == last
    assert lift["change_pct"] == round((last - first) / first * 100)
    assert lift["next"] is not None
    assert lift["next"]["weight"] > 0 and lift["next"]["reps"] > 0
    assert lift["next"]["bodyweight"] is False


async def test_hint_toggle_off_hides_the_target(fresh_db, user_id):
    await _seed_curl(fresh_db, user_id)
    await fresh_db.conn().execute(
        "UPDATE users SET progression_hint_enabled = 0 WHERE telegram_id = ?", (user_id,)
    )
    await fresh_db.conn().commit()

    [lift] = (await progress_card.card_json(user_id))["lifts"]

    assert lift["next"] is None


async def test_single_session_has_no_change_and_lifts_are_capped(fresh_db, user_id, monkeypatch):
    monkeypatch.setattr(progress_card, "MAX_LIFTS", 2)
    group = await _group_id(fresh_db, user_id, "Спина")
    for i, name in enumerate(["Тяга А", "Тяга Б", "Тяга В"]):
        ex_id = await fresh_db.create_exercise(user_id, name, group)
        # Чаще делается — выше: у «Тяги А» три сессии, у остальных по одной.
        days = ["2026-10-01", "2026-10-05", "2026-10-10"] if i == 0 else ["2026-10-02"]
        for day in days:
            await _session(fresh_db, user_id, ex_id, day, [(60, 8)])

    card = await progress_card.card_json(user_id)

    assert [lift["name"] for lift in card["lifts"]] == ["Тяга А", "Тяга Б"]
    assert card["more"] == 1
    assert card["lifts"][1]["change_pct"] is None


async def test_no_sets_in_window_means_no_card(fresh_db, user_id):
    assert await progress_card.card_json(user_id) is None


async def test_progress_preset_is_recognised_in_every_language():
    for lang in i18n.SUPPORTED:
        assert ai_trainer.is_progress_preset(i18n.t_in(lang, "ai.screen.preset.progress.question"))
    assert not ai_trainer.is_progress_preset("How's my bench?")
    assert not ai_trainer.is_progress_preset("")


async def test_ask_attaches_the_card_only_to_the_progress_preset(fresh_db, monkeypatch):
    import httpx

    import api_v1
    from tests.test_api_v1_ai import _linked_client

    def client_factory():
        return httpx.AsyncClient(transport=httpx.ASGITransport(app=api_v1.build_app()), base_url="http://test")

    monkeypatch.setattr(ai_trainer, "is_configured", lambda: True)
    seen_app = []

    async def fake_ask(user_id, question, history, **kwargs):
        seen_app.append(ai_trainer.APP_CLIENT.get())
        return "Растёт всё, кроме бицепса."

    monkeypatch.setattr(ai_trainer, "ask", fake_ask)
    client = await _linked_client(fresh_db, client_factory, telegram_id=111)
    await _seed_curl(fresh_db, 111)

    preset = i18n.t_in("ru", "ai.screen.preset.progress.question")
    body = (await client.post("/ai/ask", json={"question": preset})).json()
    assert body["progress"]["lifts"][0]["name"] == "Сгибание рук в блоке"

    other = (await client.post("/ai/ask", json={"question": "Сколько белка есть?"})).json()
    assert other["progress"] is None
    assert seen_app == [True, True]


async def test_app_progress_preset_tells_the_coach_to_keep_it_short():
    """Под ответом в приложении — таблица: тренеру в хвост вопроса едет строка
    «не перечисляй цифры». В боте и в чате «программа атлету» таблицы нет."""
    preset = i18n.t_in("en", "ai.screen.preset.progress.question")
    token = ai_trainer.APP_CLIENT.set(True)
    try:
        assert ai_trainer._progress_card_line(preset) == ai_trainer.APP_PROGRESS_CARD_NOTE
        assert ai_trainer._progress_card_line(preset, for_client=True) == ""
        assert ai_trainer._progress_card_line("How much protein?") == ""
    finally:
        ai_trainer.APP_CLIENT.reset(token)
    assert ai_trainer._progress_card_line(preset) == ""
