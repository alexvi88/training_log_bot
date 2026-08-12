"""Недельный объём по группам мышц на главном экране.

Бот считал его с июля и показывал ровно одному читателю — AI-тренеру, и только
если тот сам решит дёрнуть инструмент. На экраны цифра не выходила нигде, так что
вопрос «что я на этой неделе не тренировал» человек мог задать только словами.

Проверок тут три вида: сборка строк (formatting), отрисовка панели (charts) и то,
что картинка обновляется от новых подходов, а не только от новых тренировок —
последнее и было главным риском, потому что кэш картинки жил на датах.
"""
import datetime as dt

import analytics
import charts
import formatting
from handlers import workout as workout_handlers

PANEL_GROUPS = [
    {"id": 1, "name": "Грудь"},
    {"id": 2, "name": "Спина"},
    {"id": 3, "name": "Ноги"},
]


def _png_size(data: bytes) -> tuple[int, int]:
    """(ширина, высота) из заголовка IHDR — он всегда идёт первым чанком."""
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    return int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big")


# ---------- коридор ----------


def test_the_corridor_boundaries_are_six_and_twelve():
    """Границы названы числами, а не «примерно»: цвет полосы на экране целиком
    зависит от того, куда попал счёт, и сдвиг границы на единицу перекрашивает
    неделю человека."""
    assert analytics.classify_weekly_volume(0) == "none"
    assert analytics.classify_weekly_volume(5) == "low"
    assert analytics.classify_weekly_volume(6) == "in_range"
    assert analytics.classify_weekly_volume(12) == "in_range"
    assert analytics.classify_weekly_volume(13) == "high"


# ---------- сборка строк ----------


def test_groups_are_ordered_by_volume_not_by_the_fixed_group_order():
    """Смысл панели — увидеть провал, не пересчитывая числа. Он виден, только
    если полосы идут монотонно: тогда дыра читается по силуэту списка."""
    title, rows = formatting.weekly_volume_panel({1: 4, 2: 14, 3: 9}, PANEL_GROUPS)

    assert [row[0] for row in rows] == ["СПИНА", "НОГИ", "ГРУДЬ"]
    assert [row[1] for row in rows] == [14, 9, 4]
    assert [row[2] for row in rows] == ["high", "in_range", "low"]
    assert title == "ОБЪЁМ ЗА 7 ДНЕЙ · 27 ПОДХОДОВ"


def test_ties_keep_a_stable_order():
    """Иначе картинка перетасовывалась бы между открытиями меню на одних и тех
    же данных — и заодно промахивался бы кэш, потому что порядок строк входит в
    его ключ."""
    counts = {1: 8, 2: 8, 3: 8}

    first = formatting.weekly_volume_panel(counts, PANEL_GROUPS)[1]
    second = formatting.weekly_volume_panel(counts, PANEL_GROUPS)[1]

    assert first == second
    assert [row[0] for row in first] == ["ГРУДЬ", "НОГИ", "СПИНА"]


def test_a_group_with_nothing_logged_stays_visible():
    """«Спину я на этой неделе не трогал» — это главное, что панель вообще
    способна сообщить. Выкинуть нулевую строку значит выкинуть ответ."""
    _, rows = formatting.weekly_volume_panel({1: 10}, PANEL_GROUPS)

    by_group = {row[0]: row for row in rows}
    assert by_group["СПИНА"] == ("СПИНА", 0, "none")
    assert by_group["НОГИ"] == ("НОГИ", 0, "none")


def test_an_empty_week_draws_no_panel_at_all():
    """А вот когда нулей семь из семи, панель сообщает только то, что человек не
    тренировался, — это и так видно по календарю под ней."""
    assert formatting.weekly_volume_panel({}, PANEL_GROUPS) == ("", [])
    assert formatting.weekly_volume_panel({1: 0, 2: 0}, PANEL_GROUPS) == ("", [])


def test_sets_without_a_muscle_group_are_not_silently_dropped():
    """У упражнения группы может не быть вовсе. Подходы сделаны — значит они в
    сумме, иначе итог в заголовке не сойдётся с полосами."""
    title, rows = formatting.weekly_volume_panel({1: 6, None: 3}, PANEL_GROUPS)

    assert (formatting.UNGROUPED_LABEL, 3, "low") in rows
    assert title == "ОБЪЁМ ЗА 7 ДНЕЙ · 9 ПОДХОДОВ"


def test_the_ungrouped_row_appears_only_when_it_has_something():
    _, rows = formatting.weekly_volume_panel({1: 6, None: 0}, PANEL_GROUPS)

    assert formatting.UNGROUPED_LABEL not in [row[0] for row in rows]


def test_the_title_declines_the_word():
    """21 ПОДХОД, а не 21 ПОДХОДОВ — заголовок висит на главном экране, и
    рассогласование там читается как небрежность во всём остальном."""
    def title_for(sets: int) -> str:
        return formatting.weekly_volume_panel({1: sets}, PANEL_GROUPS)[0]

    assert title_for(1).endswith("1 ПОДХОД")
    assert title_for(3).endswith("3 ПОДХОДА")
    assert title_for(8).endswith("8 ПОДХОДОВ")
    assert title_for(21).endswith("21 ПОДХОД")


# ---------- шкала и отрисовка ----------


def test_the_scale_always_shows_the_whole_corridor():
    """Если бы шкала кончалась на самой большой группе, у человека с четырьмя
    подходами весь коридор уехал бы за правый край."""
    assert charts._volume_scale_max([("ГРУДЬ", 2, "low")]) >= analytics.WEEKLY_VOLUME_MAX
    assert charts._volume_scale_max([]) >= analytics.WEEKLY_VOLUME_MAX


def test_one_heavy_group_does_not_flatten_the_rest():
    """30 подходов на спину — реальная неделя, и шкала обязана её вместить, иначе
    полоса упрётся в край и перебор станет неотличим от нормы."""
    assert charts._volume_scale_max([("СПИНА", 30, "high"), ("ГРУДЬ", 6, "in_range")]) == 30


def test_the_panel_renders_and_makes_the_image_taller():
    rows = [("СПИНА", 14, "high"), ("ГРУДЬ", 9, "in_range"), ("НОГИ", 0, "none")]

    without = charts.render_menu_dashboard("3 тренировки за 30 дней")
    with_panel = charts.render_menu_dashboard(
        "3 тренировки за 30 дней",
        volume_rows=rows,
        volume_title="ОБЪЁМ ЗА 7 ДНЕЙ · 23 ПОДХОДА",
    )

    assert _png_size(with_panel)[1] > _png_size(without)[1]
    assert _png_size(with_panel)[0] == _png_size(without)[0]  # шире не становится


def test_an_empty_row_list_leaves_the_old_picture_untouched():
    """Пустой список строк — это отсутствие панели, а не панель без строк: у
    нового пользователя картинка должна остаться в точности прежней."""
    assert charts.render_menu_dashboard("1 тренировка за 30 дней") == \
        charts.render_menu_dashboard("1 тренировка за 30 дней", volume_rows=[], volume_title="")


# ---------- окно в семь дней ----------


async def _log_sets(db, user_id, ex_id, count, started_at=None):
    if started_at is None:
        workout_id = await db.create_workout(user_id)
    else:
        workout_id = await db.create_finished_workout(user_id, started_at, started_at)
    block_id = await db.create_block(workout_id, "single")
    await db.add_block_exercise(block_id, ex_id, 0)
    for i in range(count):
        await db.add_set(block_id, ex_id, i, 0, 100.0, 8)
    if started_at is None:
        await db.finish_workout(workout_id)
    return workout_id


async def test_only_the_last_seven_days_count(fresh_db, user_id):
    """Окно скользящее — считать с понедельника значило бы показывать нули каждый
    понедельник, ровно в тот день, когда человек открывает бота планировать
    неделю."""
    db = fresh_db
    group_id = (await db.list_muscle_groups(None, global_only=True))[0]["id"]
    ex_id = await db.create_exercise(user_id, "Жим", group_id)
    today = dt.date.today()
    long_ago = (today - dt.timedelta(days=20)).isoformat() + "T10:00:00"

    await _log_sets(db, user_id, ex_id, 4, started_at=long_ago)
    await _log_sets(db, user_id, ex_id, 3)

    window_start = today - dt.timedelta(days=analytics.VOLUME_WINDOW_DAYS - 1)
    counts = await db.weekly_volume_by_group(user_id, window_start.isoformat(), today.isoformat())

    assert counts.get(group_id) == 3


# ---------- кэш картинки ----------


async def test_new_sets_in_a_closed_workout_refresh_the_picture(fresh_db, user_id):
    """Кэш картинки жил на (дата, число тренировок, последняя дата). Объём же
    меняется от подходов: дописал четыре подхода в уже закрытую тренировку — даты
    те же, а полосы обязаны поехать."""
    db = fresh_db
    workout_handlers._heatmap_cache.pop(user_id, None)
    group_id = (await db.list_muscle_groups(None, global_only=True))[0]["id"]
    ex_id = await db.create_exercise(user_id, "Жим", group_id)
    workout_id = await _log_sets(db, user_id, ex_id, 3)

    _, first = await workout_handlers._menu_view(user_id)

    block_id = (await db.list_blocks_for_workout(workout_id))[0]["id"]
    for i in range(4):
        await db.add_set(block_id, ex_id, 10 + i, 0, 100.0, 8)
    _, second = await workout_handlers._menu_view(user_id)

    assert first is not None and second is not None
    assert first != second


async def test_the_same_data_is_served_from_cache(fresh_db, user_id):
    """Панель добавила два запроса на каждое открытие меню — отрисовка при этом
    обязана остаться закэшированной, она тяжёлая."""
    db = fresh_db
    workout_handlers._heatmap_cache.pop(user_id, None)
    group_id = (await db.list_muscle_groups(None, global_only=True))[0]["id"]
    ex_id = await db.create_exercise(user_id, "Жим", group_id)
    await _log_sets(db, user_id, ex_id, 3)

    _, first = await workout_handlers._menu_view(user_id)
    _, second = await workout_handlers._menu_view(user_id)

    assert first is second  # тот же объект, а не просто равные байты


async def test_a_user_without_workouts_gets_no_panel_and_no_image(fresh_db, user_id):
    workout_handlers._heatmap_cache.pop(user_id, None)

    text, png = await workout_handlers._menu_view(user_id)

    assert png is None
    assert text == workout_handlers._ONBOARDING


async def test_the_real_groups_reach_the_panel(fresh_db, user_id):
    """Панель строится по списку групп пользователя, а не по тем, в которых он
    что-то делал — иначе она перестала бы отвечать на «чего я не тренировал»."""
    db = fresh_db
    groups = await db.list_muscle_groups(user_id)
    group_id = groups[0]["id"]
    ex_id = await db.create_exercise(user_id, "Жим", group_id)
    await _log_sets(db, user_id, ex_id, 7)

    today = dt.date.today()
    window_start = today - dt.timedelta(days=analytics.VOLUME_WINDOW_DAYS - 1)
    counts = await db.weekly_volume_by_group(user_id, window_start.isoformat(), today.isoformat())
    _, rows = formatting.weekly_volume_panel(counts, groups)

    expected = [
        g for g in groups if g["name"].strip().lower() not in formatting.VOLUME_HIDDEN_GROUPS
    ]
    assert len(rows) == len(expected)
    assert rows[0][1] == 7  # тренированная группа — сверху
    assert all(row[2] == "none" for row in rows[1:])


async def test_the_catch_all_group_is_left_out(fresh_db, user_id):
    """«Другое» — мешок, куда падает всё, что не легло в шесть основных: у
    коридора 6–12 для него нет смысла, а строку и место он занимает наравне с
    грудью. Из суммы в заголовке тоже исключён, иначе число не сошлось бы с
    видимыми полосами."""
    db = fresh_db
    groups = await db.list_muscle_groups(user_id)
    other = next(g for g in groups if g["name"].strip().lower() == "другое")
    chest = next(g for g in groups if g["name"] == "Грудь")

    title, rows = formatting.weekly_volume_panel({other["id"]: 9, chest["id"]: 6}, groups)

    assert "ДРУГОЕ" not in [row[0] for row in rows]
    assert title.endswith("6 ПОДХОДОВ")
