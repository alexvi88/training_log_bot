"""Форвард поста в фитнес-канале → тренер разбирает: что дело, что бред.

Фильтр — `_looks_like_a_forwarded_post` — и сам хендлер проверяются раздельно:
фильтр решает, наш это апдейт или нет (и должен пропускать всё, что не форвард
с текстом подходящей длины, дальше по цепочке роутеров), хендлер — что
происходит, когда фильтр сработал.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import ai_limits
import ai_trainer
import config
import running_texts
from handlers import factcheck


def _message(text="х" * 50, forwarded=True, user_id=777, *, caption=None, photo=None):
    """Дубль форварда.

    Все «не наши» типы вложений выставляются в None явно: у MagicMock любой
    неупомянутый атрибут — правдивый мок, так что без этого фильтр видел бы в
    каждом сообщении и видео, и документ разом (и отсеивал бы всё)."""
    message = MagicMock()
    message.from_user = SimpleNamespace(id=user_id)
    message.text = text
    message.caption = caption
    message.photo = photo
    message.video = None
    message.video_note = None
    message.animation = None
    message.document = None
    message.forward_origin = SimpleNamespace(type="channel") if forwarded else None
    message.reply = AsyncMock(return_value=SimpleNamespace(edit_text=AsyncMock()))
    return message


@pytest.fixture(autouse=True)
def _cheap_day(monkeypatch):
    monkeypatch.setattr(ai_limits, "daily_spend_usd", AsyncMock(return_value=0.0))


@pytest.fixture(autouse=True)
def _configured(monkeypatch):
    monkeypatch.setattr(ai_trainer, "is_configured", lambda: True)


@pytest.fixture(autouse=True)
def _clear_busy():
    """`factcheck._busy` — module-level state; каждый тест должен начинать с
    пустого замка, а не с того, что мог оставить (или не оставить) прошлый."""
    factcheck._busy.clear()
    yield
    factcheck._busy.clear()


# ---------- фильтр ----------


def test_filter_ignores_non_forwarded_text():
    """Обычное сообщение — не наш случай, пусть решает следующий роутер."""
    assert not factcheck._looks_like_a_forwarded_post(_message(forwarded=False))


def test_filter_takes_a_forwarded_photo_with_a_caption():
    """Пост из канала чаще всего приходит картинкой с текстом в caption — и
    именно они проваливались мимо разбора в первой версии, потому что фильтр
    смотрел только message.text."""
    message = _message(text=None, caption="х" * 50, photo=[SimpleNamespace(file_size=1024)])
    assert factcheck._looks_like_a_forwarded_post(message)


def test_filter_ignores_forwarded_media_with_no_text_at_all():
    """Голая картинка без подписи — разбирать нечего, пусть идёт дальше."""
    message = _message(text=None, photo=[SimpleNamespace(file_size=1024)])
    assert not factcheck._looks_like_a_forwarded_post(message)


def test_filter_ignores_forwarded_video():
    """Видео — не наш случай: перехватить и промолчать хуже, чем не
    перехватывать (ролик подхода разбирает чат тренера, а не фактчек)."""
    message = _message()
    message.video = SimpleNamespace(duration=10)
    assert not factcheck._looks_like_a_forwarded_post(message)


def test_filter_ignores_too_short_forward():
    """«го», «спс», обрывок стикер-подписи — не пост, разбирать нечего."""
    assert not factcheck._looks_like_a_forwarded_post(_message(text="го"))


def test_filter_ignores_too_long_forward():
    assert not factcheck._looks_like_a_forwarded_post(_message(text="х" * 4001))


def test_filter_accepts_forwarded_post_of_reasonable_length():
    assert factcheck._looks_like_a_forwarded_post(_message())


def test_filter_falls_through_when_ai_is_not_configured(monkeypatch):
    """AI не настроен — форвард долетает до fallback.unhandled_text, а не
    съедается молча этим роутером."""
    monkeypatch.setattr(ai_trainer, "is_configured", lambda: False)
    assert not factcheck._looks_like_a_forwarded_post(_message())


# ---------- хендлер ----------


@pytest.mark.asyncio
async def test_forward_blocked_by_question_quota_gets_no_verdict(monkeypatch):
    block = ai_limits.Block(kind=ai_limits.KIND_QUESTION, log="exhausted", user_text="Лимит на сегодня")
    monkeypatch.setattr(ai_limits, "check", AsyncMock(return_value=block))
    fact_check = AsyncMock()
    monkeypatch.setattr(ai_trainer, "fact_check_post", fact_check)
    message = _message()

    await factcheck.factcheck_forward(message)

    fact_check.assert_not_called()
    message.reply.assert_awaited_once_with("Лимит на сегодня", reply_markup=None)


@pytest.mark.asyncio
async def test_forward_shows_placeholder_then_edits_to_verdict(monkeypatch):
    monkeypatch.setattr(ai_limits, "check", AsyncMock(return_value=None))
    monkeypatch.setattr(
        ai_trainer, "fact_check_post", AsyncMock(return_value="Дело — так и работает.")
    )
    increment = AsyncMock()
    monkeypatch.setattr("handlers.factcheck.db.try_increment_ai_question_count", increment)
    message = _message(user_id=42)

    await factcheck.factcheck_forward(message)

    message.reply.assert_awaited_once()
    assert message.reply.await_args.args[0] in running_texts.FACT_CHECK_POOL
    sent = message.reply.return_value
    sent.edit_text.assert_awaited_once()
    assert "Дело" in sent.edit_text.await_args.args[0]
    increment.assert_awaited_once_with(42, config.AI_QUESTION_DAILY_LIMIT)


@pytest.mark.asyncio
async def test_forward_does_not_charge_quota_when_model_call_fails(monkeypatch):
    """Провайдер упал — вопрос не должен списаться, как и у обычного чата."""
    monkeypatch.setattr(ai_limits, "check", AsyncMock(return_value=None))
    monkeypatch.setattr(
        ai_trainer, "fact_check_post", AsyncMock(side_effect=RuntimeError("boom"))
    )
    increment = AsyncMock()
    monkeypatch.setattr("handlers.factcheck.db.try_increment_ai_question_count", increment)
    message = _message()

    await factcheck.factcheck_forward(message)

    increment.assert_not_called()
    sent = message.reply.return_value
    sent.edit_text.assert_awaited_once()
    assert "сломалось" in sent.edit_text.await_args.args[0]


@pytest.mark.asyncio
async def test_preview_account_gets_warning_but_still_receives_verdict(monkeypatch):
    """Свой аккаунт до «Понятно» — предупреждение не отменяет действие
    (см. ai_limits.py и такую же развилку в остальных вызовах check)."""
    block = ai_limits.Block(
        kind=ai_limits.KIND_QUESTION, log="preview", user_text="Предупреждение", preview=True
    )
    monkeypatch.setattr(ai_limits, "check", AsyncMock(return_value=block))
    monkeypatch.setattr(ai_limits, "reply", AsyncMock())
    monkeypatch.setattr(ai_trainer, "fact_check_post", AsyncMock(return_value="Бред."))
    monkeypatch.setattr("handlers.factcheck.db.try_increment_ai_question_count", AsyncMock())
    message = _message()

    await factcheck.factcheck_forward(message)

    ai_limits.reply.assert_awaited_once()
    assert message.reply.await_args.args[0] in running_texts.FACT_CHECK_POOL


# ---------- busy-замок ----------
#
# Пачка пересланных постов подряд от одного человека раньше обходила квоту:
# ai_limits.check читает счётчик до ответа модели, а списывается он только
# после — и вторая (третья, ...) реплика в этом же промежутке видела тот же
# непотраченный счётчик, что и первая.


def test_try_claim_busy_rejects_a_second_call_for_the_same_user():
    assert factcheck._try_claim_busy(1) is True
    assert factcheck._try_claim_busy(1) is False
    factcheck._busy.discard(1)
    assert factcheck._try_claim_busy(1) is True


def test_try_claim_busy_does_not_block_other_users():
    assert factcheck._try_claim_busy(1) is True
    assert factcheck._try_claim_busy(2) is True


@pytest.mark.asyncio
async def test_second_forward_from_the_same_user_is_rejected_while_the_first_is_in_flight(monkeypatch):
    """Не квота, не check — сам факт «уже разбираю этот же форвард» должен
    остановить второй запрос ДО того, как он вообще коснётся ai_limits."""
    check = AsyncMock(return_value=None)
    monkeypatch.setattr(ai_limits, "check", check)
    monkeypatch.setattr(ai_trainer, "fact_check_post", AsyncMock(return_value="Дело."))
    monkeypatch.setattr("handlers.factcheck.db.try_increment_ai_question_count", AsyncMock())
    factcheck._busy.add(777)  # как будто разбор прошлого форварда этого же человека ещё идёт
    message = _message()

    await factcheck.factcheck_forward(message)

    check.assert_not_called()
    message.reply.assert_awaited_once_with("Секунду, ещё разбираю прошлый пост 😅")


@pytest.mark.asyncio
async def test_busy_is_released_even_when_the_model_call_fails(monkeypatch):
    """Замок не должен протекать навсегда: сорвавшийся вызов обязан
    освободить `_busy`, иначе следующий форвард этого же человека молчал бы
    до перезапуска процесса."""
    monkeypatch.setattr(ai_limits, "check", AsyncMock(return_value=None))
    monkeypatch.setattr(ai_trainer, "fact_check_post", AsyncMock(side_effect=RuntimeError("boom")))
    message = _message()

    await factcheck.factcheck_forward(message)

    assert 777 not in factcheck._busy
