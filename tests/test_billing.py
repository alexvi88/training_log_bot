"""billing.py + db: остаток вопросов, списание, выдача и возврат.

Главное, за чем тут следят: платное не должно ни отбирать оплаченное раньше
срока, ни выдавать одно и то же дважды, ни тратить купленный пак, пока целы
бесплатные вопросы месяца.
"""

import datetime as dt

import pytest

import billing
import config

pytestmark = pytest.mark.asyncio


async def _ask(fresh_db, user_id, times):
    for _ in range(times):
        await billing.charge_question(user_id)


async def test_fresh_user_has_the_free_month(fresh_db, user_id):
    allow = await billing.allowance(user_id)

    assert allow.is_pro is False
    assert allow.free_left == config.AI_QUESTION_MONTHLY_FREE
    assert allow.pack_left == 0
    assert allow.allowed is True
    assert allow.paywalled is False


async def test_free_month_runs_out_and_that_is_the_paywall(fresh_db, user_id, monkeypatch):
    monkeypatch.setattr(config, "AI_QUESTION_MONTHLY_FREE", 3)

    await _ask(fresh_db, user_id, 3)
    allow = await billing.allowance(user_id)

    assert allow.left == 0
    assert allow.blocked_by == "month"
    # Именно деньги, а не «приходи завтра»: завтра ничего не изменится.
    assert allow.paywalled is True


async def test_daily_cap_still_bites_before_the_month(fresh_db, user_id, monkeypatch):
    """Дневной потолок никуда не делся: он про нагрузку, а не про деньги, и
    упереться в него можно с целым бесплатным месяцем в запасе."""
    monkeypatch.setattr(config, "AI_QUESTION_MONTHLY_FREE", 100)
    monkeypatch.setattr(config, "AI_QUESTION_DAILY_LIMIT", 2)

    await _ask(fresh_db, user_id, 2)
    allow = await billing.allowance(user_id)

    assert allow.left == 0
    assert allow.blocked_by == "day"
    # Витрину тут показывать нельзя — человеку надо просто дождаться завтра.
    assert allow.paywalled is False


async def test_pro_ignores_the_month_and_gets_the_wider_day(fresh_db, user_id, monkeypatch):
    monkeypatch.setattr(config, "AI_QUESTION_MONTHLY_FREE", 2)
    monkeypatch.setattr(config, "AI_QUESTION_DAILY_LIMIT_PRO", 50)
    await _ask(fresh_db, user_id, 5)  # бесплатные уже потрачены

    await billing.grant(user_id, billing.PRO_MONTH)
    allow = await billing.allowance(user_id)

    assert allow.is_pro is True
    assert allow.allowed is True
    assert allow.left == 45


async def test_pack_is_spent_only_after_the_free_month(fresh_db, user_id, monkeypatch):
    """Пак покупают, чтобы дожить до следующего месяца, — тратить его первым
    значит съесть купленное и оставить бесплатные догнивать до сброса."""
    monkeypatch.setattr(config, "AI_QUESTION_MONTHLY_FREE", 2)
    monkeypatch.setattr(config, "PACK_QUESTIONS", 10)
    await billing.grant(user_id, billing.QUESTION_PACK)

    await _ask(fresh_db, user_id, 2)
    assert (await fresh_db.get_billing(user_id))["pack_questions"] == 10

    await _ask(fresh_db, user_id, 3)
    assert (await fresh_db.get_billing(user_id))["pack_questions"] == 7


async def test_pack_extends_the_month_beyond_free(fresh_db, user_id, monkeypatch):
    monkeypatch.setattr(config, "AI_QUESTION_MONTHLY_FREE", 2)
    monkeypatch.setattr(config, "PACK_QUESTIONS", 5)
    await billing.grant(user_id, billing.QUESTION_PACK)

    await _ask(fresh_db, user_id, 2)
    allow = await billing.allowance(user_id)

    assert allow.free_left == 0
    assert allow.pack_left == 5
    assert allow.allowed is True


async def test_second_month_adds_days_instead_of_resetting(fresh_db, user_id):
    first = await fresh_db.extend_pro(user_id, 30)
    second = await fresh_db.extend_pro(user_id, 30)

    assert dt.datetime.fromisoformat(second) - dt.datetime.fromisoformat(first) == dt.timedelta(days=30)


async def test_expired_access_is_not_pro_anymore(fresh_db, user_id):
    await fresh_db.extend_pro(user_id, -1)

    assert await billing.is_pro(user_id) is False
    assert (await billing.allowance(user_id)).is_pro is False


async def test_same_charge_id_never_pays_twice(fresh_db, user_id):
    """Telegram умеет доставить successful_payment повторно — второй заход не
    должен выдавать месяц ещё раз."""
    assert await fresh_db.record_star_payment(user_id, "ch-1", billing.PRO_MONTH, 150, "p") is True
    assert await fresh_db.record_star_payment(user_id, "ch-1", billing.PRO_MONTH, 150, "p") is False


async def test_refund_takes_back_exactly_what_was_given(fresh_db, user_id, monkeypatch):
    monkeypatch.setattr(config, "PACK_QUESTIONS", 30)
    await billing.grant(user_id, billing.QUESTION_PACK)

    await billing.revoke(user_id, billing.QUESTION_PACK)

    assert (await fresh_db.get_billing(user_id))["pack_questions"] == 0


async def test_refund_of_a_spent_pack_does_not_leave_a_debt(fresh_db, user_id, monkeypatch):
    monkeypatch.setattr(config, "AI_QUESTION_MONTHLY_FREE", 0)
    monkeypatch.setattr(config, "PACK_QUESTIONS", 5)
    await billing.grant(user_id, billing.QUESTION_PACK)
    await _ask(fresh_db, user_id, 3)

    await billing.revoke(user_id, billing.QUESTION_PACK)

    # Ниже нуля остаток не уходит: вернувший пак не должен уйти в минус.
    assert (await fresh_db.get_billing(user_id))["pack_questions"] == 0


async def test_pack_never_goes_negative_under_the_last_question(fresh_db, user_id, monkeypatch):
    monkeypatch.setattr(config, "AI_QUESTION_MONTHLY_FREE", 0)
    monkeypatch.setattr(config, "PACK_QUESTIONS", 1)
    await billing.grant(user_id, billing.QUESTION_PACK)

    await _ask(fresh_db, user_id, 3)

    assert (await fresh_db.get_billing(user_id))["pack_questions"] == 0


async def test_offer_waits_until_the_diary_means_something(fresh_db, user_id, monkeypatch):
    """Витрина до PAYWALL_MIN_WORKOUTS читается как «бот оказался платным»."""
    monkeypatch.setattr(config, "PAYWALL_MIN_WORKOUTS", 2)
    assert await billing.may_offer(user_id) is False

    for _ in range(2):
        await fresh_db.create_finished_workout(
            user_id, "2026-08-01T10:00:00", "2026-08-01T11:00:00"
        )

    assert await billing.may_offer(user_id) is True


async def test_kill_switch_hides_the_offer(fresh_db, user_id, monkeypatch):
    monkeypatch.setattr(config, "PAYWALL_MIN_WORKOUTS", 0)
    monkeypatch.setattr(config, "STARS_PAYMENTS_ENABLED", False)

    assert await billing.may_offer(user_id) is False


async def test_broken_stored_date_does_not_hand_out_access(fresh_db, user_id):
    await fresh_db.add_pack_questions(user_id, 0)
    await fresh_db.conn().execute(
        "UPDATE user_billing SET pro_until = 'не дата' WHERE telegram_id = ?", (user_id,)
    )
    await fresh_db.conn().commit()

    assert await billing.is_pro(user_id) is False


async def test_revenue_counts_only_what_stayed(fresh_db, user_id):
    await fresh_db.record_star_payment(user_id, "ch-1", billing.PRO_MONTH, 150, "p")
    await fresh_db.record_star_payment(user_id, "ch-2", billing.QUESTION_PACK, 50, "p")
    await fresh_db.mark_payment_refunded("ch-2")

    revenue = await fresh_db.star_revenue(30)

    assert revenue["stars"] == 150
    assert revenue["payments"] == 1
    assert revenue["buyers_total"] == 1


async def test_revenue_block_reads_as_money(fresh_db, user_id):
    await fresh_db.record_star_payment(user_id, "ch-1", billing.PRO_MONTH, 150, "p")

    text = billing.format_revenue(
        await fresh_db.star_revenue(30), await fresh_db.star_revenue_by_product(30)
    )

    assert "150 ⭐" in text
    assert "Тренер на месяц" in text


async def test_empty_revenue_block_still_points_somewhere(fresh_db, user_id):
    text = billing.format_revenue(await fresh_db.star_revenue(30), [])

    assert "/premium" in text
