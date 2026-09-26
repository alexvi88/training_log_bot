"""Render progress charts to PNG bytes (matplotlib, Agg backend, in-memory)."""

import datetime as dt
import io
import textwrap

import matplotlib

matplotlib.use("Agg")

import matplotlib.dates as mdates  # noqa: E402
from matplotlib.backends.backend_agg import FigureCanvasAgg  # noqa: E402
from matplotlib.figure import Figure  # noqa: E402
from matplotlib.patches import FancyBboxPatch, Rectangle  # noqa: E402

import i18n  # noqa: E402
from analytics import WEEKLY_VOLUME_MAX, WEEKLY_VOLUME_MIN, linear_trend  # noqa: E402
from formatting import format_weight  # noqa: E402

# ---------- палитра ----------
#
# Одна гамма на все картинки бота — та же, что у iOS-приложения
# (training_log_bot_ios: TrainingLog/DesignSystem/Theme.swift). Владелец хочет,
# чтобы сводка в Telegram, график прогресса и визитка тренировки выглядели как
# экраны приложения, а не как отдельный тёмный «терминал». Цвета заданы здесь
# один раз: рендеры ниже берут их по смыслу (фон, текст, акцент), а не по hex.
#
# Контраст текста проверен на фоне, где он лежит (WCAG AA ≥ 4.5:1):
# INK 17.4, INK_DIM 4.9, BRICK 5.1, LAMP_TEXT 4.9 — на PAPER; на белой
# карточке (SURFACE) каждый ещё немного выше. FAINT и LAMP — только для
# декора (дорожки, полосы), не для текста.
PAPER = "#faf7f4"        # фон страницы
SURFACE = "#ffffff"      # приподнятая карточка/плитка
SURFACE_SUNK = "#f1ece6" # утопленная поверхность: пустая дорожка полосы
HAIRLINE = (22 / 255, 18 / 255, 16 / 255, 0.08)   # rgba(22,18,16,.08) — рамки, линейки
INK = "#161210"          # основной текст
INK_DIM = "#6f6c6b"      # вторичный текст, подписи
FAINT = "#a6a5a4"        # только декор
BRICK = "#b4472f"        # бренд: главная линия, выбранное, акцент
BRICK_DEEP = "#963a26"
LAMP = "#e9a83d"         # подсветка: рекорды, огонёк (заливки, не текст)
LAMP_TEXT = "#8f6414"    # та же лампа, но читаемая как текст на светлом
GREEN = "#2e7d4f"        # «в норме» у коридора объёма


def _arrow(delta: float) -> str:
    return "↑" if delta > 0 else ("↓" if delta < 0 else "→")


def _trend_title(title: str, trend, values: list[float], show_weekly_rate: bool) -> str:
    """Заголовок графика со стрелкой изменения.

    Стрелка уже несёт знак, поэтому число рядом с ней печатается по модулю:
    «↓ -38.0» читалось как опечатка — тот же баг, что правил
    `formatting.format_delta`. Вес идёт через `format_weight`, чтобы не
    оставалось лишнего нуля («↓ 38», не «↓ 38.0»).
    """
    if show_weekly_rate:
        direction = 1 if trend.direction == "up" else (-1 if trend.direction == "down" else 0)
        suffix = i18n.t("chart.trend.per_week_suffix")
        return f"{title}  {_arrow(direction)} {abs(trend.slope_per_week):.2f}{suffix}"
    delta = values[-1] - values[0]
    return f"{title}  {_arrow(delta)} {format_weight(abs(delta))}"


# Figures are built directly rather than through pyplot: pyplot keeps a single
# global registry of figures per process, and these renders run in worker threads
# (asyncio.to_thread) — two users opening "Прогресс" at the same moment would be
# racing each other for that registry. Going through Figure() also removes the
# need to remember plt.close(), so a forgotten one can't leak.
def _new_figure(**kwargs) -> Figure:
    """A figure with an Agg canvas attached — pyplot normally does this, and
    without it anything that measures text (get_renderer) has no renderer to
    ask."""
    fig = Figure(**kwargs)
    FigureCanvasAgg(fig)
    return fig


def _fig_to_png(fig, dpi: int = 150, tight: bool = True) -> bytes:
    """tight=False — для фигур, у которых вся раскладка посчитана в дюймах и
    рассчитывает на точный размер кадра: bbox_inches="tight" обрезает по
    содержимому и сдвигает всё, что позиционировалось от краёв (разделительные
    линейки сводки уезжали за границу кадра именно так)."""
    buf = io.BytesIO()
    fig.savefig(
        buf, format="png", dpi=dpi,
        bbox_inches="tight" if tight else None, facecolor=fig.get_facecolor(),
    )
    buf.seek(0)
    return buf.read()


def render_metric_over_sessions(
    points: list[tuple[dt.datetime, float]],
    title: str,
    ylabel: str,
    show_weekly_rate: bool = True,
) -> bytes:
    """`show_weekly_rate` picks the title annotation: the per-week trend rate
    (used by the bodyweight diary) or the plain total change across the
    plotted points (used by the exercise progress chart, where a rate reads
    as noise next to "how much did it actually grow")."""
    fig = _new_figure(figsize=(6, 3.5))
    fig.patch.set_facecolor(PAPER)
    ax = fig.subplots()
    ax.set_facecolor(PAPER)
    dates = [p[0] for p in points]
    values = [p[1] for p in points]
    ax.plot(dates, values, marker="o", color=BRICK, linewidth=2, markersize=5, zorder=3)

    trend = linear_trend(points)
    if trend is not None and len(points) >= 2:
        t0 = dates[0].date()
        xs_days = [(d.date() - t0).days for d in dates]
        slope_per_day = trend.slope_per_week / 7
        trend_y = [trend.intercept + slope_per_day * x for x in xs_days]
        ax.plot(dates, trend_y, linestyle="--", color=INK_DIM, linewidth=1.2, zorder=2)
        ax.set_title(_trend_title(title, trend, values, show_weekly_rate),
                     color=INK, fontweight="bold")
    else:
        ax.set_title(title, color=INK, fontweight="bold")

    ax.set_ylabel(ylabel, color=INK_DIM)
    ax.tick_params(colors=INK_DIM, labelcolor=INK_DIM)
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    for side in ("left", "bottom"):
        ax.spines[side].set_color(FAINT)
    # Ticks only at dates that actually have a point — matplotlib's default date
    # locator picks evenly-spaced calendar dates, which can land on a day with
    # no session and misleadingly imply one happened there.
    max_ticks = 10
    step = max(1, -(-len(dates) // max_ticks))  # ceil division
    ax.set_xticks(dates[::step])
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%d.%m"))
    fig.autofmt_xdate()
    ax.grid(True, color=HAIRLINE, linewidth=0.8)
    ax.set_axisbelow(True)
    # Lower dpi than the other charts here: this one gets re-rendered and
    # re-uploaded to Telegram on every period-switch tap, so shaving ~35% off
    # the PNG (barely visible at this chart's small chat-embedded size) trims
    # both the encode and the upload time.
    return _fig_to_png(fig, dpi=100)


# Фон и акцент сводки. Имена достались от тепловой карты, которая раньше жила на
# главном экране: EMPTY — пустая дорожка полосы объёма, FILLED — бренд-акцент
# (плашка звания).
HEATMAP_EMPTY = SURFACE_SUNK
HEATMAP_FILLED = BRICK


# Цвета статусов недельного объёма (см. analytics.classify_weekly_volume). Цвет
# здесь — единственный носитель «мало / норма / перебор»: у груди, бицепса и
# трицепса в базе один и тот же эмодзи 💪, так что значком группы их не
# различить, а подпись занята названием.
VOLUME_COLOURS = {
    "low": LAMP,
    "in_range": GREEN,
    "high": BRICK,
}

# Геометрия строки объёма в долях ширины картинки: название группы прижато
# вправо к _VOL_LABEL_RIGHT, дорожка занимает середину, число — у правого края.
_VOL_LABEL_RIGHT = 0.25
_VOL_TRACK_LEFT = 0.275
_VOL_TRACK_RIGHT = 0.945
_VOL_NUMBER_RIGHT = 0.99

# Толщина полосы в точках. Скруглённые концы даёт кап линии, а не
# FancyBboxPatch: у панели объёма оси не равномасштабны, и скругление,
# заданное в данных, растянулось бы в овал.
_VOL_BAR_WIDTH = 7.5


def _volume_norm_label(lo: int, hi: int) -> str:
    """Подпись целевого коридора объёма. Вынесена в функцию (а не строка внутри
    _draw_volume_panel), чтобы её можно было проверить в тесте на язык, не
    рендеря саму картинку — ax.text() не отдаёт обратно то, что в него положили."""
    return i18n.t("chart.volume.norm", min=lo, max=hi)


def _volume_scale_max(rows: list[tuple[str, int, str]]) -> int:
    """Правый край шкалы. Коридор всегда виден целиком с запасом, но одна
    ударная неделя (30 подходов на спину) не должна сплющить остальные полосы
    в ничто."""
    busiest = max((sets for _, sets, _ in rows), default=0)
    return max(WEEKLY_VOLUME_MAX + 4, busiest)


def _draw_volume_panel(
    ax, rows: list[tuple[str, int, str]], title: str, bg: str, fg: str, muted: str
) -> None:
    scale = _volume_scale_max(rows)
    span = _VOL_TRACK_RIGHT - _VOL_TRACK_LEFT

    def x_of(sets: int) -> float:
        return _VOL_TRACK_LEFT + span * min(sets, scale) / scale

    ax.set_facecolor(bg)
    ax.axis("off")
    ax.set_xlim(0, 1)
    ax.set_ylim(len(rows) - 0.35, -1.4)  # inverted: самая нагруженная группа сверху

    ax.text(DASH_LEFT, -0.95, title, color=muted, fontsize=DASH_FS_LABEL,
            fontweight="bold", va="center")

    # Целевой коридор — одной подложкой на все строки, а не отдельной пометкой у
    # каждой полосы: норма одна и та же, и рисовать её семь раз значило бы семь
    # раз просить глаз её перечитать.
    lo, hi = x_of(WEEKLY_VOLUME_MIN), x_of(WEEKLY_VOLUME_MAX)
    ax.add_patch(
        Rectangle(
            (lo, -0.5), hi - lo, len(rows),
            linewidth=0, facecolor=INK, alpha=0.05, zorder=0,
        )
    )
    for edge in (lo, hi):
        ax.plot(
            [edge, edge], [-0.5, len(rows) - 0.5],
            color=INK_DIM, alpha=0.6, linewidth=0.9,
            linestyle=(0, (1.5, 2)), zorder=1,
        )
    # Подпись коридора — в строке заголовка, а не под последней полосой: снизу она
    # висела в пустоте между панелью и календарём и читалась как подпись к нему.
    ax.text(
        (lo + hi) / 2, -0.95,
        _volume_norm_label(WEEKLY_VOLUME_MIN, WEEKLY_VOLUME_MAX),
        color=INK_DIM, fontsize=DASH_FS_CAPTION, ha="center", va="center",
    )

    for row, (label, sets, status) in enumerate(rows):
        ax.text(_VOL_LABEL_RIGHT, row, label, color=muted, fontsize=DASH_FS_CAPTION,
                ha="right", va="center")
        ax.plot(
            [_VOL_TRACK_LEFT, _VOL_TRACK_RIGHT], [row, row],
            color=HEATMAP_EMPTY, linewidth=_VOL_BAR_WIDTH,
            solid_capstyle="round", zorder=2,
        )
        # Ноль полосой не рисуется вовсе: полоска нулевой длины со скруглённым
        # капом всё равно выглядит как «немного есть», а здесь ровно наоборот.
        if sets > 0:
            ax.plot(
                [_VOL_TRACK_LEFT, x_of(sets)], [row, row],
                color=VOLUME_COLOURS[status], linewidth=_VOL_BAR_WIDTH,
                solid_capstyle="round", zorder=3,
            )
        ax.text(
            _VOL_NUMBER_RIGHT, row, str(sets),
            color=fg if sets else muted, fontsize=DASH_FS_NUMBER,
            fontweight="bold" if sets else "normal", ha="right", va="center",
        )


# ---------- сводка на главном экране ----------

# Сводка живёт в том же сообщении меню, что и раньше жила одна тепловая карта.
# Всё позиционирование — в дюймах: Telegram масштабирует фото под ширину пузыря,
# поэтому размер элемента на экране равен его размеру в исходнике, умноженному на
# (ширина пузыря / ширина исходника). Отсюда единственное правило раскладки:
# исходник держим узким. Одна колонка, а не две.
DASH_WIDTH_IN = 6.67          # 1000 px при 150 dpi
DASH_LEFT, DASH_RIGHT = 0.04, 0.96
DASH_GAP = 0.34               # распорка между виджетами, с линейкой
DASH_PAD = 0.08               # распорка внутри группы, без линейки
DASH_CARD = SURFACE
# Линейка — та же волосяная HAIRLINE, но уже смешанная с PAPER в непрозрачный
# цвет: тест ищет её по точному пикселю, а полупрозрачная дала бы смесь.
DASH_RULE = "#e8e5e2"

# Типографика сводки: шесть роле́й вместо одиннадцати случайных размеров. До этого
# в одной картинке жили 6, 6.5, 7, 7.5, 8, 8.5, 9, 10, 13.5, 15 и 23 pt, причём
# подписи одного смысла — имя группы мышц и имя движения — отличались на полпункта.
# Полпункта не видно как решение, зато видно как неряшливость. Роль выбирается по
# тому, чем элемент является, а не по тому, сколько места осталось.
DASH_FS_DISPLAY = 23    # крупное число шапки, одно на всю картинку
DASH_FS_VALUE = 15      # число карточки: плитка, движение
DASH_FS_NUMBER = 10     # число в строке: подходы, изменение e1RM
DASH_FS_LABEL = 8.5     # подпись блока, плашка звания
DASH_FS_CAPTION = 7.5   # имя элемента, примечание блока
DASH_FS_MICRO = 6.5     # абсолютная прибавка под процентом в карточке движения

# Высоты виджетов в дюймах. Строка коридора и карточка движения заданы шагом,
# чтобы блок рос от числа строк, а не растягивал их.
_DASH_HEAD_H = 0.46
_DASH_TILES_H = 0.86
_DASH_VOL_STEP = 0.30

# Левый край плашки звания в шапке.
_DASH_BADGE_X = 0.70


def _shrink_to_fit(fig, txt, max_width: float, min_size: float = 13.0) -> None:
    """Уменьшает кегль надписи, пока она не влезет в `max_width` (доля ширины
    картинки). Ниже `min_size` не опускается: строка, набранная вдвое мельче
    заявленной роли, всё равно сломает шапку — пусть лучше подойдёт к плашке
    вплотную, чем превратится в подпись.

    Мерить приходится через рендерер: ширина строки известна только шрифту, а
    заранее считать её по числу знаков — это гадание, из-за которого шапка и
    налезала на плашку.
    """
    limit = max_width * fig.get_figwidth() * fig.dpi
    renderer = fig.canvas.get_renderer()
    while txt.get_fontsize() > min_size and txt.get_window_extent(renderer).width > limit:
        txt.set_fontsize(txt.get_fontsize() - 0.5)

# Плитки роста — 2 строки по 3, без спарклайна: рост уже назван числом
# (процентом), и линия рядом с ним не добавляет факта, только площадь. Единица
# та же, что у коридора объёма — воздух между блоками сводки везде одинаковый.
_LIFT_UNIT_IN = _DASH_VOL_STEP
_LIFT_TOP = -1.4          # верх полосы — как у панели объёма
_LIFT_COLS = 3
# Плитки крупнее, чем были: без календаря высота сводки освободилась, и это
# место логичнее отдать самому виджету с прогрессом, а не оставлять пустым.
_LIFT_ROW_H = 2.9         # высота одной плитки, в тех же единицах
_LIFT_ROW_GAP = 0.35
_LIFT_ROWS_TOP = (0.0, _LIFT_ROW_H + _LIFT_ROW_GAP)
_LIFT_BOTTOM = _LIFT_ROWS_TOP[-1] + _LIFT_ROW_H + 0.25
_DASH_LIFTS_H = _LIFT_UNIT_IN * (_LIFT_BOTTOM - _LIFT_TOP)


def _dash_card(ax, x, y, w, h, colour=DASH_CARD, edge=HAIRLINE) -> None:
    """Плоская карточка со скруглением и волосяной рамкой — как в приложении:
    белая плитка на тёплом фоне почти не отличается по светлоте, рамка её и
    отделяет (тени и градиенты приложение не использует)."""
    ax.add_patch(
        FancyBboxPatch((x, y), w, h, boxstyle="round,pad=0,rounding_size=0.02",
                       linewidth=0.8 if edge else 0, edgecolor=edge or "none",
                       facecolor=colour, zorder=0)
    )


def _dash_section(ax, title: str, note: str = "", note_colour: str = INK_DIM) -> None:
    """Подпись виджета. Всегда на DASH_LEFT, примечание — на DASH_RIGHT: три
    разных левых края в одной картинке читаются как небрежность, и именно так
    выглядела первая версия."""
    ax.text(DASH_LEFT, -0.98, title, color=INK_DIM, fontsize=DASH_FS_LABEL,
            fontweight="bold", va="center")
    if note:
        ax.text(DASH_RIGHT, -0.98, note, color=note_colour, fontsize=DASH_FS_CAPTION,
                ha="right", va="center")


def _dash_growth_tiles(
    fig, ax, tiles, fg: str, dim: str, accent: str, title: str = "", note: str = "",
) -> None:
    """Плитки роста e1RM: 2 строки по 3, имя — процент — «227кг vs 220кг».

    `tiles` — то, что вернул formatting.menu_lift_tiles: только выросшие
    движения, отсортированные по проценту роста. Процент — «лампой» в текстовом
    варианте (LAMP_TEXT, как рост на экране прогресса в приложении), а не
    зелёным: зелёный уже занят статусом коридора объёма («в норме»), и тот же
    цвет у процента роста читался бы как ещё один статус, а не как отдельная
    цифра.

    Имя не обрезается многоточием, но и не вылезает за плитку — вместо этого
    все шесть имён сжимаются по кеглю до общего размера, в котором помещается
    самое длинное: единый кегль сначала находится для каждой плитки отдельно
    (_shrink_to_fit), а затем всем шести назначается наименьший из найденных.
    Разный кегль от плитки к плитке выглядел бы небрежностью, а не решением —
    длинное каталожное имя из Hevy иначе либо рисовалось поверх соседней
    плитки, либо становилось заметно мельче своих соседей.
    """
    ax.set_ylim(_LIFT_BOTTOM, _LIFT_TOP)
    if title:
        _dash_section(ax, title, note)
    tile_w = (DASH_RIGHT - DASH_LEFT - 0.02 * (_LIFT_COLS - 1)) / _LIFT_COLS
    pad_x = 0.028
    name_texts = []
    for i, (name, pct, abs_str) in enumerate(tiles):
        row, col = divmod(i, _LIFT_COLS)
        x = DASH_LEFT + col * (tile_w + 0.02)
        y = _LIFT_ROWS_TOP[row]
        _dash_card(ax, x, y, tile_w, _LIFT_ROW_H)
        name_texts.append(ax.text(x + pad_x, y + _LIFT_ROW_H * 0.24, name, color=dim,
                                   fontsize=DASH_FS_CAPTION, va="center"))
        ax.text(x + pad_x, y + _LIFT_ROW_H * 0.58, pct, color=accent, fontsize=DASH_FS_VALUE,
                fontweight="bold", va="center")
        ax.text(x + pad_x, y + _LIFT_ROW_H * 0.85, abs_str, color=dim,
                fontsize=DASH_FS_MICRO, va="center")
    for txt in name_texts:
        _shrink_to_fit(fig, txt, tile_w - 2 * pad_x, min_size=5.0)
    uniform_size = min((txt.get_fontsize() for txt in name_texts), default=DASH_FS_CAPTION)
    for txt in name_texts:
        txt.set_fontsize(uniform_size)


def render_menu_dashboard(
    headline: str,
    badge: str = "",
    tiles: list[tuple[str, str]] | None = None,
    volume_rows: list[tuple[str, int, str]] | None = None,
    volume_title: str = "",
    lift_tiles: list[tuple[str, str, str]] | None = None,
    lifts_title: str = "",
    lifts_note: str = "",
) -> bytes:
    """Сводка для сообщения меню: крупное число, плитки, коридор по группам и
    плитки роста e1RM.

    Каждый виджет — своя ось со своей системой координат, между ними полосы-
    распорки. Воздух добавляется распоркой, а не растягиванием виджета: внутри
    виджета шаг строк и отступ — одна и та же величина, и растянув его, получаешь
    разъехавшиеся строки вместо отступа. Разделительные линейки рисуются детьми
    этих же распорок — артист уровня фигуры до пикселей не доходил, а
    непрозрачный фон соседней оси его закрывал.

    Любой из виджетов можно не передавать: у нового пользователя нет ни объёма,
    ни движений, и пустой блок сообщал бы только то, что он пуст.
    """
    BG, FG, MUTED, DIM = PAPER, INK, INK_DIM, INK_DIM

    tiles = list(tiles or ())
    rows = list(volume_rows or ())
    lift_tiles = list(lift_tiles or ())

    # «pad:» — распорка без линейки, «gap:» — с линейкой. Заголовок и плитки
    # разделять нечем: это одна группа, крупное число и его расшифровка.
    layout: list[tuple[str, float]] = [("head", _DASH_HEAD_H)]
    if tiles:
        layout += [("pad:tiles", DASH_PAD), ("tiles", _DASH_TILES_H)]
    if rows:
        layout += [("gap:vol", DASH_GAP), ("vol", 0.34 + _DASH_VOL_STEP * len(rows))]
    if lift_tiles:
        layout += [("gap:lifts", DASH_GAP), ("lifts", _DASH_LIFTS_H)]

    # Безусловная нижняя распорка: последняя полоса иначе заканчивается ровно
    # на нижнем пикселе фигуры. Без плиток роста (или вовсе без объёма —
    # у новичка, у которого есть только тоннаж) панель объёма сама становится
    # последней и прилипает к краю — обрезка читалась как баг вёрстки, а не
    # как задуманный край карточки. DASH_PAD — та же величина, что и «воздух»
    # внутри секции плиток роста под последним рядом (≈0.075in против
    # DASH_PAD=0.08in — по сути один и тот же отступ), так что нижнее поле не
    # выглядит ни уже, ни шире того, что уже есть в самом низком из виджетов.
    layout += [("pad:bottom", DASH_PAD)]

    fig_h = sum(h for _, h in layout)
    fig = _new_figure(figsize=(DASH_WIDTH_IN, fig_h), dpi=150)
    fig.patch.set_facecolor(BG)

    bands: dict[str, tuple[float, float]] = {}
    cursor = fig_h
    for key, height in layout:
        cursor -= height
        bands[key] = (cursor, height)

    def band(key: str):
        bottom, height = bands[key]
        ax = fig.add_axes([0, bottom / fig_h, 1, height / fig_h])
        ax.set_facecolor("none")   # фон уже у фигуры; свой закрывал бы линейки
        ax.axis("off")
        ax.set_xlim(0, 1)
        return ax

    ax = band("head")
    ax.set_ylim(1, 0)
    head = ax.text(DASH_LEFT, 0.50, headline, color=FG, fontsize=DASH_FS_DISPLAY,
                   fontweight="bold", va="center")
    # Заголовок без серии длиннее заголовка с серией: «3 тренировки за 30 дней»
    # против «9 недель подряд». Двадцать три пункта такую строку заводят прямо
    # под плашку звания, и у нового пользователя — то есть у единственного, кто
    # эту формулировку видит, — шапка выходила слипшейся. Кегль уменьшается
    # ровно настолько, чтобы строка кончилась до плашки.
    _shrink_to_fit(fig, head, (_DASH_BADGE_X - 0.02 if badge else DASH_RIGHT) - DASH_LEFT)
    if badge:
        # Шире, чем кажется нужным: в лестнице званий есть «Ветеран подвала», и
        # плашка по короткому «Атлет» его бы обрезала. Эмодзи звания сюда не
        # едет — matplotlib рисует 🪨 и 👑 квадратиком, шрифта с эмодзи в
        # контейнере нет.
        _dash_card(ax, _DASH_BADGE_X, 0.28, DASH_RIGHT - _DASH_BADGE_X, 0.44, HEATMAP_FILLED, edge=None)
        ax.text((_DASH_BADGE_X + DASH_RIGHT) / 2, 0.50, badge, color=SURFACE, fontsize=DASH_FS_LABEL,
                fontweight="bold", ha="center", va="center")

    if tiles:
        ax = band("tiles")
        ax.set_ylim(1, 0)
        tile_w = (DASH_RIGHT - DASH_LEFT - 0.02 * (len(tiles) - 1)) / len(tiles)
        fig_px = fig.get_figwidth() * fig.dpi
        renderer = fig.canvas.get_renderer()
        for i, tile in enumerate(tiles):
            label, value, *rest = tile
            sub = rest[0] if rest else ""
            x = DASH_LEFT + i * (tile_w + 0.02)
            inner = tile_w - 2 * 0.022
            _dash_card(ax, x, 0.10, tile_w, 0.80)
            label_txt = ax.text(x + 0.022, 0.34, label, color=DIM, fontsize=DASH_FS_CAPTION, va="center")
            # Подпись не должна вылезать за плитку ни на одном языке: ширина
            # известна только шрифту, поэтому ужимаем по рендереру, как шапку.
            _shrink_to_fit(fig, label_txt, inner, min_size=5.5)
            value_txt = ax.text(x + 0.022, 0.66, value, color=FG, fontsize=DASH_FS_VALUE,
                                fontweight="bold", va="center")
            if sub:
                # Приписка мелко и приглушённо сразу за числом, по базовой линии
                # числа — «43  14 за 30 дней» читается как одно целое.
                value_w = value_txt.get_window_extent(renderer).width / fig_px
                sub_txt = ax.text(x + 0.022 + value_w + 0.012, 0.69, sub, color=MUTED,
                                  fontsize=DASH_FS_LABEL, va="center")
                _shrink_to_fit(fig, sub_txt, inner - value_w - 0.012, min_size=5.0)

    if rows:
        ax = band("vol")
        _draw_volume_panel(ax, rows, volume_title, BG, FG, MUTED)

    if lift_tiles:
        _dash_growth_tiles(fig, band("lifts"), lift_tiles, FG, DIM, LAMP_TEXT, lifts_title, lifts_note)

    for key in bands:
        if not key.startswith("gap:"):
            continue
        bottom, height = bands[key]
        gap_ax = fig.add_axes([0, bottom / fig_h, 1, height / fig_h])
        gap_ax.set_facecolor("none")
        gap_ax.axis("off")
        gap_ax.set_xlim(0, 1)
        gap_ax.set_ylim(0, 1)
        gap_ax.plot([DASH_LEFT, DASH_RIGHT], [0.5, 0.5], color=DASH_RULE,
                    linewidth=1.0, solid_capstyle="butt")

    return _fig_to_png(fig, tight=False)


# At the card's fixed 6.6in width, monospace 12pt fits ~59 characters between the
# margins; wrapping a little short of that leaves room for glyphs wider than the
# measured average.
_CARD_BODY_WIDTH = 52


def _wrap_card_line(line: str) -> list[str]:
    """Wrap one body line to the card's width, keeping a set line's two-space
    indent on its continuations so it stays visually attached to its exercise."""
    if len(line) <= _CARD_BODY_WIDTH:
        return [line]
    indent = "  " if line.startswith("  ") else ""
    return textwrap.wrap(
        line,
        width=_CARD_BODY_WIDTH,
        subsequent_indent=indent + "  ",
        break_long_words=True,
    ) or [line]


def _workout_card_header() -> str:
    """Заголовок карточки-шаринга. Функция, а не строка на месте использования —
    тестируемость: карточка не своя строка, а растр, и без функции язык
    заголовка нельзя было бы проверить иначе как чтением пикселей."""
    return i18n.t("chart.workout_card.header")


def _quote_marks() -> tuple[str, str]:
    """Открывающая/закрывающая кавычка для цитаты рекорда в карточке.

    Ёлочки «» — только для русского: в английском тексте, по TONE_OF_VOICE
    (## English voice), кавычки прямые. Символ зависит от языка, а не текста,
    поэтому это пара из каталога, а не литерал."""
    return i18n.t("chart.quote_open"), i18n.t("chart.quote_close")


#  Раньше у бота была своя тёмная «терминальная» карточка, а светлая гамма
#  приложения шла только в /v1 (api_v1_history.py просит theme="app").
#  Владелец захотел один стиль картинок у бота и приложения, поэтому обе темы
#  теперь — одна палитра из блока наверху модуля. Параметр theme оставлен,
#  чтобы не ломать вызовы и кэш по ключу; различать темы снова — значит
#  завести здесь вторую запись, а не трогать рендер.
#  RECORD — строка рекорда (★): рекорды в приложении подсвечиваются лампой.
_APP_CARD_PALETTE = {
    "BG": PAPER, "FG": INK, "ACCENT": BRICK, "MUTED": INK_DIM,
    "NOTE": LAMP_TEXT, "RECORD": LAMP_TEXT,
}
_CARD_THEMES = {"bot": _APP_CARD_PALETTE, "app": _APP_CARD_PALETTE}


def render_workout_card(
    title: str,
    body_lines: list[str],
    footer: str,
    note: str | None = None,
    theme: str = "bot",
) -> bytes:
    """Render a workout breakdown as a shareable card image.

    Kept emoji-free on purpose: matplotlib's bundled font renders emoji as
    blank boxes, so the card relies on colour and weight for hierarchy instead.

    `theme` picks the palette (see `_CARD_THEMES`) — everything below this
    point (layout, wrapping, row styles) is identical between them, only the
    colours change.
    """
    colors = _CARD_THEMES[theme]
    BG = colors["BG"]
    FG = colors["FG"]
    ACCENT = colors["ACCENT"]
    MUTED = colors["MUTED"]
    NOTE = colors["NOTE"]
    RECORD = colors["RECORD"]

    # (text, style) rows, top to bottom.
    rows: list[tuple[str, str]] = [(_workout_card_header(), "header"), (title, "muted"), ("", "normal")]
    if note:
        quote_open, quote_close = _quote_marks()
        chunks = textwrap.wrap(note, width=46) or [note]
        chunks[0] = quote_open + chunks[0]
        chunks[-1] = chunks[-1] + quote_close
        for chunk in chunks:
            rows.append((chunk, "note"))
        rows.append(("", "normal"))
    for line in body_lines:
        # exercise headers start at column 0; set lines are indented with two spaces.
        style = "exercise" if line and not line.startswith(" ") else "normal"
        # Строка рекорда приходит со звёздочкой (formatting.build_workout_card) —
        # на картинке она выделяется цветом, как и подпись внизу.
        if line.lstrip().startswith("★"):
            style = "record"
        # Wrapped for the same reason the note above is: the figure is a fixed
        # 6.6in wide and nothing clips text, so an over-long line (a long exercise
        # name, or an exercise with many distinct sets) just ran off the right
        # edge of the shared image.
        for chunk in _wrap_card_line(line):
            rows.append((chunk, style))
    rows.append(("─" * 28, "muted"))
    rows.append((footer, "accent"))

    line_h = 0.30
    top_pad, bottom_pad = 0.40, 0.32
    fig_w = 6.6
    fig_h = top_pad + bottom_pad + len(rows) * line_h

    fig = _new_figure(figsize=(fig_w, fig_h), dpi=150)
    fig.patch.set_facecolor(BG)
    ax = fig.add_axes([0, 0, 1, 1])
    ax.set_facecolor(BG)
    ax.axis("off")
    ax.set_xlim(0, 1)
    ax.set_ylim(0, fig_h)

    styles = {
        "header": dict(color=ACCENT, fontsize=17, fontweight="bold"),
        "muted": dict(color=MUTED, fontsize=11),
        "exercise": dict(color=FG, fontsize=12, fontweight="bold"),
        "accent": dict(color=ACCENT, fontsize=12, fontweight="bold"),
        "record": dict(color=RECORD, fontsize=12, fontweight="bold"),
        "note": dict(color=NOTE, fontsize=11, style="italic"),
        "normal": dict(color=FG, fontsize=12),
    }

    y = fig_h - top_pad
    for text, style in rows:
        ax.text(0.05, y, text, family="monospace", va="top", **styles[style])
        y -= line_h
    return _fig_to_png(fig)
