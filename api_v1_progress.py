"""REST `/v1` для экрана прогресса упражнения (домен: прогресс) — только чтение.

Тот же график и тот же текст, что бот рисует картинкой
(handlers/history.py._render_progress_view + charts.render_metric_over_sessions),
но структурой: приложение рисует нативно и НИЧЕГО не считает само.

Почему это важно именно здесь. Раньше клиент получал сырые подходы
(`GET /exercises/{id}/progress`) и строил график сам — и ошибался трижды
подряд: точка на каждый подход (линия пилила внутри одной тренировки), по оси
вес снаряда вместо e1RM, ни тренда, ни периода. Считает всё это
progress_data.chart_series — один расчёт на бота и на приложение, — а здесь
остаётся перевод в JSON. Старый эндпоинт с подходами не трогаем: он про
«покажи мне мою историю», а этот — про экран прогресса.

Что в ответе, кроме чисел:
  - `metric` — "e1rm" или "reps": у упражнения своим весом килограммов нет
    вовсе, и ось подписывается повторами. Решает это не клиент, а тот же
    признак, по которому решает бот (SessionStats.is_bodyweight_mode);
  - `metric_label`, `comparison.text` — готовые локализованные строки. Довод
    тот же, что у достижений и сводки: формулировка живёт в locales/*.json
    одним экземпляром, а не вторым — в уже установленной версии приложения.

Поэтому сборка обёрнута в i18n.use_lang(user["lang"]) — как в
api_v1_achievements и api_v1_dashboard. Без обёртки языком ответа стал бы тот,
который первым дёрнул модуль в этом процессе (CLAUDE.md, «Ловушка,
встретившаяся шесть раз»), и англоязычный атлет получил бы русский текст.
"""

from __future__ import annotations

from typing import Any, Optional

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

import analytics
import db
import formatting
import i18n
import keyboards
import progress_data
from api_v1_common import ApiError, authed_user_id, query_int

# Потолок `limit`. Бот под кнопкой «все» шлёт 9999 (keyboards.progress_chart_keyboard),
# так что потолок обязан быть выше — иначе «все» молча превратилось бы в «часть».
# Тянуть лишнего из базы он не даёт: история упражнения читается целиком в любом
# случае (рекорды считаются по ней всегда), а limit только режет уже собранный
# список в памяти.
MAX_LIMIT = 10_000


async def _owned_exercise(exercise_id: int, user_id: int):
    """Та же проверка владения, что и в api_v1.py: id из URL мог быть угадан
    и принадлежать чужому упражнению."""
    exercise = await db.get_exercise(exercise_id)
    if exercise is None or exercise["user_id"] != user_id:
        raise ApiError(404, "not_found", "exercise not found")
    return exercise


def _point_value(value: float, is_bodyweight: bool) -> float | int:
    """Повторы — целым числом, e1RM — с одним знаком.

    Один знак, а не сырой float: e1RM — это оценка, и «34.79999999999999» на
    оси графика точнее не делает, зато ломает сравнение в тестах клиента.
    """
    return int(value) if is_bodyweight else round(value, 1)


def _trend_json(points: list, values: list) -> Optional[dict[str, Any]]:
    """Та же analytics.linear_trend, что рисует пунктир на графике бота.

    `null` при одной точке (и при пустой истории): наклон по одной точке — это
    не «ноль роста», а «не о чем говорить», и нулём клиент нарисовал бы
    горизонтальную линию тренда там, где линии быть не должно.
    """
    trend = analytics.linear_trend(points)
    if trend is None:
        return None
    return {
        "slope_per_week": round(trend.slope_per_week, 2),
        # Изменение считается по ПОКАЗАННЫМ значениям (уже округлённым), а не по
        # исходным: клиент вычитает первую точку из последней прямо на экране, и
        # расхождение в десятую выглядело бы как ошибка сервера.
        "total_change": round(values[-1] - values[0], 1),
    }


def _records_json(book: Optional[analytics.GoldBook], is_bodyweight: bool) -> Optional[dict[str, Any]]:
    """Рекорды — по ВСЕЙ истории, а не по выбранному периоду, и из того же
    analytics.gold_book, что бот печатает блоком «золото» под графиком.

    Так же, как у бота: переключение «10 / 20 / все» меняет график и дельту, но
    рекорд остаётся рекордом — иначе он «терялся» бы при выборе короткого
    периода. `null`, когда записанных подходов нет вовсе.

    `best_set` — лучший подход в той метрике, которой подписан график: у
    упражнения своим весом лучший e1RM всегда нулевой (вес снаряда ноль), и
    «0×0» вместо «×11» было бы не рекордом, а мусором на экране.
    """
    if book is None:
        return None
    best_set = (
        formatting.format_set(book.max_reps_weight, book.max_reps)
        if is_bodyweight
        else formatting.format_set(book.best_e1rm_weight, book.best_e1rm_reps)
    )
    return {
        "best_weight": round(book.max_weight, 1),
        "best_set": best_set,
        "best_e1rm": round(book.best_e1rm, 1),
    }


def _comparison_json(
    values: list[float], is_bodyweight: bool, full: bool, unit: str
) -> Optional[dict[str, Any]]:
    """Заголовочная строка экрана прогресса: «e1RM: ↑12.5кг с первой тренировки».

    Считается по КРАЮ показанного окна (первая точка против последней), ровно
    как у бота в formatting.format_progress_screen, а не по двум последним
    тренировкам (analytics.compare_to_previous_session — это про карточку
    только что законченной тренировки, другой экран). Иначе число в заголовке
    спорило бы с нарисованной рядом линией: она про весь период, а он — про
    последний день.

    `null`, когда точка одна: сравнивать не с чем, и бот в этом случае пишет
    не дельту, а приглашение сходить второй раз.
    """
    if len(values) < 2:
        return None
    since = i18n.t("progress.since_first") if full else i18n.t("progress.since_period")
    delta = values[-1] - values[0]
    if is_bodyweight:
        text = i18n.t("progress.reps_delta", delta=formatting.format_delta_reps(int(delta)), since=since)
    else:
        text = i18n.t("progress.e1rm_delta", delta=formatting.format_delta(delta, unit), since=since)
    return {"delta": round(delta, 1), "text": text}


async def exercise_progress_sessions(request: Request) -> JSONResponse:
    """График прогресса упражнения: ОДНА точка на тренировку, посчитанная сервером.

    `limit` — сколько последних тренировок показать (10, 20 или «все» — те же
    значения, что у кнопок бота, keyboards.progress_chart_keyboard); по
    умолчанию keyboards.DEFAULT_PROGRESS_LIMIT. Режется хвост, а не начало:
    экран прогресса — про то, что происходит сейчас.

    Всё, что клиенту остаётся, — нарисовать: значения точек уже посчитаны по
    формуле этого атлета (users.e1rm_formula) и в его единицах (веса лежат в
    базе уже в них, см. db.scale_user_set_weights), подписи — на его языке.
    """
    user_id = await authed_user_id(request)
    user = await db.get_user(user_id)
    if user is None:
        raise ApiError(404, "not_found", "user not found")
    exercise_id = int(request.path_params["exercise_id"])
    await _owned_exercise(exercise_id, user_id)

    limit = query_int(
        request, "limit", keyboards.DEFAULT_PROGRESS_LIMIT, minimum=1, maximum=MAX_LIMIT
    )
    sessions = await progress_data.load_sessions(exercise_id, user["e1rm_formula"])
    series = progress_data.chart_series(sessions)
    notes = await db.list_workout_notes_for_exercise(exercise_id)

    shown = series.sessions[-limit:]
    moments = series.points[-limit:]
    values = [_point_value(v, series.is_bodyweight) for _, v in moments]

    with i18n.use_lang(user["lang"]):
        payload = {
            "metric": "reps" if series.is_bodyweight else "e1rm",
            "metric_label": i18n.t("history.chart_metric_reps") if series.is_bodyweight else "e1RM",
            # У повторов единицы нет — не «штуки», а ничего: подпись оси целиком
            # в metric_label. null, а не пустая строка, по той же причине, что у
            # подписи плитки в api_v1_dashboard: клиент решает по наличию.
            "unit": None if series.is_bodyweight else formatting.unit_label(user["unit"]),
            "points": [
                {
                    "date": s.started_at[:10],
                    "workout_id": s.workout_id,
                    "value": value,
                    "top_set": (
                        formatting.format_set(s.top_set.weight, s.top_set.reps)
                        if s.top_set
                        else None
                    ),
                    "sets": len(s.sets),
                    "note": notes.get(s.workout_id),
                }
                for s, value in zip(shown, values, strict=True)
            ],
            "trend": _trend_json(moments, values),
            "records": _records_json(
                analytics.gold_book(sessions, user["e1rm_formula"]), series.is_bodyweight
            ),
            "comparison": _comparison_json(
                values, series.is_bodyweight, len(shown) == len(series.points), user["unit"]
            ),
        }
    return JSONResponse(payload)


routes = [
    Route(
        "/exercises/{exercise_id:int}/progress/sessions",
        exercise_progress_sessions,
        methods=["GET"],
    ),
]
