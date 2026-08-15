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

from analytics import WEEKLY_VOLUME_MAX, WEEKLY_VOLUME_MIN, linear_trend  # noqa: E402
from formatting import format_weight  # noqa: E402


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
        return f"{title}  {_arrow(direction)} {abs(trend.slope_per_week):.2f}/нед"
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
    ax = fig.subplots()
    dates = [p[0] for p in points]
    values = [p[1] for p in points]
    ax.plot(dates, values, marker="o", color="#3366cc")

    trend = linear_trend(points)
    if trend is not None and len(points) >= 2:
        t0 = dates[0].date()
        xs_days = [(d.date() - t0).days for d in dates]
        slope_per_day = trend.slope_per_week / 7
        trend_y = [trend.intercept + slope_per_day * x for x in xs_days]
        ax.plot(dates, trend_y, linestyle="--", color="#cc3333", alpha=0.7)
        ax.set_title(_trend_title(title, trend, values, show_weekly_rate))
    else:
        ax.set_title(title)

    ax.set_ylabel(ylabel)
    # Ticks only at dates that actually have a point — matplotlib's default date
    # locator picks evenly-spaced calendar dates, which can land on a day with
    # no session and misleadingly imply one happened there.
    max_ticks = 10
    step = max(1, -(-len(dates) // max_ticks))  # ceil division
    ax.set_xticks(dates[::step])
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%d.%m"))
    fig.autofmt_xdate()
    ax.grid(True, alpha=0.3)
    # Lower dpi than the other charts here: this one gets re-rendered and
    # re-uploaded to Telegram on every period-switch tap, so shaving ~35% off
    # the PNG (barely visible at this chart's small chat-embedded size) trims
    # both the encode and the upload time.
    return _fig_to_png(fig, dpi=100)


# Фон и акцент сводки. Имена достались от тепловой карты, которая раньше жила на
# главном экране; те же два цвета носят на себе плашка звания, карточки движений
# и заголовки блоков.
HEATMAP_EMPTY = "#1e242e"
HEATMAP_FILLED = "#4f8cff"  # same accent used elsewhere (e.g. render_workout_card)


# Цвета статусов недельного объёма (см. analytics.classify_weekly_volume). Цвет
# здесь — единственный носитель «мало / норма / перебор»: у груди, бицепса и
# трицепса в базе один и тот же эмодзи 💪, так что значком группы их не
# различить, а подпись занята названием.
VOLUME_COLOURS = {
    "low": "#e0a845",
    "in_range": "#45b97c",
    "high": "#e2685a",
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
            linewidth=0, facecolor=HEATMAP_FILLED, alpha=0.17, zorder=0,
        )
    )
    for edge in (lo, hi):
        ax.plot(
            [edge, edge], [-0.5, len(rows) - 0.5],
            color=HEATMAP_FILLED, alpha=0.5, linewidth=0.9,
            linestyle=(0, (1.5, 2)), zorder=1,
        )
    # Подпись коридора — в строке заголовка, а не под последней полосой: снизу она
    # висела в пустоте между панелью и календарём и читалась как подпись к нему.
    ax.text(
        (lo + hi) / 2, -0.95,
        f"НОРМА {WEEKLY_VOLUME_MIN}–{WEEKLY_VOLUME_MAX}",
        color=HEATMAP_FILLED, fontsize=DASH_FS_CAPTION, ha="center", va="center",
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
DASH_CARD = "#171d26"
DASH_RULE = "#2b3543"

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


def _dash_card(ax, x, y, w, h, colour=DASH_CARD) -> None:
    ax.add_patch(
        FancyBboxPatch((x, y), w, h, boxstyle="round,pad=0,rounding_size=0.02",
                       linewidth=0, facecolor=colour, zorder=0)
    )


def _dash_section(ax, title: str, note: str = "", note_colour: str = HEATMAP_FILLED) -> None:
    """Подпись виджета. Всегда на DASH_LEFT, примечание — на DASH_RIGHT: три
    разных левых края в одной картинке читаются как небрежность, и именно так
    выглядела первая версия."""
    ax.text(DASH_LEFT, -0.98, title, color="#9aa4b2", fontsize=DASH_FS_LABEL,
            fontweight="bold", va="center")
    if note:
        ax.text(DASH_RIGHT, -0.98, note, color=note_colour, fontsize=DASH_FS_CAPTION,
                ha="right", va="center")


def _dash_growth_tiles(
    fig, ax, tiles, fg: str, dim: str, accent: str, title: str = "", note: str = "",
) -> None:
    """Плитки роста e1RM: 2 строки по 3, имя — процент — «227кг vs 220кг».

    `tiles` — то, что вернул formatting.menu_lift_tiles: только выросшие
    движения, отсортированные по проценту роста. Процент — синим (тот же
    акцент, что у плашки звания и примечаний блоков), а не зелёным: зелёный
    уже занят статусом коридора объёма («в норме»), и тот же цвет у процента
    роста читался бы как ещё один статус, а не как отдельная цифра.

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
    BG, FG, MUTED, DIM = "#12161d", "#e6e6e6", "#9aa4b2", "#6b7684"

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
        _dash_card(ax, _DASH_BADGE_X, 0.28, DASH_RIGHT - _DASH_BADGE_X, 0.44, HEATMAP_EMPTY)
        ax.text((_DASH_BADGE_X + DASH_RIGHT) / 2, 0.50, badge, color=HEATMAP_FILLED, fontsize=DASH_FS_LABEL,
                fontweight="bold", ha="center", va="center")

    if tiles:
        ax = band("tiles")
        ax.set_ylim(1, 0)
        tile_w = (DASH_RIGHT - DASH_LEFT - 0.02 * (len(tiles) - 1)) / len(tiles)
        for i, (label, value) in enumerate(tiles):
            x = DASH_LEFT + i * (tile_w + 0.02)
            _dash_card(ax, x, 0.10, tile_w, 0.80)
            ax.text(x + 0.022, 0.34, label, color=DIM, fontsize=DASH_FS_CAPTION, va="center")
            ax.text(x + 0.022, 0.66, value, color=FG, fontsize=DASH_FS_VALUE,
                    fontweight="bold", va="center")

    if rows:
        ax = band("vol")
        _draw_volume_panel(ax, rows, volume_title, BG, FG, MUTED)

    if lift_tiles:
        _dash_growth_tiles(fig, band("lifts"), lift_tiles, FG, DIM, HEATMAP_FILLED, lifts_title, lifts_note)

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


def render_workout_card(
    title: str,
    body_lines: list[str],
    footer: str,
    note: str | None = None,
) -> bytes:
    """Render a workout breakdown as a dark, shareable card image.

    Kept emoji-free on purpose: matplotlib's bundled font renders emoji as
    blank boxes, so the card relies on colour and weight for hierarchy instead.
    """
    BG = "#12161d"
    FG = "#e6e6e6"
    ACCENT = "#4f8cff"
    MUTED = "#9aa4b2"
    NOTE = "#d9c98a"

    # (text, style) rows, top to bottom.
    rows: list[tuple[str, str]] = [("ТРЕНИРОВКА", "header"), (title, "muted"), ("", "normal")]
    if note:
        chunks = textwrap.wrap(note, width=46) or [note]
        chunks[0] = "«" + chunks[0]
        chunks[-1] = chunks[-1] + "»"
        for chunk in chunks:
            rows.append((chunk, "note"))
        rows.append(("", "normal"))
    for line in body_lines:
        # exercise headers start at column 0; set lines are indented with two spaces.
        style = "exercise" if line and not line.startswith(" ") else "normal"
        # Строка рекорда приходит со звёздочкой (formatting.build_workout_card) —
        # на картинке она выделяется цветом, как и подпись внизу.
        if line.lstrip().startswith("★"):
            style = "accent"
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
        "note": dict(color=NOTE, fontsize=11, style="italic"),
        "normal": dict(color=FG, fontsize=12),
    }

    y = fig_h - top_pad
    for text, style in rows:
        ax.text(0.05, y, text, family="monospace", va="top", **styles[style])
        y -= line_h
    return _fig_to_png(fig)
