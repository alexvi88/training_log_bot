"""handlers/donate.py: экран доната, три шага платежа звёздами и идемпотентность.

Три шага проверяются по отдельности, потому что ломаются они тоже по
отдельности: счёт с чужим payload, pre_checkout без ответа за 10 секунд и
повторно доставленный successful_payment — три разных способа либо не
поблагодарить, либо поблагодарить (и уведомить админа) дважды.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from aiogram.types import CallbackQuery

import config
import keyboards
from handlers import donate as handler


def _message(user_id=777, username="tester"):
    message = MagicMock()
    message.from_user = SimpleNamespace(id=user_id, username=username, language_code=None)
    message.answer = AsyncMock()
    message.answer_invoice = AsyncMock()
    message.delete = AsyncMock()
    message.bot = MagicMock()
    message.bot.send_message = AsyncMock()
    return message


def _callback(data, user_id=777):
    # spec=CallbackQuery matters: handler.open_donate's ui.safe_edit branches on
    # isinstance(target, CallbackQuery) — an un-spec'd MagicMock fails that
    # check and silently answers on the wrong object (the callback itself,
    # not its message).
    callback = MagicMock(spec=CallbackQuery)
    callback.data = data
    callback.from_user = SimpleNamespace(id=user_id, username="tester", language_code=None)
    callback.answer = AsyncMock()
    callback.message = _message(user_id)
    return callback


def _paid(charge="ch-1", stars=150, user_id=777):
    message = _message(user_id)
    message.successful_payment = SimpleNamespace(
        invoice_payload=f"donate:{stars}:{user_id}",
        telegram_payment_charge_id=charge,
        total_amount=stars,
        currency="XTR",
    )
    return message


# ---------- главное меню ----------


def test_donate_button_shows_up_when_enabled():
    kb = keyboards.main_menu(has_active_workout=False, show_donate=True)
    callbacks = [b.callback_data for row in kb.inline_keyboard for b in row]
    assert "menu:donate" in callbacks


def test_donate_button_is_hidden_when_disabled():
    kb = keyboards.main_menu(has_active_workout=False, show_donate=False)
    callbacks = [b.callback_data for row in kb.inline_keyboard for b in row]
    assert "menu:donate" not in callbacks


def test_donate_button_is_the_very_last_row():
    """Полноширинной строкой в самом низу — под AI-тренером."""
    kb = keyboards.main_menu(has_active_workout=False, show_donate=True)
    last_row = kb.inline_keyboard[-1]
    assert len(last_row) == 1
    assert last_row[0].callback_data == "menu:donate"


# ---------- экран доната ----------


async def test_donate_screen_offers_three_presets_and_a_back_button(monkeypatch):
    monkeypatch.setattr(config, "DONATIONS_ENABLED", True)
    callback = _callback("menu:donate")

    await handler.open_donate(callback)

    kb = callback.message.answer.await_args.kwargs["reply_markup"]
    callbacks = [b.callback_data for row in kb.inline_keyboard for b in row]
    assert callbacks == ["donate:pay:50", "donate:pay:150", "donate:pay:500", "donate:back"]


async def test_disabled_donations_hide_the_screen_and_alert_instead(monkeypatch):
    monkeypatch.setattr(config, "DONATIONS_ENABLED", False)
    callback = _callback("menu:donate")

    await handler.open_donate(callback)

    callback.message.answer.assert_not_awaited()
    assert callback.answer.await_args.kwargs["show_alert"] is True


# ---------- шаг 1: счёт ----------


async def test_invoice_goes_out_in_stars_without_a_provider(monkeypatch):
    """Валюта XTR и пустой provider_token — не забытые параметры, а
    единственный способ продать цифровое внутри Telegram."""
    monkeypatch.setattr(config, "DONATIONS_ENABLED", True)
    callback = _callback("donate:pay:150")

    await handler.send_donate_invoice(callback)

    kwargs = callback.message.answer_invoice.await_args.kwargs
    assert kwargs["currency"] == "XTR"
    assert kwargs["provider_token"] == ""
    assert kwargs["prices"][0].amount == 150
    assert kwargs["payload"] == "donate:150:777"


async def test_invoice_rejects_an_amount_outside_the_presets(monkeypatch):
    monkeypatch.setattr(config, "DONATIONS_ENABLED", True)
    callback = _callback("donate:pay:999")

    await handler.send_donate_invoice(callback)

    callback.message.answer_invoice.assert_not_awaited()
    assert callback.answer.await_args.kwargs["show_alert"] is True


async def test_kill_switch_refuses_to_sell(monkeypatch):
    monkeypatch.setattr(config, "DONATIONS_ENABLED", False)
    callback = _callback("donate:pay:150")

    await handler.send_donate_invoice(callback)

    callback.message.answer_invoice.assert_not_awaited()


# ---------- шаг 2: pre_checkout ----------


async def test_pre_checkout_lets_a_good_payment_through(monkeypatch):
    monkeypatch.setattr(config, "DONATIONS_ENABLED", True)
    query = MagicMock()
    query.invoice_payload = "donate:150:777"
    query.total_amount = 150
    query.from_user = SimpleNamespace(id=777, language_code=None)
    query.answer = AsyncMock()

    await handler.donate_pre_checkout(query)

    query.answer.assert_awaited_once_with(ok=True)


async def test_pre_checkout_refuses_someone_elses_invoice(monkeypatch):
    """Счёт, пересланный в другой чат, не должен списаться у того, кто его
    оплатил вместо адресата."""
    monkeypatch.setattr(config, "DONATIONS_ENABLED", True)
    query = MagicMock()
    query.invoice_payload = "donate:150:777"
    query.total_amount = 150
    query.from_user = SimpleNamespace(id=999, language_code=None)
    query.answer = AsyncMock()

    await handler.donate_pre_checkout(query)

    assert query.answer.await_args.kwargs["ok"] is False


async def test_pre_checkout_refuses_a_mismatched_amount():
    """Сумма в payload'е и сумма в самом счёте разошлись — списывать нечего
    проверять дальше, отказ."""
    query = MagicMock()
    query.invoice_payload = "donate:150:777"
    query.total_amount = 500
    query.from_user = SimpleNamespace(id=777, language_code=None)
    query.answer = AsyncMock()

    await handler.donate_pre_checkout(query)

    assert query.answer.await_args.kwargs["ok"] is False


async def test_pre_checkout_refuses_when_donations_got_disabled_mid_flight(monkeypatch):
    monkeypatch.setattr(config, "DONATIONS_ENABLED", False)
    query = MagicMock()
    query.invoice_payload = "donate:150:777"
    query.total_amount = 150
    query.from_user = SimpleNamespace(id=777, language_code=None)
    query.answer = AsyncMock()

    await handler.donate_pre_checkout(query)

    assert query.answer.await_args.kwargs["ok"] is False


# ---------- шаг 3: successful_payment ----------


async def test_payment_is_recorded_in_the_donations_table(fresh_db, monkeypatch):
    monkeypatch.setattr(config, "ADMIN_ID", 42)
    await fresh_db.get_or_create_user(telegram_id=777, username="tester")

    await handler.donate_paid(_paid())

    assert (await fresh_db.donation_totals(30)) == (150, 1)


async def test_payment_thanks_the_donor(fresh_db):
    message = _paid()

    await handler.donate_paid(message)

    text = message.answer.await_args.args[0]
    assert "150" in text
    assert "железо" in text


async def test_payment_notifies_admin_with_the_charge_id(fresh_db, monkeypatch):
    monkeypatch.setattr(config, "ADMIN_ID", 42)
    message = _paid(charge="ch-xyz")

    await handler.donate_paid(message)

    admin_text = message.bot.send_message.await_args.args[1]
    assert "ch-xyz" in admin_text
    assert message.bot.send_message.await_args.args[0] == 42


async def test_redelivered_payment_does_not_thank_or_notify_twice(fresh_db, monkeypatch):
    monkeypatch.setattr(config, "ADMIN_ID", 42)
    await fresh_db.get_or_create_user(telegram_id=777, username="tester")
    first = _paid(charge="ch-dup")
    second = _paid(charge="ch-dup")

    await handler.donate_paid(first)
    await handler.donate_paid(second)

    first.answer.assert_awaited_once()
    second.answer.assert_not_awaited()
    first.bot.send_message.assert_awaited_once()
    assert (await fresh_db.donation_totals(30)) == (150, 1)


# ---------- db.donations ----------


async def test_record_donation_is_idempotent_by_charge_id(fresh_db, user_id):
    fresh = await fresh_db.record_donation(user_id, "ch-1", 50)
    again = await fresh_db.record_donation(user_id, "ch-1", 50)

    assert fresh is True
    assert again is False
    assert (await fresh_db.donation_totals(30)) == (50, 1)


async def test_donation_totals_count_distinct_donors(fresh_db, user_id):
    other = (await fresh_db.get_or_create_user(telegram_id=222, username="second"))["telegram_id"]
    await fresh_db.record_donation(user_id, "ch-1", 50)
    await fresh_db.record_donation(user_id, "ch-2", 150)  # тот же человек, второй донат
    await fresh_db.record_donation(other, "ch-3", 500)

    stars, people = await fresh_db.donation_totals(30)

    assert stars == 700
    assert people == 2
