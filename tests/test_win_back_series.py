"""Пуш «Возвращение» (win_back): серия с потолком и тексты, правдивые на любой
её день.

Раньше серия шла с 21-го дня каждые 10 дней без конца, а тексты говорили
«Три недели…» — и та же строка уходила на 31-й, 91-й, 301-й день. Правило
гайда: утверждение о данных — только когда данные его подтверждают. Теперь
серия кончается на 51-м дне (engagement.WIN_BACK_STOP_DAY), а тексты срок
паузы не называют вовсе.
"""

import datetime as dt
import re

import pytest

import engagement
import i18n
import push_ios
import push_texts

TODAY = dt.date(2026, 10, 1)

# Любое конкретное число недель или дней в тексте серии — неправда хотя бы на
# одном её дне (21-й — три недели, 51-й — уже семь).
_DURATION = re.compile(
    r"(недел|месяц|\bтри\b|\d+\s*(дн|день)|\bweek|\bmonth|\bfortnight|\bthree\b|\d+\s*days?\b)",
    re.IGNORECASE,
)


async def _last_workout_days_ago(db, user_id: int, days: int) -> None:
    start = dt.datetime.combine(TODAY - dt.timedelta(days=days), dt.time(10))
    await db.create_finished_workout(
        user_id, start.isoformat(), (start + dt.timedelta(hours=1)).isoformat()
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("days", [21, 31, 41, 51])
async def test_win_back_goes_out_on_every_day_of_the_series(fresh_db, user_id, days):
    await _last_workout_days_ago(fresh_db, user_id, days)
    decision = await engagement.build_daily_push(user_id, TODAY)
    assert decision is not None and decision.category == push_texts.WIN_BACK


@pytest.mark.asyncio
@pytest.mark.parametrize("days", [61, 71, 91, 301])
async def test_win_back_is_silent_after_the_cap(fresh_db, user_id, days):
    await _last_workout_days_ago(fresh_db, user_id, days)
    decision = await engagement.build_daily_push(user_id, TODAY)
    assert decision is None or decision.category != push_texts.WIN_BACK


def _all_win_back_texts() -> list[str]:
    texts = []
    for lang in ("ru", "en"):
        texts += push_texts.TEXTS_BY_LANG[lang][push_texts.WIN_BACK]
        texts += push_ios.BODY_POOLS_BY_LANG[lang][push_texts.WIN_BACK]
        texts.append(push_ios.TITLES_BY_LANG[lang][push_texts.WIN_BACK])
    return texts


def test_no_win_back_text_names_a_duration():
    texts = _all_win_back_texts()
    assert len(texts) > 20
    offenders = [t for t in texts if _DURATION.search(t)]
    assert not offenders, offenders


@pytest.mark.asyncio
async def test_last_push_of_the_series_does_not_say_three_weeks(fresh_db, user_id):
    """51-й день, каждый вариант пула по очереди (мешок без повторов)."""
    await _last_workout_days_ago(fresh_db, user_id, 51)
    for lang in ("ru", "en"):
        pool = push_texts.TEXTS_BY_LANG[lang][push_texts.WIN_BACK]
        with i18n.use_lang(lang):
            for _ in pool:
                decision = await engagement.build_daily_push(user_id, TODAY)
                assert decision is not None and decision.category == push_texts.WIN_BACK
                assert "три недели" not in decision.text.lower()
                assert "three weeks" not in decision.text.lower()
                await fresh_db.conn().execute("DELETE FROM pushes WHERE telegram_id = ?", (user_id,))
                await fresh_db.conn().commit()
