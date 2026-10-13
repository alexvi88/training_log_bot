"""Сервис для тренеров (api_v1_coach.py, coach_service.py): связка по
согласию атлета, что тренер видит и чего не видит никогда, назначение
программы, комментарий с пушем, AI-сводка с подменённой моделью и снос
аккаунта любой из сторон."""

from __future__ import annotations

import datetime as dt
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

import ai_limits
import ai_trainer
import api_v1
import apns
import coach_service
import config
import db
import timeutil

pytestmark = pytest.mark.asyncio

COACH = 501
CLIENT = 502
STRANGER = 503


@pytest.fixture
def pushes(monkeypatch):
    monkeypatch.setattr(apns, "is_configured", lambda: True)
    send = AsyncMock(return_value=True)
    monkeypatch.setattr(apns, "send_alert", send)
    return send


async def _client(fresh_db, telegram_id: int, lang: str = "ru") -> httpx.AsyncClient:
    await fresh_db.get_or_create_user(telegram_id=telegram_id, username=f"user{telegram_id}")
    await fresh_db.set_user_lang(telegram_id, lang)
    token = await fresh_db.issue_api_token(telegram_id)
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=api_v1.build_app()), base_url="http://test")
    client.headers["Authorization"] = f"Bearer {token}"
    return client


async def _linked(fresh_db, **accept):
    coach = await _client(fresh_db, COACH)
    athlete = await _client(fresh_db, CLIENT)
    state = (await coach.post("/coach", json={"display_name": "  Иван   Петров "})).json()
    assert state["is_coach"] is True and state["display_name"] == "Иван Петров"
    code = state["invite"]["code"]
    body = {"name": "Маша", **accept}
    resp = await athlete.post(f"/coach/invites/{code}/accept", json=body)
    assert resp.status_code == 201, resp.text
    return coach, athlete, code


async def _finished_workout(api: httpx.AsyncClient, exercise_id: int, weight: float, *, date=None, routine_id=None):
    body = {"routine_id": routine_id} if routine_id is not None else {}
    wid = (await api.post("/workouts/active", json=body)).json()["id"]
    resp = await api.post(f"/workouts/{wid}/sets", json={"exercise_id": exercise_id, "weight": weight, "reps": 5})
    assert resp.status_code == 201, resp.text
    assert (await api.post(f"/workouts/{wid}/finish", json={})).status_code == 200
    if date is not None:
        assert (await api.patch(f"/workouts/{wid}/date", json={"date": date})).status_code == 200
    return wid


async def _exercise(api: httpx.AsyncClient, name: str) -> int:
    resp = await api.post("/exercises", json={"name": name})
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


# ---------- связка и согласие ----------


async def test_invite_preview_shows_coach_and_what_is_visible_before_consent(fresh_db):
    coach = await _client(fresh_db, COACH)
    athlete = await _client(fresh_db, CLIENT)
    code = (await coach.post("/coach", json={"display_name": "Иван"})).json()["invite"]["code"]

    preview = (await athlete.get(f"/coach/invites/{code.lower()}")).json()
    assert preview["coach_name"] == "Иван"
    assert preview["already_linked"] is False and preview["is_own"] is False
    assert preview["visibility"] == {"bodyweight": False}
    assert "ai_chats" in coach_service.NEVER_VISIBLE and "workouts" in coach_service.VISIBLE_ALWAYS
    # Превью ничего не открывает: тренер атлета ещё не видит.
    assert (await coach.get("/coach/clients")).json()["clients"] == []
    assert (await coach.get(f"/coach/clients/{CLIENT}")).status_code == 404


async def test_accept_links_with_defaults_and_errors(fresh_db):
    coach, athlete, code = await _linked(fresh_db)

    clients = (await coach.get("/coach/clients")).json()["clients"]
    assert [c["name"] for c in clients] == ["Маша"]
    assert clients[0]["visibility"] == {"bodyweight": False}
    assert set(clients[0]["signals"]) >= {
        "workouts_this_week", "days_since_last_workout", "missing", "new_records", "behind_program",
    }

    again = await athlete.post(f"/coach/invites/{code}/accept", json={"name": "Маша"})
    assert again.status_code == 409 and again.json()["error"] == "coach_already_linked"
    own = await coach.post(f"/coach/invites/{code}/accept", json={"name": "Я"})
    assert own.status_code == 400 and own.json()["error"] == "own_invite"
    bad = await athlete.get("/coach/invites/NOPE2345")
    assert bad.status_code == 404 and "кода" in bad.json()["message"]
    no_name = await (await _client(fresh_db, STRANGER)).post(f"/coach/invites/{code}/accept", json={"name": " "})
    assert no_name.status_code == 400

    mine = (await athlete.get("/coach-links")).json()["coaches"]
    assert mine[0]["coach_name"] == "Иван Петров" and mine[0]["my_name"] == "Маша"


async def test_renewed_code_stops_opening_but_keeps_existing_clients(fresh_db):
    coach, athlete, code = await _linked(fresh_db)
    renewed = (await coach.post("/coach/invite")).json()
    assert renewed["code"] != code and renewed["app_link"].endswith(renewed["code"])
    assert (await athlete.get(f"/coach/invites/{code}")).status_code == 404
    assert (await coach.get(f"/coach/clients/{CLIENT}")).status_code == 200


async def test_not_a_coach_gets_403_with_human_text(fresh_db):
    stranger = await _client(fresh_db, STRANGER)
    resp = await stranger.get("/coach/clients")
    assert resp.status_code == 403 and resp.json()["error"] == "not_a_coach"
    assert "Я тренер" in resp.json()["message"]
    state = (await stranger.get("/coach")).json()
    assert state == {"is_coach": False, "display_name": None, "invite": None, "client_count": 0}


# ---------- что видно тренеру ----------


async def test_non_linked_coach_sees_nothing(fresh_db):
    coach, athlete, _ = await _linked(fresh_db)
    other_coach = await _client(fresh_db, STRANGER)
    await other_coach.post("/coach", json={"display_name": "Чужой"})
    ex = await _exercise(athlete, "Жим")
    wid = await _finished_workout(athlete, ex, 60)
    for path in (
        f"/coach/clients/{CLIENT}", f"/coach/clients/{CLIENT}/workouts",
        f"/coach/clients/{CLIENT}/workouts/{wid}", f"/coach/clients/{CLIENT}/bodyweight",
    ):
        resp = await other_coach.get(path)
        assert resp.status_code == 404, path
        assert resp.json()["error"] == "not_linked"
    resp = await other_coach.post(f"/coach/clients/{CLIENT}/workouts/{wid}/comments", json={"text": "x"})
    assert resp.status_code == 404
    # И сам атлет не может подглядеть в чужого «клиента» через свой id.
    assert (await athlete.get(f"/coach/clients/{COACH}")).status_code == 404


async def test_bodyweight_needs_the_athletes_toggle(fresh_db):
    coach, athlete, _ = await _linked(fresh_db)
    await athlete.post("/bodyweight", json={"weight": 70.5})

    resp = await coach.get(f"/coach/clients/{CLIENT}/bodyweight")
    assert resp.status_code == 403 and resp.json()["error"] == "coach_hidden"

    patched = await athlete.patch(f"/coach-links/{COACH}", json={"show_bodyweight": True})
    assert patched.status_code == 200 and patched.json()["visibility"] == {"bodyweight": True}
    weights = (await coach.get(f"/coach/clients/{CLIENT}/bodyweight")).json()
    assert [e["weight"] for e in weights["entries"]] == [70.5] and weights["unit"] == "kg"

    await athlete.patch(f"/coach-links/{COACH}", json={"show_bodyweight": False})
    assert (await coach.get(f"/coach/clients/{CLIENT}/bodyweight")).status_code == 403
    assert (await athlete.patch(f"/coach-links/{COACH}", json={"show_bodyweight": "yes"})).status_code == 400


async def test_food_diary_is_not_part_of_the_coach_service(fresh_db):
    """Дневник питания тренеру не открывается ничем: ни ручки, ни флага.
    Тело от сборки первой версии (`show_food`) не ломает запрос — поле молча
    пропускается."""
    coach, athlete, _ = await _linked(fresh_db, show_bodyweight=True, show_food=True)
    await athlete.post("/food", json={"name": "Гречка", "kcal": 300})
    patched = await athlete.patch(f"/coach-links/{COACH}", json={"show_food": True})
    assert patched.status_code == 200 and patched.json()["visibility"] == {"bodyweight": True}
    assert not (await fresh_db.get_coach_link(COACH, CLIENT))["show_food"]
    assert (await coach.get(f"/coach/clients/{CLIENT}/food")).status_code == 404
    assert "Гречка" not in (await coach.get(f"/coach/clients/{CLIENT}")).text
    assert "food" not in coach_service.VISIBLE_OPT_IN


async def test_ai_chats_and_ai_comments_never_reach_the_coach(fresh_db):
    coach, athlete, _ = await _linked(fresh_db, show_bodyweight=True)
    ex = await _exercise(athlete, "Жим")
    wid = await _finished_workout(athlete, ex, 60)
    secret_answer = "СЕКРЕТНЫЙ-ОТВЕТ-AI"
    await fresh_db.set_workout_ai_comment(wid, "СЕКРЕТНЫЙ-РАЗБОР-AI")
    await fresh_db.add_ai_conversation_turn(CLIENT, "СЕКРЕТНЫЙ-ВОПРОС-AI", secret_answer, [])

    bodies = []
    for path in (
        "/coach/clients", f"/coach/clients/{CLIENT}", f"/coach/clients/{CLIENT}/workouts",
        f"/coach/clients/{CLIENT}/workouts/{wid}", f"/coach/clients/{CLIENT}/bodyweight",
    ):
        resp = await coach.get(path)
        assert resp.status_code == 200, path
        bodies.append(resp.text)
    joined = "\n".join(bodies)
    assert "СЕКРЕТНЫЙ" not in joined
    assert "user502" not in joined  # ник Telegram атлета
    assert "ai_comment" not in joined


async def test_revoke_by_athlete_and_removal_by_coach_end_access(fresh_db):
    coach, athlete, code = await _linked(fresh_db)
    assert (await athlete.delete(f"/coach-links/{COACH}")).json() == {"revoked": True}
    resp = await coach.get(f"/coach/clients/{CLIENT}")
    assert resp.status_code == 404 and resp.json()["error"] == "not_linked"
    assert (await athlete.delete(f"/coach-links/{COACH}")).status_code == 404

    assert (await athlete.post(f"/coach/invites/{code}/accept", json={"name": "Маша"})).status_code == 201
    assert (await coach.delete(f"/coach/clients/{CLIENT}")).json() == {"removed": True}
    assert (await athlete.get("/coach-links")).json()["coaches"] == []


async def test_workouts_signals_and_records(fresh_db):
    coach, athlete, _ = await _linked(fresh_db)
    ex = await _exercise(athlete, "Жим")
    old_day = (dt.date.today() - dt.timedelta(days=20)).isoformat()
    await _finished_workout(athlete, ex, 60, date=old_day)
    wid = await _finished_workout(athlete, ex, 80)

    workouts = (await coach.get(f"/coach/clients/{CLIENT}/workouts")).json()["workouts"]
    assert [w["id"] for w in workouts][0] == wid
    assert workouts[0]["exercise_names"] == ["Жим"] and workouts[0]["coach_comment_count"] == 0

    detail = (await coach.get(f"/coach/clients/{CLIENT}/workouts/{wid}")).json()
    sets = detail["workout"]["blocks"][0]["exercises"][0]["sets"]
    assert [s["weight"] for s in sets] == [80]
    assert detail["unit"] == "kg" and detail["comments"] == []

    overview = (await coach.get(f"/coach/clients/{CLIENT}")).json()
    signals = overview["client"]["signals"]
    assert signals["days_since_last_workout"] == 0 and signals["missing"] is False
    assert signals["new_records"] == 1
    assert overview["records"][0]["exercise"] == "Жим"
    # Тот же вид, что у рекордов «Итога недели» и подходов в приложении.
    assert overview["records"][0]["text"] == "80кг × 5"

    # Идущая тренировка тренеру не видна.
    active = (await athlete.post("/workouts/active", json={})).json()["id"]
    assert (await coach.get(f"/coach/clients/{CLIENT}/workouts/{active}")).status_code == 404


async def test_missing_signal_after_a_week_without_workouts(fresh_db):
    coach, athlete, _ = await _linked(fresh_db)
    ex = await _exercise(athlete, "Присед")
    # «Сегодня» — по часам атлета (timeutil.user_today, часовой пояс из
    # users), как считает coach_service.client_signals, а не по часам
    # сервера: вечером по UTC у атлета на UTC+3 уже завтра, и dt.date.today()
    # давал 10 дней вместо 9.
    today = timeutil.user_today(await db.get_user(CLIENT))
    await _finished_workout(athlete, ex, 60, date=(today - dt.timedelta(days=9)).isoformat())
    signals = (await coach.get("/coach/clients")).json()["clients"][0]["signals"]
    assert signals["missing"] is True and signals["days_since_last_workout"] == 9
    assert signals["workouts_this_week"] == 0


# ---------- программа ----------


async def test_assign_program_copies_it_and_coach_sees_done_days(fresh_db):
    coach, athlete, _ = await _linked(fresh_db)
    bench = await _exercise(coach, "Жим лёжа тренера")
    program = (await coach.post("/programs", json={"name": "Сплит"})).json()
    day = (await coach.post(f"/programs/{program['id']}/days", json={"name": "День А"})).json()
    day_id = day["id"] if "id" in day else day["days"][-1]["id"]
    await coach.post(f"/routines/{day_id}/exercises", json={"exercise_id": bench, "target": "3×8"})
    await coach.post(f"/programs/{program['id']}/days", json={"name": "День Б"})  # пустой день не едет

    resp = await coach.post(f"/coach/clients/{CLIENT}/programs", json={"program_id": program["id"]})
    assert resp.status_code == 201, resp.text
    copy_id = resp.json()["program_id"]

    mine = (await athlete.get("/programs")).json()
    copy = next(p for p in mine if p["id"] == copy_id)
    assert copy["coach_name"] == "Иван Петров" and copy["source"] == "coach"
    detail = (await athlete.get(f"/programs/{copy_id}")).json()
    assert detail["coach_name"] == "Иван Петров" and [d["name"] for d in detail["days"]] == ["День А"]
    routine = (await athlete.get(f"/routines/{detail['days'][0]['id']}")).json()
    assert routine["exercises"][0]["target"] == "3×8"

    overview = (await coach.get(f"/coach/clients/{CLIENT}")).json()
    assigned = overview["programs"][0]
    assert assigned["done_days"] == 0 and assigned["days"][0]["done_at"] is None

    ex_id = routine["exercises"][0]["exercise_id"]
    await _finished_workout(athlete, ex_id, 50, routine_id=detail["days"][0]["id"])
    assigned = (await coach.get(f"/coach/clients/{CLIENT}")).json()["programs"][0]
    assert assigned["done_days"] == 1 and assigned["days"][0]["done_at"] is not None

    # Чужая программа и пустая — понятные отказы.
    assert (await coach.post(f"/coach/clients/{CLIENT}/programs", json={"program_id": 999999})).status_code == 404
    empty = (await coach.post("/programs", json={"name": "Пусто"})).json()
    resp = await coach.post(f"/coach/clients/{CLIENT}/programs", json={"program_id": empty["id"]})
    assert resp.status_code == 400 and resp.json()["error"] == "coach_program_empty"


async def test_behind_program_signal(fresh_db):
    coach, athlete, _ = await _linked(fresh_db)
    ex = await _exercise(coach, "Тяга")
    program = (await coach.post("/programs", json={"name": "Тяги"})).json()
    day = (await coach.post(f"/programs/{program['id']}/days", json={"name": "День"})).json()
    day_id = day["id"] if "id" in day else day["days"][-1]["id"]
    await coach.post(f"/routines/{day_id}/exercises", json={"exercise_id": ex})
    await coach.post(f"/coach/clients/{CLIENT}/programs", json={"program_id": program["id"]})
    old = (dt.datetime.now(dt.timezone.utc).replace(tzinfo=None) - dt.timedelta(days=10)).isoformat(timespec="seconds")
    await fresh_db.conn().execute("UPDATE coach_assignments SET created_at = ?", (old,))
    await fresh_db.conn().commit()
    signals = (await coach.get("/coach/clients")).json()["clients"][0]["signals"]
    assert signals["behind_program"] is True


# ---------- комментарий ----------


async def test_comment_pushes_the_athlete_and_shows_under_the_workout(fresh_db, pushes):
    coach, athlete, _ = await _linked(fresh_db)
    await athlete.post("/push/register", json={"device_token": "tok-athlete"})
    ex = await _exercise(athlete, "Жим")
    wid = await _finished_workout(athlete, ex, 60)

    resp = await coach.post(f"/coach/clients/{CLIENT}/workouts/{wid}/comments", json={"text": " Отлично, держи темп "})
    assert resp.status_code == 201, resp.text
    assert resp.json()["text"] == "Отлично, держи темп" and resp.json()["pushed"] == 1
    args, kwargs = pushes.call_args
    assert args[0] == CLIENT and args[1] == "tok-athlete"
    assert args[2] == "Комментарий тренера" and "Отлично, держи темп" in args[3]
    assert kwargs["route"] == {"screen": "workout_detail", "workout_id": wid}

    comments = (await athlete.get(f"/workouts/{wid}/coach-comments")).json()["comments"]
    assert comments[0]["coach_name"] == "Иван Петров" and comments[0]["is_read"] is False
    again = (await athlete.get(f"/workouts/{wid}/coach-comments")).json()["comments"]
    assert again[0]["is_read"] is True
    seen = (await coach.get(f"/coach/clients/{CLIENT}/workouts/{wid}")).json()["comments"]
    assert seen[0]["is_read"] is True

    assert (await coach.post(f"/coach/clients/{CLIENT}/workouts/{wid}/comments", json={"text": ""})).status_code == 400
    long = await coach.post(f"/coach/clients/{CLIENT}/workouts/{wid}/comments", json={"text": "a" * 1001})
    assert long.status_code == 400
    # Чужой тренировки атлет не читает.
    assert (await coach.get(f"/workouts/{wid}/coach-comments")).status_code == 404

    active = (await athlete.post("/workouts/active", json={})).json()["id"]
    resp = await coach.post(f"/coach/clients/{CLIENT}/workouts/{active}/comments", json={"text": "рано"})
    assert resp.status_code == 409 and resp.json()["error"] == "coach_workout_active"

    # Удалил тренировку — ушли и комментарии к ней.
    assert (await athlete.delete(f"/workouts/{wid}")).status_code == 200
    cur = await fresh_db.conn().execute("SELECT COUNT(*) FROM coach_comments")
    assert (await cur.fetchone())[0] == 0


async def test_comment_push_goes_in_the_athletes_language(fresh_db, pushes):
    coach, athlete, _ = await _linked(fresh_db)
    await fresh_db.set_user_lang(CLIENT, "en")
    await athlete.post("/push/register", json={"device_token": "tok"})
    ex = await _exercise(athlete, "Bench")
    wid = await _finished_workout(athlete, ex, 60)
    await coach.post(f"/coach/clients/{CLIENT}/workouts/{wid}/comments", json={"text": "Nice"})
    args, _ = pushes.call_args
    assert args[2] == "Your coach commented" and args[3].startswith("Иван Петров on your workout")


# ---------- AI-сводка ----------


class _FakeCompletions:
    def __init__(self, text: str):
        self.text = text
        self.calls: list[dict] = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        usage = SimpleNamespace(prompt_tokens=100, completion_tokens=50, total_tokens=150,
                                prompt_tokens_details=None, completion_tokens_details=None)
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=self.text))], usage=usage,
        )


def _summary_calls(completions) -> list[dict]:
    """Только вызовы сводки — комментарий тренера к тренировке после финиша
    идёт в ту же подменённую модель фоном."""
    return [c for c in completions.calls if c["extra_headers"] == ai_trainer._cache_headers(COACH, scope="coach_summary")]


@pytest.fixture
def fake_model(monkeypatch):
    completions = _FakeCompletions("Маше пора в зал.")
    fake = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    monkeypatch.setattr(ai_trainer, "is_configured", lambda: True)
    monkeypatch.setattr(ai_trainer, "_get_client", lambda: fake)
    return completions


async def test_ai_summary_only_sends_clients_who_consented(fresh_db, fake_model):
    coach, athlete, _ = await _linked(fresh_db)
    ex = await _exercise(athlete, "Жим")
    await _finished_workout(athlete, ex, 60)

    # Атлет приложения (есть iOS-токен) без согласия на AI — его данных в модели
    # нет, и модель не зовётся.
    await fresh_db.register_push_token(CLIENT, "ios", "a" * 64)
    result = (await coach.post("/coach/ai-summary", json={})).json()
    assert result == {"text": None, "clients": 0, "skipped_no_consent": 1}
    assert _summary_calls(fake_model) == []

    await athlete.patch("/settings", json={"ai_consent": True})
    result = (await coach.post("/coach/ai-summary", json={})).json()
    assert result["text"] == "Маше пора в зал." and result["clients"] == 1
    sent = _summary_calls(fake_model)[0]["messages"]
    assert "Маша" in sent[1]["content"] and "Жим" in sent[1]["content"]
    assert sent[0]["content"].endswith(__import__("i18n").t_in("ru", "ai.language_tail"))
    assert await fresh_db.get_ai_question_count_today(COACH) == 1
    cur = await fresh_db.conn().execute("SELECT COUNT(*) FROM cost_events WHERE user_id = ?", (COACH,))
    assert (await cur.fetchone())[0] == 1

    one = (await coach.post("/coach/ai-summary", json={"client_id": CLIENT})).json()
    assert one["clients"] == 1
    assert (await coach.post("/coach/ai-summary", json={"client_id": STRANGER})).status_code == 404


async def test_ai_summary_includes_telegram_only_client_without_consent(fresh_db, fake_model):
    """Чисто телеграмный атлет листа согласия не видит — его данные идут в
    сводку тренера так же, как в его недельный итог (_background_ai_allowed)."""
    coach, athlete, _ = await _linked(fresh_db)
    ex = await _exercise(athlete, "Жим")
    await _finished_workout(athlete, ex, 60)
    result = (await coach.post("/coach/ai-summary", json={})).json()
    assert result["clients"] == 1 and result["skipped_no_consent"] == 0


async def test_ai_summary_respects_the_coach_consent_and_limits(fresh_db, fake_model, monkeypatch):
    coach, athlete, _ = await _linked(fresh_db)
    await athlete.patch("/settings", json={"ai_consent": True})
    resp = await coach.post("/coach/ai-summary", json={}, headers={config.AI_CONSENT_CLIENT_HEADER: "1"})
    assert resp.status_code == 403 and resp.json()["error"] == "ai_consent_required"

    async def blocked(user_id, kind):
        assert kind == ai_limits.KIND_QUESTION
        return ai_limits.Block(kind=kind, log="test", user_text="На сегодня вопросы кончились.")

    monkeypatch.setattr(ai_limits, "check", blocked)
    resp = await coach.post("/coach/ai-summary", json={})
    assert resp.status_code == 429 and resp.json()["message"] == "На сегодня вопросы кончились."
    assert _summary_calls(fake_model) == []


# ---------- снос аккаунта ----------


async def _full_relationship(fresh_db, pushes=None):
    coach, athlete, _ = await _linked(fresh_db)
    ex = await _exercise(athlete, "Жим")
    wid = await _finished_workout(athlete, ex, 60)
    await coach.post(f"/coach/clients/{CLIENT}/workouts/{wid}/comments", json={"text": "Ок"})
    cex = await _exercise(coach, "Тяга")
    program = (await coach.post("/programs", json={"name": "Сплит"})).json()
    day = (await coach.post(f"/programs/{program['id']}/days", json={"name": "День"})).json()
    day_id = day["id"] if "id" in day else day["days"][-1]["id"]
    await coach.post(f"/routines/{day_id}/exercises", json={"exercise_id": cex})
    assert (await coach.post(f"/coach/clients/{CLIENT}/programs", json={"program_id": program["id"]})).status_code == 201
    return coach, athlete


async def _pair_rows() -> dict[str, int]:
    out = {}
    for table in ("coach_links", "coach_comments", "coach_assignments", "coach_invites", "coach_profiles"):
        cur = await db.conn().execute(f"SELECT COUNT(*) FROM {table}")
        out[table] = (await cur.fetchone())[0]
    return out


async def test_deleting_the_athlete_removes_links_comments_and_assignments(fresh_db):
    coach, athlete = await _full_relationship(fresh_db)
    assert (await _pair_rows())["coach_comments"] == 1
    resp = await athlete.delete("/account?confirm=delete")
    assert resp.status_code == 200, resp.text
    assert await fresh_db.user_data_left(CLIENT) == {}
    rows = await _pair_rows()
    assert rows["coach_links"] == rows["coach_comments"] == rows["coach_assignments"] == 0
    assert rows["coach_profiles"] == 1  # тренер остался тренером
    assert (await coach.get("/coach/clients")).json()["clients"] == []


async def test_deleting_the_coach_removes_profile_invite_and_pairs(fresh_db):
    coach, athlete = await _full_relationship(fresh_db)
    resp = await coach.delete("/account?confirm=delete")
    assert resp.status_code == 200, resp.text
    assert await fresh_db.user_data_left(COACH) == {}
    assert await _pair_rows() == {
        "coach_links": 0, "coach_comments": 0, "coach_assignments": 0, "coach_invites": 0, "coach_profiles": 0,
    }
    # Программа, назначенная тренером, осталась у атлета своей — без подписи.
    mine = (await athlete.get("/programs")).json()
    assert [p["coach_name"] for p in mine] == [None]
    assert (await athlete.get("/coach-links")).json()["coaches"] == []


# ---------- прогресс клиента ----------


async def test_client_progress_weeks_lifts_and_records(fresh_db):
    coach, athlete, _ = await _linked(fresh_db)
    ex = await _exercise(athlete, "Жим")
    today = dt.date.today()
    await _finished_workout(athlete, ex, 60, date=(today - dt.timedelta(days=40)).isoformat())
    await _finished_workout(athlete, ex, 62.5, date=(today - dt.timedelta(days=14)).isoformat())
    await _finished_workout(athlete, ex, 70)

    resp = await coach.get(f"/coach/clients/{CLIENT}/progress")
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["unit"] == "kg" and data["weeks"] == 12 and data["total_workouts"] == 3
    assert len(data["weekly"]) == 12
    monday = today - dt.timedelta(days=today.weekday())
    assert data["weekly"][-1]["week_start"] == monday.isoformat()
    assert data["weekly"][-1] == {"week_start": monday.isoformat(), "workouts": 1, "tonnage": 350.0}
    assert data["frequency"] == {"workouts": 3, "per_week": 0.2, "this_week": 1}

    [lift] = data["lifts"]
    assert lift["exercise_id"] == ex and lift["exercise"] == "Жим" and lift["metric"] == "e1rm"
    assert [p["top_set"] for p in lift["points"]] == ["60×5", "62.5×5", "70×5"]
    assert [p["value"] for p in lift["points"]] == [70.0, 72.9, 81.7]
    assert lift["points"][-1]["week_start"] == monday.isoformat()

    [record] = data["records"]
    assert record["best_set"] == "70×5" and record["best_e1rm"] == 81.7 and record["best_weight"] == 70
    assert record["sessions"] == 3 and record["metric"] == "e1rm"
    [fresh] = data["recent_records"]
    assert fresh["best_set"] == "70×5" and data["recent_record_days"] == 28

    # Окно недель — из ?weeks= с потолком.
    short = (await coach.get(f"/coach/clients/{CLIENT}/progress?weeks=4")).json()
    assert len(short["weekly"]) == 4 and short["frequency"]["workouts"] == 2
    assert [p["top_set"] for p in short["lifts"][0]["points"]] == ["62.5×5", "70×5"]
    assert len((await coach.get(f"/coach/clients/{CLIENT}/progress?weeks=500")).json()["weekly"]) == 52
    assert (await coach.get(f"/coach/clients/{CLIENT}/progress?weeks=1")).status_code == 400


async def test_client_progress_empty_and_bodyweight_exercise(fresh_db):
    coach, athlete, _ = await _linked(fresh_db)
    empty = (await coach.get(f"/coach/clients/{CLIENT}/progress")).json()
    assert empty["total_workouts"] == 0 and empty["lifts"] == [] and empty["records"] == []
    assert empty["recent_records"] == [] and all(w["workouts"] == 0 for w in empty["weekly"])

    ex = await _exercise(athlete, "Подтягивания")
    await _finished_workout(athlete, ex, 0)
    data = (await coach.get(f"/coach/clients/{CLIENT}/progress")).json()
    # Одна тренировка: упражнение ещё не «частое» (меньше двух сессий) —
    # графика нет, но рекорд есть.
    assert data["lifts"] == []
    [record] = data["records"]
    assert record["metric"] == "reps" and record["best_reps"] == 5


async def test_client_progress_names_in_the_coachs_language(fresh_db):
    coach, athlete, _ = await _linked(fresh_db)
    await fresh_db.set_user_lang(COACH, "en")
    # Каталожное упражнение у русского атлета лежит под русским именем.
    ex = await _exercise(athlete, "Жим штанги лёжа")
    await _finished_workout(athlete, ex, 60, date=(dt.date.today() - dt.timedelta(days=8)).isoformat())
    await _finished_workout(athlete, ex, 70)
    data = (await coach.get(f"/coach/clients/{CLIENT}/progress")).json()
    assert data["lifts"][0]["exercise"] == "Barbell Bench Press"
    assert data["records"][0]["exercise"] == "Barbell Bench Press"


async def test_client_progress_refused_to_stranger_and_former_coach(fresh_db):
    coach, athlete, code = await _linked(fresh_db)
    ex = await _exercise(athlete, "Жим")
    await _finished_workout(athlete, ex, 60)
    stranger = await _client(fresh_db, STRANGER)
    await stranger.post("/coach", json={"display_name": "Чужой"})
    resp = await stranger.get(f"/coach/clients/{CLIENT}/progress")
    assert resp.status_code == 404 and resp.json()["error"] == "not_linked"
    assert "Жим" not in resp.text
    # Атлет закрыл доступ — бывший тренер прогресса больше не видит.
    assert (await athlete.delete(f"/coach-links/{COACH}")).status_code == 200
    resp = await coach.get(f"/coach/clients/{CLIENT}/progress")
    assert resp.status_code == 404 and resp.json()["error"] == "not_linked"
    # И тренер, убравший клиента сам, — тоже.
    assert (await athlete.post(f"/coach/invites/{code}/accept", json={"name": "Маша"})).status_code == 201
    assert (await coach.get(f"/coach/clients/{CLIENT}/progress")).status_code == 200
    assert (await coach.delete(f"/coach/clients/{CLIENT}")).status_code == 200
    assert (await coach.get(f"/coach/clients/{CLIENT}/progress")).status_code == 404
    # Атлет не подглядывает «прогресс» тренера через свой id.
    assert (await athlete.get(f"/coach/clients/{COACH}/progress")).status_code == 404
