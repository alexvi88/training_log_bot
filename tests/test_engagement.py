"""Pure signal-detection logic behind the daily engagement pushes."""

import datetime as dt

import analytics
import engagement
from analytics import SessionStats, SetRow


def d(s: str) -> dt.date:
    return dt.date.fromisoformat(s)


# ---------- streak at risk ----------


def test_streak_at_risk_only_on_weekend():
    dashboard = analytics.Dashboard(total_workouts=10, this_week=0, last_30_days=4, days_since_last=3, week_streak=3)
    monday = d("2026-07-06")  # Monday
    saturday = d("2026-07-11")
    assert engagement.is_streak_at_risk(dashboard, monday) is False
    assert engagement.is_streak_at_risk(dashboard, saturday) is True


def test_streak_at_risk_requires_streak_and_empty_week():
    saturday = d("2026-07-11")
    no_streak = analytics.Dashboard(10, 0, 4, 3, week_streak=1)
    already_trained = analytics.Dashboard(10, 1, 4, 0, week_streak=3)
    assert engagement.is_streak_at_risk(no_streak, saturday) is False
    assert engagement.is_streak_at_risk(already_trained, saturday) is False


# ---------- skip milestones ----------


def test_skip_milestone_matches_only_exact_days():
    assert engagement.skip_milestone(3) == 3
    assert engagement.skip_milestone(7) == 7
    assert engagement.skip_milestone(14) == 14
    assert engagement.skip_milestone(4) is None
    assert engagement.skip_milestone(1) is None
    assert engagement.skip_milestone(21) is None
    assert engagement.skip_milestone(None) is None


# ---------- win-back ----------


def test_win_back_starts_at_21_then_every_10_days():
    assert engagement.is_win_back_day(20) is False
    assert engagement.is_win_back_day(21) is True
    assert engagement.is_win_back_day(25) is False
    assert engagement.is_win_back_day(31) is True
    assert engagement.is_win_back_day(41) is True
    assert engagement.is_win_back_day(None) is False



# ---------- plateau ----------


def _session(weight: float, reps_per_set: list[int]) -> SessionStats:
    return SessionStats(
        workout_id=1, started_at="2026-07-01T10:00:00",
        sets=[SetRow(weight, r) for r in reps_per_set],
    )


def test_plateau_needs_three_sessions():
    sessions = [_session(60, [12, 12]), _session(60, [12, 12])]
    assert engagement.is_plateau(sessions) is False


def test_plateau_true_when_weight_stuck_and_reps_high():
    sessions = [_session(60, [12, 13]), _session(60, [14, 12]), _session(60, [12, 12])]
    assert engagement.is_plateau(sessions) is True


def test_plateau_false_when_weight_progressed():
    sessions = [_session(60, [12, 12]), _session(62.5, [12, 12]), _session(65, [12, 12])]
    assert engagement.is_plateau(sessions) is False


def test_plateau_false_when_reps_below_threshold():
    # same weight three times, but reps are low -> genuinely still working up to it, not a plateau
    sessions = [_session(60, [8, 8]), _session(60, [9, 8]), _session(60, [8, 9])]
    assert engagement.is_plateau(sessions) is False


def test_plateau_ignores_bodyweight_zero_weight():
    sessions = [_session(0, [15, 15]), _session(0, [16, 15]), _session(0, [15, 16])]
    assert engagement.is_plateau(sessions) is False


def test_plateau_only_looks_at_the_last_three_sessions():
    # an old plateau that was already broken shouldn't retrigger
    sessions = [_session(60, [12, 12]), _session(60, [12, 12]), _session(65, [12, 12]), _session(70, [12, 12])]
    assert engagement.is_plateau(sessions) is False


# ---------- newbie nudge ----------


def test_newbie_nudge_fires_day_after_signup_then_every_5_days():
    assert engagement.is_newbie_nudge_day(0) is False
    assert engagement.is_newbie_nudge_day(1) is True
    assert engagement.is_newbie_nudge_day(2) is False
    assert engagement.is_newbie_nudge_day(6) is True
    assert engagement.is_newbie_nudge_day(11) is True


def test_newbie_nudge_stops_after_30_days():
    assert engagement.is_newbie_nudge_day(31) is False
    assert engagement.is_newbie_nudge_day(36) is False


# ---------- тихие часы и неизвестный пояс ----------


def test_quiet_hours_cover_the_night():
    assert engagement.is_quiet_hour(22) is True
    assert engagement.is_quiet_hour(2) is True   # ровно та жалоба: «разбудили в два ночи»
    assert engagement.is_quiet_hour(8) is True
    assert engagement.is_quiet_hour(9) is False
    assert engagement.is_quiet_hour(19) is False
    assert engagement.is_quiet_hour(21) is False


def test_unknown_tz_send_hour_is_awake_across_the_whole_audience_band():
    """Час для неизвестного пояса обязан быть бодрым во всём диапазоне аудитории.

    Это инвариант, а не проверка константы: правка тихих часов или диапазона
    поясов не должна тихо вернуть ночные пуши тем, кто пояс не указывал.
    """
    for offset in engagement.UNKNOWN_TZ_BAND:
        local_hour = (engagement.UNKNOWN_TZ_SEND_HOUR_UTC + offset) % 24
        assert not engagement.is_quiet_hour(local_hour), f"UTC+{offset} получил бы пуш в {local_hour}:00"


def test_zero_offset_means_unknown_not_utc():
    """Дефолт схемы и «настройку не трогали» — одно значение, значит ноль не
    сообщает, что человек живёт по UTC."""
    assert engagement.tz_is_known(0) is False
    assert engagement.tz_is_known(3) is True
    assert engagement.tz_is_known(-3) is True


def test_unknown_tz_user_is_not_pushed_at_the_server_evening(monkeypatch):
    """19:00 UTC — это 02:00 в Новосибирске. Пуш уходит в час, безопасный для всех."""
    monkeypatch.setattr(engagement, "_utc_now", lambda: dt.datetime(2026, 7, 27, 19, 0))
    assert engagement.should_send_now(0, 19) is False

    monkeypatch.setattr(
        engagement, "_utc_now",
        lambda: dt.datetime(2026, 7, 27, engagement.UNKNOWN_TZ_SEND_HOUR_UTC, 0),
    )
    assert engagement.should_send_now(0, 19) is True


def test_night_push_is_dropped_not_shifted(monkeypatch):
    """ENGAGEMENT_HOUR берётся из окружения, так что «час отправки» сам может
    оказаться ночным — тихие часы поверх него, и пуш не уходит вовсе."""
    monkeypatch.setattr(engagement, "_utc_now", lambda: dt.datetime(2026, 7, 27, 23, 0))
    assert engagement.should_send_now(3, 2) is False    # у пользователя 02:00
    # ...а утром того же дня, когда сигнал ещё актуален, ничего не «догоняет»:
    monkeypatch.setattr(engagement, "_utc_now", lambda: dt.datetime(2026, 7, 28, 6, 0))
    assert engagement.should_send_now(3, 2) is False    # 09:00 по местному, но час отправки не тот


# ---------- tonnage formatting ----------


def test_format_tonnage_switches_units_at_1000kg():
    assert engagement.format_tonnage(850) == "850кг"
    assert engagement.format_tonnage(1000) == "1.0т"
    assert engagement.format_tonnage(4200) == "4.2т"
