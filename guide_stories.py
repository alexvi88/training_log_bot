"""«Гайды» в приложении: статьи сайта (`seo.guide.*`) сторис из 5–8 слайдов.

Внизу вкладки «Меню» iOS-приложения — ряд круглых обложек; тап открывает
сторис. Текст — те же гайды, что на сайте (seo_library.GUIDES, все языки
сайта), без единого нового абзаца: слайд раздела — заголовок `sN.h` и первые
2–3 предложения `sN_html` без разметки, последний слайд — «Коротко»
(`takeaways`) и кнопка в нужное место приложения. Новое здесь только у кнопок
(подписи `guides.action.*`, готовые вопросы тренеру `guides.question.<slug>`) и
у личных строк (`guides.plateau.*`, `guides.newbie.reason`, `guides.volume.*`).

Разбор статьи на слайды не зависит от атлета и кэшируется по языку
(`_static_guide`), личное поверх него — `build`:

- `featured` — одна статья первой в ряду и только когда данные это
  подтверждают (TONE_OF_VOICE.md, «Пуш обязан быть правдой»): упражнение
  встало (вердикт analytics.classify_stall — тот же, что у тренера в
  get_stalled_lifts) → «Как выйти с плато» с упражнением на слайде раздела и
  кнопкой его прогресса; меньше FEATURE_NEWBIE_WORKOUTS тренировок, записанных
  у нас (импорт не считается — как у подсказок новичку), → «Первая неделя в
  зале»; иначе — никто.
- чип на слайде «подходы на мышцу в неделю» — подходы по группам за последние
  7 дней, тем же счётом, что панель объёма на главном экране
  (db.weekly_volume_by_group); подходов нет — чипа нет.

Картинки — кадры наших рисунков упражнений (exercise_media.frame_name,
только слаги из FRAME_REVISION), по смыслу статьи.
"""

from __future__ import annotations

import copy
import datetime as dt
import functools
import os
import re
from html import unescape
from typing import Any, Optional

import analytics
import config
import dashboard_data
import db
import exercise_media
import formatting
import i18n
import seed_data
import seo_library
import seo_pages
import timeutil

# Порядок ряда в приложении. Сначала то, что нужно атлету каждую неделю
# (прогрессия, отдых, плато, подходы на мышцу, разминка, как снимать подход,
# программы), потом понятия и разборы, переезд из Hevy/Strong — последним
# среди силовых (им пользуются один раз), питание — отдельным хвостом.
ORDER: tuple[str, ...] = (
    "progressive-overload", "rest-between-sets", "break-a-plateau", "sets-per-muscle-per-week",
    "warm-up-before-lifting", "film-your-set", "choose-a-program", "first-week-in-the-gym",
    "beginner-gym-program", "muscle-gain-program", "gym-program-for-women", "increase-bench-press",
    "reps-for-muscle-growth", "training-frequency", "compound-vs-isolation", "strength-vs-hypertrophy",
    "how-long-should-a-workout-be", "junk-volume", "common-gym-mistakes", "beginner-gains",
    "strength-training-after-40", "estimate-1rm-safely", "how-to-keep-a-workout-log",
    "switch-from-hevy-or-strong",
    # Питание и спортпит.
    "protein-per-day", "calories-for-muscle-gain", "cutting-without-losing-muscle", "creatine",
    "whey-protein", "pre-and-post-workout-meal", "caffeine-before-workout", "supplements-that-work",
)

# Полки экрана «Гайды» (ключ → статьи по порядку); подпись полки —
# `guides.shelf.<ключ>`. Статья может стоять на двух полках. Витрина
# (featured) поднимается первой на каждой своей полке.
SHELVES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("start", ("first-week-in-the-gym", "beginner-gym-program", "beginner-gains", "common-gym-mistakes",
               "choose-a-program", "how-to-keep-a-workout-log", "switch-from-hevy-or-strong")),
    ("growth", ("progressive-overload", "break-a-plateau", "sets-per-muscle-per-week", "reps-for-muscle-growth",
                "training-frequency", "junk-volume", "strength-vs-hypertrophy", "increase-bench-press")),
    ("training", ("warm-up-before-lifting", "rest-between-sets", "how-long-should-a-workout-be",
                  "compound-vs-isolation", "film-your-set", "estimate-1rm-safely", "strength-training-after-40")),
    ("programs", ("beginner-gym-program", "muscle-gain-program", "gym-program-for-women")),
    ("nutrition", seo_library.NUTRITION_GUIDES),
)

# Короткий ряд в «Меню»: до MENU_SIZE статей — витрина первой, дальше по
# ситуации атлета (новичку — «Первые шаги», остальным — «Рост» и «Техника»),
# последней — одна из «Питания».
MENU_SIZE = 4
MENU_NEWBIE = ("first-week-in-the-gym", "beginner-gym-program", "common-gym-mistakes", "beginner-gains")
MENU_REGULAR = ("progressive-overload", "rest-between-sets", "sets-per-muscle-per-week", "warm-up-before-lifting")
MENU_NUTRITION = "protein-per-day"

PLATEAU = "break-a-plateau"
NEWBIE = "first-week-in-the-gym"
VOLUME = "sets-per-muscle-per-week"

# Разделов на сторис — не больше: дальше человек сторис не долистывает, а
# целиком статья — по ссылке «Читать полностью».
MAX_SECTIONS = 6
# Слайд раздела: 2–3 предложения и не длиннее этого (обрезка — по границе
# предложения).
MAX_SENTENCES = 3
MAX_CHARS = 280
MAX_TAKEAWAYS = 4

# Разделы, которые в сторис не идут: «как я помогаю» (про сам продукт — его
# смысл передаёт кнопка на последнем слайде), «Частые вопросы» (вопрос-ответ
# не режется на 2–3 предложения) и «Пример недели» (расписание по дням, а не
# мысль). Номера — `sN` статьи; из оставшихся берутся первые MAX_SECTIONS.
SKIP_SECTIONS: dict[str, tuple[int, ...]] = {
    "how-to-keep-a-workout-log": (6,), "rest-between-sets": (6,), "break-a-plateau": (7,),
    "warm-up-before-lifting": (6,), "choose-a-program": (5,), "film-your-set": (6,),
    "compound-vs-isolation": (7, 8), "strength-vs-hypertrophy": (7, 8),
    "how-long-should-a-workout-be": (7, 8), "first-week-in-the-gym": (7, 8),
    "strength-training-after-40": (7, 8), "beginner-gains": (8,), "junk-volume": (7, 8),
    "beginner-gym-program": (7, 8), "muscle-gain-program": (7, 8), "gym-program-for-women": (7, 8),
    "increase-bench-press": (7, 8), "reps-for-muscle-growth": (7, 8), "training-frequency": (7, 8),
    "whey-protein": (8,), "calories-for-muscle-gain": (8,), "cutting-without-losing-muscle": (8,),
    "caffeine-before-workout": (8,), "pre-and-post-workout-meal": (5, 8),
}

# Кадры статьи — слаги рисунков (exercise_media.FRAME_REVISION). Слайды идут
# по кругу: первый кадр каждого слага, потом второй.
_BENCH, _SQUAT, _DEADLIFT = "barbell_bench_press_medium_grip", "barbell_squat", "barbell_deadlift"
_PULLUP, _CURL, _PUSHDOWN = "pullups", "dumbbell_bicep_curl", "triceps_pushdown_rope_attachment"
_LEG_PRESS, _GOBLET, _ROW = "leg_press", "goblet_squat", "seated_cable_rows"
FRAMES: dict[str, tuple[str, ...]] = {
    "progressive-overload": (_SQUAT, _LEG_PRESS, _BENCH),
    "rest-between-sets": (_BENCH, _CURL),
    "break-a-plateau": (_BENCH, _SQUAT, _DEADLIFT),
    "sets-per-muscle-per-week": (_CURL, _PUSHDOWN, "side_lateral_raise"),
    "warm-up-before-lifting": (_DEADLIFT, _GOBLET),
    "film-your-set": (_SQUAT, _DEADLIFT),
    "choose-a-program": (_SQUAT, _PULLUP, _BENCH),
    "first-week-in-the-gym": (_GOBLET, _LEG_PRESS, "pushups"),
    "beginner-gym-program": (_GOBLET, "wide_grip_lat_pulldown"),
    "muscle-gain-program": ("incline_dumbbell_press", "barbell_curl", "bent_over_barbell_row"),
    "gym-program-for-women": ("barbell_hip_thrust", "romanian_deadlift"),
    "increase-bench-press": (_BENCH, "close_grip_barbell_bench_press"),
    "reps-for-muscle-growth": (_CURL, _LEG_PRESS),
    "training-frequency": (_SQUAT, _PULLUP),
    "compound-vs-isolation": (_BENCH, _CURL),
    "strength-vs-hypertrophy": (_SQUAT, _CURL),
    "how-long-should-a-workout-be": (_ROW, _BENCH),
    "junk-volume": ("incline_dumbbell_press", _PUSHDOWN),
    "common-gym-mistakes": (_DEADLIFT, _SQUAT),
    "beginner-gains": ("pushups", _LEG_PRESS),
    "strength-training-after-40": (_LEG_PRESS, _ROW, _GOBLET),
    "estimate-1rm-safely": (_DEADLIFT, _BENCH),
    "how-to-keep-a-workout-log": (_BENCH, _SQUAT),
    "switch-from-hevy-or-strong": (_BENCH, _PULLUP),
    "protein-per-day": (_CURL, _BENCH),
    "calories-for-muscle-gain": (_SQUAT, _BENCH),
    "cutting-without-losing-muscle": (_PULLUP, "plank"),
    "creatine": (_DEADLIFT, _SQUAT),
    "whey-protein": ("barbell_curl", _ROW),
    "pre-and-post-workout-meal": ("seated_dumbbell_press", _LEG_PRESS),
    "caffeine-before-workout": (_DEADLIFT, _PULLUP),
    "supplements-that-work": ("standing_military_press", _CURL),
}

# Кнопка на последнем слайде: куда ведёт статья. `exercise_progress` у плато
# ставит build, когда упражнение нашлось; `coach_question` — с готовым
# вопросом `guides.question.<slug>`.
ACTION_KINDS = ("progress", "exercise_progress", "programs", "import", "video", "coach_question", "rest_timer")
ACTIONS: dict[str, str] = {
    "progressive-overload": "progress", "break-a-plateau": "progress", "sets-per-muscle-per-week": "progress",
    "training-frequency": "progress", "reps-for-muscle-growth": "progress", "beginner-gains": "progress",
    "junk-volume": "progress", "estimate-1rm-safely": "progress", "how-to-keep-a-workout-log": "progress",
    "increase-bench-press": "progress", "strength-vs-hypertrophy": "progress",
    "rest-between-sets": "rest_timer", "how-long-should-a-workout-be": "rest_timer",
    "film-your-set": "video",
    "choose-a-program": "programs", "first-week-in-the-gym": "programs", "beginner-gym-program": "programs",
    "muscle-gain-program": "programs", "gym-program-for-women": "programs",
    "switch-from-hevy-or-strong": "import",
    "warm-up-before-lifting": "coach_question", "compound-vs-isolation": "coach_question",
    "common-gym-mistakes": "coach_question", "strength-training-after-40": "coach_question",
    **{g: "coach_question" for g in seo_library.NUTRITION_GUIDES},
}

# Новичок для витрины: меньше стольких законченных тренировок, записанных у нас.
FEATURE_NEWBIE_WORKOUTS = 3
# Плато для витрины: столько недель без нового лучшего e1RM — не меньше.
PLATEAU_MIN_WEEKS = analytics.STALL_MIN_SPAN_WEEKS
# Групп в чипе объёма — не больше: строка чипа короткая.
VOLUME_CHIP_GROUPS = 3

# Сайт по умолчанию, если SITE_BASE_URL/PUBLIC_BASE_URL не настроены (тесты,
# локальный запуск): ссылка «Читать полностью» должна быть абсолютной.
DEFAULT_SITE = "https://athletediary.app"

# ---------- текст статьи → слайды ----------

_BLOCK_END = re.compile(r"</(?:p|li|h[1-6])>", re.I)
_TAG = re.compile(r"<[^>]+>")
_SENTENCE_END = re.compile(r"[.!?…]+[»”\")]*\s+")
# Сокращения, после точки в которых предложение не кончается, хотя дальше
# идёт заглавная («z. B. Kniebeugen», «Pelland et al. (2024)», «u. a.»,
# «ve ark.»). Сравнение — без регистра. Русских нет: в статьях после «т. е.»
# идёт строчная, и её ловит правило про строчную ниже.
_ABBREVIATIONS = frozenset({
    "z", "b", "bzw", "ca", "usw", "etc", "e", "g", "i", "vs", "al", "ex", "p",
    "vb", "dr", "nr", "u", "a", "ggf", "inkl", "evtl", "sog", "ark",
})
_TERMINAL = ".!?…:;"


def _blocks(html_text: str) -> list[str]:
    """Абзацы и пункты списков текстом, по одному на элемент. Пункт без точки
    в конце получает её: «Сон. Спишь…» читается подряд, а не слипается."""
    out = []
    for part in _BLOCK_END.split(html_text):
        # Теги внутри абзаца (<b>, <a>, <code>) — без пробела: границы абзацев
        # и пунктов уже разрезаны выше, а пробел на месте тега отклеил бы
        # запятую или кавычку от слова.
        text = " ".join(unescape(_TAG.sub("", part)).split())
        if not text:
            continue
        if text[-1] not in _TERMINAL and not text.endswith(("»", "”", ")", '"')):
            text += "."
        out.append(text)
    return out


def sentences(text: str) -> list[str]:
    """Предложения абзаца. Не конец предложения: точка после сокращения
    (_ABBREVIATIONS) и любой знак, после которого идёт строчная."""
    out: list[str] = []
    start = 0
    for m in _SENTENCE_END.finditer(text):
        before = text[start:m.start()].split()
        last = before[-1].strip("(«\"").lower() if before else ""
        if text[m.start()] == "." and last.rstrip(".") in _ABBREVIATIONS:
            continue
        # Со строчной предложение не начинается: «3. hafta» — порядковое
        # числительное по-турецки, а не конец фразы.
        if text[m.end():m.end() + 1].islower():
            continue
        out.append(text[start:m.end()].strip())
        start = m.end()
    tail = text[start:].strip()
    if tail:
        out.append(tail)
    return out


def _cut_words(text: str, limit: int) -> str:
    """Запасной случай — одно предложение длиннее лимита: по границе слова,
    с «…» после целого слова, без висящей запятой или тире."""
    cut = text[: limit - 1]
    if " " in cut:
        cut = cut[: cut.rfind(" ")]
    return cut.rstrip(" ,;:—–-") + "…"


def clip(html_text: str, *, limit: int = MAX_CHARS, max_sentences: int = MAX_SENTENCES) -> str:
    """Первые 2–3 предложения текста без разметки, не длиннее `limit`.

    Обрезка — по границе предложения. Вводная фраза перед списком («ответь на
    три вопроса:») последней не остаётся: без продолжения она обещает то,
    чего на слайде нет."""
    pieces = [s for block in _blocks(html_text) for s in sentences(block)]
    chosen: list[str] = []
    for s in pieces:
        if len(chosen) >= max_sentences:
            break
        if chosen and len(" ".join([*chosen, s])) > limit:
            break
        chosen.append(s)
    while len(chosen) > 1 and chosen[-1].endswith(":"):
        chosen.pop()
    text = " ".join(chosen)
    if len(text) > limit:
        return _cut_words(text, limit)
    if text.endswith(":"):
        text = text[:-1].rstrip() + "…"
    return text


def section_numbers(slug: str) -> list[int]:
    skip = SKIP_SECTIONS.get(slug, ())
    numbers = [i for i in range(1, seo_library.GUIDE_SECTIONS[slug] + 1) if i not in skip]
    return numbers[:MAX_SECTIONS]


def frame_paths(slug: str) -> list[str]:
    """Кадры статьи по кругу: первый кадр каждого слага, потом второй."""
    slugs = FRAMES[slug]
    return [
        exercise_media.media_url(exercise_media.frame_name(s, n))
        for n in (1, 2) for s in slugs
    ]


def site_lang(lang: str) -> str:
    """Язык атлета → язык сайта (у сайта те же коды, чужое — английский)."""
    return lang if lang in seo_pages.LANGS else seo_pages.X_DEFAULT_LANG


def guide_url(slug: str, lang: str) -> str:
    base = config.site_base_url() or DEFAULT_SITE
    return base + seo_pages.url_for(seo_pages.Page("guide", slug), site_lang(lang))


def _slide(kind: str, title: str, text: str, image: str, items: Optional[list[str]] = None) -> dict[str, Any]:
    return {"kind": kind, "title": title, "text": text, "image": image, "chip": None,
            "items": items or [], "action": None}


# Обложки гайдов — свои рисунки (`media/guides/<slug>.jpg`, скрипт
# `scripts/gen_guide_covers.py`): тренер в подвале под тему гайда. Нет файла —
# обложкой и первым слайдом служит кадр упражнения, как раньше.
GUIDE_COVER_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "media", "guides")


def cover_path(slug: str) -> str | None:
    if os.path.isfile(os.path.join(GUIDE_COVER_DIR, f"{slug}.jpg")):
        return f"/media/guides/{slug}.jpg"
    return None


@functools.lru_cache(maxsize=None)
def _static_guide(slug: str, lang: str) -> dict[str, Any]:
    """Статья на языке `lang` слайдами — без личного (featured, reason, чип,
    кнопка). Кэш по (статья, язык): каталоги не меняются за жизнь процесса.
    Отдаётся копией (static_guide) — build дописывает в неё личное."""
    lang = site_lang(lang)
    t = lambda key, **kw: i18n.t_in(lang, key, **kw)  # noqa: E731
    params = seo_library.link_params(lang)
    images = frame_paths(slug)
    image = lambda i: images[i % len(images)]  # noqa: E731
    cover = cover_path(slug) or images[0]
    slides = [_slide("cover", t(f"seo.guide.{slug}.h1"), clip(t(f"seo.guide.{slug}.lead")), cover)]
    for i in section_numbers(slug):
        slides.append(_slide(
            "section", t(f"seo.guide.{slug}.s{i}.h"), clip(t(f"seo.guide.{slug}.s{i}_html", **params)),
            image(len(slides)),
        ))
    items = [x.strip() for x in t(f"seo.guide.{slug}.takeaways", **params).split("\n") if x.strip()]
    slides.append(_slide("takeaways", t("seo.guide.takeaways.h"), "", image(len(slides)),
                         items[:MAX_TAKEAWAYS]))
    return {
        "slug": slug,
        "title": t(f"seo.guide.{slug}.h1"),
        "card": t(f"seo.guide.{slug}.card"),
        "cover": cover,
        "featured": False,
        "reason": None,
        "url": guide_url(slug, lang),
        "slides": slides,
    }


def static_guide(slug: str, lang: str) -> dict[str, Any]:
    return copy.deepcopy(_static_guide(slug, lang))


def action_for(slug: str, lang: str, *, exercise: Optional[tuple[int, str]] = None) -> dict[str, Any]:
    """Кнопка последнего слайда. `exercise` — (id, имя) застрявшего
    упражнения: тогда плато ведёт в его прогресс, а не в общий."""
    kind = ACTIONS[slug]
    t = lambda key, **kw: i18n.t_in(lang, key, **kw)  # noqa: E731
    if exercise is not None:
        ex_id, name = exercise
        return {"kind": "exercise_progress", "label": t("guides.action.exercise_progress", exercise=name),
                "exercise_id": ex_id, "question": None}
    question = t(f"guides.question.{slug}") if kind == "coach_question" else None
    return {"kind": kind, "label": t(f"guides.action.{kind}"), "exercise_id": None, "question": question}


# ---------- личное: что встало, новичок, объём ----------


async def stalled_lift(user_id: int, user: Any) -> Optional[dict[str, Any]]:
    """Упражнение с весом, которое встало: вердикт analytics.classify_stall
    (dead_end или regressing — как stall_facts.stalled у тренера) и не меньше
    PLATEAU_MIN_WEEKS недель от лучшего e1RM окна до последней сессии. Из
    нескольких — самое частое в окне. None — ничего не встало."""
    today = timeutil.user_today(user)
    window = analytics.STALL_WINDOW_WEEKS
    # Шире окна, как у тренера (ai_trainer._stalled_lifts): прибавка веса на
    # краю окна не должна читаться прибавкой с первого дня выборки.
    since = (today - dt.timedelta(weeks=window + 4)).isoformat()
    rows = await db.list_sets_by_exercise_since(user_id, since)
    by_exercise: dict[int, list[analytics.SetRow]] = {}
    names: dict[int, str] = {}
    for r in rows:
        by_exercise.setdefault(r["exercise_id"], []).append(
            analytics.SetRow(r["weight"], r["reps"], r["workout_id"], r["started_at"], r["rpe"])
        )
        names[r["exercise_id"]] = r["display_name"]
    best: Optional[dict[str, Any]] = None
    for ex_id, set_rows in by_exercise.items():
        sessions = analytics.group_sets_by_session(set_rows)
        for s in sessions:
            s.formula = user["e1rm_formula"]
        verdict = analytics.classify_stall(sessions, today)
        if verdict is None or verdict.kind not in ("dead_end", "regressing"):
            continue
        peak, latest = verdict.e1rm_peak, verdict.e1rm_latest
        if verdict.weight_since is None or peak is None or latest is None:
            continue  # вес тела: e1RM нет, и чип про него был бы неправдой
        weeks = (dt.date.fromisoformat(latest[0]) - dt.date.fromisoformat(peak[0])).days // 7
        if weeks < PLATEAU_MIN_WEEKS:
            continue
        found = {"exercise_id": ex_id, "name": names[ex_id], "e1rm": peak[3], "weeks": weeks,
                 "sessions": verdict.sessions_in_window}
        if best is None or (found["sessions"], found["weeks"]) > (best["sessions"], best["weeks"]):
            best = found
    return best


async def volume_chip(user_id: int, user: Any, lang: str) -> Optional[str]:
    """«За 7 дней: грудь 9 · спина 11 · ноги 4» — подходы по встроенным
    группам за последние 7 дней (тот же счёт, что панель объёма главного
    экрана), самые нагруженные первыми. None — подходов за неделю нет."""
    today = timeutil.user_today(user)
    start = today - dt.timedelta(days=6)
    counts = await db.weekly_volume_by_group(user_id, start.isoformat(), today.isoformat())
    totals: dict[str, int] = {}
    for group in await db.list_muscle_groups(user_id):
        name = group["name"]
        if seed_data.muscle_group_slug(name) not in dashboard_data.BODY_VOLUME_GROUPS or not counts.get(group["id"]):
            continue
        totals[name] = totals.get(name, 0) + counts[group["id"]]
    if not totals:
        return None
    top = sorted(totals.items(), key=lambda kv: (-kv[1], kv[0]))[:VOLUME_CHIP_GROUPS]
    groups = " · ".join(
        f"{seed_data.localized_muscle_group_name(name, lang)} {n}" for name, n in top
    )
    return i18n.t_in(lang, "guides.volume.chip", groups=groups)


def shelves(lang: str, featured: Optional[str]) -> list[dict[str, Any]]:
    out = []
    for key, slugs in SHELVES:
        ordered = list(slugs)
        if featured in ordered:
            ordered.remove(featured)
            ordered.insert(0, featured)
        out.append({"key": key, "title": i18n.t_in(lang, f"guides.shelf.{key}"), "slugs": ordered})
    return out


def menu(featured: Optional[str], newbie: bool) -> list[str]:
    out = [featured] if featured else []
    for slug in MENU_NEWBIE if newbie else MENU_REGULAR:
        if len(out) >= MENU_SIZE - 1:
            break
        if slug not in out:
            out.append(slug)
    if MENU_NUTRITION not in out:
        out.append(MENU_NUTRITION)
    return out[:MENU_SIZE]


async def build_response(user_id: int, user: Any) -> dict[str, Any]:
    """Тело GET /v1/guides: `guides` (витрина первой), `menu`, `shelves`."""
    lang = user["lang"] or "en"
    guides = await build(user_id, user)
    featured = guides[0]["slug"] if guides and guides[0]["featured"] else None
    newbie = await db.count_workouts(user_id, in_app_only=True) < FEATURE_NEWBIE_WORKOUTS
    return {"guides": guides, "menu": menu(featured, newbie), "shelves": shelves(lang, featured)}


async def build(user_id: int, user: Any) -> list[dict[str, Any]]:
    """Список гайдов для атлета: витрина первой, дальше ORDER."""
    lang = user["lang"] or "en"
    unit = user["unit"] or "kg"
    guides = {slug: static_guide(slug, lang) for slug in ORDER}
    for slug, guide in guides.items():
        guide["slides"][-1]["action"] = action_for(slug, lang)

    featured: Optional[str] = None
    stalled = await stalled_lift(user_id, user)
    if stalled is not None:
        featured = PLATEAU
        guide = guides[PLATEAU]
        e1rm = f"{formatting.format_weight(round(stalled['e1rm']))}{_unit_label(unit, lang)}"
        guide["reason"] = i18n.t_in(lang, "guides.plateau.reason", exercise=stalled["name"], weeks=stalled["weeks"])
        guide["slides"][1]["chip"] = i18n.t_in(
            lang, "guides.plateau.chip", exercise=stalled["name"], e1rm=e1rm, weeks=stalled["weeks"],
        )
        guide["slides"][-1]["action"] = action_for(
            PLATEAU, lang, exercise=(stalled["exercise_id"], stalled["name"]),
        )
    elif await db.count_workouts(user_id, in_app_only=True) < FEATURE_NEWBIE_WORKOUTS:
        featured = NEWBIE
        # «Только начинаешь» — правда, только если и перенесённой истории нет:
        # переехавшему из Hevy статья всё равно первой (наш ввод для него новый),
        # но без строки, которая про него неправда.
        if await db.count_workouts(user_id) < FEATURE_NEWBIE_WORKOUTS:
            guides[NEWBIE]["reason"] = i18n.t_in(lang, "guides.newbie.reason")

    chip = await volume_chip(user_id, user, lang)
    if chip is not None:
        guides[VOLUME]["slides"][1]["chip"] = chip

    order = list(ORDER)
    if featured is not None:
        guides[featured]["featured"] = True
        order.remove(featured)
        order.insert(0, featured)
    return [guides[slug] for slug in order]


def _unit_label(unit: str, lang: str) -> str:
    with i18n.use_lang(lang):
        return formatting.unit_label(unit)
