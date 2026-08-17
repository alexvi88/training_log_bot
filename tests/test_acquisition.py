"""Атрибуция источников: что означает метка в /start-ссылке, как она ложится в
базу (один раз, на первом касании) и что показывает воронка в админке."""
import datetime as dt
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import Message

import acquisition
from handlers import sharing, workout


def test_no_payload_is_organic():
    """Каталоги ботов и поиск метку поставить не дают — это всё равно источник."""
    assert acquisition.parse_start_payload(None).source == acquisition.SOURCE_ORGANIC
    assert acquisition.parse_start_payload("").source == acquisition.SOURCE_ORGANIC


def test_channel_payload_keeps_slug():
    attribution = acquisition.parse_start_payload("src_kachalka_1")
    assert attribution.source == "src_kachalka_1"
    assert attribution.referrer_id is None


def test_channel_slug_is_sanitized():
    """Слаг собирает руками человек: мусор и регистр не должны плодить
    источники-двойники (src_Качалка и src_kachalka — не два канала)."""
    assert acquisition.parse_start_payload("src_KACHALKA").source == "src_kachalka"
    assert acquisition.parse_start_payload("src_kachalka!!").source == "src_kachalka"
    # Кириллица в start-параметре не живёт вовсе: от такой метки не остаётся
    # ничего, и честнее сказать «не разобрал», чем завести источник «src_».
    assert acquisition.parse_start_payload("src_качалка").source == acquisition.SOURCE_UNKNOWN


def test_referral_payload_carries_author():
    attribution = acquisition.parse_start_payload("ref_777")
    assert attribution.source == acquisition.SOURCE_REFERRAL
    assert attribution.referrer_id == 777


def test_referral_without_valid_id_keeps_source():
    """Ссылку могли поправить руками — приглашение остаётся приглашением,
    просто без автора, а не превращается в неразобранный мусор."""
    attribution = acquisition.parse_start_payload("ref_abc")
    assert attribution.source == acquisition.SOURCE_REFERRAL
    assert attribution.referrer_id is None


def test_reserved_sources_cannot_be_forged():
    """У organic/legacy нет префикса, так что подделать их ссылкой нельзя —
    иначе кто угодно дописывал бы себе органику или прятался в legacy."""
    assert acquisition.parse_start_payload("organic").source == acquisition.SOURCE_UNKNOWN
    assert acquisition.parse_start_payload("legacy").source == acquisition.SOURCE_UNKNOWN


def test_self_referral_counts_as_organic():
    assert acquisition.attribution_for("ref_42", 42).source == acquisition.SOURCE_ORGANIC
    assert acquisition.attribution_for("ref_42", 43).referrer_id == 42


def test_referral_link_round_trip():
    link = acquisition.referral_link("kachalka_bot", 42)
    assert link == "https://t.me/kachalka_bot?start=ref_42"
    payload = link.split("?start=")[1]
    assert acquisition.parse_start_payload(payload).referrer_id == 42


def test_channel_link_round_trip():
    link = acquisition.channel_link("kachalka_bot", "Качалка Daily")
    payload = link.split("?start=")[1]
    # Всё, что уехало в закупку, обязано вернуться тем же источником — иначе
    # деньги потрачены на ссылку, которую отчёт не узнает.
    assert acquisition.parse_start_payload(payload).source == payload


# ---------- запись в базу ----------


def _make_message(user_id: int, text: str = "/start"):
    message = MagicMock(spec=Message)
    message.from_user = SimpleNamespace(id=user_id, username="tester", language_code=None)
    message.text = text
    message.answer = AsyncMock()
    message.answer_photo = AsyncMock()
    return message


async def _state(user_id: int) -> FSMContext:
    key = StorageKey(bot_id=1, chat_id=user_id, user_id=user_id)
    return FSMContext(storage=MemoryStorage(), key=key)


async def test_first_touch_wins(fresh_db, user_id):
    db = fresh_db
    assert await db.set_user_source(user_id, "src_first") is True
    assert await db.set_user_source(user_id, "src_second") is False
    row = await db.get_user(user_id)
    assert row["source"] == "src_first"


async def test_start_with_channel_link_records_source(fresh_db):
    db = fresh_db
    message = _make_message(555, "/start src_kachalka")
    command = SimpleNamespace(args="src_kachalka")

    await workout.cmd_start(message, await _state(555), command)

    assert (await db.get_user(555))["source"] == "src_kachalka"


async def test_start_with_referral_link_records_author(fresh_db, user_id):
    db = fresh_db
    command = SimpleNamespace(args=acquisition.referral_payload(user_id))

    await workout.cmd_start(_make_message(556, "/start"), await _state(556), command)

    row = await db.get_user(556)
    assert row["source"] == acquisition.SOURCE_REFERRAL
    assert row["referrer_id"] == user_id


async def test_returning_user_keeps_original_source(fresh_db):
    """Старожил, перешедший по рекламной ссылке, не привлечён ею: приписать его
    каналу — значит заплатить второй раз за того, кто уже был в боте."""
    db = fresh_db
    await workout.cmd_start(_make_message(557), await _state(557), SimpleNamespace(args=None))
    await workout.cmd_start(
        _make_message(557, "/start src_kachalka"), await _state(557), SimpleNamespace(args="src_kachalka")
    )
    assert (await db.get_user(557))["source"] == acquisition.SOURCE_ORGANIC


async def test_menu_button_needs_no_command(fresh_db, user_id):
    """Кнопка «🏠 Меню» ходит в тот же хендлер без объекта команды."""
    await workout.cmd_start(_make_message(user_id, "🏠 Меню"), await _state(user_id))


async def test_shared_card_recipient_credits_its_owner(fresh_db, user_id):
    """Визитка программы — такой же канал привлечения, как реклама, и автор у
    неё есть: тот, кто её сделал."""
    db = fresh_db
    token = await db.create_shared_item(user_id, "exercise", json.dumps({"name": "Жим лёжа"}))
    message = _make_message(600, f"/start sh_{token}")

    await sharing.open_shared(message, SimpleNamespace(args=f"sh_{token}"), await _state(600))

    row = await db.get_user(600)
    assert row["source"] == acquisition.SOURCE_SHARED_CARD
    assert row["referrer_id"] == user_id


async def test_broken_shared_link_still_counts_the_visit(fresh_db):
    """По мёртвой ссылке человек всё равно пришёл — терять его из отчёта незачем."""
    db = fresh_db
    message = _make_message(601, "/start sh_deadtoken")

    await sharing.open_shared(message, SimpleNamespace(args="sh_deadtoken"), await _state(601))

    row = await db.get_user(601)
    assert row["source"] == acquisition.SOURCE_SHARED_CARD
    assert row["referrer_id"] is None


# ---------- воронка ----------


async def _finished_workout(db, user_id: int, started_at: str | None = None) -> int:
    group_id = await db.create_muscle_group(user_id, "Грудь")
    ex_id = await db.create_exercise(user_id, "Жим лёжа", group_id)
    workout_id = await db.create_workout(user_id)
    block_id = await db.create_block(workout_id, "single")
    await db.add_block_exercise(block_id, ex_id, 0)
    await db.add_set(block_id, ex_id, 1, 0, 100.0, 5)
    await db.finish_workout(workout_id)
    if started_at is not None:
        await db.conn().execute(
            "UPDATE workouts SET started_at = ? WHERE id = ?", (started_at, workout_id)
        )
        await db.conn().commit()
    return workout_id


async def test_funnel_counts_activation_per_source(fresh_db):
    db = fresh_db
    for telegram_id, source in ((1, "src_a"), (2, "src_a"), (3, "src_b")):
        await db.get_or_create_user(telegram_id, f"u{telegram_id}")
        await db.set_user_source(telegram_id, source)
    await _finished_workout(db, 1)  # из src_a дошёл до первой тренировки один

    rows = {r["source"]: r for r in await db.acquisition_funnel(days=30)}

    assert rows["src_a"]["users"] == 2
    assert rows["src_a"]["activated"] == 1
    assert rows["src_a"]["alive"] == 1
    assert rows["src_b"]["users"] == 1
    assert rows["src_b"]["activated"] == 0


async def test_funnel_alive_window_excludes_stale(fresh_db):
    db = fresh_db
    await db.get_or_create_user(1, "u1")
    await db.set_user_source(1, "src_a")
    long_ago = (dt.datetime.now() - dt.timedelta(days=40)).isoformat(timespec="seconds")
    await _finished_workout(db, 1, started_at=long_ago)

    row = (await db.acquisition_funnel(days=90, alive_days=7))[0]

    assert row["activated"] == 1  # тренировка была
    assert row["alive"] == 0  # но давно


async def test_funnel_skips_pre_attribution_users(fresh_db):
    """Те, кто был в боте до появления атрибуции, не должны раздувать ни один
    источник — иначе первый отчёт покажет успех, которого не было."""
    db = fresh_db
    await db.get_or_create_user(1, "u1")
    await db.set_user_source(1, acquisition.SOURCE_LEGACY)
    await db.get_or_create_user(2, "u2")
    await db.set_user_source(2, "src_a")

    sources = {r["source"] for r in await db.acquisition_funnel(days=30)}

    assert sources == {"src_a"}


async def test_funnel_window_cuts_by_signup_date(fresh_db):
    db = fresh_db
    await db.get_or_create_user(1, "u1")
    await db.set_user_source(1, "src_old")
    long_ago = (dt.datetime.now() - dt.timedelta(days=60)).isoformat(timespec="seconds")
    await db.conn().execute("UPDATE users SET created_at = ? WHERE telegram_id = 1", (long_ago,))
    await db.conn().commit()

    assert await db.acquisition_funnel(days=30) == []
    assert [r["source"] for r in await db.acquisition_funnel(days=90)] == ["src_old"]


async def _started_workout(db, user_id: int) -> int:
    """Тренировка открыта, но подхода в ней ещё нет — «начал», но не «записал»."""
    return await db.create_workout(user_id)


async def _workout_with_one_set(db, user_id: int) -> int:
    """Подход есть, но тренировка не закрыта — «записал», но не «завершил»."""
    group_id = await db.create_muscle_group(user_id, "Спина")
    ex_id = await db.create_exercise(user_id, "Тяга", group_id)
    workout_id = await db.create_workout(user_id)
    block_id = await db.create_block(workout_id, "single")
    await db.add_block_exercise(block_id, ex_id, 0)
    await db.add_set(block_id, ex_id, 1, 0, 60.0, 8)
    return workout_id


async def test_onboarding_funnel_separates_every_step(fresh_db):
    db = fresh_db
    for telegram_id in (1, 2, 3, 4):
        await db.get_or_create_user(telegram_id, f"u{telegram_id}")
        await db.set_user_source(telegram_id, "src_a")
    # 1 — ничего не сделал; 2 — открыл тренировку и бросил; 3 — записал подход,
    # не закрыл; 4 — закрыл первую тренировку целиком.
    await _started_workout(db, 2)
    await _workout_with_one_set(db, 3)
    await _finished_workout(db, 4)

    row = (await db.onboarding_funnel(days=30))[0]

    assert row["users"] == 4
    assert row["started"] == 3  # 2, 3 и 4 все открывали тренировку
    assert row["logged_set"] == 2  # только 3 и 4 дошли до подхода
    assert row["finished"] == 1  # только 4 закрыл


async def test_onboarding_funnel_skips_pre_attribution_and_out_of_window_users(fresh_db):
    db = fresh_db
    await db.get_or_create_user(1, "legacy")
    await db.set_user_source(1, acquisition.SOURCE_LEGACY)
    await db.get_or_create_user(2, "old")
    await db.set_user_source(2, "src_a")
    long_ago = (dt.datetime.now() - dt.timedelta(days=60)).isoformat(timespec="seconds")
    await db.conn().execute("UPDATE users SET created_at = ? WHERE telegram_id = 2", (long_ago,))
    await db.conn().commit()

    assert await db.onboarding_funnel(days=30) == []


async def test_format_onboarding_funnel_shows_every_step_percent():
    text = acquisition.format_onboarding_funnel(
        [_row(source="src_kachalka", users=4, started=3, logged_set=2, finished=1)], days=30
    )
    assert "kachalka" in text
    assert "75%" in text  # started
    assert "50%" in text  # logged_set
    assert "25%" in text  # finished


def test_format_onboarding_funnel_empty_tells_no_newcomers():
    text = acquisition.format_onboarding_funnel([], days=30)
    assert "не было" in text


async def test_top_referrers(fresh_db):
    db = fresh_db
    await db.get_or_create_user(10, "inviter")
    for guest in (11, 12):
        await db.get_or_create_user(guest, f"g{guest}")
        await db.set_user_source(guest, acquisition.SOURCE_REFERRAL, referrer_id=10)
    await _finished_workout(db, 11)

    rows = await db.top_referrers()

    assert len(rows) == 1
    assert rows[0]["referrer_id"] == 10
    assert rows[0]["username"] == "inviter"
    assert rows[0]["invited"] == 2
    assert rows[0]["activated"] == 1


# ---------- тексты отчёта ----------


def _row(**kwargs):
    return kwargs


def test_format_funnel_shows_activation_percent():
    text = acquisition.format_funnel(
        [_row(source="src_kachalka", users=4, activated=1, engaged=0, alive=1)], days=30
    )
    assert "kachalka" in text
    assert "25%" in text


def test_format_funnel_empty_tells_next_step():
    text = acquisition.format_funnel([], days=30)
    assert "src_" in text  # пустой отчёт объясняет, как завести ссылку


def test_format_referrers_empty_tells_next_step():
    assert "картинке" in acquisition.format_referrers([])


# ---------- еженедельная сводка воронки для админа ----------


def test_weekly_funnel_digest_reports_the_week_totals():
    rows = [
        _row(source="src_a", users=6, started=5, logged_set=4, finished=3),
        _row(source="src_b", users=4, started=1, logged_set=0, finished=0),
    ]
    text = acquisition.build_weekly_funnel_digest(rows, days=7)
    assert "10 новых" in text
    assert "6 начали тренировку" in text
    assert "4 записали подход" in text
    assert "3 завершили первую" in text


def test_weekly_funnel_digest_names_the_worst_converting_source():
    """src_b — 0 из 4 дошли до конца, и это худший результат при достаточной
    выборке — src_c с одним человеком в неё не входит (шум, не сигнал)."""
    rows = [
        _row(source="src_a", users=6, started=5, logged_set=4, finished=3),
        _row(source="src_b", users=4, started=1, logged_set=0, finished=0),
        _row(source="src_c", users=1, started=0, logged_set=0, finished=0),
    ]
    text = acquisition.build_weekly_funnel_digest(rows, days=7)
    assert acquisition._source_title("src_b") in text
    assert "0 из 4" in text
    assert acquisition._source_title("src_c") not in text


def test_weekly_funnel_digest_is_honest_about_zero_newcomers():
    text = acquisition.build_weekly_funnel_digest([], days=7)
    assert "не пришло" in text
