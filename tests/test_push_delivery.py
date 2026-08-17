"""Push delivery: every push goes out as the coach photo with the text as its caption."""

import datetime as dt
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter

import engagement
import i18n
import push_texts

pytestmark = pytest.mark.asyncio

_TODAY = dt.date(2026, 5, 4)


def _bot(file_id: str = "AgAD_new_upload"):
    bot = MagicMock()
    bot.send_photo = AsyncMock(
        return_value=SimpleNamespace(photo=[SimpleNamespace(file_id=file_id)])
    )
    return bot


@pytest.fixture(autouse=True)
def reset_cached_file_id(monkeypatch):
    monkeypatch.setattr(engagement, "_push_image_file_id", None)
    yield
    monkeypatch.setattr(engagement, "_push_image_file_id", None)


async def test_push_is_sent_as_a_photo_with_the_text_as_caption(fresh_db, user_id):
    bot = _bot()
    decision = engagement.PushDecision(push_texts.SKIP_3, "ПРИВЕТ АТЛЕТ! Третий день без зала.")

    await engagement._deliver(bot, user_id, decision, _TODAY)

    bot.send_photo.assert_awaited_once()
    kwargs = bot.send_photo.await_args.kwargs
    assert kwargs["chat_id"] == user_id
    assert kwargs["caption"] == decision.text
    assert kwargs["reply_markup"] is not None
    assert await fresh_db.has_push_today(user_id, _TODAY.isoformat())


async def test_push_upload_is_cached_and_reused_by_file_id(fresh_db, user_id):
    bot = _bot(file_id="AgAD_cached")
    decision = engagement.PushDecision(push_texts.SKIP_3, "ПРИВЕТ АТЛЕТ! Третий день без зала.")

    await engagement._deliver(bot, user_id, decision, _TODAY)
    first_photo = bot.send_photo.await_args.kwargs["photo"]

    await engagement._deliver(bot, user_id, decision, _TODAY)
    second_photo = bot.send_photo.await_args.kwargs["photo"]

    assert second_photo == "AgAD_cached"
    assert first_photo != second_photo


async def test_push_cta_continues_the_coach_line_per_category(fresh_db, user_id):
    """Кнопка — последняя строка пуша: под «серия на кону» она говорит «Спасти
    серию», а категории без своей строки получают нейтральный дефолт."""
    bot = _bot()

    await engagement._deliver(
        bot, user_id, engagement.PushDecision(push_texts.STREAK_AT_RISK, "текст"), _TODAY
    )
    kb = bot.send_photo.await_args.kwargs["reply_markup"]
    assert kb.inline_keyboard[0][0].text == "▶ Спасти серию"

    await engagement._deliver(
        bot, user_id, engagement.PushDecision(push_texts.SKIP_3, "текст"), _TODAY
    )
    kb = bot.send_photo.await_args.kwargs["reply_markup"]
    # DEFAULT_PUSH_CTA — ключ каталога (push.cta.default), не готовый текст:
    # кнопка рендерит его через keyboards.push_cta_keyboard -> i18n.t(...), а
    # тут сверяем результат этого рендера, а не сам ключ.
    assert kb.inline_keyboard[0][0].text == "▶ Начать тренировку"


async def test_cta_is_translated_only_once(fresh_db, user_id, caplog):
    """_deliver уже резолвит ключ в готовый текст CTA — если keyboards.push_cta_keyboard
    зовёт i18n.t() на этом готовом тексте ещё раз (её параметр по докстрингу — КЛЮЧ,
    не готовая строка), она ищет в каталоге строку вроде «▶ Начать тренировку» как
    ключ, не находит и логирует ложный WARNING «ключ не найден» — который к тому же
    засоряет i18n._warned_missing_keys и может заглушить настоящую будущую пропажу."""
    i18n.reload()  # чистый _warned_missing_keys — иначе прошлый тест мог уже «предупредить»
    bot = _bot()

    with caplog.at_level("WARNING", logger="i18n"):
        await engagement._deliver(
            bot, user_id, engagement.PushDecision(push_texts.SKIP_3, "текст"), _TODAY
        )

    assert "не найден" not in caplog.text
    kb = bot.send_photo.await_args.kwargs["reply_markup"]
    assert kb.inline_keyboard[0][0].text == "▶ Начать тренировку"


async def test_a_digest_without_cta_still_offers_the_menu(fresh_db, user_id):
    """С with_cta=False своей кнопки у пуша нет — но и тупиком он быть не
    должен: тренер подводит итоги недели и советует, что добрать, а вся сводка
    с объёмом и рекордами лежит ровно в одном экране отсюда.

    Колбэк — тот же, что у карточки законченной тренировки: он открывает меню,
    НЕ удаляя сообщение, с которого пришли. Дайджест перечитывают."""
    await fresh_db.mark_tz_set_by_user(user_id)
    bot = _bot()
    decision = engagement.PushDecision(push_texts.WEEKLY_DIGEST, "текст", with_cta=False)

    await engagement._deliver(bot, user_id, decision, _TODAY)

    kb = bot.send_photo.await_args.kwargs["reply_markup"]
    assert [b.text for row in kb.inline_keyboard for b in row] == ["🏠 Меню"]
    assert kb.inline_keyboard[0][0].callback_data == "live:back_to_menu"


async def test_a_push_with_its_own_cta_does_not_add_the_menu(fresh_db, user_id):
    """«▶ Начать тренировку» — и есть то действие, ради которого пуш послан;
    вторая строка под ним только растащила бы внимание."""
    await fresh_db.mark_tz_set_by_user(user_id)
    bot = _bot()

    await engagement._deliver(
        bot, user_id, engagement.PushDecision(push_texts.SKIP_3, "текст"), _TODAY
    )

    kb = bot.send_photo.await_args.kwargs["reply_markup"]
    assert "🏠 Меню" not in [b.text for row in kb.inline_keyboard for b in row]


async def test_a_caption_over_telegrams_limit_is_truncated(fresh_db, user_id):
    """The AI weekly digest is free-form model output and can run long."""
    bot = _bot()
    long_text = "ПРИВЕТ АТЛЕТ! " + "а" * 2000
    decision = engagement.PushDecision(push_texts.AI_WEEKLY, long_text)

    await engagement._deliver(bot, user_id, decision, _TODAY)

    caption = bot.send_photo.await_args.kwargs["caption"]
    assert len(caption) == engagement.CAPTION_LIMIT
    assert caption.endswith("…")


async def test_push_goes_out_without_sound(fresh_db, user_id):
    """Весь бот отправляет молча (DefaultBotProperties), и пуш был единственным
    местом, которое это перебивало и звенело — в том числе ночью."""
    bot = _bot()
    decision = engagement.PushDecision(push_texts.SKIP_3, "текст")

    await engagement._deliver(bot, user_id, decision, _TODAY)

    assert bot.send_photo.await_args.kwargs["disable_notification"] is True


async def test_retry_after_resend_is_also_silent(fresh_db, user_id):
    """Повтор после 429 — вторая копия того же вызова, и звук возвращался бы
    именно через неё."""
    bot = MagicMock()
    bot.send_photo = AsyncMock(
        side_effect=[
            TelegramRetryAfter(method=MagicMock(), message="flood", retry_after=0),
            SimpleNamespace(photo=[SimpleNamespace(file_id="fid")]),
        ]
    )

    await engagement._deliver(bot, user_id, engagement.PushDecision(push_texts.SKIP_3, "текст"), _TODAY)

    assert bot.send_photo.await_count == 2
    for call in bot.send_photo.await_args_list:
        assert call.kwargs["disable_notification"] is True


async def test_blocked_user_drops_out_of_the_push_pool(fresh_db, user_id):
    """Раньше блокировка только логировалась: человек оставался в пуле навсегда,
    и по воскресеньям модель писала дайджест для того, кто его не получит."""
    await fresh_db.create_finished_workout(
        user_id, started_at="2026-07-01T10:00:00", finished_at="2026-07-01T11:00:00"
    )
    assert [uid for uid, _ in await fresh_db.list_engagement_eligible_user_ids()] == [user_id]

    bot = MagicMock()
    bot.send_photo = AsyncMock(
        side_effect=TelegramForbiddenError(method=MagicMock(), message="bot was blocked by the user")
    )

    await engagement._deliver(bot, user_id, engagement.PushDecision(push_texts.SKIP_3, "текст"), _TODAY)

    assert (await fresh_db.get_user(user_id))["pushes_enabled"] == 0
    assert await fresh_db.list_engagement_eligible_user_ids() == []
    # запись о пуше не делается: он не дошёл
    assert not await fresh_db.has_push_today(user_id, _TODAY.isoformat())


async def test_one_failed_delivery_does_not_stop_the_rest_of_the_tick(fresh_db, user_id, monkeypatch):
    """_deliver runs inside a loop over every due user. An exception escaping it
    aborted the whole tick, so everyone after the failing recipient got nothing
    — and by the next tick their send hour had passed, losing the push for the
    day rather than merely delaying it."""
    import db as dbmod
    import engagement as eng

    other = (await dbmod.get_or_create_user(telegram_id=333, username="third"))["telegram_id"]
    for uid in (user_id, other):
        await dbmod.create_finished_workout(
            uid, started_at="2026-07-01T10:00:00", finished_at="2026-07-01T11:00:00"
        )

    delivered = []

    async def send_photo(*, chat_id, **kwargs):
        if not delivered:
            delivered.append(chat_id)
            raise TelegramBadRequest(method=MagicMock(), message="chat not found")
        delivered.append(chat_id)
        return SimpleNamespace(photo=[SimpleNamespace(file_id="fid")])

    bot = MagicMock()
    bot.send_photo = AsyncMock(side_effect=send_photo)

    async def build(telegram_id, today):
        return engagement.PushDecision(push_texts.SKIP_3, "текст")

    monkeypatch.setattr(eng, "build_daily_push", build)
    monkeypatch.setattr(eng, "should_send_now", lambda tz, hour: True)
    monkeypatch.setattr(eng, "SEND_DELAY", 0)

    await eng._send_daily_pushes(bot)

    assert sorted(delivered) == sorted([user_id, other])
