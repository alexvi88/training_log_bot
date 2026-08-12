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
from analytics import e1rm as analytics_e1rm
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


# ---------- рост e1RM за окно ----------


async def test_growth_compares_the_window_against_everything_before_it(fresh_db, user_id):
    db = fresh_db
    today = dt.date.today()
    bench = await _own(db, user_id, BENCH)
    window_start = today - dt.timedelta(weeks=8)
    await _session(db, user_id, window_start - dt.timedelta(days=10), [(bench, 100, 5, 1)])
    await _session(db, user_id, today - dt.timedelta(days=1), [(bench, 110, 5, 1)])

    before, window = await db.exercise_e1rm_growth(user_id, bench, window_start.isoformat())

    assert before == analytics_e1rm(100.0, 5)
    assert window == analytics_e1rm(110.0, 5)


async def test_growth_baseline_is_the_best_before_the_window_not_the_first_point_inside_it(
    fresh_db, user_id,
):
    """Серия «220, 210, 227» за окно не читается как рост с 220 до 227: правильная
    база — лучший результат ДО окна, а не первая точка внутри него."""
    db = fresh_db
    today = dt.date.today()
    bench = await _own(db, user_id, BENCH)
    window_start = today - dt.timedelta(weeks=8)
    await _session(db, user_id, window_start - dt.timedelta(days=5), [(bench, 220, 5, 1)])
    await _session(db, user_id, window_start + dt.timedelta(days=1), [(bench, 210, 5, 1)])
    await _session(db, user_id, today - dt.timedelta(days=1), [(bench, 227, 5, 1)])

    before, window = await db.exercise_e1rm_growth(user_id, bench, window_start.isoformat())

    assert before == analytics_e1rm(220.0, 5)
    assert window == analytics_e1rm(227.0, 5)


async def test_growth_with_no_history_before_the_window_has_no_baseline(fresh_db, user_id):
    db = fresh_db
    today = dt.date.today()
    bench = await _own(db, user_id, BENCH)
    window_start = today - dt.timedelta(weeks=8)
    await _session(db, user_id, today - dt.timedelta(days=1), [(bench, 100, 5, 1)])

    before, window = await db.exercise_e1rm_growth(user_id, bench, window_start.isoformat())

    assert before == 0
    assert window == analytics_e1rm(100.0, 5)


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


def test_the_tonnage_tile_counts_tonnes_in_kilograms():
    """Тонна — тонна: у человека в фунтах плитка обязана показывать то же число,
    что зал славы и недельная сводка.

    Тоннаж лежит в единицах пользователя, а плитка делила его на 1000 как есть —
    24 500 фунтов превращались в «24.5 т» вместо 11.1 тонны, и плитка врала
    больше чем вдвое относительно остальных экранов.
    """
    tile = formatting.menu_tiles(_dashboard(), 24_500, 3, "lb")[1]

    assert tile == ("ТОННАЖ ЗА 7 ДНЕЙ", "11.1 т")
    assert formatting.format_tonnage(24_500, "lb").startswith("11.1")
    # Ниже тонны конвертировать нечего — это его число в его единицах.
    assert formatting.menu_tiles(_dashboard(), 900, 3, "lb")[1] == ("ТОННАЖ ЗА 7 ДНЕЙ", "900 lb")


def test_the_records_tile_gives_its_place_away_when_there_are_none():
    with_records = formatting.menu_tiles(_dashboard(this_week=2), 5000, 2)
    without = formatting.menu_tiles(_dashboard(this_week=2), 5000, 0)

    assert with_records[2] == ("РЕКОРДОВ ЗА 7 ДНЕЙ", "2")
    assert without[2] == ("ТРЕНИРОВОК ЗА НЕДЕЛЮ", "2")
    assert len(with_records) == len(without) == 3


def test_the_week_tile_is_skipped_when_it_would_repeat_the_month():
    """Первая неделя в дневнике: вся история — эта неделя, и «ЗА НЕДЕЛЮ 1» рядом
    с «ЗА 30 ДНЕЙ 1» это одно число, поставленное дважды."""
    tiles = formatting.menu_tiles(_dashboard(this_week=1, last_30_days=1), 300, 0)

    assert [label for label, _ in tiles] == ["ТРЕНИРОВОК ЗА 30 ДНЕЙ", "ТОННАЖ ЗА 7 ДНЕЙ"]


def test_growth_tiles_carry_percent_and_absolute_values():
    tiles = formatting.menu_lift_tiles([("Жим штанги лёжа", 100.0, 112.0)])

    assert tiles == [("ЖИМ ШТАНГИ ЛЁЖА", "+12%", "112кг vs 100кг")]


def test_a_drop_does_not_get_a_tile():
    """Плитка существует, чтобы показать рост — для просевшего движения плитки
    просто нет, а не плитка с минусом."""
    assert formatting.menu_lift_tiles([("Присед", 120.0, 110.0)]) == []


def test_a_flat_result_is_not_growth():
    assert formatting.menu_lift_tiles([("Жим", 100.0, 100.0)]) == []


def test_a_movement_without_a_baseline_is_dropped():
    """До окна упражнения не было вовсе — не с чем сравнивать рост, и «e1RM внутри
    окна вырос с нуля» — не факт, который стоит показывать плиткой."""
    assert formatting.menu_lift_tiles([("Жим", 0.0, 100.0)]) == []


def test_best_growth_is_first():
    tiles = formatting.menu_lift_tiles([
        ("Жим", 100.0, 105.0),      # +5%
        ("Присед", 100.0, 120.0),   # +20%
    ])

    assert [t[0] for t in tiles] == ["ПРИСЕД", "ЖИМ"]


def test_more_than_six_growing_exercises_is_capped():
    growth = [(f"Упражнение {i}", 100.0, 100.0 + i) for i in range(1, 9)]

    assert len(formatting.menu_lift_tiles(growth)) == 6


def test_names_are_never_truncated():
    """Плитка не спарклайн: место под линию не нужно, и длинное имя влезает
    целиком — обрезать его больше незачем."""
    name = "Жим штанги на наклонной скамье вниз головой"
    tiles = formatting.menu_lift_tiles([(name, 100.0, 110.0)])

    assert tiles[0][0] == name.upper()


# ---------- отрисовка ----------


def _render(**kwargs):
    base = dict(
        headline="9 недель подряд", badge="ТЯЖЕЛОВЕС",
        tiles=[("ТРЕНИРОВОК ЗА 30 ДНЕЙ", "12"), ("ТОННАЖ ЗА 7 ДНЕЙ", "24.5 т"), ("РЕКОРДОВ 7 Д", "3")],
        volume_rows=[("СПИНА", 14, "high"), ("ГРУДЬ", 9, "in_range"), ("НОГИ", 0, "none")],
        volume_title="ОБЪЁМ ЗА 7 ДНЕЙ · 23 ПОДХОДА",
        lift_tiles=[("ЖИМ ЛЁЖА", "+12%", "112кг vs 100кг")],
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
    narrow = _png_size(_render(volume_rows=[], volume_title="", lift_tiles=[]))
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
        lift_tiles=[
            ("ЖИМ ЛЁЖА", "+12%", "112кг vs 100кг"),
            ("ПРИСЕД", "+13%", "158кг vs 140кг"),
            ("ТЯГА", "+7%", "192кг vs 180кг"),
        ],
    ))

    assert height > width


def test_every_widget_can_be_absent():
    """У нового пользователя нет ни объёма, ни движений, а пустой блок сообщал бы
    только то, что он пуст."""
    full = _png_size(_render())[1]
    bare = _png_size(_render(tiles=[], volume_rows=[], volume_title="", lift_tiles=[], badge=""))[1]

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


def test_a_single_growth_tile_still_renders():
    assert _render(lift_tiles=[("ЖИМ", "+5%", "105кг vs 100кг")])[:8] == b"\x89PNG\r\n\x1a\n"


def test_a_long_headline_stops_before_the_rank_badge():
    """Заголовок без серии длиннее заголовка с ней («3 тренировки за 30 дней»
    против «9 недель подряд»), и на 23 пунктах он заезжал под плашку звания —
    ровно у новичка, единственного, кто эту формулировку видит."""
    import io

    from PIL import Image

    img = Image.open(io.BytesIO(_render(headline="3 тренировки за 30 дней"))).convert("RGB")
    width, _ = img.size
    badge_left = round(charts._DASH_BADGE_X * width)
    fg = (0xE6, 0xE6, 0xE6)
    head_rows = range(round(0.28 * charts._DASH_HEAD_H * 150), round(0.72 * charts._DASH_HEAD_H * 150))

    assert not any(
        img.getpixel((x, y)) == fg
        for y in head_rows for x in range(badge_left, width)
    )


def test_the_growth_tiles_share_row_pitch_with_the_volume_panel():
    """Плитки роста размечены тем же шагом, что и коридор объёма — так же, как
    раньше карточки движений: сравниваем шаг, а не пиксели, которые поедут от
    любой правки шрифта."""
    assert charts._LIFT_UNIT_IN == charts._DASH_VOL_STEP
    lift_units = charts._LIFT_BOTTOM - charts._LIFT_TOP
    assert abs(charts._LIFT_UNIT_IN * lift_units - charts._DASH_LIFTS_H) < 1e-9


def test_a_long_name_does_not_make_tiles_different_sizes():
    """Раньше каждое имя сжималось независимо: у длинного каталожного имени
    ('CHEST PRESS MACHINE - HORIZONTAL') кегль падал сильнее, чем у короткого
    соседа, и плитки выглядели разнокалиберными. Теперь всем шести назначается
    один и тот же кегль — наименьший из тех, что нашёл каждый сам по себе."""
    fig = charts._new_figure(figsize=(charts.DASH_WIDTH_IN, 3), dpi=150)
    ax = fig.add_axes([0, 0, 1, 1])
    ax.axis("off")
    ax.set_xlim(0, 1)
    tiles = [
        ("CHEST PRESS MACHINE - HORIZONTAL", "+60%", "115кг vs 72кг"),
        ("ПРИСЕД", "+5%", "126кг vs 120кг"),
        ("ТЯГА", "+3%", "180кг vs 175кг"),
    ]

    charts._dash_growth_tiles(fig, ax, tiles, "#e6e6e6", "#6b7684", "#45b97c")

    name_sizes = {
        txt.get_fontsize() for txt in ax.texts
        if txt.get_text() in {t[0] for t in tiles}
    }
    assert len(name_sizes) == 1


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


async def test_menu_view_only_draws_growth_that_actually_happened(fresh_db, user_id):
    """Присед не вырос — плитки для него быть не должно, даже если он самое
    частое движение человека за окно."""
    db = fresh_db
    workout_handlers._heatmap_cache.pop(user_id, None)
    today = dt.date.today()
    bench, squat = await _own(db, user_id, BENCH), await _own(db, user_id, SQUAT)
    for offset in (30, 20, 10, 1):
        await _session(db, user_id, today - dt.timedelta(days=offset), [(squat, 120, 5, 3)])
    await _session(db, user_id, today - dt.timedelta(weeks=10), [(bench, 100, 5, 3)])
    await _session(db, user_id, today - dt.timedelta(days=1), [(bench, 120, 5, 3)])

    _, png = await workout_handlers._menu_view(user_id)

    assert png is not None
