"""Английская локализация пуш-текстов: формула, отсутствие кириллицы, ротация
и язык в фоновом цикле — см. TONE_OF_VOICE.md ("English voice") и
push_texts.py (устройство пулов по каталогу)."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import db as dbmod
import engagement
import i18n
import i18n_coverage
import push_texts

# Три первые проверки ниже синхронные (читают TEXTS_BY_LANG напрямую, без БД) —
# помечаем asyncio только там, где он реально нужен, а не всем модулем сразу,
# иначе pytest-asyncio предупреждает на каждой синхронной функции.


def test_every_category_has_a_nonempty_english_pool():
    for category in push_texts._CATEGORIES:
        pool = push_texts.TEXTS_BY_LANG["en"][category]
        assert pool, f"no English copy for {category}"
        # тот же размер, что и русский пул: одинаковый набор ключей
        # push.<category>.<n> в обоих каталогах (см. push_texts._load_pool).
        assert len(pool) == len(push_texts.TEXTS_BY_LANG["ru"][category]), category


def test_no_cyrillic_in_any_english_push_variant():
    leaks = {}
    for category in push_texts._CATEGORIES:
        for text in push_texts.TEXTS_BY_LANG["en"][category]:
            if i18n_coverage.has_cyrillic(text):
                leaks.setdefault(category, []).append(text)
    assert not leaks, f"кириллица в английских пуш-текстах: {leaks}"


def test_yo_athlete_formula_in_every_english_push():
    for category in push_texts._CATEGORIES:
        for text in push_texts.TEXTS_BY_LANG["en"][category]:
            assert text.startswith("YO ATHLETE!") or text.startswith("YO ATHLETE."), text
            assert "YO ATHLETE," not in text, text
            for banned in ("fighter", "buddy", " user "):
                assert banned not in text.lower(), text


@pytest.mark.asyncio
async def test_english_bag_rotation_has_no_repeats_until_exhausted(fresh_db, user_id):
    pool = push_texts.TEXTS_BY_LANG["en"][push_texts.WIN_BACK]
    with i18n.use_lang("en"):
        seen = [await push_texts.pick_text(user_id, push_texts.WIN_BACK) for _ in range(len(pool))]
    assert sorted(seen) == sorted(pool)


@pytest.mark.asyncio
async def test_english_bag_reshuffles_after_the_pool_is_exhausted(fresh_db, user_id):
    pool = push_texts.TEXTS_BY_LANG["en"][push_texts.SKIP_3]
    with i18n.use_lang("en"):
        first_cycle = [await push_texts.pick_text(user_id, push_texts.SKIP_3) for _ in range(len(pool))]
        second_cycle = [await push_texts.pick_text(user_id, push_texts.SKIP_3) for _ in range(len(pool))]
    assert sorted(first_cycle) == sorted(pool)
    assert sorted(second_cycle) == sorted(pool)


@pytest.mark.asyncio
async def test_background_loop_sets_language_per_user(fresh_db, monkeypatch):
    """Джоба идёт по многим пользователям в ОДНОЙ задаче — без явной установки
    языка на каждого все получили бы язык того, кто попался в цикле первым
    (см. engagement._send_daily_pushes и i18n.current_lang: один contextvar
    на весь async-таск). Два пользователя с разными языками должны получить
    каждый свой текст в одном и том же тике."""
    ru_user = (await dbmod.get_or_create_user(telegram_id=501, username="ru_user"))["telegram_id"]
    en_user = (await dbmod.get_or_create_user(telegram_id=502, username="en_user"))["telegram_id"]
    await dbmod.set_user_lang(en_user, "en")
    for uid in (ru_user, en_user):
        await dbmod.create_finished_workout(
            uid, started_at="2026-07-01T10:00:00", finished_at="2026-07-01T11:00:00"
        )

    captured: dict[int, str] = {}

    async def send_photo(*, chat_id, caption, **kwargs):
        captured[chat_id] = caption
        return SimpleNamespace(photo=[SimpleNamespace(file_id="fid")])

    bot = MagicMock()
    bot.send_photo = AsyncMock(side_effect=send_photo)

    async def build(telegram_id, today):
        text = await push_texts.pick_text(telegram_id, push_texts.SKIP_3)
        return engagement.PushDecision(push_texts.SKIP_3, text)

    monkeypatch.setattr(engagement, "build_daily_push", build)
    monkeypatch.setattr(engagement, "should_send_now", lambda tz, hour: True)
    monkeypatch.setattr(engagement, "SEND_DELAY", 0)

    await engagement._send_daily_pushes(bot)

    assert set(captured) == {ru_user, en_user}
    assert captured[ru_user].startswith("ПРИВЕТ АТЛЕТ!")
    assert captured[en_user].startswith("YO ATHLETE!")
    assert not i18n_coverage.has_cyrillic(captured[en_user])
