"""События жизненного цикла, которые пишет сама база (lifecycle_events.py):
пишутся там, где случилось действие, по разу, с клиентом из контекста входа и
только с токенами из закрытых наборов; плюс сырая выгрузка /events_csv."""

from __future__ import annotations

import csv
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

import api_v1
import config
import db
import lifecycle_events as le
import main
import product_metrics
from handlers import admin

TG = le.PLATFORM_TG
IOS = le.PLATFORM_IOS


async def _events(user_id: int, *, skip_signup: bool = True) -> list[tuple]:
    cur = await db.conn().execute(
        "SELECT event, props, platform FROM analytics_events WHERE user_id = ? ORDER BY id",
        (user_id,),
    )
    rows = [(r[0], json.loads(r[1]) if r[1] else None, r[2]) for r in await cur.fetchall()]
    return [r for r in rows if not (skip_signup and r[0] == le.SIGNUP)]


async def _names(user_id: int) -> list[str]:
    return [e[0] for e in await _events(user_id)]


async def _exercise(user_id: int, name: str = "Жим лёжа") -> int:
    return await db.create_exercise(user_id, name, None)


async def _block(workout_id: int, exercise_id: int) -> int:
    return await db.get_or_create_single_block_for_exercise(workout_id, exercise_id)


# --- чистые помощники ------------------------------------------------------------


def test_buckets_are_stable_tokens():
    assert [le.sets_bucket(n) for n in (0, 1, 5, 6, 10, 11, 20, 21, 30, 31, 500)] == [
        "0", "1_5", "1_5", "6_10", "6_10", "11_20", "11_20", "21_30", "21_30", "31p", "31p",
    ]
    assert le.duration_bucket(None) == "none"
    assert le.duration_bucket(0) == "none"
    assert le.duration_bucket(10 * 60) == "lt15"
    assert le.duration_bucket(45 * 60) == "30_60"
    assert le.duration_bucket(3 * 3600) == "120p"
    assert le.count_bucket(0) == "0" and le.count_bucket(300) == "201p"


def test_client_set_source_accepts_only_known_tokens():
    assert le.client_set_source("lock_screen") == "lock_screen"
    assert le.client_set_source(" Watch ") == "watch"
    # «бот» приложение назвать себя не может, мусор и не-строки — не источник.
    assert le.client_set_source("bot") is None
    assert le.client_set_source("100 кг × 8") is None
    assert le.client_set_source(5) is None


def test_origin_and_kinds_fall_back_to_safe_tokens():
    assert le.program_origin("ai") == "ai"
    assert le.program_origin("something_new") == "other"
    assert le.program_origin(None) == "other"
    assert le.import_source("hevy") == "hevy"
    assert le.import_source("weird") == "other"
    assert le.ai_question_kind("photo") == "photo"
    assert le.ai_question_kind("Как дела?") == "text"
    assert le.signup_source("src_kachalka-1") == "src_kachalka-1"
    assert le.signup_source("Привет") is None


# --- signup ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_signup_once_with_source_and_platform(fresh_db):
    await db.get_or_create_user(700, "a", "ru", signup_source="src_kachalka")
    await db.get_or_create_user(700, "a", "ru", signup_source="referral")  # уже есть — не новичок
    events = await _events(700, skip_signup=False)
    assert events == [("signup", {"source": "src_kachalka"}, TG)]

    app_user = await db.create_app_only_user(language_code="en")
    assert await _events(app_user["telegram_id"], skip_signup=False) == [("signup", None, IOS)]

    demo = await db.create_app_only_user(language_code="en", track_signup=False)
    assert await _events(demo["telegram_id"], skip_signup=False) == []


# --- тренировка в боте ----------------------------------------------------------


@pytest.mark.asyncio
async def test_no_context_no_events(fresh_db, user_id):
    """Фоновая задача или скрипт: действия есть, людей за ними нет — событий тоже."""
    workout_id, _ = await db.get_or_create_active_workout(user_id)
    ex = await _exercise(user_id)
    await db.append_set(await _block(workout_id, ex), ex, 0, 100, 8)
    await db.finish_workout(workout_id)
    assert await _events(user_id) == []


@pytest.mark.asyncio
async def test_workout_lifecycle_and_milestones_once(fresh_db, user_id):
    with le.using(TG):
        ex = await _exercise(user_id)
        for _ in range(3):
            workout_id, created = await db.get_or_create_active_workout(user_id)
            assert created
            block = await _block(workout_id, ex)
            await db.append_set(block, ex, 0, 100, 8)
            await db.append_set(block, ex, 0, 100, 7)
            assert await db.finish_workout(workout_id)
            # Второе «Завершить» той же тренировки — не второе событие.
            assert not await db.finish_workout(workout_id)

    names = await _names(user_id)
    assert names.count("workout_started") == 3
    assert names.count("set_logged") == 6
    assert names.count("workout_finished") == 3
    assert names.count("first_set") == 1
    assert names.count("first_workout_finished") == 1
    assert names.count("second_workout_finished") == 1
    # Порядок воронки: веха — сразу за своим действием.
    assert names[:4] == ["workout_started", "set_logged", "first_set", "set_logged"]
    events = await _events(user_id)
    started = next(e for e in events if e[0] == "workout_started")
    assert started == ("workout_started", {"kind": "live", "plan": "no"}, TG)
    logged = next(e for e in events if e[0] == "set_logged")
    assert logged == ("set_logged", {"source": "bot", "workout": "live"}, TG)
    finished = next(e for e in events if e[0] == "workout_finished")
    assert finished[1]["sets"] == "1_5"
    assert finished[1]["kind"] == "live"
    assert finished[1]["duration"] in {"none", "lt15"}


@pytest.mark.asyncio
async def test_first_set_not_repeated_after_events_pruned(fresh_db, user_id):
    """Ленту событий чистит срок хранения — веха сверяется и с данными."""
    with le.using(TG):
        ex = await _exercise(user_id)
        workout_id, _ = await db.get_or_create_active_workout(user_id)
        block = await _block(workout_id, ex)
        await db.append_set(block, ex, 0, 100, 8)
        await db.conn().execute("DELETE FROM analytics_events")
        await db.append_set(block, ex, 0, 100, 8)
    assert await _names(user_id) == ["set_logged"]


@pytest.mark.asyncio
async def test_imported_history_is_one_event_and_not_a_milestone(fresh_db, user_id):
    with le.using(TG):
        batch = await db.create_import_batch(user_id, "hevy")
        ex = await _exercise(user_id)
        wid = await db.create_finished_workout(
            user_id, "2025-01-01T10:00:00", "2025-01-01T11:00:00", source="import", import_batch_id=batch
        )
        block = await db.create_block(wid, "single")
        await db.add_set(block, ex, 1, 0, 100, 5)
        await db.finish_import_batch(batch, 1, 1)
        # Первая СВОЯ тренировка после импорта — всё ещё первая.
        workout_id, _ = await db.get_or_create_active_workout(user_id)
        await db.append_set(await _block(workout_id, ex), ex, 0, 100, 8)
        await db.finish_workout(workout_id)
    events = await _events(user_id)
    assert events[0] == ("import_done", {"source": "hevy", "workouts": "1_10", "sets": "1_10"}, TG)
    names = [e[0] for e in events]
    assert "first_set" in names and "first_workout_finished" in names
    assert names.count("set_logged") == 1


@pytest.mark.asyncio
async def test_backfill_and_edit_are_labelled(fresh_db, user_id):
    with le.using(TG):
        ex = await _exercise(user_id)
        wid, _ = await db.get_or_create_backfill_workout(user_id, "2026-01-05T12:00:00")
        await db.append_set(await _block(wid, ex), ex, 0, 50, 10)
        await db.finish_workout(wid, finished_at="2026-01-05T12:00:00")
        # Дописал подход в уже законченную — правка в истории.
        await db.append_set(await _block(wid, ex), ex, 0, 50, 9)
    events = await _events(user_id)
    assert events[0] == ("workout_started", {"kind": "backfill", "plan": "no"}, TG)
    finished = next(e for e in events if e[0] == "workout_finished")
    assert finished[1]["kind"] == "backfill" and finished[1]["duration"] == "none"
    logged = [e[1]["workout"] for e in events if e[0] == "set_logged"]
    assert logged == ["backfill", "edit"]


@pytest.mark.asyncio
async def test_programs_coach_ai_video(fresh_db, user_id):
    coach = (await db.get_or_create_user(222, "coach"))["telegram_id"]
    with le.using(IOS, "2.0 (80)"):
        await db.create_program(user_id, "Сила", source="ai")
        await db.create_program(user_id, "Своя")
        assert await db.create_program(user_id, "Своя") is None  # имя занято — программы нет
        assert await db.create_coach_link(coach, user_id, "Атлет", show_bodyweight=False)
        assert not await db.create_coach_link(coach, user_id, "Атлет", show_bodyweight=False)
        await db.try_increment_ai_question_count(user_id, 0, kind="photo")
        await db.try_increment_ai_question_count(user_id, 0, kind="мусор")
        await db.increment_ai_video_count(user_id)
    assert await _events(user_id) == [
        ("program_created", {"origin": "ai"}, IOS),
        ("program_created", {"origin": "manual"}, IOS),
        ("coach_linked", None, IOS),
        ("ai_question", {"kind": "photo"}, IOS),
        ("ai_question", {"kind": "text"}, IOS),
        ("video_review", None, IOS),
    ]
    # Тренер в запросе атлета событий не получает.
    assert await _events(coach) == []
    cur = await db.conn().execute(
        "SELECT DISTINCT app_version FROM analytics_events WHERE user_id = ? AND event != 'signup'",
        (user_id,),
    )
    assert [r[0] for r in await cur.fetchall()] == ["2.0 (80)"]


# --- клиент из входа --------------------------------------------------------------


@pytest.mark.asyncio
async def test_bot_middleware_sets_tg(fresh_db):
    seen = {}

    async def handler(event, data):
        seen["platform"] = le.platform()

    event = SimpleNamespace(from_user=SimpleNamespace(id=9001, language_code="en", first_name="A"))
    with le.using(None):
        await main.SetUserLanguageMiddleware()(handler, event, {})
    assert seen["platform"] == TG


@pytest.mark.asyncio
async def test_v1_set_source_from_body_and_platform_ios(fresh_db, user_id):
    token = await db.issue_api_token(user_id)
    ex = await _exercise(user_id)
    headers = {"authorization": f"Bearer {token}", "x-app-version": "2.1 (90)"}
    transport = httpx.ASGITransport(app=api_v1.build_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post("/workouts/active", headers=headers, json={})
        assert resp.status_code in (200, 201), resp.text
        workout_id = resp.json()["id"]
        for source in ("lock_screen", None, "hacker"):
            body = {"exercise_id": ex, "weight": 100, "reps": 8}
            if source:
                body["source"] = source
            resp = await client.post(f"/workouts/{workout_id}/sets", headers=headers, json=body)
            assert resp.status_code == 201, resp.text
        # Повтор той же попытки (тот же ключ) — подход уже есть, события нет.
        body = {"exercise_id": ex, "weight": 90, "reps": 5, "idempotency_key": "k1"}
        for _ in range(2):
            resp = await client.post(f"/workouts/{workout_id}/sets", headers=headers, json=body)
            assert resp.status_code == 201, resp.text
    lifecycle = [e for e in await _events(user_id) if e[0] != "action"]
    assert lifecycle[0] == ("workout_started", {"kind": "live", "plan": "no"}, IOS)
    sources = [e[1]["source"] for e in lifecycle if e[0] == "set_logged"]
    assert sources == ["lock_screen", "app", "app", "app"]
    assert [e[0] for e in lifecycle].count("first_set") == 1


# --- события приложения ------------------------------------------------------------


def test_client_error_and_share_events_are_whitelisted():
    assert product_metrics.clean_props(
        "error_shown", {"code": "question_limit_exceeded", "screen": "tab_coach", "message": "Лимит"}
    ) == {"code": "question_limit_exceeded", "screen": "tab_coach"}
    # Текст вместо кода не проходит.
    assert product_metrics.clean_props("error_shown", {"code": "Не понял вес"}) == {}
    assert product_metrics.clean_props("offline_queued", {"source": "lock_screen"}) == {"source": "lock_screen"}
    assert product_metrics.clean_props("workout_card_share", {"source": "finish"}) == {"source": "finish"}
    assert product_metrics.clean_props(
        "workout_card_shared", {"source": "history", "activity": "cancelled"}
    ) == {"source": "history", "activity": "cancelled"}


def test_lifecycle_names_are_not_client_events():
    """Приложение не может прислать `set_logged` за сервер — иначе подход
    считался бы дважды."""
    assert not (le.ALL_EVENTS & set(product_metrics.CLIENT_EVENTS))


# --- /events_csv -------------------------------------------------------------------


def test_events_csv_days_argument(monkeypatch):
    monkeypatch.setattr(config, "ANALYTICS_RETENTION_DAYS", 400)
    assert admin.parse_events_csv_days(None) == 30
    assert admin.parse_events_csv_days("") == 30
    assert admin.parse_events_csv_days("7") == 7
    assert admin.parse_events_csv_days("0") == 1
    assert admin.parse_events_csv_days("9999") == 400
    assert admin.parse_events_csv_days("неделя") is None


@pytest.mark.asyncio
async def test_events_csv_sends_events_and_users(fresh_db, monkeypatch):
    monkeypatch.setattr(config, "ADMIN_ID", 1)
    monkeypatch.setattr(config, "TEST_USER_ID", None)
    await db.get_or_create_user(1, "admin")
    await db.get_or_create_user(501, "athlete", "en", signup_source="organic")
    await db.set_user_source(501, "organic")
    with le.using(TG):
        await db.create_program(501, "План", source="catalog")
    sent: list[tuple[list[list[str]], str | None]] = []

    async def answer_document(document, caption=None, **_):
        with open(document.path, encoding="utf-8") as fh:
            sent.append((list(csv.reader(fh)), caption))

    message = SimpleNamespace(
        from_user=SimpleNamespace(id=1), answer=AsyncMock(), answer_document=answer_document,
    )
    await admin.cmd_events_csv(message, None, SimpleNamespace(args="30"))
    (events, caption), (users, _) = sent
    assert events[0] == list(admin.EVENTS_CSV_COLUMNS)
    by_user = {(row[1], row[3]): row for row in events[1:]}
    assert by_user[("501", "signup")][4] == '{"source":"organic"}'
    assert by_user[("501", "program_created")][2] == "tg"
    assert by_user[("1", "signup")][6] == "1"  # свой аккаунт помечен, не выкинут
    assert "строк" in caption and "Обрезано" not in caption
    assert users[0] == list(admin.EVENT_USERS_CSV_COLUMNS)
    athlete = next(row for row in users[1:] if row[0] == "501")
    assert athlete[2:6] == ["organic", "en", "tg", "0"]


@pytest.mark.asyncio
async def test_events_csv_reports_truncation(fresh_db, monkeypatch):
    monkeypatch.setattr(config, "ADMIN_ID", 1)
    monkeypatch.setattr(admin, "EVENTS_CSV_MAX_ROWS", 2)
    for uid in (601, 602, 603):
        await db.get_or_create_user(uid, None)
    captions = []

    async def answer_document(document, caption=None, **_):
        captions.append(caption)

    message = SimpleNamespace(
        from_user=SimpleNamespace(id=1), answer=AsyncMock(), answer_document=answer_document,
    )
    await admin.cmd_events_csv(message, None, None)
    assert "Обрезано: всего 3, выгружены первые 2" in captions[0]


@pytest.mark.asyncio
async def test_events_csv_admin_only(fresh_db, monkeypatch):
    monkeypatch.setattr(config, "ADMIN_ID", 1)
    message = SimpleNamespace(from_user=SimpleNamespace(id=2), answer=AsyncMock(), answer_document=AsyncMock())
    await admin.cmd_events_csv(message, None, None)
    message.answer.assert_not_awaited()
    message.answer_document.assert_not_awaited()
