"""Сводка на главном экране: агрегаты под неё и её отрисовка.

Раньше в сообщении меню жила одна тепловая карта года плюс три строки
статистики. Сводка добавила к ним плитки, коридор по группам и тренд e1RM по
движениям, которые человек делает чаще всего, — и вместе с ними три агрегата,
которых в базе не считал никто: тоннаж по дням, серия e1RM по упражнению и
счётчик рекордов.

Отдельная тема здесь — кэш картинки. Он жил на «(дата, число тренировок,
последняя дата)», а всё новое на экране меняется от подходов, а не от
тренировок: без пересборки ключа сводка застывала до следующей открытой
тренировки.
"""
import datetime as dt
from types import SimpleNamespace

import charts
import formatting
from handlers import workout as workout_handlers

BENCH = "Жим штанги лёжа"
SQUAT = "Присед со штангой"
PULLUPS = "Подтягивания"


def _dashboard(week_streak=0, this_week=0, last_30_days=0, total_workouts=1):
    return SimpleNamespace(
        week_streak=week_streak, this_week=this_week,
        last_30_days=last_30_days, total_workouts=total_workouts,
    )


async def _own(db, user_id, template_name):
    template = next(
        t for t in await db.list_all_exercise_templates() if t["name"] == template_name
    )
    return await db.fork_exercise_from_template(user_id, template["id"])


async def _session(db, user_id, day, sets):
    """Одна завершённая тренировка в конкретный день. `sets` — [(ex_id, вес, повторы, сколько)]."""
    stamp = dt.datetime.combine(day, dt.time(19, 0)).isoformat()
    workout_id = await db.create_finished_workout(user_id, stamp, stamp)
    for ex_id, weight, reps, count in sets:
        block_id = await db.create_block(workout_id, "single")
        await db.add_block_exercise(block_id, ex_id, 0)
        for i in range(count):
            await db.add_set(block_id, ex_id, i, 0, weight, reps)
    return workout_id


# ---------- самые частые движения ----------


async def test_frequency_counts_workouts_not_sets(fresh_db, user_id):
    """Двадцать подходов за один заход — это не «часто», а один тяжёлый день, и
    линия прогресса по нему состоит из одной точки."""
    db = fresh_db
    today = dt.date.today()
    bench, squat = await _own(db, user_id, BENCH), await _own(db, user_id, SQUAT)
    await _session(db, user_id, today - dt.timedelta(days=1), [(bench, 100, 5, 20)])
    for offset in (2, 3, 4):
        await _session(db, user_id, today - dt.timedelta(days=offset), [(squat, 120, 5, 1)])

    top = await db.top_exercises_by_frequency(
        user_id, (today - dt.timedelta(weeks=8)).isoformat(), today.isoformat()
    )

    assert [row["display_name"] for row in top] == ["Присед со штангой"]
    assert top[0]["sessions"] == 3


async def test_a_single_session_is_not_enough_for_a_trend(fresh_db, user_id):
    """Тренд по одной тренировке — это точка, а не тренд. Лучше не показать
    движение вовсе, чем показать плоскую линию и назвать её прогрессом."""
    db = fresh_db
    today = dt.date.today()
    bench = await _own(db, user_id, BENCH)
    await _session(db, user_id, today, [(bench, 100, 5, 3)])

    top = await db.top_exercises_by_frequency(
        user_id, (today - dt.timedelta(weeks=8)).isoformat(), today.isoformat()
    )

    assert top == []


async def test_frequency_ignores_what_happened_before_the_window(fresh_db, user_id):
    """Окно — восемь недель: сводка про то, что человек делает сейчас, а не про
    то, чем он занимался год назад."""
    db = fresh_db
    today = dt.date.today()
    bench, squat = await _own(db, user_id, BENCH), await _own(db, user_id, SQUAT)
    for offset in (300, 301, 302, 303):
        await _session(db, user_id, today - dt.timedelta(days=offset), [(bench, 100, 5, 3)])
    for offset in (1, 3):
        await _session(db, user_id, today - dt.timedelta(days=offset), [(squat, 120, 5, 3)])

    top = await db.top_exercises_by_frequency(
        user_id, (today - dt.timedelta(weeks=8)).isoformat(), today.isoformat()
    )

    assert [row["display_name"] for row in top] == ["Присед со штангой"]


async def test_ties_are_ordered_stably(fresh_db, user_id):
    """Порядок входит в ключ кэша картинки: перетасовка на ничьих гоняла бы
    отрисовку на одних и тех же данных."""
    db = fresh_db
    today = dt.date.today()
    bench, squat = await _own(db, user_id, BENCH), await _own(db, user_id, SQUAT)
    for offset in (1, 2):
        await _session(db, user_id, today - dt.timedelta(days=offset),
                       [(bench, 100, 5, 3), (squat, 120, 5, 3)])

    args = (user_id, (today - dt.timedelta(weeks=8)).isoformat(), today.isoformat())
    first = [row["display_name"] for row in await db.top_exercises_by_frequency(*args)]

    assert first == [row["display_name"] for row in await db.top_exercises_by_frequency(*args)]
    assert first == ["Жим штанги лёжа", "Присед со штангой"]


# ---------- серия e1RM ----------


async def test_one_point_per_session_ascending(fresh_db, user_id):
    """Внутри дня e1RM гуляет от разминочных и откатных подходов: линия из
    подходов была бы пилой вместо тренда."""
    db = fresh_db
    today = dt.date.today()
    bench = await _own(db, user_id, BENCH)
    await _session(db, user_id, today - dt.timedelta(days=6), [(bench, 100, 5, 3)])
    await _session(db, user_id, today - dt.timedelta(days=3), [(bench, 110, 5, 3)])

    series = await db.exercise_e1rm_series(user_id, bench)

    assert len(series) == 2
    assert series[0] < series[1]


async def test_the_series_takes_the_best_set_of_the_day(fresh_db, user_id):
    db = fresh_db
    today = dt.date.today()
    bench = await _own(db, user_id, BENCH)
    stamp = dt.datetime.combine(today, dt.time(19, 0)).isoformat()
    workout_id = await db.create_finished_workout(user_id, stamp, stamp)
    block_id = await db.create_block(workout_id, "single")
    await db.add_block_exercise(block_id, bench, 0)
    await db.add_set(block_id, bench, 0, 0, 60.0, 10)    # разминка
    await db.add_set(block_id, bench, 1, 0, 100.0, 5)    # рабочий
    await db.add_set(block_id, bench, 2, 0, 80.0, 8)     # откатный

    import analytics
    series = await db.exercise_e1rm_series(user_id, bench)

    assert series == [analytics.e1rm(100.0, 5)]


async def test_the_series_is_capped_at_the_asked_number_of_sessions(fresh_db, user_id):
    db = fresh_db
    today = dt.date.today()
    bench = await _own(db, user_id, BENCH)
    for offset in range(10):
        await _session(db, user_id, today - dt.timedelta(days=offset), [(bench, 100, 5, 1)])

    assert len(await db.exercise_e1rm_series(user_id, bench, sessions=4)) == 4


# ---------- тоннаж и рекорды ----------


async def test_tonnage_counts_bodyweight_as_load(fresh_db, user_id):
    """Подтягивания весили ноль тонн, пока собственный вес не начали считать —
    та же арифметика, что в остальных местах (см. effective_load)."""
    db = fresh_db
    today = dt.date.today()
    await db.add_bodyweight_log(user_id, 80.0)
    pullups = await _own(db, user_id, PULLUPS)
    await _session(db, user_id, today, [(pullups, 0, 10, 1)])

    daily = await db.daily_tonnage(user_id, today.isoformat(), today.isoformat())

    assert daily[today.isoformat()] == 800.0


async def test_tonnage_stays_inside_its_window(fresh_db, user_id):
    db = fresh_db
    today = dt.date.today()
    bench = await _own(db, user_id, BENCH)
    await _session(db, user_id, today, [(bench, 100, 5, 1)])
    await _session(db, user_id, today - dt.timedelta(days=30), [(bench, 100, 5, 1)])

    daily = await db.daily_tonnage(
        user_id, (today - dt.timedelta(days=6)).isoformat(), today.isoformat()
    )

    assert sum(daily.values()) == 500.0


async def test_a_first_ever_session_is_not_a_record(fresh_db, user_id):
    """Первая в жизни тренировка движения формально бьёт рекорд каждым подходом.
    Поздравлять с тем, что человек что-то попробовал, — обесценивать плитку."""
    db = fresh_db
    today = dt.date.today()
    bench = await _own(db, user_id, BENCH)
    await _session(db, user_id, today, [(bench, 100, 5, 3)])

    assert await db.e1rm_record_count(user_id, today.isoformat()) == 0


async def test_beating_an_older_best_counts_as_a_record(fresh_db, user_id):
    db = fresh_db
    today = dt.date.today()
    bench, squat = await _own(db, user_id, BENCH), await _own(db, user_id, SQUAT)
    await _session(db, user_id, today - dt.timedelta(days=20),
                   [(bench, 100, 5, 3), (squat, 120, 5, 3)])
    await _session(db, user_id, today, [(bench, 110, 5, 3), (squat, 110, 5, 3)])

    # Жим вырос, присед просел — рекорд ровно один.
    assert await db.e1rm_record_count(user_id, (today - dt.timedelta(days=6)).isoformat()) == 1


# ---------- сборка подписей ----------


def test_the_headline_prefers_the_streak():
    assert formatting.menu_headline(_dashboard(week_streak=9, last_30_days=12)) == "9 недель подряд"
    assert formatting.menu_headline(_dashboard(week_streak=2, last_30_days=8)) == "2 недели подряд"


def test_without_a_streak_the_headline_talks_about_the_month():
    """«0 недель подряд» — не достижение, а укор, причём за то, что человек
    только начал или один раз пропустил."""
    assert formatting.menu_headline(_dashboard(week_streak=0, last_30_days=5)) == \
        "5 тренировок за 30 дней"
    assert formatting.menu_headline(_dashboard(week_streak=1, last_30_days=1)) == \
        "1 тренировка за 30 дней"


def test_tonnage_switches_to_kilograms_when_there_are_no_tonnes():
    """«0,4 т» читается хуже, чем «400 кг»."""
    assert formatting.menu_tiles(_dashboard(), 24_500, 3)[1] == ("ТОННАЖ ЗА 7 ДНЕЙ", "24.5 т")
    assert formatting.menu_tiles(_dashboard(), 400, 3)[1] == ("ТОННАЖ ЗА 7 ДНЕЙ", "400 кг")


def test_the_records_tile_gives_its_place_away_when_there_are_none():
    with_records = formatting.menu_tiles(_dashboard(this_week=2), 5000, 2)
    without = formatting.menu_tiles(_dashboard(this_week=2), 5000, 0)

    assert with_records[2] == ("РЕКОРДОВ ЗА 7 ДНЕЙ", "2")
    assert without[2] == ("ТРЕНИРОВОК ЗА НЕДЕЛЮ", "2")
    assert len(with_records) == len(without) == 3


def test_lift_cards_carry_the_current_value_and_the_change():
    cards = formatting.menu_lift_cards([("Жим штанги лёжа", [100.0, 108.0, 112.0])])

    assert cards == [("ЖИМ ШТАНГИ ЛЁЖА", [100.0, 108.0, 112.0], "112 кг", "+12")]


def test_a_drop_is_not_dressed_up_as_growth():
    assert formatting.menu_lift_cards([("Присед", [120.0, 110.0])])[0][3] == "-10"


def test_the_change_is_measured_across_the_whole_series():
    """Между двумя последними точками e1RM гуляет от самочувствия, и такой «минус»
    сообщал бы про сон, а не про прогресс."""
    cards = formatting.menu_lift_cards([("Жим", [100.0, 130.0, 125.0])])

    assert cards[0][3] == "+25"


def test_a_flat_series_shows_no_change_at_all():
    assert formatting.menu_lift_cards([("Жим", [100.0, 100.0])])[0][3] == ""


def test_a_long_movement_name_is_truncated_not_wrapped():
    """Карточка — треть ширины картинки: длинное имя залезало бы на соседнюю."""
    label = formatting.menu_lift_cards([("Жим штанги на наклонной скамье вниз головой", [90.0])])[0][0]

    assert label.endswith("…")
    assert len(label) <= 22


def test_a_movement_without_a_series_is_dropped():
    assert formatting.menu_lift_cards([("Жим", [])]) == []


# ---------- отрисовка ----------


def _render(**kwargs):
    today = dt.date(2026, 8, 4)
    base = dict(
        day_counts={today: 1}, today=today, start=today - dt.timedelta(weeks=30),
        headline="9 недель подряд", badge="ТЯЖЕЛОВЕС",
        tiles=[("ТРЕНИРОВОК ЗА 30 ДНЕЙ", "12"), ("ТОННАЖ ЗА 7 ДНЕЙ", "24.5 т"), ("РЕКОРДОВ 7 Д", "3")],
        volume_rows=[("СПИНА", 14, "high"), ("ГРУДЬ", 9, "in_range"), ("НОГИ", 0, "none")],
        volume_title="ОБЪЁМ ЗА 7 ДНЕЙ · 23 ПОДХОДА",
        lifts=[("ЖИМ ЛЁЖА", [100.0, 112.0], "112 кг", "+12")],
    )
    base.update(kwargs)
    return charts.render_menu_dashboard(**base)


def _png_size(data: bytes) -> tuple[int, int]:
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    return int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big")


def test_the_source_width_is_fixed_whatever_is_inside():
    """Единственное правило раскладки: Telegram масштабирует фото под ширину
    пузыря, значит размер элемента на экране обратно пропорционален ширине
    исходника. Широкая картинка мельчит всё, что в ней есть, поэтому ширина —
    константа, а расти сводке позволено только вниз."""
    narrow = _png_size(_render(volume_rows=[], volume_title="", lifts=[]))
    wide_payload = _png_size(_render())

    assert narrow[0] == wide_payload[0] == round(charts.DASH_WIDTH_IN * 150)


def test_a_full_summary_is_a_portrait():
    """Семь групп мышц и три движения — обычный набор у человека с историей, и
    вот на нём картинка обязана быть выше, чем шире: две колонки потребовали бы
    широкого исходника и смельчили бы всё."""
    width, height = _png_size(_render(
        volume_rows=[
            ("СПИНА", 14, "high"), ("ТРИЦЕПС", 11, "in_range"), ("ГРУДЬ", 9, "in_range"),
            ("ПЛЕЧИ", 7, "in_range"), ("БИЦЕПС", 5, "low"), ("НОГИ", 4, "low"),
            ("ДРУГОЕ", 0, "none"),
        ],
        lifts=[
            ("ЖИМ ЛЁЖА", [100.0, 112.0], "112 кг", "+12"),
            ("ПРИСЕД", [140.0, 158.0], "158 кг", "+18"),
            ("ТЯГА", [180.0, 192.0], "192 кг", "+12"),
        ],
    ))

    assert height > width


def test_every_widget_can_be_absent():
    """У нового пользователя нет ни объёма, ни движений, а пустой блок сообщал бы
    только то, что он пуст."""
    full = _png_size(_render())[1]
    bare = _png_size(_render(tiles=[], volume_rows=[], volume_title="", lifts=[], badge=""))[1]

    assert bare < full


def test_the_dividers_are_actually_drawn():
    """Их дважды не было видно: сначала линейки закрывал непрозрачный фон соседней
    оси, потом артист уровня фигуры вообще не доходил до пикселей. Оба раза по
    картинке казалось, что «почти видно», — поэтому проверка по пикселям."""
    import io

    from PIL import Image

    img = Image.open(io.BytesIO(_render())).convert("RGB")
    rule = tuple(int(charts.DASH_RULE[i:i + 2], 16) for i in (1, 3, 5))
    column = [img.getpixel((img.size[0] // 2, y)) for y in range(img.size[1])]

    assert sum(1 for px in column if px == rule) >= 3   # плитки/объём, объём/год, год/движения


def test_a_flat_series_does_not_divide_by_zero():
    assert _render(lifts=[("ЖИМ", [100.0, 100.0, 100.0], "100 кг", "")])[:8] == b"\x89PNG\r\n\x1a\n"


def test_an_all_zero_series_does_not_take_the_whole_menu_down():
    """Ноль во всей серии — не выдумка: e1RM упражнения на своём весе равен нулю,
    пока человек ни разу не взвесился, а подтягивания легко попадают в топ-3
    частых. Порог шума считался от среднего, то есть тоже нулём, — и главный
    экран падал целиком, вместе с /start, потому что сводка рисуется на обоих."""
    assert _render(lifts=[("ПОДТЯГИВАНИЯ", [0.0, 0.0, 0.0, 0.0], "0 кг", "")])[:8] == (
        b"\x89PNG\r\n\x1a\n"
    )


def test_a_single_point_series_renders_without_a_line():
    assert _render(lifts=[("ЖИМ", [100.0], "100 кг", "")])[:8] == b"\x89PNG\r\n\x1a\n"


# ---------- экран меню ----------


async def test_a_user_without_workouts_gets_the_onboarding_text(fresh_db, user_id):
    workout_handlers._heatmap_cache.pop(user_id, None)

    text, png = await workout_handlers._menu_view(user_id)

    assert png is None
    assert text == workout_handlers._ONBOARDING


async def test_new_sets_in_a_closed_workout_refresh_the_summary(fresh_db, user_id):
    """Ключ кэша собран из того, что рисуется. По прежнему ключу (дата, число
    тренировок, последняя дата) дописанные подходы картинку не двигали."""
    db = fresh_db
    workout_handlers._heatmap_cache.pop(user_id, None)
    today = dt.date.today()
    bench = await _own(db, user_id, BENCH)
    workout_id = await _session(db, user_id, today, [(bench, 100, 5, 3)])

    _, first = await workout_handlers._menu_view(user_id)

    block_id = (await db.list_blocks_for_workout(workout_id))[0]["id"]
    for i in range(4):
        await db.add_set(block_id, bench, 10 + i, 0, 100.0, 8)
    _, second = await workout_handlers._menu_view(user_id)

    assert first is not None and second is not None
    assert first != second
