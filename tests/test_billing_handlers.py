"""handlers/billing.py: счёт, подтверждение, выдача и возврат.

Три шага платежа Telegram проверяются по отдельности, потому что ломаются они
тоже по отдельности: счёт с чужим payload, pre_checkout без ответа за 10 секунд
и повторно доставленный successful_payment — три разных способа либо не отдать
оплаченное, либо отдать его дважды.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import billing
import config
from handlers import ai_trainer as ai_handler
from handlers import billing as handler

pytestmark = pytest.mark.asyncio


def _message(user_id=777, text=None, username="tester"):
    message = MagicMock()
    message.from_user = SimpleNamespace(id=user_id, username=username)
    message.text = text
    message.answer = AsyncMock()
    message.answer_invoice = AsyncMock()
    message.bot = MagicMock()
    message.bot.send_message = AsyncMock()
    message.bot.refund_star_payment = AsyncMock()
    return message


def _callback(data, user_id=777):
    callback = MagicMock()
    callback.data = data
    callback.from_user = SimpleNamespace(id=user_id, username="tester")
    callback.answer = AsyncMock()
    callback.message = _message(user_id)
    return callback


def _paid(charge="ch-1", payload=f"{billing.PRO_MONTH}:777", amount=150, user_id=777):
    message = _message(user_id)
    message.successful_payment = SimpleNamespace(
        invoice_payload=payload,
        telegram_payment_charge_id=charge,
        total_amount=amount,
        currency="XTR",
    )
    return message


async def test_invoice_goes_out_in_stars_without_a_provider(fresh_db, user_id):
    """Валюта XTR и пустой provider_token — это не забытые параметры, а
    единственный способ продать цифровое внутри Telegram."""
    callback = _callback(f"buy:{billing.PRO_MONTH}")

    await handler.send_invoice(callback)

    kwargs = callback.message.answer_invoice.await_args.kwargs
    assert kwargs["currency"] == "XTR"
    assert kwargs["provider_token"] == ""
    assert kwargs["prices"][0].amount == config.PRO_PRICE_STARS
    assert kwargs["payload"] == f"{billing.PRO_MONTH}:777"


async def test_unknown_product_does_not_bill_anything(fresh_db):
    callback = _callback("buy:луна")

    await handler.send_invoice(callback)

    callback.message.answer_invoice.assert_not_awaited()
    assert callback.answer.await_args.kwargs["show_alert"] is True


async def test_kill_switch_refuses_to_sell(fresh_db, monkeypatch):
    monkeypatch.setattr(config, "STARS_PAYMENTS_ENABLED", False)
    callback = _callback(f"buy:{billing.PRO_MONTH}")

    await handler.send_invoice(callback)

    callback.message.answer_invoice.assert_not_awaited()


async def test_pre_checkout_lets_a_good_payment_through(fresh_db):
    query = MagicMock()
    query.invoice_payload = f"{billing.PRO_MONTH}:777"
    query.from_user = SimpleNamespace(id=777)
    query.answer = AsyncMock()

    await handler.pre_checkout(query)

    query.answer.assert_awaited_once_with(ok=True)


async def test_pre_checkout_refuses_someone_elses_invoice(fresh_db):
    """Счёт, пересланный в другой чат, не должен открывать доступ тому, кто его
    оплатил вместо адресата."""
    query = MagicMock()
    query.invoice_payload = f"{billing.PRO_MONTH}:777"
    query.from_user = SimpleNamespace(id=999)
    query.answer = AsyncMock()

    await handler.pre_checkout(query)

    assert query.answer.await_args.kwargs["ok"] is False


async def test_pre_checkout_refuses_an_unknown_product(fresh_db):
    query = MagicMock()
    query.invoice_payload = "луна:777"
    query.from_user = SimpleNamespace(id=777)
    query.answer = AsyncMock()

    await handler.pre_checkout(query)

    assert query.answer.await_args.kwargs["ok"] is False


async def test_payment_grants_the_month(fresh_db, monkeypatch):
    monkeypatch.setattr(config, "ADMIN_ID", None)
    await fresh_db.get_or_create_user(telegram_id=777, username="tester")

    await handler.on_paid(_paid())

    assert await billing.is_pro(777) is True
    assert (await fresh_db.get_star_payment("ch-1"))["stars"] == 150


async def test_redelivered_payment_does_not_grant_twice(fresh_db, monkeypatch):
    monkeypatch.setattr(config, "ADMIN_ID", None)
    await fresh_db.get_or_create_user(telegram_id=777, username="tester")

    await handler.on_paid(_paid())
    first = (await fresh_db.get_billing(777))["pro_until"]
    await handler.on_paid(_paid())

    assert (await fresh_db.get_billing(777))["pro_until"] == first


async def test_pack_payment_adds_questions(fresh_db, monkeypatch):
    monkeypatch.setattr(config, "ADMIN_ID", None)
    await fresh_db.get_or_create_user(telegram_id=777, username="tester")

    await handler.on_paid(_paid(payload=f"{billing.QUESTION_PACK}:777", amount=50))

    assert (await fresh_db.get_billing(777))["pack_questions"] == config.PACK_QUESTIONS


async def test_payment_for_a_retired_product_is_not_silently_eaten(fresh_db, monkeypatch):
    """Деньги уже списаны, поэтому нельзя ни промолчать, ни ответить
    «неизвестный товар» — человека надо вывести на возврат."""
    monkeypatch.setattr(config, "ADMIN_ID", None)
    message = _paid(payload="луна:777")

    await handler.on_paid(message)

    assert await fresh_db.get_star_payment("ch-1") is None
    assert "верну" in message.answer.await_args.args[0]


async def test_admin_gets_the_charge_id_to_refund_with(fresh_db, monkeypatch):
    monkeypatch.setattr(config, "ADMIN_ID", 12345)
    await fresh_db.get_or_create_user(telegram_id=777, username="tester")
    message = _paid()

    await handler.on_paid(message)

    assert "ch-1" in message.bot.send_message.await_args.args[1]


async def test_admin_notification_failure_does_not_break_the_grant(fresh_db, monkeypatch):
    monkeypatch.setattr(config, "ADMIN_ID", 12345)
    await fresh_db.get_or_create_user(telegram_id=777, username="tester")
    message = _paid()
    message.bot.send_message = AsyncMock(side_effect=RuntimeError("админ заблокировал бота"))

    await handler.on_paid(message)

    assert await billing.is_pro(777) is True


async def test_refund_returns_stars_and_takes_access_back(fresh_db, monkeypatch):
    monkeypatch.setattr(config, "ADMIN_ID", 12345)
    await fresh_db.get_or_create_user(telegram_id=777, username="tester")
    await handler.on_paid(_paid())

    message = _message(user_id=12345, text="/refund ch-1")
    await handler.cmd_refund(message)

    message.bot.refund_star_payment.assert_awaited_once()
    assert await billing.is_pro(777) is False
    assert (await fresh_db.get_star_payment("ch-1"))["refunded_at"] is not None


async def test_refund_is_admin_only(fresh_db, monkeypatch):
    monkeypatch.setattr(config, "ADMIN_ID", 12345)
    await fresh_db.get_or_create_user(telegram_id=777, username="tester")
    await handler.on_paid(_paid())

    message = _message(user_id=777, text="/refund ch-1")
    await handler.cmd_refund(message)

    message.bot.refund_star_payment.assert_not_awaited()
    assert await billing.is_pro(777) is True


async def test_refund_twice_is_refused(fresh_db, monkeypatch):
    monkeypatch.setattr(config, "ADMIN_ID", 12345)
    await fresh_db.get_or_create_user(telegram_id=777, username="tester")
    await handler.on_paid(_paid())
    first = _message(user_id=12345, text="/refund ch-1")
    await handler.cmd_refund(first)

    second = _message(user_id=12345, text="/refund ch-1")
    await handler.cmd_refund(second)

    second.bot.refund_star_payment.assert_not_awaited()
    assert "уже возвращён" in second.answer.await_args.args[0]


async def test_refund_rejected_by_telegram_keeps_the_record_intact(fresh_db, monkeypatch):
    """Telegram отказал — значит звёзды у человека остались, и доступ забирать
    нельзя: иначе он платил и остался ни с чем."""
    monkeypatch.setattr(config, "ADMIN_ID", 12345)
    await fresh_db.get_or_create_user(telegram_id=777, username="tester")
    await handler.on_paid(_paid())

    message = _message(user_id=12345, text="/refund ch-1")
    message.bot.refund_star_payment = AsyncMock(side_effect=RuntimeError("CHARGE_ALREADY_REFUNDED"))
    await handler.cmd_refund(message)

    assert await billing.is_pro(777) is True
    assert (await fresh_db.get_star_payment("ch-1"))["refunded_at"] is None


async def test_offer_screen_promises_the_diary_stays_free(fresh_db, user_id):
    """Главное обещание витрины: пейволла на собственную историю не будет."""
    message = _message(user_id=user_id)

    await handler.cmd_premium(message)

    text = message.answer.await_args.args[0]
    assert "Дневник" in text
    assert str(config.PRO_PRICE_STARS) in text


async def test_offer_screen_shows_a_paid_user_their_date(fresh_db, user_id):
    await billing.grant(user_id, billing.PRO_MONTH)
    message = _message(user_id=user_id)

    await handler.cmd_premium(message)

    assert "ТРЕНЕР ОПЛАЧЕН" in message.answer.await_args.args[0]


async def test_offer_screen_off_when_selling_is_off(fresh_db, user_id, monkeypatch):
    monkeypatch.setattr(config, "STARS_PAYMENTS_ENABLED", False)
    message = _message(user_id=user_id)

    await handler.cmd_premium(message)

    assert message.answer.await_args.kwargs["reply_markup"] is None

# --- Пейволл в самом тренере ------------------------------------------------
#
# Экран лимита — единственное место, где человек узнаёт о платном сам, не
# заходя в /premium. Ошибиться тут можно двумя способами, и оба дорогие: обещать
# «приходи завтра» тому, у кого кончился месяц (завтра ничего не изменится), и
# показать витрину новичку (читается как «бот оказался платным»).


async def test_day_limit_still_says_come_back_tomorrow(fresh_db, user_id, monkeypatch):
    monkeypatch.setattr(config, "AI_QUESTION_DAILY_LIMIT", 1)
    monkeypatch.setattr(ai_handler, "ai_keyboard", AsyncMock(return_value="ai-kb"))
    await billing.charge_question(user_id)

    text, markup = await ai_handler._limit_screen(user_id, await billing.allowance(user_id))

    assert text == ai_handler.DAILY_LIMIT_TEXT
    assert markup == "ai-kb"


async def test_month_limit_offers_the_paid_way_out(fresh_db, user_id, monkeypatch):
    monkeypatch.setattr(config, "AI_QUESTION_MONTHLY_FREE", 1)
    monkeypatch.setattr(config, "PAYWALL_MIN_WORKOUTS", 0)
    await billing.charge_question(user_id)

    text, markup = await ai_handler._limit_screen(user_id, await billing.allowance(user_id))

    assert "месяц" in text
    # Дневник остаётся при человеке — это обещание держится и на пейволле.
    assert "Дневник" in text
    assert markup is not None


async def test_month_limit_stays_quiet_for_a_newcomer(fresh_db, user_id, monkeypatch):
    monkeypatch.setattr(config, "AI_QUESTION_MONTHLY_FREE", 1)
    monkeypatch.setattr(config, "PAYWALL_MIN_WORKOUTS", 5)
    monkeypatch.setattr(ai_handler, "ai_keyboard", AsyncMock(return_value="ai-kb"))
    await billing.charge_question(user_id)

    text, _ = await ai_handler._limit_screen(user_id, await billing.allowance(user_id))

    assert "платный доступ" not in text
    assert "Первого числа" in text
