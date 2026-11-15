"""Гигиена аналитики: свои аккаунты (включая все демо-входы) одной точкой
для сводки и выгрузки, полнота CSV атлетов, воронка до входа в выгрузке,
белый список `source` у картинки тренировки."""

from __future__ import annotations

import csv
import datetime as dt
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import config
import db
import product_metrics
from handlers import admin

DAY = dt.date(2026, 3, 10)


def _demo_env(monkeypatch):
    monkeypatch.setattr(config, "ADMIN_ID", 1)
    monkeypatch.setattr(config, "TEST_USER_ID", None)
    monkeypatch.setattr(config, "REVIEW_DEMO_USERNAME", "reviewer")
    monkeypatch.setattr(config, "REVIEW_DEMO_PASSWORD", "pw")
    monkeypatch.setattr(config, "WALK_DEMO_USERNAME", "screenshots")
    monkeypatch.setattr(config, "WALK_DEMO_PASSWORD", "pw2")
    monkeypatch.setattr(config, "CATALOG_DEMO_USERNAME", "catalog")
    monkeypatch.setattr(config, "CATALOG_DEMO_PASSWORD", "pw3")


async def _demo_accounts() -> dict[str, int]:
    """Демо-входы по паролю — так же, как их заводит review_demo.ensure_demo_user."""
    out = {}
    for login in ("reviewer", "screenshots", "screenshots-new", "screenshots-l2", "catalog"):
        user = await db.create_app_only_user(language_code="en", track_signup=False)
        await db.link_auth_identity(user["telegram_id"], "review_demo", login)
        out[login] = user["telegram_id"]
    return out


async def _event(uid, event, when: str, platform="ios", props=None):
    await db.log_analytics_events(
        [(uid, event, json.dumps(props) if props else None, platform, "1.0", when)]
    )


async def test_own_accounts_cover_admin_and_every_password_demo(fresh_db, monkeypatch):
    _demo_env(monkeypatch)
    demo = await _demo_accounts()
    apple = await db.create_app_only_user(language_code="en")
    await db.link_auth_identity(apple["telegram_id"], "apple", "apple-1")

    own = await db.own_account_ids()

    assert own == {1, *demo.values()}
    assert apple["telegram_id"] not in own


async def test_rollup_skips_demo_accounts(fresh_db, monkeypatch):
    _demo_env(monkeypatch)
    demo = await _demo_accounts()
    real = await db.create_app_only_user(language_code="en")
    stamp = f"{DAY.isoformat()}T10:00:00"
    await db.conn().execute("UPDATE users SET created_at = ?", (stamp,))
    await db.conn().commit()
    for uid in (real["telegram_id"], *demo.values()):
        await _event(uid, "app_open", f"{DAY.isoformat()}T09:00:00")

    metrics = await product_metrics.rollup_day(DAY)

    assert metrics["active_users"] == 1 and metrics["active_ios"] == 1
    assert metrics["new_users"] == 1


async def _run_events_csv(days="30"):
    sent: list[tuple[str, list[list[str]], str | None]] = []

    async def answer_document(document, caption=None, **_):
        with open(document.path, encoding="utf-8") as fh:
            sent.append((document.filename or document.path, list(csv.reader(fh)), caption))

    message = SimpleNamespace(
        from_user=SimpleNamespace(id=1), answer=AsyncMock(), answer_document=answer_document,
    )
    await admin.cmd_events_csv(message, None, SimpleNamespace(args=days))
    return sent


def _now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


async def test_events_csv_flags_demo_accounts_as_own(fresh_db, monkeypatch):
    _demo_env(monkeypatch)
    demo = await _demo_accounts()
    await db.get_or_create_user(501, "athlete", "en")
    for uid in (501, *demo.values()):
        await _event(uid, "app_open", _now_iso())

    (_, events, _), (_, users, _) = await _run_events_csv()

    own = {row[1]: row[6] for row in events[1:]}
    assert own[str(demo["reviewer"])] == "1"
    assert own[str(demo["screenshots-l2"])] == "1"
    assert own[str(demo["catalog"])] == "1"
    assert own["501"] == "0"
    user_own = {row[0]: row[6] for row in users[1:]}
    assert user_own[str(demo["screenshots-new"])] == "1" and user_own["501"] == "0"


async def test_users_csv_has_a_row_for_every_event_user(fresh_db, monkeypatch):
    """Команда боту без /start: событие есть, записи `users` нет — строка
    атлета всё равно есть, с has_account=0."""
    _demo_env(monkeypatch)
    await db.get_or_create_user(501, "athlete", "en")
    await _event(501, "tg_message", _now_iso(), platform="tg")
    await _event(7970618911, "tg_message", _now_iso(), platform="tg", props={"type": "command"})

    (_, events, _), (_, users, _) = await _run_events_csv()

    assert users[0] == list(admin.EVENT_USERS_CSV_COLUMNS)
    event_ids = {row[1] for row in events[1:]}
    user_rows = {row[0]: row for row in users[1:]}
    assert event_ids <= set(user_rows)
    ghost = dict(zip(admin.EVENT_USERS_CSV_COLUMNS, user_rows["7970618911"], strict=True))
    assert ghost["has_account"] == "0" and ghost["signup_platform"] == "" and ghost["has_app"] == "0"
    assert dict(zip(admin.EVENT_USERS_CSV_COLUMNS, user_rows["501"], strict=True))["has_account"] == "1"


async def test_ios_signup_always_has_app_even_without_live_token(fresh_db, monkeypatch):
    """App-only аккаунт, чей токен /v1 уже снят (выход), — всё равно с
    приложением: signup_platform=ios и has_app=0 вместе не бывает."""
    _demo_env(monkeypatch)
    app_only = (await db.create_app_only_user(language_code="en"))["telegram_id"]
    await db.get_or_create_user(501, "athlete", "en")
    await _event(app_only, "app_open", _now_iso())
    await _event(501, "tg_message", _now_iso(), platform="tg")

    _, (_, users, _) = await _run_events_csv()

    rows = {row[0]: dict(zip(admin.EVENT_USERS_CSV_COLUMNS, row, strict=True)) for row in users[1:]}
    assert rows[str(app_only)]["signup_platform"] == "ios"
    assert rows[str(app_only)]["has_app"] == "1"
    assert rows["501"]["signup_platform"] == "tg" and rows["501"]["has_app"] == "0"
    for row in rows.values():
        assert not (row["signup_platform"] == "ios" and row["has_app"] == "0")


async def test_events_csv_sends_funnel_steps_before_signin(fresh_db, monkeypatch):
    _demo_env(monkeypatch)
    await db.get_or_create_user(501, "athlete", "en")
    await _event(501, "tg_message", _now_iso(), platform="tg")
    install = "0b1c2d3e-4f50-4617-8899-aabbccddeeff"
    await db.log_funnel_event(install, "onboarding_slide_1", "en")
    await db.log_funnel_event(install, "signin_shown", "en")
    await db.log_funnel_event("11111111-2222-4333-8444-555555555555", "onboarding_slide_1", None)

    sent = await _run_events_csv()

    assert len(sent) == 3
    name, funnel, caption = sent[2]
    assert "funnel_30d_" in name
    assert funnel[0] == list(admin.FUNNEL_CSV_COLUMNS)
    assert [row[1:] for row in funnel[1:]] == [
        [install, "onboarding_slide_1", "en"],
        [install, "signin_shown", "en"],
        ["11111111-2222-4333-8444-555555555555", "onboarding_slide_1", ""],
    ]
    assert "3" in caption


async def test_events_csv_without_funnel_sends_two_files(fresh_db, monkeypatch):
    _demo_env(monkeypatch)
    await db.get_or_create_user(501, "athlete", "en")
    await _event(501, "tg_message", _now_iso(), platform="tg")
    assert len(await _run_events_csv()) == 2


@pytest.mark.parametrize("event", ["workout_card_share", "workout_card_shared"])
def test_workout_card_source_accepts_coach_program(event):
    """Сторис программы из чата тренера едет теми же событиями картинки
    тренировки с source=coach_program (WorkoutCardSource в приложении)."""
    for source in ("finish", "history", "coach_program"):
        assert product_metrics.clean_props(event, {"source": source})["source"] == source
    assert "source" not in product_metrics.clean_props(event, {"source": "somewhere_else"})
