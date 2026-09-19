"""busy_lock.try_claim — общий примитив «занято», вынесенный из
handlers/ai_trainer.py (см. его докстринг) так, чтобы REST-слой (`/v1`) не
заводил третью копию того же `if user_id in busy: ... busy.add(...)`.

Сама атомарность (нет `await` между проверкой и записью) проверяется не
здесь юнит-тестом — её обеспечивает asyncio, а не этот код, — а сквозными
тестами настоящей гонки в tests/test_api_v1_ai.py/test_api_v1_food.py/
test_food_diary.py (две реально параллельные корутины, а не последовательные
вызовы). Здесь — только контракт самой функции.
"""

import busy_lock


def test_first_claim_succeeds_and_reserves():
    busy: set[int] = set()
    assert busy_lock.try_claim(busy, 42) is True
    assert 42 in busy


def test_second_claim_for_the_same_user_fails_while_reserved():
    busy = {42}
    assert busy_lock.try_claim(busy, 42) is False
    # Отказ не трогает бронь — она снимается только явным discard() вызывающей
    # стороной, не самим try_claim.
    assert 42 in busy


def test_different_users_do_not_block_each_other():
    busy = {42}
    assert busy_lock.try_claim(busy, 43) is True
    assert busy == {42, 43}


def test_claim_after_release_succeeds_again():
    busy: set[int] = set()
    assert busy_lock.try_claim(busy, 1) is True
    busy.discard(1)
    assert busy_lock.try_claim(busy, 1) is True
