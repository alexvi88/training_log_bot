"""Публичные страницы для поиска: переезд из Hevy/Strong, калькуляторы,
нормативы силы, готовые программы, техника упражнений, рассказ о боте и
сравнения.

Зачем: первое касание человека, который ищет «как перенести тренировки из
Hevy» или «калькулятор 1ПМ», — поиск (и поиск внутри ChatGPT/Perplexity), а
не каталог ботов. Кнопки ведут через `/go/bot?f=<блок>` и
`/go/appstore?f=<блок>`: там клик считается (site_stats) и уходит
редиректом на `t.me/<бот>?start=src_seo_<блок>` (acquisition.py) или в App
Store — так `/growth` видит и клики, и тех, кто дошёл до первой тренировки.

Язык — в пути, сегментом из строчных букв: `/ru/...`, `/en/...`, `/es/`,
`/pt/` (бразильский), `/pt-pt/`, `/de/`, `/fr/`, `/tr/` — `lang_segment` и
`lang_from_segment`, других способов собрать адрес нет. У каждой страницы
ровно один адрес на язык, его и называет `canonical`, а все языки связаны
`hreflang` (`pt-BR`/`pt-PT` у португальского, x-default — английская).
Ключей `seo.*` нет в каталоге языка — `i18n` берёт английский. Голый `/` — выбор языка тем же `legal_pages.page_lang` и
редирект.

Кнопка «скачать» зависит от устройства (по User-Agent, на сервере, поэтому
`Vary: User-Agent`). Главный продукт — приложение для iPhone: App Store
первой кнопкой на любом устройстве, кроме Android (там приложения нет —
главный бот). На компьютере рядом — QR-код в App Store, а бот — строкой
помельче под кнопкой, как на iPhone. Язык страницы на это не влияет. Обе
ссылки есть в любом варианте — робот, который пришёл без браузерного
User-Agent, видит обе. Исключение одно — страница про сам бот (`telegram`):
там первой идёт кнопка бота, App Store — строкой под ней.

Висят на приложении MCP-сервера через `custom_route`, как /privacy
(legal_pages.py). Картинки упражнений — уже публичный
`/v1/media/exercises/<файл>`; своя статика — `/site/*` (seo_static.py).

Тексты — каталог (`seo.*` в locales/<язык>.json), данные каталога —
через те же локализаторы, что у бота и /v1 (seed_data.localized_*,
exercise_descriptions.get_description): русское имя упражнения здесь только
идентичность для поиска картинки и описания, на экран идёт локализованное.
Строк, вычисленных на импорте, нет — всё собирается в момент запроса.

Ключи с суффиксом `_html` — доверенная разметка из каталога (списки, ссылки,
жирный) и подставляются как есть; остальные экранируются.

Аналитики в браузере нет: ни счётчиков, ни cookies, ни сторонних скриптов.
Посещение считает сервер (site_stats.py) — без адреса клиента и без связи с
аккаунтом; политика конфиденциальности это описывает. Единственный свой
скрипт учёта — маячок `site_stats.beacon_script` в конце страницы: показанная
на экране страница шлёт `POST /go/seen` (`seen_route`), и только такой
просмотр /growth считает человеческим — серверный запрос страницы шлёт и
скрейпер с браузерным User-Agent. У 404 маячка нет.

Встраиваемый калькулятор (`/embed/<язык>/1rm`) — единственный адрес, которые разрешено показывать в чужом iframe
(`frame-ancestors *`); у всех остальных `frame-ancestors 'none'`.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
from dataclasses import dataclass
from html import escape
from typing import Any, Callable, Optional
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from starlette.requests import Request
from starlette.responses import FileResponse, HTMLResponse, PlainTextResponse, RedirectResponse, Response

import acquisition
import config
import exercise_descriptions
import exercise_media
import i18n
import keyboards
import legal_pages
import qr_code
import seed_data
import seo_design
import seo_static
import seo_tools
import site_stats

LANGS = ("ru", "en", "es", "pt", "pt-PT", "de", "fr", "tr")
X_DEFAULT_LANG = "en"
# Язык → код для hreflang и `<html lang>`: у португальского два варианта, и
# поиску надо сказать, какой из них бразильский; остальные — как есть.
_HREFLANG = {"pt": "pt-BR", "pt-PT": "pt-PT"}
OG_LOCALES = {"ru": "ru_RU", "en": "en_US", "es": "es_ES", "pt": "pt_BR", "pt-PT": "pt_PT",
              "de": "de_DE", "fr": "fr_FR", "tr": "tr_TR"}
# Переключатель языка подписан автонимами — теми же, что кнопки языка у бота
# (keyboards.LANG_NAMES): их не переводят, человек ищет свой язык написанным
# по-своему.
AUTONYMS = keyboards.LANG_NAMES


def lang_segment(lang: str) -> str:
    """Язык → сегмент пути: `/ru/`, `/pt-pt/` — в адресе только строчные."""
    return lang.lower()


_LANG_BY_SEGMENT = {lang_segment(code): code for code in LANGS}


def lang_from_segment(segment: str) -> Optional[str]:
    """Сегмент пути → язык сайта или None (`pt-pt` → `pt-PT`)."""
    return _LANG_BY_SEGMENT.get(segment)


def lang_of_path(path: str) -> Optional[str]:
    """Язык по первому сегменту пути: `/pt-pt/tools` → `pt-PT`, чужой путь — None."""
    return lang_from_segment(path.lstrip("/").split("/", 1)[0])


def hreflang(lang: str) -> str:
    """Код языка для hreflang, `<html lang>` и Content-Language."""
    return _HREFLANG.get(lang, lang)


def lang_from_code(code: str) -> Optional[str]:
    """Язык по сегменту пути или коду `<html lang>` без учёта регистра: `pt-BR`,
    `pt-pt`, `ru`. Для адресов, которые собирает скрипт страницы (карточка
    результата) из `document.documentElement.lang`, и `?lang=` манифеста."""
    code = (code or "").lower()
    for lang in LANGS:
        if code in (lang_segment(lang), hreflang(lang).lower()):
            return lang
    return None
BOT_TAG_PREFIX = "src_seo_"

# Упражнения с отдельной страницей техники: адрес → слаг картинок
# (exercise_media.EXERCISE_IMAGE_SLUGS). Только те, у которых есть пара
# кадров, описание на обоих языках и свой текст (вступление, постановка,
# ошибки, мышцы, снаряд и порог входа) — это держит тест. Остальные
# упражнения каталога с кадрами (вариации хвата и угла: «… поочерёдно»,
# «… в Смите за спиной») своей страницы не получают — показываются
# карточкой в заменах и на страницах «мышца × снаряд»: своего текста на
# каждую вариацию нет, а страница без него — тонкая.
EXERCISES: tuple[tuple[str, str], ...] = (
    ("back-squat", "barbell_squat"),
    ("bench-press", "barbell_bench_press_medium_grip"),
    ("deadlift", "barbell_deadlift"),
    ("romanian-deadlift", "romanian_deadlift"),
    ("overhead-press", "standing_military_press"),
    ("barbell-row", "bent_over_barbell_row"),
    ("pull-up", "pullups"),
    ("dips", "dips_chest_version"),
    ("leg-press", "leg_press"),
    ("lat-pulldown", "wide_grip_lat_pulldown"),
    ("seated-cable-row", "seated_cable_rows"),
    ("dumbbell-bench-press", "dumbbell_bench_press"),
    ("incline-dumbbell-press", "incline_dumbbell_press"),
    ("incline-bench-press", "barbell_incline_bench_press_medium_grip"),
    ("seated-dumbbell-press", "seated_dumbbell_press"),
    ("lateral-raise", "side_lateral_raise"),
    ("barbell-curl", "barbell_curl"),
    ("hammer-curl", "hammer_curls"),
    ("triceps-pushdown", "triceps_pushdown"),
    ("skull-crusher", "ez_bar_skullcrusher"),
    ("dumbbell-lunge", "dumbbell_lunges"),
    ("bulgarian-split-squat", "split_squat_with_dumbbells"),
    ("goblet-squat", "goblet_squat"),
    ("front-squat", "front_barbell_squat"),
    ("glute-bridge", "barbell_glute_bridge"),
    ("back-extension", "hyperextensions_back_extensions"),
    ("standing-calf-raise", "standing_calf_raises"),
    ("one-arm-dumbbell-row", "one_arm_dumbbell_row"),
    ("good-morning", "good_morning"),
    ("push-up", "pushups"),
    ("plank", "plank"),
    ("hanging-leg-raise", "hanging_leg_raise"),
    # Вторая волна — базовые движения с самым частым поиском.
    ("decline-bench-press", "decline_barbell_bench_press"),
    ("close-grip-bench-press", "close_grip_barbell_bench_press"),
    ("machine-chest-press", "machine_bench_press"),
    ("cable-crossover", "cable_crossover"),
    ("pec-deck", "butterfly"),
    ("dumbbell-fly", "dumbbell_flyes"),
    ("rope-pushdown", "triceps_pushdown_rope_attachment"),
    ("overhead-cable-extension", "cable_rope_overhead_triceps_extension"),
    ("triceps-kickback", "tricep_dumbbell_kickback"),
    ("bench-dips", "bench_dips"),
    ("ez-bar-curl", "ez_bar_curl"),
    ("dumbbell-curl", "dumbbell_bicep_curl"),
    ("incline-dumbbell-curl", "incline_dumbbell_curl"),
    ("concentration-curl", "concentration_curls"),
    ("preacher-curl", "preacher_curl"),
    ("arnold-press", "arnold_dumbbell_press"),
    ("face-pull", "face_pull"),
    ("rear-delt-fly", "reverse_flyes"),
    ("upright-row", "upright_barbell_row"),
    ("front-raise", "front_dumbbell_raise"),
    ("machine-shoulder-press", "machine_shoulder_military_press"),
    ("chin-up", "chin_up"),
    ("t-bar-row", "t_bar_row_with_handle"),
    ("sumo-deadlift", "sumo_deadlift"),
    ("rack-pull", "rack_pulls"),
    ("straight-arm-pulldown", "straight_arm_pulldown"),
    ("trap-bar-deadlift", "trap_bar_deadlift"),
    ("inverted-row", "inverted_row"),
    ("hack-squat", "hack_squat"),
    ("leg-extension", "leg_extensions"),
    ("lying-leg-curl", "lying_leg_curls"),
    ("seated-leg-curl", "seated_leg_curl"),
    ("hip-thrust", "barbell_hip_thrust"),
    ("step-up", "dumbbell_step_ups"),
    ("barbell-lunge", "barbell_lunge"),
    ("stiff-leg-deadlift", "stiff_legged_barbell_deadlift"),
    ("crunch", "crunches"),
    ("cable-crunch", "cable_crunch"),
    ("ab-wheel-rollout", "ab_roller"),
    ("side-plank", "side_bridge"),
    # Четвёртая волна — ходовые вариации со своим разбором.
    ("dumbbell-shrug", "dumbbell_shrug"),
    ("barbell-shrug", "barbell_shrug"),
    ("hanging-knee-raise", "knee_hip_raise_on_parallel_bars"),
    ("wrist-curl", "seated_palm_up_barbell_wrist_curl"),
    ("reverse-wrist-curl", "seated_palms_down_barbell_wrist_curl"),
    ("cable-lateral-raise", "standing_low_pulley_deltoid_raise"),
    ("cable-curl", "standing_biceps_cable_curl"),
    ("triceps-dips", "dips_triceps_version"),
    ("overhead-dumbbell-extension", "standing_dumbbell_triceps_extension"),
    ("reverse-crunch", "reverse_crunch"),
    ("reverse-pec-deck", "reverse_machine_flyes"),
    ("dumbbell-skull-crusher", "lying_dumbbell_tricep_extension"),
    ("bicycle-crunch", "air_bike"),
    ("close-grip-lat-pulldown", "close_grip_front_lat_pulldown"),
    ("rope-hammer-curl", "cable_hammer_curls_rope_attachment"),
    ("russian-twist", "russian_twist"),
    ("machine-bicep-curl", "machine_bicep_curl"),
    ("smith-machine-bench-press", "smith_machine_bench_press"),
    ("machine-row", "leverage_high_row"),
    ("reverse-curl", "reverse_barbell_curl"),
    ("lying-leg-raise", "flat_bench_lying_leg_raise"),
    ("hammer-strength-chest-press", "leverage_chest_press"),
    ("smith-machine-squat", "smith_machine_squat"),
    ("standing-dumbbell-press", "standing_dumbbell_press"),
    ("ab-crunch-machine", "ab_crunch_machine"),
    ("decline-crunch", "decline_crunch"),
    ("decline-dumbbell-press", "decline_dumbbell_bench_press"),
    ("underhand-lat-pulldown", "underhand_cable_pulldowns"),
    ("seated-barbell-press", "seated_barbell_military_press"),
    ("close-grip-push-up", "push_ups_close_triceps_position"),
    ("pallof-press", "pallof_press"),
    ("incline-dumbbell-fly", "incline_dumbbell_flyes"),
    ("reverse-grip-barbell-row", "reverse_grip_bent_over_rows"),
    ("seated-calf-raise", "seated_calf_raise"),
    ("close-grip-dumbbell-press", "close_grip_dumbbell_press"),
    ("cable-wood-chop", "standing_cable_wood_chop"),
    ("chest-supported-dumbbell-row", "dumbbell_incline_row"),
    ("dumbbell-pullover", "straight_arm_dumbbell_pullover"),
    ("hip-abduction-machine", "thigh_abductor"),
    ("hip-adduction-machine", "thigh_adductor"),
    ("dead-bug", "dead_bug"),
    ("jm-press", "jm_press"),
    ("sit-up", "sit_up"),
    ("bodyweight-glute-bridge", "butt_lift_bridge"),
    ("cable-glute-kickback", "glute_kickback"),
    ("bent-over-dumbbell-row", "bent_over_two_dumbbell_row"),
    ("zottman-curl", "zottman_curl"),
    ("standing-leg-curl", "standing_leg_curl"),
    ("reverse-hyperextension", "reverse_hyperextension"),
    ("kettlebell-swing", "one_arm_kettlebell_swings"),
    ("bodyweight-squat", "bodyweight_squat"),
    ("walking-lunge", "barbell_walking_lunge"),
    ("reverse-lunge", "dumbbell_rear_lunge"),
    ("dumbbell-squat", "dumbbell_squat"),
    ("deficit-deadlift", "deficit_deadlift"),
    ("box-squat", "box_squat"),
    ("floor-press", "floor_press"),
    ("assisted-pull-up", "band_assisted_pull_up"),
    ("dumbbell-stiff-leg-deadlift", "stiff_legged_dumbbell_deadlift"),
    ("weighted-pull-up", "weighted_pull_ups"),
)
_EXERCISE_BY_URL = dict(EXERCISES)
# Упражнение в демо «подход строкой» на главной — по слагу картинок, не по имени.
_DEMO_SLUG = "barbell_bench_press_medium_grip"

IMPORT_SOURCES = ("hevy", "strong", "csv")
TOOLS = ("1rm", "warmup", "rpe", "wilks", "tonnage", "rest-timer", "goal", "strength-percentile", "531", "weekly-volume",
         "protein", "calories")
# Калькуляторы питания — своей группой на странице калькуляторов.
NUTRITION_TOOLS = ("protein", "calories")
EMBEDS = ("1rm",)
# Убранные адреса: 301 туда, где теперь то же самое (калькулятор блинов
# снят — раскладку на сторону показывает разминка).
REMOVED_TOOLS = {"plates": "/tools"}
REMOVED_EMBEDS = {"plates": "1rm"}
COMPETITORS = ("hevy", "strong", "fitbod", "jefit")
# Списки альтернатив: «чем заменить Hevy/Strong» — мы первые, остальные факты.
ALTERNATIVES = ("hevy", "strong", "fitbod", "jefit")
FEATURES = ("ai-coach", "video-review", "telegram", "workout-log-app")
TEMPLATES = ("workout-log",)
# Нормативы: адрес → ключ упражнения в seo_tools.LIFT_SHARE и адрес техники.
STRENGTH_LIFTS = (("bench-press", "bench", "bench-press"), ("squat", "squat", "back-squat"),
                  ("deadlift", "deadlift", "deadlift"), ("total", "total", ""))
STRENGTH_SEXES = (("men", "m"), ("women", "f"))
_LIFT_BY_URL = {url: (key, ex) for url, key, ex in STRENGTH_LIFTS}
_SEX_BY_URL = dict(STRENGTH_SEXES)

# Метка у ссылки в бота — по разделу, не по странице: слаг acquisition режет
# до 32 символов, а ссылка `t.me/<бот>?start=src_seo_<блок>` целиком должна
# влезть в 48 — так же, как у каталогов ботов (GROWTH.md). `*_log` — кнопка
# «запиши этот подход» под результатом калькулятора.
_BLOCKS = {
    "home": "home",
    "ai-coach": "ai",
    "video-review": "video",
    "telegram": "tg",
    "import:hevy": "imp_hevy",
    "import:strong": "imp_strng",
    "import:csv": "imp_csv",
    "tools": "tools",
    "tool:1rm": "rm1",
    "tool:wilks": "wilks",
    "tool:warmup": "warmup",
    "tool:rpe": "rpe",
    "log:1rm": "rm1_log",
    "log:wilks": "wlk_log",
    "log:warmup": "wu_log",
    "log:rpe": "rpe_log",
    "log:strength": "str_log",
    "strength": "str",
    "programs": "progs",
    "program": "prog",
    "exercises": "exs",
    "exercise": "ex",
    "vs:hevy": "vs_hevy",
    "vs:strong": "vs_strng",
    "vs:fitbod": "vs_fitbod",
    "vs:jefit": "vs_jefit",
    "alternatives:hevy": "alt_hevy",
    "alternatives:strong": "alt_strng",
    "alternatives:fitbod": "alt_fbod",
    "alternatives:jefit": "alt_jfit",
    "exercise_alt": "ex_alt",
    "splits": "split",
    "split": "split",
    "workout": "wkt",
    "muscle_equip": "mus_eq",
    "apps": "cmp_apps",
    "tool:531": "t531",
    "log:531": "t531_log",
    "tool:weekly-volume": "vol",
    "log:weekly-volume": "vol_log",
    "workout-log-app": "app",
    "template": "tpl",
    "about": "about",
    "muscles": "mus",
    "muscle": "mus",
    "compare": "cmp",
    "pair": "cmp",
    "glossary": "gls",
    "term": "gls",
    "guides": "guide",
    "guide": "guide",
    "tool:tonnage": "tonnage",
    "tool:rest-timer": "timer",
    "tool:goal": "goal",
    "tool:strength-percentile": "pct",
    "log:tonnage": "ton_log",
    "log:rest-timer": "tmr_log",
    "log:goal": "goal_log",
    "log:strength-percentile": "pct_log",
    "tool:protein": "protein",
    "tool:calories": "kcal",
    "log:protein": "prot_log",
    "log:calories": "kcal_log",
}
# Метка для ссылки из llms.txt: страницы у неё нет.
LLMS_BLOCK = "llms"
_KNOWN_BLOCKS = frozenset(_BLOCKS.values()) | {LLMS_BLOCK}

# Сколько смысловых блоков у статьи-страницы (ключи seo.<id>.s<n>.h / .p_html).
_ARTICLE_SECTIONS = {"ai-coach": 4, "video-review": 4, "telegram": 9, "workout-log-app": 7}

# Вопросы FAQ по страницам (ключи seo.faq.<id>.q / .a_html). Ответы — только
# то, что делает код: числа потолков — из config через _article_params.
FAQ = {
    "home": ("what", "free", "alt_hevy", "move", "video", "platforms"),
    "ai-coach": ("what", "history", "free", "video", "platforms"),
    "import": ("move", "dupes", "undo", "free"),
    "vs:hevy": ("alt_hevy", "move", "free", "video"),
    "vs:strong": ("move", "free", "video", "platforms"),
    "vs:fitbod": ("what", "free", "history", "platforms"),
    "vs:jefit": ("what", "free", "move", "video"),
    "telegram": ("tg_install", "tg_voice", "free", "platforms"),
    "workout-log-app": ("what", "free", "platforms", "move", "tg_install"),
    "template": ("tpl_why_csv", "tpl_lb", "tpl_names", "dupes", "undo"),
}

# Источники для сравнений — живые страницы, по которым сверены факты таблицы.
_VS_SOURCES = {
    "hevy": (
        "https://apps.apple.com/app/id1458862350",
        "https://help.hevyapp.com/hc/en-us/articles/38385724273047-Hevy-Trainer-Explained-How-It-Builds-Your-Workout-Program",
        "https://www.hevyapp.com/features/hevy-gpt/",
        "https://help.hevyapp.com/hc/en-us/articles/38001424401943-How-to-Import-Strong-App-CSV-Files-and-Export-Your-Data-in-Hevy",
    ),
    "strong": (
        "https://apps.apple.com/app/id464254577",
        "https://www.strong.app/",
    ),
    "fitbod": (
        "https://apps.apple.com/us/app/fitbod-gym-fitness-planner/id1041517543",
        "https://help.fitbod.me/hc/en-us/articles/360004904714-How-to-Subscribe-to-Fitbod",
        "https://help.fitbod.me/hc/en-us/articles/30542136101527-How-the-Trial-Works",
        "https://fitbod.me/blog/fitbod-algorithm/",
        "https://help.fitbod.me/hc/en-us/sections/35305345636375-Integrations",
    ),
    "jefit": (
        "https://apps.apple.com/us/app/jefit-workout-plan-gym-tracker/id449810000",
        "https://www.jefit.com/elite",
        "https://www.jefit.com/ai-workout-tracker",
        "https://play.google.com/store/apps/details?id=je.fit",
    ),
}
_VS_ROWS = ("price", "free", "ai", "import", "video")

# Источники методики нормативов и GL — открытые страницы, по которым сверены числа.
STRENGTH_SOURCES = (
    "https://gitlab.com/openpowerlifting/opl-data/-/blob/main/crates/coefficients/src/dots.rs",
    "https://gitlab.com/openpowerlifting/opl-data/-/tree/main/meet-data/usapl",
)
GL_SOURCE = "https://gitlab.com/openpowerlifting/opl-data/-/blob/main/crates/coefficients/src/goodlift.rs"

OG_IMAGE_PATH = "/og/coach.jpg"
_OG_IMAGE_FILE = os.path.join(os.path.dirname(__file__), "media", "push", "coach_incoming_call.jpg")
_APP_ID_RE = re.compile(r"/id(\d+)")


# Адрес страницы без языка: разделы без аргумента и с одним (`/tools/1rm`).
_PATH_FIXED = {
    "tools": "/tools", "strength": "/strength", "programs": "/programs", "exercises": "/exercises",
    "muscles": "/muscles", "compare": "/compare", "glossary": "/glossary", "guides": "/guides", "about": "/about",
    "splits": "/splits",
}
_PATH_PREFIX = {
    "import": "/import/", "tool": "/tools/", "program": "/programs/", "exercise": "/exercises/", "vs": "/vs/",
    "alternatives": "/alternatives/", "muscle": "/muscles/", "pair": "/compare/", "term": "/glossary/",
    "guide": "/guides/", "template": "/templates/", "split": "/splits/", "workout": "/workouts/", "apps": "/compare/",
}
# Метка блока зависит от аргумента (`vs:hevy`), а не только от раздела.
_BLOCK_BY_ARG = ("import", "tool", "vs", "alternatives")


@dataclass(frozen=True)
class Page:
    kind: str
    arg: str = ""
    arg2: str = ""

    @property
    def path(self) -> str:
        """Путь без языка: "" у главной, "/tools/1rm" и т.д."""
        if self.kind == "home":
            return ""
        if self.kind in FEATURES:
            return f"/{self.kind}"
        if self.kind in ("strength_lift", "strength_bw"):
            return f"/strength/{self.arg}/{self.arg2}"
        if self.kind == "exercise_alt":
            return f"/exercises/{self.arg}/alternatives"
        if self.kind == "muscle_equip":
            return f"/muscles/{self.arg}/{self.arg2}"
        if self.kind in _PATH_FIXED:
            return _PATH_FIXED[self.kind]
        if self.kind in _PATH_PREFIX:
            return _PATH_PREFIX[self.kind] + self.arg
        raise ValueError(self.kind)

    @property
    def key(self) -> str:
        """Ключ страницы без языка: `home`, `tools-1rm` — для картинки превью."""
        return self.path.strip("/").replace("/", "-") or "home"

    @property
    def block(self) -> str:
        if self.kind in _BLOCK_BY_ARG:
            return _BLOCKS[f"{self.kind}:{self.arg}"]
        if self.kind in ("strength_lift", "strength_bw"):
            return _BLOCKS["strength"]
        return _BLOCKS[self.kind]


def pages() -> list[Page]:
    result = [Page("home")]
    result += [Page(f) for f in FEATURES]
    result += [Page("import", s) for s in IMPORT_SOURCES]
    result.append(Page("tools"))
    result += [Page("tool", t) for t in TOOLS]
    result.append(Page("strength"))
    result += [Page("strength_lift", url, sex) for url, _k, _e in STRENGTH_LIFTS for sex, _s in STRENGTH_SEXES]
    import seo_strength_bw

    result += seo_strength_bw.pages()
    result.append(Page("programs"))
    result += [Page("program", p["key"]) for p in seed_data.WORKOUT_PROGRAMS]
    result.append(Page("exercises"))
    result += [Page("exercise", url) for url, _slug in EXERCISES]
    result += [Page("vs", c) for c in COMPETITORS]
    result += [Page("alternatives", c) for c in ALTERNATIVES]
    result += [Page("template", t) for t in TEMPLATES]
    result.append(Page("about"))
    import seo_library
    import seo_plans
    import seo_swaps

    result += seo_library.pages()
    result += seo_swaps.pages()
    result += seo_plans.pages()
    return result


# Что сайт отдаёт в индекс: только ru/en. Машинные переводы (25.10) совпали
# день в день с обвалом показов — они открыты людям и ссылкам (noindex,follow),
# но не просятся в поиск и не лежат в sitemap.
#
# «Узкие» страницы закрывать нельзя вслепую: по выгрузке GSC за 16–24.10
# «чем заменить» (exercise_alt) дали 39 из 61 клика и ~5.2k показов, пары и
# «мышца × снаряд» — ещё ~1.5k. Закрыт только вес-тела-норматив (strength_bw):
# ни одного показа.
INDEXED_LANGS = ("ru", "en")
THIN_KINDS = frozenset({"strength_bw"})


def indexable(page: Page, lang: str) -> bool:
    return lang in INDEXED_LANGS and page.kind not in THIN_KINDS


def url_for(page: Page, lang: str) -> str:
    return f"/{lang_segment(lang)}{page.path}"


def og_url(page: Page, lang: str) -> str:
    """Картинка превью страницы (og:image): `/og/<сегмент языка>/<ключ>.png`."""
    return f"/og/{lang_segment(lang)}/{page.key}.png"


def embed_url(tool: str, lang: str) -> str:
    return f"/embed/{lang_segment(lang)}/{tool}"


def template_file_url(name: str, lang: str) -> str:
    return f"/{lang_segment(lang)}/templates/{name}"


# ---------- ссылки наружу ----------


def bot_username() -> str:
    # Живое имя бота (его узнаёт sharing.get_bot_username у Telegram) главнее
    # настройки — та нужна, пока бот ещё ни разу не спросил себя.
    from handlers import sharing

    return sharing._bot_username or config.BOT_USERNAME


def bot_payload(block: str) -> str:
    return f"{BOT_TAG_PREFIX}{block}"


def bot_link(block: str) -> str:
    return f"https://t.me/{bot_username()}?start={bot_payload(block)}"


def go_url(target: str, block: str) -> str:
    """Кнопка на странице: через /go/*, чтобы клик был виден в /growth."""
    return f"/go/{target}?f={block}"


def app_store_link(block: str) -> Optional[str]:
    """Ссылка в App Store с кампанией `ct=site_<блок>` (App Store Connect →
    Аналитика → Кампании). `pt` и `mt` из настройки сохраняются как есть;
    `ct` у Apple — до 40 символов, метки блоков короче."""
    url = config.app_store_url()
    if not url:
        return None
    parts = urlsplit(url)
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if k != "ct"]
    query.append(("ct", f"site_{block}"[:40]))
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))


def app_store_id() -> Optional[str]:
    url = config.app_store_url()
    match = _APP_ID_RE.search(url or "")
    return match.group(1) if match else None


def base_url(request: Optional[Request]) -> str:
    """Начало абсолютных адресов (canonical, hreflang, sitemap, robots).

    Свой домен сайта (`config.site_base_url`) главнее адреса сервера:
    страницы отвечают на любом хосте, но поисковик должен склеить их с
    одним. Без настроек — адрес сервера, без него — хост запроса."""
    base = config.site_base_url()
    if base:
        return base
    if request is not None:
        return str(request.base_url).rstrip("/")
    return ""


def _own_hosts(request: Optional[Request]) -> tuple[str, ...]:
    hosts = {urlsplit(base_url(request)).hostname or ""}
    if request is not None:
        hosts.add((request.url.hostname or "").lower())
    return tuple(h.removeprefix("www.") for h in hosts if h)


def device_of(request: Optional[Request]) -> str:
    if request is None:
        return "bot"
    return site_stats.classify_ua(request.headers.get("user-agent"))[0]


# ---------- разметка: общие куски ----------


def _t(lang: str) -> Callable[..., str]:
    return lambda key, **kw: i18n.t_in(lang, key, **kw)


def _e(text: str) -> str:
    return escape(text, quote=True)


def _strip_emoji(text: str) -> str:
    """«🌿 Всё тело — 2 дня» → «Всё тело — 2 дня» для <title>."""
    return re.sub(r"^[^\w(]+", "", text).strip()


def _js_str(text: str) -> str:
    """Строка каталога → JS-литерал; `</` не закрывает <script> раньше времени."""
    return json.dumps(text, ensure_ascii=False).replace("</", "<\\/")


def _json_ld(data: Any) -> str:
    raw = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    return '<script type="application/ld+json">' + raw.replace("</", "<\\/") + "</script>"


def _num(value: float) -> str:
    return f"{value:g}"


def _w(value: float, unit_label: str) -> str:
    """Вес: число и единица через неразрывный пробел (TONE_OF_VOICE, сайт).
    Пробел внутри span единицы — в пиксельном шрифте он уменьшается вместе с ней."""
    return f"{_num(round(value, 1))}<span class=u>\u00a0{_e(unit_label)}</span>"


def _primary_store(lang: str, device: str) -> bool:
    """Главная кнопка — App Store? Везде, кроме Android: приложение для
    iPhone — главный продукт, а на Android его нет. Язык не влияет —
    параметр оставлен, чтобы политику можно было развести по языкам в
    одном месте."""
    return device != "android"


def _cta_buttons(lang: str, block: str, device: str, *, bot_first: bool = False) -> str:
    """Главная кнопка и запасной путь. Запасной — всегда строка помельче
    под кнопкой (`alt-link`), а не вторая кнопка того же размера: выбор
    между двумя одинаковыми кнопками — это отсутствие главной."""
    t = _t(lang)
    bot = (f'<a class="btn primary" href="{_e(go_url("bot", block))}">{seo_design.icon("telegram")}'
           f'{_e(t("seo.cta.bot"))}</a>')
    if not config.app_store_url():
        return f"<div class=btn-row>{bot}</div>"
    store = (f'<a class="btn primary" href="{_e(go_url("appstore", block))}">{seo_design.icon("apple")}'
             f'{_e(t("seo.cta.app_store"))}</a>')
    if bot_first or not _primary_store(lang, device):
        return (f"<div class=btn-row>{bot}</div>"
                f'<p class=alt-link>{t("seo.cta.or_store_html", href=_e(go_url("appstore", block)))}</p>')
    return (f"<div class=btn-row>{store}</div>"
            f'<p class=alt-link>{t("seo.cta.or_bot_html", href=_e(go_url("bot", block)))}</p>')


def diary_target(lang: str, device: str) -> str:
    """Куда ведёт кнопка «запиши в дневник»: туда же, куда главная кнопка
    страницы на этом устройстве (iPhone — App Store, Android — бот)."""
    return "appstore" if config.app_store_url() and _primary_store(lang, device) else "bot"


def _qr(lang: str, block: str, request: Optional[Request]) -> str:
    t = _t(lang)
    target = base_url(request) + go_url("appstore", block)
    return (f'<div class=qr role=img aria-label="{_e(t("seo.cta.qr_alt"))}">{qr_code.svg(target)}'
            f"<small>{_e(t('seo.cta.qr'))}</small></div>")


def _cta(lang: str, block: str, text: str, request: Optional[Request], *, bot_first: bool = False) -> str:
    device = device_of(request)
    show_qr = device == "desktop" and bool(config.app_store_url())
    body = ((_qr(lang, block, request) if show_qr else "")
            + f"<div>{_cta_buttons(lang, block, device, bot_first=bot_first)}</div>")
    return (f'<div class="cta reveal"><p class=cta-text>{text}</p>'
            f'<div class="cta-body{" has-qr" if show_qr else ""}">{body}</div></div>')


def _head_cta(lang: str, block: str, device: str) -> str:
    t = _t(lang)
    if config.app_store_url() and _primary_store(lang, device):
        return (f'<a class="btn small primary head-cta" href="{_e(go_url("appstore", block))}">'
                f'{_e(t("seo.cta.head_store"))}</a>')
    return f'<a class="btn small primary head-cta bot" href="{_e(go_url("bot", block))}">{_e(t("seo.cta.head_bot"))}</a>'


def _nav_pages() -> tuple[tuple[Page, str], ...]:
    """Шапка — шесть разделов-хабов; всё остальное — внутри них и в подвале."""
    return (
        (Page("guides"), "seo.nav.guides"),
        (Page("exercises"), "seo.nav.exercises"),
        (Page("muscles"), "seo.nav.muscles"),
        (Page("programs"), "seo.nav.programs"),
        (Page("compare"), "seo.nav.compare"),
        (Page("tools"), "seo.nav.tools"),
    )


# Раздел шапки, к которому относится страница (подсветка и хлебные крошки).
_SECTION = {
    "tools": "tools", "tool": "tools", "strength": "tools", "strength_lift": "tools", "strength_bw": "tools", "template": "tools",
    "guides": "guides", "guide": "guides", "glossary": "guides", "term": "guides",
    "muscles": "muscles", "muscle": "muscles", "muscle_equip": "muscles",
    "exercises": "exercises", "exercise": "exercises", "exercise_alt": "exercises",
    "programs": "programs", "program": "programs", "splits": "programs", "split": "programs", "workout": "programs",
    "compare": "compare", "pair": "compare", "vs": "compare", "alternatives": "compare", "apps": "compare",
}


def section_of(page: Page) -> str:
    return _SECTION.get(page.kind, "")


def _nav_links(lang: str, current: Page) -> str:
    t = _t(lang)
    section = section_of(current)
    links = []
    for page, key in _nav_pages():
        here = " aria-current=page" if page.kind == section else ""
        links.append(f'<a href="{url_for(page, lang)}"{here}>{_e(t(key))}</a>')
    return "".join(links)


def _lang_links(lang: str, page: Page) -> str:
    """Та же страница на каждом языке сайта — пункты переключателя языка.
    Подписи — автонимы, у ссылки hreflang и lang своего языка; текущий язык
    отмечен aria-current."""
    items = []
    for code in LANGS:
        here = " aria-current=true" if code == lang else ""
        items.append(f'<li><a href="{url_for(page, code)}" hreflang={hreflang(code)} lang={hreflang(code)}{here}>'
                     f"{_e(AUTONYMS[code])}</a></li>")
    return "<ul>" + "".join(items) + "</ul>"


def _lang_switcher(lang: str, page: Page) -> str:
    """Переключатель в шапке: `<details>` без скрипта, на кнопке — код языка."""
    label = _e(i18n.t_in(lang, "screen.language.button"))
    return (f'<details class=langs><summary class=lang><span class=vh>{label}: {_e(AUTONYMS[lang])}</span>'
            f'<span aria-hidden=true>{_e(hreflang(lang).upper())}</span></summary>'
            f'<nav class=langs-panel aria-label="{label}">{_lang_links(lang, page)}</nav></details>')


def _parents(page: Page) -> list[Page]:
    k = page.kind
    if k in ("tool", "strength", "template"):
        return [Page("tools")]
    if k == "strength_lift":
        return [Page("tools"), Page("strength")]
    if k == "strength_bw":
        import seo_strength_bw

        _key, sex, _bw = seo_strength_bw.parts(page)
        return [Page("tools"), Page("strength"), Page("strength_lift", page.arg, "men" if sex == "m" else "women")]
    if k == "exercise_alt":
        return [Page("exercises"), Page("exercise", page.arg)]
    if k == "muscle_equip":
        return [Page("muscles"), Page("muscle", page.arg)]
    if k in ("split", "workout"):
        return [Page("programs"), Page("splits")]
    single = {"program": "programs", "exercise": "exercises", "muscle": "muscles", "pair": "compare",
              "vs": "compare", "alternatives": "compare", "term": "glossary", "guide": "guides", "splits": "programs",
              "apps": "compare"}
    return [Page(single[k])] if k in single else []


def _crumb_label(lang: str, page: Page) -> str:
    for p, key in _nav_pages():
        if p == page:
            return _t(lang)(key)
    if page.kind == "import":
        # Заголовок переезда — обещание целой фразой; в крошках — короткое имя.
        return _t(lang)(f"seo.import.{page.arg}.short")
    return _strip_emoji(page_h1(page, lang))


def _crumbs(lang: str, page: Page) -> list[tuple[str, Page]]:
    """Хлебные крошки: (подпись, страница) от главной до текущей."""
    t = _t(lang)
    home = (t("seo.crumb.home"), Page("home"))
    if page.kind == "home":
        return [home]
    return [home] + [(_crumb_label(lang, p), p) for p in _parents(page)] + [(_strip_emoji(page_h1(page, lang)), page)]


def _crumbs_html(lang: str, page: Page) -> str:
    trail = _crumbs(lang, page)
    if len(trail) < 2:
        return ""
    t = _t(lang)
    items = []
    for i, (name, p) in enumerate(trail):
        if i == len(trail) - 1:
            items.append(f"<li aria-current=page>{_e(name)}</li>")
        else:
            items.append(f'<li><a href="{url_for(p, lang)}">{_e(name)}</a></li>')
    return f'<nav aria-label="{_e(t("seo.crumb.label"))}"><ol class=crumbs>{"".join(items)}</ol></nav>'


def _breadcrumb_ld(lang: str, page: Page, base: str) -> Optional[dict]:
    trail = _crumbs(lang, page)
    if len(trail) < 2:
        return None
    return {
        "@context": "https://schema.org", "@type": "BreadcrumbList",
        "itemListElement": [
            {"@type": "ListItem", "position": i + 1, "name": name, "item": base + url_for(p, lang)}
            for i, (name, p) in enumerate(trail)
        ],
    }


# Дата последней заметной правки страницы — константой, а не временем файла:
# одна и та же страница отдаёт одну и ту же дату (тесты, ETag, sitemap).
SITE_UPDATED = "2026-10-30"


def page_updated(page: Page) -> str:
    import seo_library

    return seo_library.updated_for(page) or SITE_UPDATED


def human_date(iso: str, lang: str) -> str:
    """«16 октября 2026» / «October 16, 2026» — месяцы из каталога."""
    y, m, d = (int(x) for x in iso.split("-"))
    return i18n.t_in(lang, "seo.date", day=d, month=i18n.t_in(lang, f"seo.month.{m}"), year=y)


def _footer(lang: str, page: Page, request: Optional[Request]) -> str:
    t = _t(lang)

    def links(items: list[tuple[Page, str]]) -> str:
        return "<ul>" + "".join(f'<li><a href="{url_for(p, lang)}">{_e(label)}</a></li>' for p, label in items) + "</ul>"

    product = [(Page(f), _strip_emoji(t(f"seo.{f}.card.title"))) for f in FEATURES]
    product.append((Page("about"), t("seo.about.short")))
    tools = [(Page("tool", s), t(f"seo.tool.{s}.short")) for s in TOOLS] + [(Page("strength"), t("seo.strength.short"))]
    library = [(Page("guides"), t("seo.nav.guides")), (Page("glossary"), t("seo.glossary.short")),
               (Page("exercises"), t("seo.exercises.h1")), (Page("muscles"), t("seo.nav.muscles")),
               (Page("programs"), t("seo.programs.h1")), (Page("splits"), t("seo.splits.h1"))]
    compare = [(Page("compare"), t("seo.compare.short"))] + [(Page("vs", c), t(f"seo.vs.{c}.short")) for c in COMPETITORS]
    compare += [(Page("alternatives", c), t(f"seo.alt.{c}.short")) for c in ALTERNATIVES]
    moving = [(Page("import", s), t(f"seo.import.{s}.short")) for s in IMPORT_SOURCES]
    moving += [(Page("template", s), t(f"seo.template.{s}.short")) for s in TEMPLATES]
    return (
        '<footer class=site-foot><div class=wrap><div class=foot-grid>'
        f'<div class=foot-about><a class=brand href="{url_for(Page("home"), lang)}">'
        f'<img src="{seo_static.asset_url("icons/favicon-32.png")}" alt="" width=30 height=30><span>{_e(t("bot.name"))}</span></a>'
        f"<p>{_e(t('seo.footer.about'))}</p></div>"
        f"<div><h2 class=foot-h>{_e(t('seo.footer.product'))}</h2>{links(product)}</div>"
        f"<div><h2 class=foot-h>{_e(t('seo.footer.tools'))}</h2>{links(tools)}</div>"
        f"<div><h2 class=foot-h>{_e(t('seo.footer.library'))}</h2>{links(library)}</div>"
        f"<div><h2 class=foot-h>{_e(t('seo.footer.compare'))}</h2>{links(compare)}</div>"
        f"<div><h2 class=foot-h>{_e(t('seo.footer.moving'))}</h2>{links(moving)}</div>"
        "</div><div class=foot-bottom>"
        f'<nav class=foot-langs aria-label="{_e(t("screen.language.button"))}">{_lang_links(lang, page)}</nav>'
        f'<span>{_e(t("seo.brand"))} · <time datetime="{page_updated(page)}">{_e(t("seo.updated", date=human_date(page_updated(page), lang)))}</time></span>'
        f'<span><a href="/privacy?lang={lang}">{_e(t("seo.footer.privacy"))}</a> · '
        f'<a href="/terms?lang={lang}">{_e(t("seo.footer.terms"))}</a> · '
        f'<a href="/support?lang={lang}">{_e(t("seo.footer.support"))}</a></span>'
        "</div></div></footer>"
    )


def _css() -> str:
    return (seo_design.CSS.replace("__PIXEL2__", seo_static.asset_url("fonts/pixel.woff2"))
            .replace("__PIXEL__", seo_static.asset_url("fonts/pixel.woff")))


def _layout(lang: str, page: Page, request: Optional[Request], *, title: str, description: str,
            body: str, ld: Optional[list[dict]] = None, wide: bool = False) -> str:
    t = _t(lang)
    base = base_url(request)
    canonical = base + url_for(page, lang)
    device = device_of(request)
    head = [
        f"<!doctype html><html lang={hreflang(lang)}><head><meta charset=utf-8>",
        '<meta name=viewport content="width=device-width, initial-scale=1, viewport-fit=cover">',
        f"<title>{_e(title)}</title>",
        f'<meta name=description content="{_e(description)}">',
        f'<link rel=canonical href="{_e(canonical)}">',
    ]
    if not indexable(page, lang):
        head.append("<meta name=robots content=\"noindex,follow\">")
    if page.kind not in THIN_KINDS:
        # hreflang связывает только версии, которые идут в индекс.
        for code in INDEXED_LANGS:
            head.append(f'<link rel=alternate hreflang={hreflang(code)} href="{_e(base + url_for(page, code))}">')
        head.append(f'<link rel=alternate hreflang=x-default href="{_e(base + url_for(page, X_DEFAULT_LANG))}">')
    image = f"{base}{og_url(page, lang)}"
    head += [
        '<meta property="og:type" content="website">',
        f'<meta property="og:site_name" content="{_e(t("seo.brand"))}">',
        f'<meta property="og:title" content="{_e(title)}">',
        f'<meta property="og:description" content="{_e(description)}">',
        f'<meta property="og:url" content="{_e(canonical)}">',
        f'<meta property="og:locale" content="{OG_LOCALES[lang]}">',
        f'<meta property="og:image" content="{_e(image)}">',
        '<meta property="og:image:width" content="1200">',
        '<meta property="og:image:height" content="630">',
        '<meta name="twitter:card" content="summary_large_image">',
        f'<meta name="theme-color" content="{seo_static.THEME_COLOR}">',
        '<link rel=icon href="/favicon.ico" sizes="48x48">',
        f'<link rel=icon type="image/png" sizes="32x32" href="{seo_static.asset_url("icons/favicon-32.png")}">',
        '<link rel=apple-touch-icon href="/apple-touch-icon.png">',
        f'<link rel=manifest href="/site.webmanifest?lang={lang_segment(lang)}">',
        f'<link rel=preload href="{seo_static.asset_url("fonts/pixel.woff2")}" as=font type="font/woff2" crossorigin>',
    ]
    app_id = app_store_id()
    if app_id:
        head.append(f'<meta name="apple-itunes-app" content="app-id={app_id}">')
    verification = config.GOOGLE_SITE_VERIFICATION
    if verification:
        head.append(f'<meta name="google-site-verification" content="{_e(verification)}">')
    head.append(f"<style>{_css()}</style>")
    crumbs = _breadcrumb_ld(lang, page, base)
    updated = page_updated(page)
    for block in list(ld or []):
        # Дата правки — у всех «творческих» блоков (WebPage, Article, план…),
        # кроме списка вопросов и крошек: там её не бывает.
        if block.get("@type") not in ("BreadcrumbList", "FAQPage", "Organization", "WebSite", "DefinedTermSet"):
            block.setdefault("dateModified", updated)
    for block in ([crumbs] if crumbs else []) + list(ld or []):
        head.append(_json_ld(block))
    head.append("</head>")
    header = (
        f'<a class=skip href="#main">{_e(t("seo.skip"))}</a>'
        '<header class=site-head><div class=wrap><div class=head-row>'
        f'<a class=brand href="{url_for(Page("home"), lang)}"><img src="{seo_static.asset_url("icons/favicon-32.png")}" alt="" width=30 height=30>'
        f"<span>{_e(t('bot.name'))}</span></a>"
        f"<nav class=head-nav aria-label=\"{_e(t('seo.nav.label'))}\">{_nav_links(lang, page)}</nav>"
        f"<div class=head-end>{_lang_switcher(lang, page)}"
        f"{_head_cta(lang, page.block, device)}"
        # На телефоне разделы — за кнопкой меню, а не второй строкой шапки:
        # панель ложится поверх страницы (без сдвига вёрстки), тап мимо неё
        # или Esc её закрывают.
        f'<details class=menu><summary class=menu-btn><span class=vh>{_e(t("seo.nav.menu"))}</span>'
        f'{seo_design.icon("menu")}{seo_design.icon("close")}</summary>'
        f"<nav class=menu-panel aria-label=\"{_e(t('seo.nav.label'))}\"><div class=wrap>{_nav_links(lang, page)}</div></nav>"
        "</details></div></div></div></header>"
    )
    if page.kind == "home":
        inner = body
    elif wide:
        inner = _crumbs_html(lang, page) + body
    else:
        inner = f"<div class=prose>{_crumbs_html(lang, page)}{body}</div>"
    cls = "" if page.kind == "home" else " class=page"
    return ("".join(head) + f"<body{cls}>{header}<main id=main><div class=wrap>{inner}</div></main>"
            f"{_footer(lang, page, request)}<script>{_PAGE_JS}</script>"
            f"{site_stats.beacon_script(url_for(page, lang))}</body></html>")


# Мелочи всей страницы: меню разделов на телефоне и список языков
# закрываются по Esc, меню — по тапу на ссылку, языки — по тапу мимо списка
# (без скрипта оба работают как обычный <details>); запятая в числовых полях —
# точка (на русской клавиатуре запятая).
_PAGE_JS = (
    "(function(){var ds=document.querySelectorAll('details.menu,details.langs');"
    "document.addEventListener('keydown',function(e){if(e.key!=='Escape')return;"
    "ds.forEach(function(d){if(d.open){d.open=false;d.querySelector('summary').focus();}});});"
    "document.addEventListener('click',function(e){ds.forEach(function(d){"
    "if(d.open&&d.classList.contains('langs')&&!d.contains(e.target))d.open=false;});});"
    "document.querySelectorAll('details.menu .menu-panel a').forEach(function(a){"
    "a.addEventListener('click',function(){a.closest('details').open=false;});});"
    "document.querySelectorAll('input[inputmode=decimal],input[inputmode=numeric]').forEach(function(el){"
    "el.addEventListener('input',function(){if(el.value.indexOf(',')>=0)el.value=el.value.replace(/,/g,'.');},true);});})();"
)


def grid_class(count: int) -> str:
    """Колонки сетки по числу карточек: две — пополам, кратно трём — по три."""
    if count in (2, 4):
        return "grid g2"
    if count % 3 == 0 or count >= 5:
        return "grid g3"
    return "grid"


def card(href: str, title: str, meta: str, icon: Optional[str] = None, badge: Optional[str] = None,
         extra: str = "") -> str:
    """Одна карточка на весь сайт: значок (если есть), заголовок, подпись,
    стрелка; бейдж — уровень программы и подобное."""
    ic = f"<span class=ic>{seo_design.icon(icon)}</span>" if icon else ""
    badge_html = f"<em class=badge>{_e(badge)}</em>" if badge else ""
    return (f'<a class="card reveal" href="{href}">{ic}<span class=arrow>{seo_design.icon("arrow")}</span>'
            f"{badge_html}<b>{_e(_strip_emoji(title))}</b><span>{_e(meta)}</span>{extra}</a>")


def _cards(items: list[tuple[str, str, str]]) -> str:
    """(href, заголовок, подпись) → сетка карточек."""
    return f"<div class='{grid_class(len(items))}'>" + "".join(card(h, title, text) for h, title, text in items) + "</div>"


def _icon_cards(items: list[tuple[str, str, str, str]]) -> str:
    """(href, значок, заголовок, подпись) → карточки со своим значком у каждой."""
    return f"<div class='{grid_class(len(items))}'>" + "".join(card(h, title, text, ic) for h, ic, title, text in items) + "</div>"


def program_card(key: str, lang: str) -> str:
    """Карточка программы: бейдж уровня, «N дней», остальная подпись."""
    meta = seed_data.localized_program_meta(key, lang)
    badge, _sep, rest = meta.partition(" · ")
    days = len(seed_data.PROGRAM_BY_KEY[key]["days"])
    line = _t(lang)("seo.program.card_days", n=days) + (f" · {rest}" if rest else "")
    return card(url_for(Page("program", key), lang), seed_data.localized_program_name(key, lang), line, badge=badge)


def program_cards(keys: list[str], lang: str) -> str:
    return f"<div class='{grid_class(len(keys))}'>" + "".join(program_card(k, lang) for k in keys) + "</div>"


def _article_params(lang: str) -> dict[str, Any]:
    t = _t(lang)
    return {
        "ai": _e(_strip_emoji(t("btn.ai_coach"))),
        "video": _e(_strip_emoji(t("ai.screen.preset.video_hint.label"))),
        "programs": _e(_strip_emoji(t("btn.programs"))),
        "ready": _e(_strip_emoji(t("btn.ready_programs"))),
        "settings": _e(_strip_emoji(t("btn.settings"))),
        "import_btn": _e(_strip_emoji(t("btn.import_csv"))),
        "seconds": config.MAX_VIDEO_SECONDS,
        "videos": config.AI_VIDEO_DAILY_LIMIT,
        "questions": config.AI_QUESTION_DAILY_LIMIT,
        "days": config.IMPORT_UNDO_DAYS,
        "import_history": _e(_strip_emoji(t("btn.import_history"))),
        "upload": _e(_strip_emoji(t("btn.upload_all"))),
        "add": _e(_strip_emoji(t("btn.add_for_myself_plus"))),
    }


def _faq_items(lang: str, ids: tuple[str, ...]) -> list[tuple[str, str]]:
    t = _t(lang)
    params = _article_params(lang)
    params["hevy_href"] = url_for(Page("import", "hevy"), lang)
    params["strong_href"] = url_for(Page("import", "strong"), lang)
    params["video_href"] = url_for(Page("video-review"), lang)
    params["vs_href"] = url_for(Page("vs", "hevy"), lang)
    return [(t(f"seo.faq.{i}.q"), t(f"seo.faq.{i}.a_html", **params)) for i in ids]


def _faq_html(lang: str, items: list[tuple[str, str]]) -> str:
    t = _t(lang)
    return (f'<section class=reveal><span class=eyebrow>{_e(t("seo.faq.eyebrow"))}</span><h2>{_e(t("seo.faq.h"))}</h2><div class=faq>'
            + "".join(f"<details><summary>{_e(q)}</summary><div class=a><p>{a}</p></div></details>" for q, a in items)
            + "</div></section>")


def _plain(html_text: str) -> str:
    """Текст без тегов для JSON-LD и llms.txt."""
    from html import unescape

    return unescape(re.sub(r"<[^>]+>", "", html_text))


def _faq_ld(items: list[tuple[str, str]]) -> dict:
    return {
        "@context": "https://schema.org", "@type": "FAQPage",
        "mainEntity": [
            {"@type": "Question", "name": q, "acceptedAnswer": {"@type": "Answer", "text": _plain(a)}}
            for q, a in items
        ],
    }


def _webpage_ld(lang: str, page: Page, request, title: str, description: str, kind: str = "WebPage",
                extra: Optional[dict] = None) -> dict:
    base = base_url(request)
    data = {
        "@context": "https://schema.org", "@type": kind, "name": title, "description": description,
        "url": base + url_for(page, lang), "inLanguage": hreflang(lang),
        "isPartOf": {"@type": "WebSite", "name": i18n.t_in(lang, "seo.brand"), "url": base + url_for(Page("home"), lang)},
    }
    data.update(extra or {})
    return data


# ---------- заголовки страниц (h1 — он же подпись на картинке превью) ----------


def page_h1(page: Page, lang: str) -> str:
    t = _t(lang)
    if page.kind == "home":
        return t("seo.home.h1")
    if page.kind in FEATURES:
        return t(f"seo.{page.kind}.h1")
    if page.kind == "import":
        return t(f"seo.import.{page.arg}.h1")
    if page.kind == "tools":
        return t("seo.tools.h1")
    if page.kind == "tool":
        return t(f"seo.tool.{page.arg}.h1")
    if page.kind == "strength":
        return t("seo.strength.h1")
    if page.kind == "strength_lift":
        key, _ex = _LIFT_BY_URL[page.arg]
        return t(f"seo.strength.{key}.{page.arg2}.h1")
    if page.kind == "programs":
        return t("seo.programs.h1")
    if page.kind == "program":
        return seed_data.localized_program_name(page.arg, lang)
    if page.kind == "exercises":
        return t("seo.exercises.h1")
    if page.kind == "exercise":
        return seed_data.localized_exercise_name(_identity_for_media_slug(_EXERCISE_BY_URL[page.arg]), lang)
    if page.kind == "vs":
        return t(f"seo.vs.{page.arg}.h1")
    if page.kind == "strength_bw":
        import seo_strength_bw

        return seo_strength_bw.page_h1(page, lang)
    import seo_library

    return seo_library.page_h1(page, lang)


# ---------- главная ----------


def screen_picture(lang: str, name: str, alt: str, *, eager: bool = False) -> str:
    """Снимок приложения: AVIF/WebP 720px, если есть (scripts/gen_site_screens.py),
    иначе JPEG 540px. Своих снимков у языка нет — английские."""
    lang = seo_static.screens_lang(lang)
    w, h = next((sw, sh) for screen, sw, sh in seo_static.SCREENS[lang] if screen == name)
    jpg = seo_static.asset_url(f"screens/{lang}-{name}.jpg")
    attrs = "fetchpriority=high" if eager else "loading=lazy"
    img = f'<img src="{jpg}" alt="{_e(alt)}" width={w} height={h} {attrs} decoding=async>'
    if not seo_static.site_file(f"screens/{lang}-{name}-720.webp"):
        return img
    sources = "".join(f'<source type="image/{fmt}" srcset="{seo_static.asset_url(f"screens/{lang}-{name}-720.{fmt}")} 720w, {jpg} 540w" '
                      'sizes="(max-width: 600px) 64vw, 252px">' for fmt in ("avif", "webp"))
    return f"<picture>{sources}{img}</picture>"


def coach_picture(alt: str) -> str:
    src = seo_static.asset_url("coach.jpg")
    return (f'<picture><source type="image/avif" srcset="{seo_static.asset_url("coach.avif")}">'
            f'<source type="image/webp" srcset="{seo_static.asset_url("coach.webp")}">'
            f'<img src="{src}" alt="{_e(alt)}" width=720 height=720 fetchpriority=high decoding=async></picture>')


# Демо «живого» подхода в шапке главной: прошлые подходы этого упражнения —
# (вес, повторы); рекорд считается от лучшего из них той же формулой Эпли,
# что у бота (analytics.e1rm).
LIVE_HISTORY = ((80.0, 8), (90.0, 6))


def _live(lang: str, block: str, device: str) -> str:
    t = _t(lang)
    name = seed_data.localized_exercise_name(_identity_for_media_slug(_DEMO_SLUG), lang)
    unit = _e(t("seo.tool.unit_kg"))
    rows = "".join(f"<li><span>{_num(w)}\u00a0<span class=u>{unit}</span> × {r}</span>{seo_design.icon('check')}</li>"
                   for w, r in LIVE_HISTORY)
    best = max(seo_tools.e1rm(w, r) for w, r in LIVE_HISTORY)
    target = diary_target(lang, device)
    script = (
        "(function(){var f=document.getElementById('live');if(!f)return;var inp=f.querySelector('input'),list=f.querySelector('ol'),"
        f"out=f.querySelector('.live-out'),U={_js_str(t('seo.tool.unit_kg'))},T={_js_str(t('seo.tool.unit_t'))},"
        f"BEST={best!r},HIST={json.dumps([list(x) for x in LIVE_HISTORY])},MAXR={config.E1RM_MAX_REPS},"
        f"REC={_js_str(t('seo.home.live.record', e1rm='{e1rm}'))},OK={_js_str(t('seo.home.live.logged', gap='{gap}'))},"
        f"HUH={_js_str(t('seo.home.live.huh'))},TON={_js_str(t('seo.home.live.tonnage', tonnage='{tonnage}'))},"
        f"E1={_js_str(t('seo.home.live.e1rm', e1rm='{e1rm}'))};"
        "function f1(x){var r=Math.round(x*10)/10;return r%1===0?r.toFixed(0):r.toFixed(1);}"
        "function kg(x){return f1(x)+'\u00a0'+U;}"
        "function e1(w,r){return r<=1?w:w*(1+r/30);}"
        "f.addEventListener('submit',function(ev){ev.preventDefault();"
        "var m=inp.value.replace(',','.').match(/^\\s*(\\d+(?:\\.\\d+)?)\\s*(?:[x×\\u0445*]|\\s)\\s*(\\d{1,3})\\s*$/i);"
        "if(!m){out.hidden=false;out.innerHTML='<p>'+HUH+'</p>';return;}"
        "var w=parseFloat(m[1]),r=parseInt(m[2],10);if(!(w>0)||!(r>0)||w>1000||r>100){out.hidden=false;out.innerHTML='<p>'+HUH+'</p>';return;}"
        "HIST.push([w,r]);var li=document.createElement('li');li.className='new';li.innerHTML='<span>'+f1(w)+'\u00a0<span class=u>'+U+'</span> × '+r+'</span>';"
        "list.appendChild(li);var ton=HIST.reduce(function(a,s){return a+s[0]*s[1];},0),e=r>MAXR?0:e1(w,r),line;"
        "if(e>BEST+1e-9){line=REC.replace('{e1rm}',kg(e));BEST=e;li.classList.add('rec');}else{line=OK.replace('{gap}',kg(Math.max(0,BEST-e)));}"
        "out.hidden=false;out.innerHTML='<p class=coach-line>'+line+'</p><p class=nums><span>'+TON.replace('{tonnage}',ton>=1000?f1(ton/1000)+T:kg(ton))"
        "+'</span><span>'+E1.replace('{e1rm}',kg(BEST))+'</span></p>';out.appendChild(document.getElementById('live-cta').content.cloneNode(true));"
        "inp.value='';inp.placeholder='100 8';});})();"
    )
    cta = (f'<template id=live-cta><a class="btn primary small" href="{_e(go_url(target, block))}">'
           f'{seo_design.icon("check")}{_e(t("seo.home.live.cta"))}</a></template>')
    return (
        f'<form class=live id=live aria-label="{_e(t("seo.home.live.label"))}"><div class=live-ex>{_e(name)}'
        f'<small>{_e(t("seo.home.live.hint"))}</small></div><ol>{rows}</ol>'
        f'<div class=input><input type=text inputmode=decimal enterkeyhint=send autocomplete=off value="100 8" '
        f'aria-label="{_e(t("seo.home.live.input"))}">'
        f'<button class=send type=submit aria-label="{_e(t("seo.home.live.send"))}">{seo_design.icon("send")}</button></div>'
        f"<div class=live-out aria-live=polite hidden></div>{cta}</form><script>{script}</script>"
    )


def _hero(lang: str, page: Page, request) -> str:
    t = _t(lang)
    device = device_of(request)
    return (
        "<div class=hero>"
        f'<div class=hero-text><span class=eyebrow>{_e(t("seo.home.eyebrow"))}</span>'
        f"<h1>{_e(t('seo.home.h1'))}</h1><p class=lead>{t('seo.home.lead_html')}</p>"
        + _live(lang, page.block, device)
        + f"<div class=hero-cta>{_cta_buttons(lang, page.block, device)}</div>"
        "<ul class=trust>"
        + "".join(f"<li>{seo_design.icon('check')}{_e(t(f'seo.home.trust.{k}'))}</li>" for k in ("free", "platforms", "import"))
        + "</ul></div>"
        '<div class="stage hero-in"><div class=glow></div>'
        f'<div class=coach>{coach_picture(t("seo.home.coach_alt"))}</div>'
        f'<div class=phone>{screen_picture(lang, "progress", t("seo.home.shot.progress.alt"), eager=True)}</div>'
        f'<span class="chip c1">{t("seo.home.chip1_html")}</span>'
        f'<span class="chip c2">{t("seo.home.chip2_html")}</span>'
        "</div></div>"
    )


def _demo(lang: str) -> str:
    t = _t(lang)
    name = seed_data.localized_exercise_name(_identity_for_media_slug(_DEMO_SLUG), lang)
    unit = _e(t("seo.tool.unit_kg"))
    return (
        f'<div class=demo role=img aria-label="{_e(t("seo.home.demo_label"))}">'
        f"<div class=ex>{_e(name)}</div><div class=rows>"
        f"<div><span>80\u00a0{unit} × 8</span><span>✓</span></div>"
        f"<div><span>90\u00a0{unit} × 6</span><span>✓</span></div>"
        f"<div class=new><span>100\u00a0{unit} × 8</span><span>{seo_design.icon('flame')}{_e(t('seo.home.demo_record'))}</span></div></div>"
        f"<div class=input><div class=field><span class=typed>100 8</span></div>"
        f'<div class=send>{seo_design.icon("send")}</div></div></div>'
    )


def _chat(lang: str) -> str:
    t = _t(lang)
    return (
        f"<div class=chat><div class='bubble me'>{_e(t('seo.home.chat.q'))}</div>"
        f"<div class='bubble coach'><img src=\"{seo_static.asset_url('coach-face.jpg')}\" alt=\"\" width=34 height=34 loading=lazy>"
        f"<div>{_e(t('seo.home.chat.a'))}</div></div>"
        f"<div class=note>{_e(t('seo.home.chat.note'))}</div></div>"
    )


def _bento(lang: str) -> str:
    t = _t(lang)

    def tile(cls: str, ic: str, key: str, extra: str = "", href: Optional[str] = None) -> str:
        title = t(f"seo.home.b.{key}.h")
        more = (f'<a class=more href="{href}" aria-label="{_e(t("seo.home.more_about", what=title))}">'
                f'{_e(t("seo.home.more"))} →</a>' if href else "")
        return (f'<div class="tile reveal {cls}"><span class=ic>{seo_design.icon(ic)}</span>'
                f"<h3>{_e(title)}</h3><p>{_e(t(f'seo.home.b.{key}.p'))}</p>{more}{extra}</div>")

    return (
        "<div class=bento>"
        + tile("b-wide hot", "type", "log", _demo(lang), url_for(Page("telegram"), lang))
        + tile("b-tall", "coach", "coach", _chat(lang), url_for(Page("ai-coach"), lang))
        + "<div class=bento-rest>"
        + tile("b-third", "video", "video", "", url_for(Page("video-review"), lang))
        + tile("b-third", "trophy", "records", "", url_for(Page("tool", "1rm"), lang))
        + tile("b-third", "program", "programs", "", url_for(Page("programs"), lang))
        + tile("b-third", "import", "import", "", url_for(Page("import", "hevy"), lang))
        + tile("b-third", "phone", "everywhere", "", url_for(Page("telegram"), lang))
        + "</div></div>"
    )


def _story(lang: str) -> str:
    """«Одна тренировка»: шаги слева, телефон справа держится на месте и
    меняет экран под шаг (IntersectionObserver). Без скрипта и при «меньше
    движения» — первый экран и шаги текстом; на телефоне у каждого шага свой
    снимок под текстом."""
    t = _t(lang)
    screens = seo_static.SCREENS[seo_static.screens_lang(lang)]
    steps, shots = [], []
    for i, (name, _w, _h) in enumerate(screens):
        alt = t(f"seo.home.shot.{name}.alt")
        steps.append(
            f'<li class="step reveal" data-i={i}><span class=step-n>{i + 1}</span>'
            f"<h3>{_e(t(f'seo.home.story.{i + 1}.h'))}</h3><p>{_e(t(f'seo.home.story.{i + 1}.p'))}</p>"
            f"<div class='phone step-shot'>{screen_picture(lang, name, alt)}</div></li>")
        shots.append(f'<div class="screen{" on" if i == 0 else ""}" data-i={i}>{screen_picture(lang, name, alt)}</div>')
    script = (
        "(function(){var ph=document.querySelector('.story-phone');if(!ph||!('IntersectionObserver' in window))return;"
        "var sc=ph.querySelectorAll('.screen'),st=document.querySelectorAll('.story .step');"
        "var io=new IntersectionObserver(function(es){es.forEach(function(e){if(!e.isIntersecting)return;var i=e.target.dataset.i;"
        "sc.forEach(function(s){s.classList.toggle('on',s.dataset.i===i);});st.forEach(function(s){s.classList.toggle('on',s.dataset.i===i);});});},"
        "{rootMargin:'-45% 0px -45% 0px'});st.forEach(function(s){io.observe(s);});})();"
    )
    return (f"<div class=story><ol class=steps-story>{''.join(steps)}</ol>"
            f"<div class=story-side><div class='phone story-phone'>{''.join(shots)}</div></div></div><script>{script}</script>")


def _basement(lang: str) -> str:
    """Полоса «неделя в подвале»: пиксельный тренер ходит по подвалу, на доске —
    настоящие цифры за 7 дней (site_numbers). Нет цифр — нет и доски."""
    import site_numbers

    t = _t(lang)
    data = site_numbers.cached()
    board = ""
    if data:
        tons = data["kg"] / 1000
        cells = (
            (t("seo.home.week.workouts"), f"{int(data['workouts'])}"),
            (t("seo.home.week.sets"), f"{int(data['sets'])}"),
            (t("seo.home.week.tons"), f"{tons:.1f}" if tons < 100 else f"{tons:.0f}"),
        )
        board = (f'<div class=board role=group aria-label="{_e(t("seo.home.week.h"))}"><b class=board-h>{_e(t("seo.home.week.h"))}</b>'
                 + "".join(f"<div><span class=board-n>{_e(v)}</span><small>{_e(k)}</small></div>" for k, v in cells)
                 + f"<small class=board-note>{_e(t('seo.home.week.note'))}</small></div>")
    # пол — свой контейнер: тренер и собака ходят только по нему и
    # разворачиваются у края, а не уходят под доску; атлет с гантелями
    # стоит у дальнего края и делает подходы
    return (f'<section class="basement reveal" aria-label="{_e(t("seo.home.week.label"))}"><div class=basement-floor>'
            f"<span class=lifter>{sprite('curl', 2)}</span>"
            f"<span class=walker>{sprite('coach_walk', 2)}</span><span class=walker-dog>{sprite('dog', 2)}</span></div>"
            f"{board}</section>")


def sprite(name: str, scale: int = 3, label: str = "") -> str:
    """Пиксельный спрайт из ленты кадров: шаги CSS, без сглаживания; при
    «уменьшении движения» стоит первый кадр."""
    rel, w, h, frames, step_ms = seo_static.SPRITES[name]
    role = f' role=img aria-label="{_e(label)}"' if label else " aria-hidden=true"
    anim = f"animation-duration:{step_ms * frames}ms;--sx:-{w * scale * frames}px;" if frames > 1 else ""
    return (f'<span class="sprite{" anim" if frames > 1 else ""}"{role} style="width:{w * scale}px;height:{h * scale}px;'
            f"background-image:url({seo_static.asset_url(rel)});background-size:{w * scale * frames}px {h * scale}px;"
            f'{anim}--frames:{frames}"></span>')


def _gym(lang: str) -> str:
    """Подвал с вкладки «Меню» — как есть в приложении для iPhone: петля,
    отрисованная тем же кодом сцены (scripts/render_basement_loop.py), постер
    — скриншот симулятора. При «уменьшении движения» — только скриншот:
    видео прячет CSS, а скрипт ставит на паузу и не даёт ему качаться."""
    t = _t(lang)
    w, h = seo_static.SCENE_SIZE
    alt = _e(t("seo.home.gym.alt"))
    scene = seo_static.screens_lang(lang)
    poster = seo_static.asset_url(f"gym/scene-{scene}.png")
    script = (
        "(function(){var m=window.matchMedia&&matchMedia('(prefers-reduced-motion: reduce)');"
        "var v=document.querySelectorAll('.scene-video');function apply(){v.forEach(function(e){"
        "if(m&&m.matches){e.removeAttribute('autoplay');e.pause();}else if(e.paused){e.play().catch(function(){});}});}"
        "if(m){apply();m.addEventListener&&m.addEventListener('change',apply);}})();"
    )
    return (
        "<section class='gym-sec reveal'><div class=gym>"
        "<div class=gym-text><span class=eyebrow>" + _e(t("seo.home.gym.eyebrow")) + "</span>"
        f"<h2>{_e(t('seo.home.gym.h'))}</h2><p>{_e(t('seo.home.gym.p'))}</p>"
        f"<div class=you>{sprite('stand', 2)}<span class=you-tag>{_e(t('seo.home.gym.you_tag'))}</span>"
        f"<p>{_e(t('seo.home.gym.you'))}</p></div>"
        f"<p class=muted>{_e(t('seo.home.gym.note'))}</p></div>"
        f'<figure class=scene><video class=scene-video autoplay muted loop playsinline preload=metadata '
        f'disablepictureinpicture poster="{poster}" width={w} height={h} role=img aria-label="{alt}">'
        f'<source src="{seo_static.asset_url(f"gym/scene-{scene}.webm")}" type=video/webm>'
        f'<source src="{seo_static.asset_url(f"gym/scene-{scene}.mp4")}" type=video/mp4></video>'
        f'<img class=scene-still src="{poster}" alt="{alt}" width={w} height={h} loading=lazy decoding=async>'
        f"</figure><script>{script}</script>"
        "</div></section>"
    )


# «Чем заменить» с главной: самые показываемые в поиске страницы этого вида
# (выгрузка GSC за 16–24.10). Ссылка с главной — сигнал роботу, что это
# главные страницы сайта, и короткий путь к ним при переобходе.
HOME_SWAPS = ("romanian-deadlift", "machine-chest-press", "one-arm-dumbbell-row", "t-bar-row",
              "bodyweight-glute-bridge", "pec-deck", "step-up", "front-raise", "rear-delt-fly",
              "lat-pulldown", "machine-row", "close-grip-lat-pulldown")


def _home_swaps(lang: str) -> list[tuple[str, str, str]]:
    import seo_swaps

    t = _t(lang)
    return [(url_for(Page("exercise_alt", s), lang), seo_swaps.page_h1(Page("exercise_alt", s), lang), t("seo.home.swaps.meta"))
            for s in HOME_SWAPS if s in _EXERCISE_BY_URL and seo_swaps.has_page(s)]


def _render_home(lang: str, page: Page, request) -> str:
    t = _t(lang)
    imports = [(url_for(Page("import", s), lang), "import", t(f"seo.import.{s}.short"), t(f"seo.import.{s}.card"))
               for s in IMPORT_SOURCES]
    imports.append((url_for(Page("template", "workout-log"), lang), "list", t("seo.template.workout-log.short"), t("seo.template.card")))
    tools = [(url_for(Page("tool", s), lang), _TOOL_ICONS[s], t(f"seo.tool.{s}.short"), t(f"seo.tool.{s}.card")) for s in TOOLS]
    tools.append((url_for(Page("strength"), lang), "scale", t("seo.strength.short"), t("seo.strength.card")))
    library = [
        (url_for(Page("programs"), lang), "program", t("seo.programs.h1"), t("seo.programs.card")),
        (url_for(Page("splits"), lang), "chart", t("seo.splits.h1"), t("seo.splits.card")),
        (url_for(Page("exercises"), lang), "dumbbell", t("seo.exercises.h1"), t("seo.exercises.card", count=len(EXERCISES))),
        (url_for(Page("muscles"), lang), "body", t("seo.nav.muscles"), t("seo.muscles.card")),
        (url_for(Page("guides"), lang), "book", t("seo.nav.guides"), t("seo.guides.card")),
        (url_for(Page("compare"), lang), "list", t("seo.compare.short"), t("seo.compare.card")),
    ]
    faq = _faq_items(lang, FAQ["home"])

    def head(key: str) -> str:
        return (f"<div class='sec-head reveal'><span class=eyebrow>{_e(t(f'seo.home.{key}.eyebrow'))}</span>"
                f"<h2>{_e(t(f'seo.home.{key}.h'))}</h2><p>{_e(t(f'seo.home.{key}.p'))}</p></div>")

    body = (
        _hero(lang, page, request)
        + f"<section>{head('features')}{_bento(lang)}</section>"
        + f"<section>{head('shots')}{_story(lang)}</section>"
        + _basement(lang)
        + _gym(lang)
        + f"<section>{head('import')}{_icon_cards(imports)}</section>"
        + f"<section>{head('tools')}{_icon_cards(tools)}</section>"
        + f"<section>{head('library')}{_icon_cards(library)}</section>"
        + f"<section>{head('swaps')}{_cards(_home_swaps(lang))}</section>"
        + _faq_html(lang, faq)
        + _cta(lang, page.block, _e(t("seo.home.cta")), request)
    )
    base = base_url(request)
    app = {
        "@context": "https://schema.org", "@type": "SoftwareApplication",
        "name": t("seo.brand"), "alternateName": t("bot.name"),
        "description": t("seo.home.description"),
        "applicationCategory": "HealthApplication",
        "operatingSystem": "iOS, Telegram",
        "inLanguage": [hreflang(code) for code in LANGS],
        "url": base + url_for(page, lang),
        "image": base + seo_static.asset_url("icons/icon-512.png"),
        "screenshot": [base + src for src in page_images(page, lang)],
        "offers": {"@type": "Offer", "price": "0", "priceCurrency": "USD"},
        "sameAs": same_as(),
        "author": {"@type": "Person", "name": DEVELOPER},
        "publisher": {"@id": base + ORG_ID},
    }
    site = {"@context": "https://schema.org", "@type": "WebSite", "name": t("seo.brand"),
            "url": base + url_for(page, lang), "inLanguage": hreflang(lang)}
    return _layout(lang, page, request, title=t("seo.home.title"), description=t("seo.home.description"),
                   body=body, ld=[app, site, organization_ld(lang, request), _faq_ld(faq)])


# Разработчик — так, как он назван в App Store; других контактов, кроме
# страницы поддержки, сайт не публикует.
DEVELOPER = "Aleksandr Vilman"
ORG_ID = "/#org"


def same_as() -> list[str]:
    return [u for u in (config.app_store_url(), f"https://t.me/{bot_username()}") if u]


def organization_ld(lang: str, request) -> dict:
    base = base_url(request)
    return {
        "@context": "https://schema.org", "@type": "Organization", "@id": base + ORG_ID,
        "name": i18n.t_in(lang, "seo.brand"), "alternateName": i18n.t_in(lang, "bot.name"),
        "url": base + url_for(Page("home"), lang), "logo": base + seo_static.asset_url("icons/icon-512.png"),
        "sameAs": same_as(), "founder": {"@type": "Person", "name": DEVELOPER},
    }


# ---------- статьи и переезд ----------


# Значки разделов статьи-страницы — по порядку секций.
_SECTION_ICONS = {
    "ai-coach": ("program", "chart", "coach", "phone"),
    "video-review": ("video", "phone", "coach", "bolt"),
    "telegram": ("type", "trophy", "program", "bolt", "chart", "list", "coach", "import", "phone"),
    "workout-log-app": ("type", "chart", "coach", "video", "program", "import", "phone"),
}


def _video_example(lang: str) -> str:
    """Пример разбора видео — как его пишет тренер: что видно, что править
    первым, чего с ракурса не видно. Подписан как пример."""
    t = _t(lang)
    return (f"<div class='review-card'><span class=badge>{_e(t('seo.video-review.example.badge'))}</span>"
            f"<p class=review-ex>{_e(t('seo.video-review.example.exercise'))}</p>"
            f"{t('seo.video-review.example.body_html')}</div>")


def _csv_visual(lang: str) -> str:
    """Шапка переезда: строки файла → подходы в дневнике (из того же примера,
    что у шаблона дневника)."""
    import seo_template

    t = _t(lang)
    rows = seo_template.example_rows(lang)[:3]
    head = ",".join(seo_template.headers(lang)[:6])
    lines = "\n".join(",".join(r[:6]) for r in rows)
    sets = "".join(f"<li><span>{_e(r[2])}</span><span>{_e(r[4] or '—')}\u00a0<span class=u>{_e(t('seo.tool.unit_kg'))}</span> × {_e(r[5])}</span></li>"
                   for r in rows)
    return (f"<div class=csv-visual><pre><code>{_e(head)}\n{_e(lines)}</code></pre>"
            f"<span class=csv-arrow>{seo_design.icon('arrow')}</span><ul>{sets}</ul></div>")


def _feature_visual(lang: str, key: str) -> str:
    t = _t(lang)
    if key == "ai-coach":
        return _chat(lang)
    if key == "video-review":
        return _video_example(lang)
    name = "workout" if key == "telegram" else "progress"
    return f"<div class=phone>{screen_picture(lang, name, t(f'seo.home.shot.{name}.alt'), eager=True)}</div>"


def feature_head(eyebrow: str, h1: str, lead: str, visual: str, actions: str = "") -> str:
    """Шапка страницы-рассказа: текст и кнопки слева, картинка справа (на широком экране)."""
    return (f"<div class=feature-head><div class=feature-text><span class=eyebrow>{_e(eyebrow)}</span>"
            f"<h1>{_e(h1)}</h1><p class=lead>{_e(lead)}</p>{actions}</div><div class=feature-visual>{visual}</div></div>")


def _icon_h2(icon: str, text: str) -> str:
    return f"<h2 class=ic-h><span class=ic>{seo_design.icon(icon)}</span>{_e(text)}</h2>"


def _render_feature(lang: str, page: Page, request) -> str:
    t = _t(lang)
    key = page.kind
    params = _article_params(lang)
    icons = _SECTION_ICONS[key]
    sections = "".join(
        f"<section class=reveal>{_icon_h2(icons[(i - 1) % len(icons)], t(f'seo.{key}.s{i}.h'))}{t(f'seo.{key}.s{i}.p_html', **params)}</section>"
        for i in range(1, _ARTICLE_SECTIONS[key] + 1)
    )
    related = [(url_for(Page(f), lang), _strip_emoji(t(f"seo.{f}.card.title")), t(f"seo.{f}.card.text")) for f in FEATURES if f != key]
    faq = _faq_items(lang, FAQ[key]) if key in FAQ else []
    title, description = t(f"seo.{key}.title"), t(f"seo.{key}.description")
    # Страница про сам бот — единственная, где бот первой кнопкой на любом устройстве.
    bot_first = key == "telegram"
    body = (
        feature_head(t("seo.eyebrow.feature"), t(f"seo.{key}.h1"), t(f"seo.{key}.lead"), _feature_visual(lang, key),
                     _cta_buttons(lang, page.block, device_of(request), bot_first=bot_first))
        + f"<div class=prose>{sections}</div>"
        + (_coach_examples(lang) if key == "ai-coach" else "")
        + (f"<div class=prose>{_faq_html(lang, faq)}</div>" if faq else "")
        + _cta(lang, page.block, _e(t(f"seo.{key}.cta")), request, bot_first=bot_first)
        + _cards(related)
    )
    ld = [_webpage_ld(lang, page, request, title, description)] + ([_faq_ld(faq)] if faq else [])
    return _layout(lang, page, request, title=title, description=description, body=body, ld=ld, wide=True)


# Примеры разговоров с тренером на странице AI-тренера: что он умеет на
# самом деле — ответ по истории, программа по дням, разбор видео, нагрузка.
# Подписаны как пример: числа в них придуманы, а не взяты у живого атлета.
COACH_EXAMPLES = ("history", "program", "video", "load")


def _coach_examples(lang: str) -> str:
    t = _t(lang)
    face = seo_static.asset_url("coach-face.jpg")
    items = "".join(
        f"<div class='example reveal'><h3>{_e(t(f'seo.ai-coach.ex.{k}.h'))}</h3><div class=chat>"
        f"<div class='bubble me'>{_e(t(f'seo.ai-coach.ex.{k}.q'))}</div>"
        f"<div class='bubble coach'><img src=\"{face}\" alt=\"\" width=34 height=34 loading=lazy decoding=async>"
        f"<div>{t(f'seo.ai-coach.ex.{k}.a_html')}</div></div></div></div>"
        for k in COACH_EXAMPLES
    )
    return (f"<section><h2>{_e(t('seo.ai-coach.ex.h'))}</h2><p class=muted>{_e(t('seo.ai-coach.ex.note'))}</p>"
            f"<div class=examples>{items}</div></section>")


def faq_key(page: Page) -> Optional[str]:
    """Ключ FAQ страницы: по разделу (`import`) или по разделу и аргументу (`vs:hevy`)."""
    if page.kind in FAQ:
        return page.kind
    key = f"{page.kind}:{page.arg}"
    return key if key in FAQ else None


# Лендинг переезда: строки «бесплатный Hevy/Strong против дневника» — только
# сверенное на страницах сравнения (/vs/*, та же дата «на октябрь 2026»).
_SWITCH_ROWS = {"hevy": ("routines", "charts", "coach", "video", "price"),
                "strong": ("routines", "coach", "video", "price")}
_SWITCH_MARKS = {"hevy": {"routines": "part", "charts": "part", "coach": "part", "video": "no", "price": "part"},
                 "strong": {"routines": "part", "coach": "no", "video": "no", "price": "part"}}
# Начало строки заголовков выгрузки — как в самом файле, на любом языке.
_SWITCH_COLUMNS = {"hevy": "title,start_time,exercise_title,set_index,weight_kg,reps",
                   "strong": "Date,Workout Name,Exercise Name,Set Order,Weight,Reps"}


def _switch_visual(lang: str) -> str:
    """Шапка переезда: два экрана приложения и плашка итога (числа — пример)."""
    t = _t(lang)
    first = "history" if seo_static.screens_lang(lang) == "en" else "progress"
    return (f"<div class=switch-shots><div class=phone>{screen_picture(lang, first, t(f'seo.home.shot.{first}.alt'), eager=True)}</div>"
            f"<div class=phone>{screen_picture(lang, 'coach', t('seo.home.shot.coach.alt'), eager=True)}</div>"
            f"<div class=switch-badge><span>{_e(t('seo.switch.badge'))}</span><b>{_e(t('seo.switch.move.big'))}</b></div></div>")


def _switch_chips(lang: str) -> str:
    t = _t(lang)
    items = (t("seo.switch.chip.free"), t("seo.switch.chip.undo", days=config.IMPORT_UNDO_DAYS), t("seo.switch.chip.units"))
    return "<ul class=switch-chips>" + "".join(f"<li>{seo_design.icon('check')}{_e(x)}</li>" for x in items) + "</ul>"


def _switch_sections(lang: str, src: str) -> str:
    t = _t(lang)
    app = t(f"seo.vs.{src}.name")
    move = (f"<section class=reveal><h2>{_e(t('seo.switch.move.h'))}</h2><div class=switch-move>"
            f"<div class=switch-file><b>{_e(app)} · CSV</b><code>{_e(_SWITCH_COLUMNS[src])}</code></div>"
            f"<span class=csv-arrow>{seo_design.icon('arrow')}</span>"
            f"<div class=switch-result><span>{_e(t('seo.switch.move.ready'))}</span><b>{_e(t('seo.switch.move.big'))}</b>"
            f"<span>{_e(t('seo.switch.move.sets'))}</span><span>{_e(t('seo.switch.move.skip'))}</span></div></div>"
            f"<p class=muted>{_e(t('seo.switch.move.note'))}</p></section>")
    rows = "".join(
        f"<tr><th scope=row>{_e(t(f'seo.switch.row.{r}'))}</th>"
        f"<td class=us data-label=\"{_e(t('seo.brand'))}\">{_mark(lang, 'yes')}{_e(t(f'seo.switch.us.{r}', videos=config.AI_VIDEO_DAILY_LIMIT))}</td>"
        f"<td data-label=\"{_e(t('seo.switch.col.them', app=app))}\">{_mark(lang, _SWITCH_MARKS[src][r])}{_e(t(f'seo.switch.{src}.{r}'))}</td></tr>"
        for r in _SWITCH_ROWS[src]
    )
    compare = (f"<section class=reveal><h2>{_e(t('seo.switch.cmp.h', app=app))}</h2>"
               f"<div class='table-card vs cards-mobile'><table><thead><tr><th></th><th class=us>{_e(t('seo.brand'))}</th>"
               f"<th>{_e(t('seo.switch.col.them', app=app))}</th></tr></thead><tbody>{rows}</tbody></table></div>"
               f"<p class=muted>{t('seo.switch.cmp.note_html', app=_e(app), href=_e(url_for(Page('vs', src), lang)))}</p></section>")
    return move + compare


def _switch_coach(lang: str, src: str) -> str:
    t = _t(lang)
    app = t(f"seo.vs.{src}.name")
    return (f"<section class=reveal><h2>{_e(t('seo.switch.coach.h'))}</h2>"
            f"<div class=chat><div class='bubble me'>{_e(t('seo.switch.coach.q', app=app))}</div>"
            f"<div class='bubble coach'><img src=\"{seo_static.asset_url('coach-face.jpg')}\" alt=\"\" width=34 height=34 loading=lazy>"
            f"<div>{_e(t('seo.switch.coach.a'))}</div></div>"
            f"<div class=note>{_e(t('seo.switch.coach.note', questions=config.AI_QUESTION_DAILY_LIMIT, videos=config.AI_VIDEO_DAILY_LIMIT))}</div>"
            "</div></section>")


def _render_import(lang: str, page: Page, request) -> str:
    t = _t(lang)
    src = page.arg
    params = _article_params(lang)
    others = [
        (url_for(Page("import", s), lang), t(f"seo.import.{s}.short"), t(f"seo.import.{s}.card"))
        for s in IMPORT_SOURCES if s != src
    ]
    faq = _faq_items(lang, FAQ["import"])
    title, description = t(f"seo.import.{src}.title"), t(f"seo.import.{src}.description")
    actions = _cta_buttons(lang, page.block, device_of(request))
    # Hevy и Strong — лендинг переезда: зачем уходить, что получишь, потом та же
    # инструкция. CSV из таблицы — прежняя страница-инструкция.
    switch = src in _SWITCH_ROWS
    if switch:
        head = feature_head(t("seo.switch.eyebrow", app=t(f"seo.vs.{src}.name")), t(f"seo.import.{src}.h1"),
                            t(f"seo.import.{src}.lead"), _switch_visual(lang), actions + _switch_chips(lang))
    else:
        head = feature_head(t("seo.eyebrow.import"), t(f"seo.import.{src}.h1"), t(f"seo.import.{src}.lead"), _csv_visual(lang),
                            actions)
    body = (
        head
        + "<div class=prose>"
        + (_switch_sections(lang, src) if switch else "")
        + f"<section class=reveal>{_icon_h2('import', t(f'seo.import.{src}.export.h'))}{t(f'seo.import.{src}.export_html')}</section>"
        f"<section class=reveal>{_icon_h2('telegram', t('seo.import.bot.h'))}{t('seo.import.bot_html', **params)}</section>"
        f"<section class=reveal>{_icon_h2('phone', t('seo.import.app.h'))}{t('seo.import.app_html')}</section>"
        f"<section class=reveal>{_icon_h2('check', t('seo.import.what.h'))}{t('seo.import.what_html', **params)}</section>"
        + (_switch_coach(lang, src) if switch else "")
        + _faq_html(lang, faq) + "</div>"
        + _cta(lang, page.block, _e(t("seo.import.cta")), request)
        + f"<h2>{_e(t('seo.import.more.h'))}</h2>" + _cards(others)
    )
    ld = [_webpage_ld(lang, page, request, title, description), _faq_ld(faq)]
    return _layout(lang, page, request, title=title, description=description, body=body, ld=ld, wide=True)


# ---------- калькуляторы ----------

_TOOL_ICONS = {"1rm": "calc", "warmup": "warmup", "rpe": "gauge", "wilks": "trophy",
               "tonnage": "dumbbell", "rest-timer": "timer", "goal": "target", "strength-percentile": "percent",
               "531": "plates", "weekly-volume": "chart", "protein": "bolt", "calories": "flame"}


def _seg(name: str, legend: str, options: list[tuple[str, str]], checked: str) -> str:
    radios = "".join(
        f'<input type=radio id="{name}-{v}" name={name} value="{v}"{" checked" if v == checked else ""}>'
        f'<label for="{name}-{v}">{_e(label)}</label>'
        for v, label in options
    )
    return f"<fieldset><legend>{_e(legend)}</legend><div class=seg>{radios}</div></fieldset>"


def _unit_seg(lang: str) -> str:
    t = _t(lang)
    return _seg("unit", t("seo.tool.unit"), [("kg", t("seo.tool.unit_kg")), ("lb", t("seo.tool.unit_lb"))], "kg")


def _formula_seg(lang: str) -> str:
    t = _t(lang)
    return _seg("f", t("seo.tool.1rm.formula"), [("epley", "Epley"), ("brzycki", "Brzycki")], "epley")


def _rpe_select(ident: str, label: str, blank: bool = True, default: str = "", hint: str = "") -> str:
    opts = ('<option value="">—</option>' if blank else "") + "".join(
        f'<option{" selected" if v == default else ""}>{v}</option>' for v in ("10", "9.5", "9", "8.5", "8", "7.5", "7", "6.5", "6")
    )
    small = f"<small class=hint>{_e(hint)}</small>" if hint else ""
    return f"<label>{_e(label)}<select id={ident}>{opts}</select>{small}</label>"


def _num_input(ident: str, label: str, value: str, step: str = "0.5", mode: str = "decimal", max_: str = "") -> str:
    """Числовое поле — текстом с цифровой клавиатурой: type=number на iPhone
    не даёт запятую и прыгает от колёсика; запятую страница меняет на точку."""
    del step, max_
    return (f'<label>{_e(label)}<input id={ident} type=text inputmode={mode} autocomplete=off enterkeyhint=done '
            f'pattern="[0-9]*[.,]?[0-9]*" value="{value}"></label>')


def _log_cta(lang: str, tool: str, text_key: str = "seo.tool.log_cta") -> str:
    """«Запиши в дневник» под результатом: /go/diary сам решит по устройству,
    App Store или бот, — как главная кнопка страницы. Рядом — карточка
    результата для сторис, если калькулятор её умеет (ссылку ставит скрипт)."""
    t = _t(lang)
    return (f'<div class="log-cta btn-row"><a class="btn primary" href="{_e(go_url("diary", _BLOCKS[f"log:{tool}"]))}">'
            f'{seo_design.icon("check")}{_e(t(text_key))}</a>'
            f'<a class="btn small share" id=card href="#" target=_blank rel=noopener hidden>{seo_design.icon("image")}'
            f'{_e(t("seo.tool.card"))}</a></div>')


# Общие функции страницы калькулятора: значение поля или выбранной кнопки,
# число с единицей (единица — отдельным span, зазор даёт стиль).
_UI_JS = (
    "function $(i){return document.getElementById(i);}"
    "function val(n){var c=document.querySelector('[name=\"'+n+'\"]:checked');if(c)return c.value;var e=$(n);return e?e.value:'';}"
    "function fu(x,u){return fmt(x)+'<span class=u>\u00a0'+UNITS[u]+'</span>';}"
    "function bind(f){document.querySelectorAll('form.calc input,form.calc select').forEach(function(el){el.addEventListener('input',f);el.addEventListener('change',f);});f();}"
    "function done(ok){var r=document.querySelector('.result');if(r)r.classList.toggle('ok',!!ok);}"
    "function share(kind,q){var c=$('card');if(!c)return;if(!q){c.hidden=true;return;}"
    "c.href='/og/card/'+document.documentElement.lang+'/'+kind+'.png?'+q;c.hidden=false;}"
)


def _tool_1rm(lang: str, embed: bool = False) -> tuple[str, str, str]:
    t = _t(lang)
    form = (
        "<form class=calc onsubmit='return false'>"
        "<div class=row>" + _num_input("w", t("seo.tool.1rm.weight"), "100", "0.25")
        + _num_input("r", t("seo.tool.1rm.reps"), "5", "1", "numeric", "50") + "</div>"
        "<div class=row>" + _rpe_select("rpe", "RPE", hint=t("seo.tool.1rm.rpe_hint")) + _formula_seg(lang) + "</div>"
        + _unit_seg(lang) + "</form>"
    )
    kg = t("seo.tool.unit_kg")
    rows = "".join(f"<tr><td>{pct}%</td><td class=num>{_w(v, kg)}</td></tr>"
                   for pct, v in seo_tools.percent_table(seo_tools.e1rm(100, 5)))
    result = (
        f'<div class=result aria-live=polite><div class=k>{_e(t("seo.tool.1rm.result"))}</div>'
        f'<div class=big id=out>{_w(seo_tools.e1rm(100, 5), kg)}</div><div class=note id=note></div>'
        + ("" if embed else _log_cta(lang, "1rm")) +
        f'<div class=table-card><table><thead><tr><th>{_e(t("seo.tool.1rm.col_pct"))}</th>'
        f'<th class=num>{_e(t("seo.tool.1rm.col_weight"))}</th></tr></thead><tbody id=tbl>{rows}</tbody></table></div></div>'
    )
    script = (
        "var too=" + _js_str(t("seo.tool.1rm.too_many", max=config.E1RM_MAX_REPS)) + ";"
        "function run(){var w=parseFloat($('w').value),r=parseInt($('r').value,10),p=$('rpe').value,"
        "rpe=p===''?null:parseFloat(p),f=val('f'),u=val('unit'),out=$('out'),note=$('note'),tb=$('tbl');note.textContent='';"
        "if(!(w>0)||!(r>0)){out.textContent='—';tb.innerHTML='';done(0);return;}var m=e1rm(w,r,f,rpe);"
        "if(!m){out.textContent='—';note.textContent=too;tb.innerHTML='';done(0);return;}out.innerHTML=fu(m,u);done(1);share('rm1','v='+fmt(m)+'&u='+u);"
        "tb.innerHTML=C.percents.map(function(pc){return '<tr><td>'+pc+'%</td><td class=num>'+fu(m*pc/100,u)+'</td></tr>';}).join('');}"
        "bind(run);"
    )
    return form, result, script


def _tool_wilks(lang: str, embed: bool = False) -> tuple[str, str, str]:
    t = _t(lang)
    form = (
        "<form class=calc onsubmit='return false'>"
        "<div class=row>" + _num_input("total", t("seo.tool.wilks.total"), "500")
        + _num_input("bw", t("seo.tool.wilks.bw"), "80", "0.1") + "</div>"
        "<div class=row>" + _seg("sex", t("seo.tool.wilks.sex"), [("m", t("seo.tool.wilks.male")), ("f", t("seo.tool.wilks.female"))], "m")
        + _unit_seg(lang) + "</div>"
        + _seg("ev", t("seo.tool.wilks.event"), [("sbd", t("seo.tool.wilks.event_sbd")), ("b", t("seo.tool.wilks.event_b"))], "sbd")
        + _seg("eq", t("seo.tool.wilks.equipment"), [("classic", t("seo.tool.wilks.classic")), ("equipped", t("seo.tool.wilks.equipped"))], "classic")
        + "</form>"
    )
    result = (
        '<div class=result aria-live=polite><div class=pair>'
        f'<div><div class=k>IPF GL</div><div class=big id=gl>{seo_tools.ipf_gl(500, 80, "m"):.2f}</div></div>'
        f'<div><div class=k>DOTS</div><div class=big id=dots>{seo_tools.dots(500, 80, "m"):.2f}</div></div>'
        f'<div><div class=k>Wilks</div><div class=big id=wilks>{seo_tools.wilks(500, 80, "m"):.2f}</div></div></div>'
        '<div class=note id=note></div>' + ("" if embed else _log_cta(lang, "wilks", "seo.tool.log_cta_total")) + "</div>"
    )
    script = (
        "var LOWBW=" + _js_str(t("seo.tool.wilks.low_bw", min=int(seo_tools.GL_MIN_BW))) + ";"
        "function run(){var tot=parseFloat($('total').value),bw=parseFloat($('bw').value),s=val('sex'),u=val('unit'),k=u==='lb'?1/C.lbPerKg:1;"
        "var d=$('dots'),wk=$('wilks'),g=$('gl');$('note').textContent='';"
        "if(!(tot>0)||!(bw>0)){d.textContent=wk.textContent=g.textContent='—';done(0);return;}"
        "d.textContent=dots(tot*k,bw*k,s).toFixed(2);wk.textContent=wilks(tot*k,bw*k,s).toFixed(2);share('dots','v='+dots(tot*k,bw*k,s).toFixed(0));"
        "var gl=ipfGl(tot*k,bw*k,s,val('ev'),val('eq'));g.textContent=gl?gl.toFixed(2):'—';if(!gl)$('note').textContent=LOWBW;done(1);}"
        "bind(run);"
    )
    return form, result, script


def _tool_warmup(lang: str, embed: bool = False) -> tuple[str, str, str]:
    t = _t(lang)
    form = (
        "<form class=calc onsubmit='return false'>"
        "<div class=row>" + _num_input("work", t("seo.tool.warmup.work"), "100", "0.25")
        + _num_input("reps", t("seo.tool.warmup.reps"), "5", "1", "numeric", "30") + "</div>"
        "<div class=row>" + _num_input("bar", t("seo.tool.plates.bar"), "20", "0.5") + _unit_seg(lang) + "</div></form>"
    )
    kg = t("seo.tool.unit_kg")
    initial = "".join(
        f"<li><span>{_w(w, kg)} × {r}</span><small></small></li>"
        for w, r in seo_tools.warmup_sets(100, 20, seo_tools.WARMUP_STEP["kg"], 5)
    )
    result = (
        f'<div class=result aria-live=polite><div class=k>{_e(t("seo.tool.warmup.result"))}</div>'
        f'<ol class=steps-out id=sets>{initial}</ol><div class=note id=note></div>'
        + ("" if embed else _log_cta(lang, "warmup")) + "</div>"
    )
    defaults = {u: list(p) for u, p in seo_tools.DEFAULT_PLATES.items()}
    bars = {u: seo_tools.BARS[u][0] for u in ("kg", "lb")}
    script = (
        f"var STEP={json.dumps(seo_tools.WARMUP_STEP)},DEF={json.dumps(defaults)},BARS={json.dumps(bars)};"
        "var WORK=" + _js_str(t("seo.tool.warmup.work_set")) + ",SIDE=" + _js_str(t("seo.tool.warmup.side"))
        + ",LIGHT=" + _js_str(t("seo.tool.warmup.light")) + ";"
        "function side(w,b,u){var r=platesPerSide(w,b,DEF[u]);return r.plates.length?SIDE+' '+r.plates.map(fmt).join(' + '):'';}"
        "function run(){var w=parseFloat($('work').value),r=parseInt($('reps').value,10)||5,b=parseFloat($('bar').value)||0,u=val('unit'),box=$('sets');"
        "$('note').textContent='';if(!(w>0)){box.innerHTML='';done(0);return;}var list=warmupSets(w,b,STEP[u],r);"
        "if(!list.length){box.innerHTML='';$('note').textContent=LIGHT;done(0);return;}"
        "box.innerHTML=list.map(function(s){return '<li><span>'+fu(s[0],u)+' × '+s[1]+'</span><small>'+side(s[0],b,u)+'</small></li>';}).join('')"
        "+'<li class=work><span>'+fu(w,u)+' × '+r+'</span><small>'+WORK+'</small></li>';done(1);}"
        "document.querySelectorAll('[name=unit]').forEach(function(el){el.addEventListener('change',function(){$('bar').value=BARS[this.value];"
        "$('work').value=this.value==='lb'?225:100;});});bind(run);"
    )
    return form, result, script


def _rpe_table_html(lang: str) -> str:
    t = _t(lang)
    head = "".join(f"<th class=num>{r}</th>" for r in seo_tools.RPE_REPS)
    rows = "".join(
        f"<tr><th scope=row>{_num(rpe)}</th>" + "".join(f"<td class=num>{v:.1f}</td>" for v in values) + "</tr>"
        for rpe, values in seo_tools.rpe_chart()
    )
    return (f'<div class=table-card><table id=chart><thead><tr><th>{_e(t("seo.tool.rpe.col_rpe"))}</th>{head}</tr></thead>'
            f"<tbody>{rows}</tbody></table></div><p class=muted>{_e(t('seo.tool.rpe.chart_note'))}</p>")


def _tool_rpe(lang: str, embed: bool = False) -> tuple[str, str, str]:
    t = _t(lang)
    form = (
        "<form class=calc onsubmit='return false'>"
        f"<div class=lbl>{_e(t('seo.tool.rpe.done'))}</div>"
        "<div class=row>" + _num_input("w", t("seo.tool.1rm.weight"), "100", "0.25")
        + _num_input("r", t("seo.tool.1rm.reps"), "5", "1", "numeric", "12") + "</div>"
        "<div class=row>" + _rpe_select("rpe", "RPE", blank=False, default="8") + _formula_seg(lang) + "</div>"
        f"<div class=lbl>{_e(t('seo.tool.rpe.next'))}</div>"
        "<div class=row>" + _num_input("tr", t("seo.tool.1rm.reps"), "3", "1", "numeric", "12")
        + _rpe_select("trpe", "RPE", blank=False, default="9") + "</div>"
        + _unit_seg(lang) + "</form>"
    )
    result = (
        '<div class=result aria-live=polite><div class=pair>'
        f'<div><div class=k>{_e(t("seo.tool.1rm.result"))}</div><div class=big id=m>—</div></div>'
        f'<div><div class=k>{_e(t("seo.tool.rpe.target"))}</div><div class=big id=tw>—</div></div></div>'
        '<div class=note id=note></div>' + ("" if embed else _log_cta(lang, "rpe")) + "</div>"
    )
    script = (
        "var PCT=" + _js_str(t("seo.tool.rpe.pct", pct="{pct}")) + ";"
        "function run(){var w=parseFloat($('w').value),r=parseInt($('r').value,10),rpe=parseFloat($('rpe').value),f=val('f'),u=val('unit'),"
        "tr=parseInt($('tr').value,10),trpe=parseFloat($('trpe').value);$('note').textContent='';"
        "document.querySelectorAll('#chart tbody tr').forEach(function(row,i){var rr=C.rpeRows[i];row.querySelectorAll('td').forEach(function(td,j){td.textContent=rpePercent(C.rpeReps[j],rr,f).toFixed(1);});});"
        "var m=(w>0&&r>0)?e1rm(w,r,f,rpe):0;if(!m){$('m').textContent=$('tw').textContent='—';done(0);return;}$('m').innerHTML=fu(m,u);"
        "var p=(tr>0)?rpePercent(tr,trpe,f):0;if(!p){$('tw').textContent='—';done(1);return;}"
        "$('tw').innerHTML=fu(m*p/100,u);$('note').textContent=PCT.replace('{pct}',p.toFixed(1));done(1);}"
        "bind(run);"
    )
    return form, result, script


_TOOLS: dict[str, Callable[..., tuple[str, str, str]]] = {
    "1rm": _tool_1rm, "wilks": _tool_wilks, "warmup": _tool_warmup, "rpe": _tool_rpe,
}


def tool_parts(tool: str, lang: str, embed: bool = False) -> tuple[str, str, str]:
    builder = _TOOLS.get(tool)
    if builder is None:
        import seo_calcs

        builder = seo_calcs.TOOLS[tool]
    return builder(lang, embed=embed)


def _units_js(lang: str) -> str:
    t = _t(lang)
    return "{kg:" + _js_str(t("seo.tool.unit_kg")) + ",lb:" + _js_str(t("seo.tool.unit_lb")) + "}"


def _calc_script(lang: str, script: str) -> str:
    return f"<script>{seo_tools.calc_js()}var UNITS={_units_js(lang)};{_UI_JS}{script}</script>"


def _embed_snippet(lang: str, tool: str, request) -> str:
    t = _t(lang)
    src = f"{base_url(request)}{embed_url(tool, lang)}"
    code = (f'<iframe src="{src}" title="{t(f"seo.tool.{tool}.h1")}" width="100%" height="640" '
            'style="border:0;border-radius:16px;max-width:560px" loading="lazy"></iframe>')
    return (
        f'<details class=embed-box><summary>{_e(t("seo.embed.h"))}</summary><p class=muted>{_e(t("seo.embed.p"))}</p>'
        f'<textarea readonly id=embed-code rows=4 aria-label="{_e(t("seo.embed.h"))}">{_e(code)}</textarea>'
        f'<div class=btn-row style="margin-top:12px"><button class="btn small" type=button id=embed-copy>{_e(t("seo.embed.copy"))}</button></div>'
        "<script>document.getElementById('embed-copy').addEventListener('click',function(){var a=document.getElementById('embed-code');a.select();"
        "if(navigator.clipboard){navigator.clipboard.writeText(a.value);}else{document.execCommand('copy');}this.textContent="
        + _js_str(t("seo.embed.copied")) + ";});</script></details>"
    )


def _tool_ld(lang: str, page: Page, request, title: str, description: str) -> dict:
    return _webpage_ld(lang, page, request, title, description, extra={
        "mainEntity": {"@type": "WebApplication", "name": page_h1(page, lang), "applicationCategory": "HealthApplication",
                       "operatingSystem": "Any", "browserRequirements": "JavaScript",
                       "offers": {"@type": "Offer", "price": "0", "priceCurrency": "USD"}},
    })


def _render_tool(lang: str, page: Page, request) -> str:
    t = _t(lang)
    tool = page.arg
    form, result, script = tool_parts(tool, lang)
    others = [(url_for(Page("tool", s), lang), t(f"seo.tool.{s}.short"), t(f"seo.tool.{s}.card")) for s in TOOLS if s != tool]
    chart = (f"<section class=reveal><h2>{_e(t('seo.tool.rpe.chart_h'))}</h2>{_rpe_table_html(lang)}</section>"
             if tool == "rpe" else "")
    import seo_library

    pct = seo_tools.percentile_data()
    how_params = dict(seo_library.link_params(lang), max=config.E1RM_MAX_REPS, gl_source=GL_SOURCE,
                      meets=pct["meets"], built=human_date(pct["built"], lang),
                      dots_href=url_for(Page("tool", "wilks"), lang),
                      strength_href=url_for(Page("strength"), lang), rpe_href=url_for(Page("tool", "rpe"), lang))
    title, description = t(f"seo.tool.{tool}.title"), t(f"seo.tool.{tool}.description")
    body = (
        f"<span class=eyebrow>{_e(t('seo.eyebrow.tool'))}</span>"
        f"<h1>{_e(t(f'seo.tool.{tool}.h1'))}</h1><p class=lead>{_e(t(f'seo.tool.{tool}.lead'))}</p>"
        f"<div class=tool>{form}{result}</div>"
        + f"<noscript><p class=muted>{_e(t('seo.tool.noscript'))}</p></noscript>"
        + chart
        + f"<div class=prose><section class=reveal><h2>{_e(t(f'seo.tool.{tool}.how.h'))}</h2>"
        f"{t(f'seo.tool.{tool}.how_html', **how_params)}</section></div>"
        + _cta(lang, page.block, _e(t(f"seo.tool.{tool}.cta")), request)
        + f"<h2 class=group-title>{_e(t('seo.tool.more.h'))}</h2>" + _cards(others)
        + (_embed_snippet(lang, tool, request) if tool in EMBEDS else "")
        + _calc_script(lang, script)
    )
    return _layout(lang, page, request, title=title, description=description, body=body, wide=True,
                   ld=[_tool_ld(lang, page, request, title, description)])


def _render_tools(lang: str, page: Page, request) -> str:
    t = _t(lang)
    def item(s: str) -> tuple[str, str, str, str]:
        return url_for(Page("tool", s), lang), _TOOL_ICONS[s], t(f"seo.tool.{s}.short"), t(f"seo.tool.{s}.card")

    items = [item(s) for s in TOOLS if s not in NUTRITION_TOOLS]
    items.append((url_for(Page("strength"), lang), "scale", t("seo.strength.short"), t("seo.strength.card")))
    title, description = t("seo.tools.title"), t("seo.tools.description")
    body = (
        f"<span class=eyebrow>{_e(t('seo.eyebrow.tool'))}</span>"
        f"<h1>{_e(t('seo.tools.h1'))}</h1><p class=lead>{_e(t('seo.tools.lead'))}</p>"
        + f"<h2 class=group-title>{_e(t('seo.tools.training.h'))}</h2>" + _icon_cards(items)
        + f"<h2 class=group-title>{_e(t('seo.tools.nutrition.h'))}</h2>" + _icon_cards([item(s) for s in NUTRITION_TOOLS])
        + _cta(lang, page.block, _e(t("seo.tools.cta")), request)
    )
    return _layout(lang, page, request, title=title, description=description, body=body, wide=True,
                   ld=[_webpage_ld(lang, page, request, title, description, "CollectionPage")])


# ---------- нормативы силы ----------


def _level_names(lang: str) -> list[str]:
    t = _t(lang)
    return [t(f"seo.strength.level.{name}") for name, _th in seo_tools.LEVELS]


def _strength_table(lang: str, lift: str, sex: str, unit: str) -> str:
    t = _t(lang)
    unit_label = t("seo.tool.unit_kg") if unit == "kg" else t("seo.tool.unit_lb")
    head = "".join(f"<th class=num>{_e(n)}</th>" for n in _level_names(lang))
    rows = "".join(
        f"<tr><th scope=row>{_w(bw, unit_label)}</th>" + "".join(f"<td class=num>{_w(v, unit_label)}</td>" for v in values) + "</tr>"
        for bw, values in seo_tools.strength_table(lift, sex, unit)
    )
    return (f'<div class="table-card scroll-x sticky-first"><table><thead><tr><th>{_e(t("seo.strength.col_bw"))}</th>{head}</tr></thead>'
            f"<tbody>{rows}</tbody></table></div>")


def _strength_tool(lang: str, lift: str = "bench", sex: str = "m", *, bw: Optional[float] = None,
                   lift_kg: Optional[float] = None) -> tuple[str, str, str]:
    t = _t(lang)
    form = (
        "<form class=calc onsubmit='return false'>"
        + _seg("lift", t("seo.strength.tool.lift"), [(k, t(f"seo.strength.lift.{k}")) for k in seo_tools.STRENGTH_LIFTS + ("total",)], lift)
        + "<div class=row>" + _seg("sex", t("seo.tool.wilks.sex"), [("m", t("seo.tool.wilks.male")), ("f", t("seo.tool.wilks.female"))], sex)
        + _unit_seg(lang) + "</div>"
        "<div class=row>" + _num_input("bw", t("seo.tool.wilks.bw"), _num(bw) if bw else ("80" if sex == "m" else "60"), "0.1")
        + _num_input("lw", t("seo.tool.1rm.weight"), _num(lift_kg) if lift_kg else ("100" if sex == "m" else "50"), "0.25")
        + "</div>"
        + _num_input("lr", t("seo.tool.1rm.reps"), "1", "1", "numeric", "12")
        + "</form>"
    )
    result = (
        f'<div class=result aria-live=polite><div class=k>{_e(t("seo.strength.tool.result"))}</div>'
        '<div class=big id=lvl>—</div><div class=note id=note></div>' + _log_cta(lang, "strength", "seo.tool.log_cta_total") + "</div>"
    )
    script = (
        f"var NAMES={json.dumps(_level_names(lang), ensure_ascii=False)},BELOW=" + _js_str(t("seo.strength.level.below"))
        + ",SCORE=" + _js_str(t("seo.strength.tool.score", score="{score}"))
        + ",NEXT=" + _js_str(t("seo.strength.tool.next", weight="{weight}", level="{level}"))
        + ",TOP=" + _js_str(t("seo.strength.tool.top")) + ",TOO=" + _js_str(t("seo.tool.1rm.too_many", max=config.E1RM_MAX_REPS)) + ";"
        "function run(){var u=val('unit'),k=u==='lb'?1/C.lbPerKg:1,lift=val('lift'),s=val('sex'),bw=parseFloat($('bw').value)*k,"
        "w=parseFloat($('lw').value),r=parseInt($('lr').value,10)||1,out=$('lvl'),note=$('note');"
        "if(!(bw>0)||!(w>0)){out.textContent='—';note.textContent='';done(0);return;}var m=e1rm(w,r,'epley',null);"
        "if(!m){out.textContent='—';note.textContent=TOO;done(0);return;}"
        "var sc=liftScore(m*k,lift,bw,s),i=levelIndex(sc);out.textContent=i<0?BELOW:NAMES[i];share(i<0?'':'level',i<0?'':'l='+i+'&lift='+lift+'&w='+w+'&r='+r+'&bw='+$('bw').value+'&u='+u);"
        "var txt=SCORE.replace('{score}',Math.round(sc));if(i<C.levels.length-1){var nw=levelWeight(C.levels[i+1],lift,bw,s)/k;"
        "txt+=' '+NEXT.replace('{weight}',fmt(nw)+'\u00a0'+UNITS[u]).replace('{level}',NAMES[i+1]);}else{txt+=' '+TOP;}note.textContent=txt;done(1);}"
        "bind(run);"
    )
    return form, result, script


def _strength_tabs(lang: str, current: Page) -> str:
    t = _t(lang)
    links = []
    for url, key, _ex in STRENGTH_LIFTS:
        for sex_url, _s in STRENGTH_SEXES:
            p = Page("strength_lift", url, sex_url)
            here = " aria-current=page" if p == current else ""
            links.append(f'<a href="{url_for(p, lang)}"{here}>{_e(t(f"seo.strength.{key}.{sex_url}.tab"))}</a>')
    return f"<nav class=tabs aria-label=\"{_e(t('seo.strength.tabs'))}\">{''.join(links)}</nav>"


def _strength_method(lang: str) -> str:
    t = _t(lang)
    shares = seo_tools.LIFT_SHARE
    params = {
        "dots_href": url_for(Page("tool", "wilks"), lang),
        "thresholds": " · ".join(f"{n} {int(th)}" for n, (_k, th) in zip(_level_names(lang), seo_tools.LEVELS, strict=True)),
        "m_squat": _num(shares["m"]["squat"] * 100), "m_bench": _num(shares["m"]["bench"] * 100), "m_dead": _num(shares["m"]["deadlift"] * 100),
        "f_squat": _num(shares["f"]["squat"] * 100), "f_bench": _num(shares["f"]["bench"] * 100), "f_dead": _num(shares["f"]["deadlift"] * 100),
        "src_dots": STRENGTH_SOURCES[0], "src_data": STRENGTH_SOURCES[1],
    }
    return f"<section class='prose reveal'><h2>{_e(t('seo.strength.method.h'))}</h2>{t('seo.strength.method_html', **params)}</section>"


def _render_strength(lang: str, page: Page, request) -> str:
    t = _t(lang)
    form, result, script = _strength_tool(lang)
    cards = [
        (url_for(Page("strength_lift", url, sex), lang), t(f"seo.strength.{key}.{sex}.tab"), t(f"seo.strength.{key}.card"))
        for url, key, _ex in STRENGTH_LIFTS for sex, _s in STRENGTH_SEXES
    ]
    title, description = t("seo.strength.title"), t("seo.strength.description")
    body = (
        f"<span class=eyebrow>{_e(t('seo.eyebrow.strength'))}</span>"
        f"<h1>{_e(t('seo.strength.h1'))}</h1><p class=lead>{_e(t('seo.strength.lead'))}</p>"
        f"<div class=tool>{form}{result}</div>"
        + f"<section class=reveal><h2>{_e(t('seo.strength.pages.h'))}</h2>{_cards(cards)}</section>"
        + _strength_method(lang)
        + _cta(lang, page.block, _e(t("seo.strength.cta")), request)
        + _calc_script(lang, script)
    )
    return _layout(lang, page, request, title=title, description=description, body=body, wide=True,
                   ld=[_tool_ld(lang, page, request, title, description)])


def _render_strength_lift(lang: str, page: Page, request) -> str:
    import seo_strength_bw

    t = _t(lang)
    key, ex_url = _LIFT_BY_URL[page.arg]
    sex = _SEX_BY_URL[page.arg2]
    form, result, script = _strength_tool(lang, key, sex)
    tables = f"<h3>{_e(t('seo.strength.in_kg'))}</h3>{_strength_table(lang, key, sex, 'kg')}"
    if lang == "en":
        tables += f"<h3 style=\"margin-top:24px\">{_e(t('seo.strength.in_lb'))}</h3>{_strength_table(lang, key, sex, 'lb')}"
    lift_name = t(f"seo.strength.lift.{key}")
    title = t(f"seo.strength.{key}.{page.arg2}.title")
    description = t(f"seo.strength.{key}.{page.arg2}.description")
    body = (
        f"<span class=eyebrow>{_e(t('seo.eyebrow.strength'))}</span>"
        f"<h1>{_e(page_h1(page, lang))}</h1><p class=lead>{_e(t('seo.strength.lift_lead', lift=lift_name))}</p>"
        + _strength_tabs(lang, page)
        + f"<section class=reveal style=\"margin-top:24px\"><h2>{_e(t('seo.strength.tool.h'))}</h2><div class=tool>{form}{result}</div></section>"
        + f"<section class=reveal><h2>{_e(t('seo.strength.table.h'))}</h2>{tables}"
        f"<p class=muted>{_e(t('seo.strength.table_note'))}</p></section>"
        + _strength_method(lang)
        + seo_strength_bw.lift_page_links(lang, page.arg, sex)
        + (f'<p><a href="{url_for(Page("exercise", ex_url), lang)}">{_e(t("seo.strength.technique", lift=lift_name))} →</a></p>'
           if ex_url else "")
        + _cta(lang, page.block, _e(t("seo.strength.cta")), request)
        + _calc_script(lang, script)
    )
    return _layout(lang, page, request, title=title, description=description, body=body, wide=True,
                   ld=[_webpage_ld(lang, page, request, title, description)])


# ---------- программы и упражнения ----------


def _program_pages_for(identity: str) -> list[str]:
    return [p["key"] for p in seed_data.WORKOUT_PROGRAMS
            if any(name == identity for _day, exercises in p["days"] for name, _target in exercises)]


def _identity_for_media_slug(slug: str) -> str:
    for name, s in exercise_media.EXERCISE_IMAGE_SLUGS.items():
        if s == slug:
            return name
    raise KeyError(slug)


def _exercise_url_for_identity(identity: str) -> Optional[str]:
    slug = exercise_media.EXERCISE_IMAGE_SLUGS.get(identity)
    for url, s in EXERCISES:
        if s == slug:
            return url
    return None


def _render_programs(lang: str, page: Page, request) -> str:
    import seo_plans

    t = _t(lang)
    cards = [
        (url_for(Page("program", p["key"]), lang), _strip_emoji(seed_data.localized_program_name(p["key"], lang)),
         seed_data.localized_program_meta(p["key"], lang))
        for p in seed_data.WORKOUT_PROGRAMS
    ]
    title, description = t("seo.programs.title"), t("seo.programs.description")
    body = (
        f"<span class=eyebrow>{_e(t('seo.eyebrow.library'))}</span>"
        f"<h1>{_e(t('seo.programs.h1'))}</h1><p class=lead>{_e(t('seo.programs.lead'))}</p>"
        + program_cards([p["key"] for p in seed_data.WORKOUT_PROGRAMS], lang)
        + f"<h2 class=group-title>{_e(t('seo.programs.splits.h'))}</h2>"
        + _cards([(url_for(Page("split", s), lang), t(f"seo.split.{s}.h1"), t(f"seo.split.{s}.card")) for s in seo_plans.SPLIT_PAGES]
                 + [(url_for(Page("workout", w), lang), t(f"seo.wkt.{w}.h1"), t(f"seo.wkt.{w}.card")) for w in seo_plans.WORKOUTS]
                 + [(url_for(Page("splits"), lang), t("seo.splits.h1"), t("seo.splits.card"))])
        + f"<section class='prose reveal'><h2>{_e(t('seo.programs.take.h'))}</h2>{t('seo.programs.take_html', **_article_params(lang))}</section>"
        + _cta(lang, page.block, _e(t("seo.programs.cta")), request)
    )
    ld = _webpage_ld(lang, page, request, title, description, "CollectionPage", {
        "hasPart": [{"@type": "ExercisePlan", "name": name, "url": base_url(request) + href} for href, name, _m in cards],
    })
    return _layout(lang, page, request, title=title, description=description, body=body, wide=True, ld=[ld])


def _render_program(lang: str, page: Page, request) -> str:
    import seo_library
    import seo_plans

    t = _t(lang)
    link_params = seo_library.link_params(lang)
    key = page.arg
    program = seed_data.PROGRAM_BY_KEY[key]
    name = _strip_emoji(seed_data.localized_program_name(key, lang))
    days = []
    names: list[str] = []
    for i, (_day_name, exercises) in enumerate(program["days"]):
        rows = []
        for identity, target in exercises:
            shown_name = seed_data.localized_exercise_name(identity, lang)
            names.append(shown_name)
            shown = _e(shown_name)
            url = _exercise_url_for_identity(identity)
            if url:
                shown = f'<a class=ex-link href="{url_for(Page("exercise", url), lang)}">{shown}</a>'
            rows.append(f"<li><span>{shown}</span><span>{_e(seed_data.localized_target(target, lang) or '')}</span></li>")
        days.append(
            f'<div class="day reveal"><h3><small>{_e(t("seo.program.day", n=i + 1))}</small>'
            f"{_e(seed_data.localized_program_day_name(key, i, lang))}</h3><ul>{''.join(rows)}</ul></div>"
        )
    others = [p["key"] for p in seed_data.WORKOUT_PROGRAMS if p["key"] != key]
    description = seed_data.localized_program_description(key, lang)
    body = (
        f"<span class=eyebrow>{_e(seed_data.localized_program_meta(key, lang))}</span>"
        f"<h1>{_e(name)}</h1><p class=lead>{_e(description)}</p>"
        f"<section><h2>{_e(t('seo.program.days.h'))}</h2><div class=days>{''.join(days)}</div>"
        f"<p class=muted style=\"margin-top:16px\">{_e(t('seo.program.target_note'))}</p></section>"
        + seo_plans.program_week_html(key, lang)
        + f"<section class='prose reveal'><h2>{_e(t('seo.program.who.h'))}</h2>{t(f'seo.program.{key}.who_html', **link_params)}</section>"
        + f"<section class='prose reveal'><h2>{_e(t('seo.split.progress.h'))}</h2>{t('seo.split.progress_html', **link_params)}</section>"
        + _cta(lang, page.block, t("seo.program.cta_html", **_article_params(lang), name=_e(name)), request)
        + f"<h2>{_e(t('seo.program.more.h'))}</h2>" + program_cards(others, lang)
    )
    title = t(f"seo.program.{key}.title")
    plan = {
        "@context": "https://schema.org", "@type": "ExercisePlan", "name": name, "description": description,
        "url": base_url(request) + url_for(page, lang), "inLanguage": hreflang(lang),
        "exerciseType": list(dict.fromkeys(names)),
        "isPartOf": {"@type": "WebSite", "name": t("seo.brand"), "url": base_url(request) + url_for(Page("home"), lang)},
    }
    return _layout(lang, page, request, title=title, description=description, body=body, wide=True, ld=[plan])


def _media_url(filename: str, thumb: bool = False) -> str:
    return f"/v1/media/exercises/{'thumbs/' if thumb else ''}{filename}"


def _group_of(identity: str) -> str:
    for group, name in seed_data.EXERCISE_TEMPLATES:
        if name == identity:
            return group
    return ""


def _group_for(identity: str, lang: str) -> str:
    group = _group_of(identity)
    return seed_data.localized_muscle_group_name(group, lang) if group else ""


def exercise_picture(slug: str, frame: int, alt: str, *, sizes: str, widths: tuple[int, ...],
                     width: int, height: int, lazy: bool = True) -> str:
    """Кадр упражнения для сайта: <picture> с AVIF и WebP из media/site/ex
    (scripts/gen_site_exercise_images.py) — 4:3, не шире исходника. Мелкие
    превью бота (240px, для строк списка в приложении) сайт не берёт."""

    def srcset(fmt: str) -> str:
        return ", ".join(f"{seo_static.asset_url(f'ex/{slug}_{frame}-{w}.{fmt}')} {w}w" for w in widths)

    loading = " loading=lazy" if lazy else ""
    return (f'<picture><source type="image/avif" srcset="{srcset("avif")}" sizes="{sizes}">'
            f'<img src="{seo_static.asset_url(f"ex/{slug}_{frame}-{widths[-1]}.webp")}" srcset="{srcset("webp")}" sizes="{sizes}" '
            f'alt="{_e(alt)}" width={width} height={height}{loading} decoding=async></picture>')


def _exercise_card(url: str, slug: str, lang: str, show_group: bool = True) -> str:
    """Карточка упражнения: один кадр — конечное положение."""
    identity = _identity_for_media_slug(slug)
    name = seed_data.localized_exercise_name(identity, lang)
    pic = exercise_picture(slug, 2, name, sizes="(max-width: 600px) 50vw, 280px", widths=(360, 720), width=360, height=270)
    group = f"<span>{_e(_group_for(identity, lang))}</span>" if show_group else ""
    return (
        f'<a class="card thumb-card reveal" href="{url_for(Page("exercise", url), lang)}">{pic}'
        f"<b>{_e(name)}</b>{group}</a>"
    )


def _render_exercises(lang: str, page: Page, request) -> str:
    t = _t(lang)
    groups: dict[str, list[str]] = {}
    for url, slug in EXERCISES:
        groups.setdefault(_group_of(_identity_for_media_slug(slug)), []).append(_exercise_card(url, slug, lang, show_group=False))
    order = [g for g in dict.fromkeys(g for g, _n in seed_data.EXERCISE_TEMPLATES) if g in groups]
    chips = "".join(f'<a href="#g-{seed_data.muscle_group_slug(g)}">{_e(seed_data.localized_muscle_group_name(g, lang))}</a>'
                    for g in order)
    sections = f'<nav class=group-chips aria-label="{_e(t("seo.exercises.groups"))}">{chips}</nav>' + "".join(
        f"<h2 class=group-title id=g-{seed_data.muscle_group_slug(g)}>{_e(seed_data.localized_muscle_group_name(g, lang))}</h2>"
        f"<div class='grid ex-grid'>{''.join(groups[g])}</div>"
        for g in order
    )
    title, description = t("seo.exercises.title"), t("seo.exercises.description", count=len(EXERCISES))
    body = (
        f"<span class=eyebrow>{_e(t('seo.eyebrow.library'))}</span>"
        f"<h1>{_e(t('seo.exercises.h1'))}</h1><p class=lead>{_e(t('seo.exercises.lead'))}</p>"
        + sections
        + _cta(lang, page.block, _e(t("seo.exercise.cta")), request)
        + f"<p class=muted>{t('seo.exercise.credit_html')}</p>"
    )
    return _layout(lang, page, request, title=title, description=description, body=body, wide=True,
                   ld=[_webpage_ld(lang, page, request, title, description, "CollectionPage")])


def _exercise_aside(lang: str, page: Page, identity: str, request) -> str:
    """Боковая колонка страницы техники (на широком экране — липкая): мышцы,
    снаряд, порог входа, «запиши в дневник» и куда дальше."""
    import seo_library

    t = _t(lang)
    url = page.arg
    group = _group_of(identity)
    target = seed_data.EXERCISE_MUSCLE_TARGET.get(identity)
    muscles = t(f"seo.exf.{url}.muscles") if url in seo_library.EX_FACTS else ""
    muscle_pages = [m for m, g, tg in seo_library.MUSCLES if g == group and (tg is None or tg == target)]
    facts = [(t("seo.exercise.aside.group"), _e(_group_for(identity, lang)))]
    if muscles:
        facts.append((t("seo.exercise.aside.muscles"), _e(muscles)))
    if url in seo_library.EX_FACTS:
        equip, level = seo_library.EX_FACTS[url]
        facts.append((t("seo.exercise.aside.equipment"), _e(t(f"seo.equip.{equip}"))))
        facts.append((t("seo.exercise.aside.level"), f"<span class=lvl>{level}/3</span> {_e(t(f'seo.level.{level}'))}"))
    rows = "".join(f"<dt>{_e(k)}</dt><dd>{v}</dd>" for k, v in facts)
    links = [(url_for(Page("muscle", m), lang), t("seo.muscle.h1", name=seo_library.muscle_name(m, lang))) for m in muscle_pages]
    links += [(url_for(Page("pair", seo_library.pair_slug(a, b)), lang),
               t("seo.pair.h1", a=seo_library._ex_name(a, lang), b=seo_library._ex_name(b, lang)))
              for a, b in seo_library.PAIRS if url in (a, b)]
    import seo_swaps

    links = seo_swaps.aside_link(url, lang) + links
    links.append((url_for(Page("video-review"), lang), t("seo.exercise.aside.video")))
    items = "".join(f'<li><a href="{href}">{_e(label)}</a></li>' for href, label in links)
    return (f"<aside class=ex-aside><dl class=facts>{rows}</dl>"
            f"<div class=aside-cta><p>{_e(t('seo.exercise.aside.log'))}</p>{_cta_buttons(lang, page.block, device_of(request))}</div>"
            f"<h2 class=aside-h>{_e(t('seo.exercise.aside.more'))}</h2><ul class=links>{items}</ul></aside>")


def _steps(identity: str, lang: str) -> list[str]:
    text = exercise_descriptions.get_description(identity, lang) or ""
    return [re.sub(r"^\d+\.\s*", "", line).strip() for line in text.splitlines() if line.strip()]


def _muscles_worked(lang: str, page: Page, identity: str) -> str:
    """«Какие мышцы работают» — так ищут почти про любое упражнение. Только из
    данных: сверенный руками список мышц (`seo.exf.*`, у упражнений из
    `EX_FACTS`) или основная группа каталога; ссылка — на страницу мышцы."""
    import seo_library

    t = _t(lang)
    group = _group_of(identity)
    target = seed_data.EXERCISE_MUSCLE_TARGET.get(identity)
    if page.arg in seo_library.EX_FACTS:
        text = t("seo.exercise.muscles.list", muscles=t(f"seo.exf.{page.arg}.muscles"))
    else:
        primary = (seed_data.localized_muscle_target_name(target, lang) if target
                   else seed_data.localized_muscle_group_name(group, lang))
        text = t("seo.exercise.muscles.primary", muscle=primary)
    muscle_pages = [m for m, g, tg in seo_library.MUSCLES if g == group and (tg is None or tg == target)]
    links = "".join(f'<li><a href="{url_for(Page("muscle", m), lang)}">'
                    f"{_e(t('seo.muscle.h1', name=seo_library.muscle_name(m, lang)))}</a></li>" for m in muscle_pages)
    return (f"<section class=reveal><h2>{_e(t('seo.exercise.muscles.h'))}</h2><p>{_e(text)}</p>"
            + (f"<ul class=links>{links}</ul>" if links else "") + "</section>")


def _render_exercise(lang: str, page: Page, request) -> str:
    import seo_swaps

    t = _t(lang)
    slug = _EXERCISE_BY_URL[page.arg]
    identity = _identity_for_media_slug(slug)
    name = seed_data.localized_exercise_name(identity, lang)
    base = base_url(request)
    frame_sizes = "(max-width: 760px) 50vw, 370px"
    frames = (
        "<div class=frames>"
        "<figure>" + exercise_picture(slug, 1, t("seo.exercise.frame_start", name=name), sizes=frame_sizes, widths=(720,),
                                      width=720, height=540, lazy=False)
        + f'<figcaption><b>1</b><span class=cap-long>{_e(t("seo.exercise.cap_start"))}</span>'
        f'<span class=cap-short>{_e(t("seo.exercise.cap_start_short"))}</span></figcaption></figure>'
        "<figure>" + exercise_picture(slug, 2, t("seo.exercise.frame_end", name=name), sizes=frame_sizes, widths=(360, 720),
                                      width=720, height=540)
        + f'<figcaption><b>2</b><span class=cap-long>{_e(t("seo.exercise.cap_end"))}</span>'
        f'<span class=cap-short>{_e(t("seo.exercise.cap_end_short"))}</span></figcaption></figure>'
        "</div>"
    )
    steps = "".join(f"<li>{_e(s)}</li>" for s in _steps(identity, lang))
    mistakes = "".join(f"<li>{_e(m)}</li>" for m in t(f"seo.ex.{slug}.mistakes").split("\n") if m.strip())
    programs = _program_pages_for(identity)
    in_programs = ""
    if programs:
        in_programs = f"<section class=reveal><h2>{_e(t('seo.exercise.programs.h'))}</h2>{program_cards(programs, lang)}</section>"
    group = _group_of(identity)
    related = [(u, s) for u, s in EXERCISES if u != page.arg and _group_of(_identity_for_media_slug(s)) == group][:6]
    related_html = ""
    if related:
        related_html = (f"<section class=reveal><h2>{_e(t('seo.exercise.related.h', group=_group_for(identity, lang)))}</h2>"
                        f"<div class='grid ex-grid'>{''.join(_exercise_card(u, s, lang, show_group=False) for u, s in related)}</div></section>")
    lead = t(f"seo.ex.{slug}.lead")
    main_col = (
        f"<span class=eyebrow>{_e(_group_for(identity, lang))}</span>"
        f"<h1>{_e(name)}</h1><p class=lead>{_e(lead)}</p>{frames}"
        f"<section class=reveal><h2>{_e(t('seo.exercise.setup.h'))}</h2><p>{_e(t(f'seo.ex.{slug}.setup'))}</p></section>"
        f"<section class=reveal><h2>{_e(t('seo.exercise.steps.h'))}</h2><ol class=steps>{steps}</ol></section>"
        f"<section class='mistakes reveal'><h2>{_e(t('seo.exercise.mistakes.h'))}</h2><ul>{mistakes}</ul></section>"
        + _muscles_worked(lang, page, identity)
        + seo_swaps.teaser(page.arg, lang)
        + in_programs
    )
    body = (
        f"<div class=ex-layout><div class=ex-main>{main_col}</div>{_exercise_aside(lang, page, identity, request)}</div>"
        + _cta(lang, page.block, _e(t("seo.exercise.cta")), request)
        + related_html
        + f'<p style="margin-top:32px"><a href="{url_for(Page("exercises"), lang)}">{_e(t("seo.exercise.all"))}</a></p>'
        + f"<p class=muted>{t('seo.exercise.credit_html')}</p>"
    )
    home = base + url_for(Page("home"), lang)
    article = {
        "@context": "https://schema.org", "@type": "Article",
        "headline": t("seo.exercise.title", name=name)[:110], "description": lead, "inLanguage": hreflang(lang),
        "image": [base + _media_url(exercise_media.frame_name(slug, 1)), base + _media_url(exercise_media.frame_name(slug, 2))],
        "mainEntityOfPage": base + url_for(page, lang),
        "author": {"@type": "Organization", "name": t("seo.brand"), "url": home},
        "publisher": {"@type": "Organization", "name": t("seo.brand"), "url": home},
        "about": {"@type": "Thing", "name": name},
    }
    return _layout(lang, page, request, title=t("seo.exercise.title", name=name), description=lead, body=body,
                   ld=[article], wide=True)


# Отметка в таблице сравнения: yes — есть (или бесплатно), part — частично
# или за деньги, no — нет. Наша колонка — всегда по
# коду: всё бесплатно, тренер, импорт, видео.
_VS_MARKS = {
    "us": {"price": "yes", "free": "yes", "ai": "yes", "import": "yes", "video": "yes"},
    "hevy": {"price": "part", "free": "part", "ai": "part", "import": "yes", "video": "no"},
    "strong": {"price": "part", "free": "part", "ai": "no", "import": "no", "video": "no"},
    "fitbod": {"price": "no", "free": "no", "ai": "yes", "import": "part", "video": "no"},
    "jefit": {"price": "part", "free": "part", "ai": "yes", "import": "no", "video": "no"},
}
_MARK_GLYPH = {"yes": "✓", "part": "≈", "no": "✕"}


def _mark(lang: str, kind: str) -> str:
    return f'<span class="mark {kind}" role=img aria-label="{_e(_t(lang)(f"seo.vs.mark.{kind}"))}">{_MARK_GLYPH[kind]}</span>'


def _render_vs(lang: str, page: Page, request) -> str:
    t = _t(lang)
    rival = page.arg
    params = _article_params(lang)
    us_name, rival_name = t("seo.brand"), t(f"seo.vs.{rival}.name")
    rows = "".join(
        f"<tr><th scope=row>{_e(t(f'seo.vs.row.{row}'))}</th>"
        f"<td class=us data-label=\"{_e(us_name)}\">{_mark(lang, _VS_MARKS['us'][row])}{t(f'seo.vs.us.{row}_html', **params)}</td>"
        f"<td data-label=\"{_e(rival_name)}\">{_mark(lang, _VS_MARKS[rival][row])}{t(f'seo.vs.{rival}.{row}_html')}</td></tr>"
        for row in _VS_ROWS
    )
    sources = "".join(f'<li><a href="{_e(u)}" rel=nofollow>{_e(u)}</a></li>' for u in _VS_SOURCES[rival])
    faq = _faq_items(lang, FAQ[f"vs:{rival}"])
    move_href = url_for(Page("import", rival if rival in IMPORT_SOURCES else "csv"), lang)
    title, description = t(f"seo.vs.{rival}.title"), t(f"seo.vs.{rival}.description")
    body = (
        f"<span class=eyebrow>{_e(t('seo.eyebrow.vs'))}</span>"
        f"<h1>{_e(t(f'seo.vs.{rival}.h1'))}</h1><p class=lead>{_e(t(f'seo.vs.{rival}.lead'))}</p>"
        f"<p class=muted>{_e(t('seo.vs.asof'))}</p>"
        f"<div class='table-card vs cards-mobile'><table><thead><tr><th></th><th class=us>{_e(us_name)}</th>"
        f"<th>{_e(rival_name)}</th></tr></thead><tbody>{rows}</tbody></table></div>"
        f"<section class=reveal><h2>{_e(t('seo.vs.choose.h'))}</h2>{t(f'seo.vs.{rival}.choose_html')}</section>"
        f"<section class=reveal><h2>{_e(t('seo.vs.move.h'))}</h2>"
        f'<p>{t(f"seo.vs.{rival}.move_html" if rival not in IMPORT_SOURCES else "seo.vs.move_html", href=move_href, name=_e(rival_name))}</p></section>'
        + _faq_html(lang, faq)
        + _cta(lang, page.block, _e(t("seo.vs.cta")), request)
        + f"<section><h2>{_e(t('seo.vs.sources.h'))}</h2><ul class=muted>{sources}</ul>"
        f"<p class=muted>{_e(t('seo.vs.disclaimer_one', name=rival_name))}</p></section>"
    )
    ld = [_webpage_ld(lang, page, request, title, description), _faq_ld(faq)]
    return _layout(lang, page, request, title=title, description=description, body=body, ld=ld)


_RENDERERS: dict[str, Callable[[str, Page, Optional[Request]], str]] = {
    "home": _render_home,
    "ai-coach": _render_feature,
    "video-review": _render_feature,
    "telegram": _render_feature,
    "workout-log-app": _render_feature,
    "import": _render_import,
    "tools": _render_tools,
    "tool": _render_tool,
    "strength": _render_strength,
    "strength_lift": _render_strength_lift,
    "programs": _render_programs,
    "program": _render_program,
    "exercises": _render_exercises,
    "exercise": _render_exercise,
    "vs": _render_vs,
}


def _strength_bw_renderer(lang: str, page: Page, request) -> str:
    import seo_strength_bw

    return seo_strength_bw.RENDERERS["strength_bw"](lang, page, request)


_RENDERERS["strength_bw"] = _strength_bw_renderer


def render(page: Page, lang: str, request: Optional[Request] = None) -> str:
    renderer = _RENDERERS.get(page.kind)
    if renderer is None:
        import seo_library

        renderer = seo_library.renderers()[page.kind]
    return renderer(lang, page, request)


# ---------- 404 ----------


def render_not_found(lang: str, request: Optional[Request] = None) -> str:
    t = _t(lang)
    page = Page("home")
    links = [(url_for(Page("home"), lang), t("seo.crumb.home"), t("seo.notfound.home")),
             (url_for(Page("tools"), lang), t("seo.tools.h1"), t("seo.tools.lead")),
             (url_for(Page("exercises"), lang), t("seo.exercises.h1"), t("seo.exercises.lead")),
             (url_for(Page("programs"), lang), t("seo.programs.h1"), t("seo.programs.lead"))]
    body = (
        f"<div class=notfound>{sprite('coach', 2, t('seo.notfound.coach_alt'))}<div>"
        f"<span class=eyebrow>404</span><h1>{_e(t('seo.notfound.h1'))}</h1>"
        f"<p class=lead>{_e(t('seo.notfound.p'))}</p></div></div>" + _cards(links)
    )
    html = _layout(lang, page, request, title=t("seo.notfound.title"), description=t("seo.notfound.p"), body=body)
    # Маячок «страницу видел браузер» — только у страниц, которые считает
    # сервер; 404 не считается и не должен подтверждать главную.
    html = html.replace(site_stats.beacon_script(url_for(page, lang)), "", 1)
    # Без canonical и hreflang: у несуществующей страницы их нет.
    html = re.sub(r"<link rel=(?:canonical|alternate)[^>]*>", "", html)
    return html.replace("<head><meta charset=utf-8>", "<head><meta charset=utf-8><meta name=robots content=noindex>", 1)


def _not_found_route(lang: str):
    async def route(request: Request) -> HTMLResponse:
        return HTMLResponse(render_not_found(lang, request), status_code=404,
                            headers={"Cache-Control": "no-store", "Content-Language": hreflang(lang), **_SECURITY, **_NO_FRAME})

    return route


# ---------- встраиваемые калькуляторы ----------


def render_embed(tool: str, lang: str, request: Optional[Request] = None) -> str:
    t = _t(lang)
    form, result, script = tool_parts(tool, lang, embed=True)
    site = base_url(request) + url_for(Page("tool", tool), lang)
    return (
        f"<!doctype html><html lang={hreflang(lang)}><head><meta charset=utf-8>"
        '<meta name=viewport content="width=device-width, initial-scale=1">'
        f"<title>{_e(t(f'seo.tool.{tool}.h1'))}</title><meta name=robots content=noindex>"
        f'<link rel=canonical href="{_e(site)}"><style>{_css()}</style></head>'
        f"<body class=embed><main><div class=wrap><div class=tool>{form}{result}</div>"
        f'<span class=powered>{t("seo.embed.powered_html", href=_e(site))}</span></div></main>'
        f"{_calc_script(lang, script)}</body></html>"
    )


# ---------- служебные адреса ----------


def page_images(page: Page, lang: str) -> list[str]:
    """Картинки страницы для image-sitemap: кадры упражнений и снимки приложения."""
    if page.kind == "home":
        shots = seo_static.screens_lang(lang)
        return [seo_static.asset_url(f"screens/{shots}-{n}.jpg") for n, _w, _h in seo_static.SCREENS[shots]]
    if page.kind == "exercise":
        slug = _EXERCISE_BY_URL[page.arg]
        return [_media_url(exercise_media.frame_name(slug, 1)), _media_url(exercise_media.frame_name(slug, 2))]
    if page.kind == "pair":
        import seo_library

        return [_media_url(exercise_media.frame_name(_EXERCISE_BY_URL[u], 1)) for u in seo_library.pair_exercises(page.arg)]
    if page.kind == "exercise_alt":
        import seo_catalog
        import seo_swaps

        alts = seo_catalog.swaps(seo_swaps.identity_of(page.arg))
        return [_media_url(exercise_media.frame_name(exercise_media.EXERCISE_IMAGE_SLUGS[a], 2)) for a in alts]
    return []


def sitemap_xml(request: Optional[Request] = None) -> str:
    base = base_url(request)
    out = ['<?xml version="1.0" encoding="UTF-8"?>',
           '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9" xmlns:xhtml="http://www.w3.org/1999/xhtml" '
           'xmlns:image="http://www.google.com/schemas/sitemap-image/1.1">']
    for page in pages():
        if page.kind in THIN_KINDS:
            continue
        alternates = "".join(
            f'<xhtml:link rel="alternate" hreflang="{hreflang(code)}" href="{escape(base + url_for(page, code))}"/>'
            for code in INDEXED_LANGS
        ) + f'<xhtml:link rel="alternate" hreflang="x-default" href="{escape(base + url_for(page, X_DEFAULT_LANG))}"/>'
        for lang in INDEXED_LANGS:
            images = "".join(f"<image:image><image:loc>{escape(base + src)}</image:loc></image:image>"
                             for src in page_images(page, lang))
            out.append(f"<url><loc>{escape(base + url_for(page, lang))}</loc><lastmod>{page_updated(page)}</lastmod>"
                       f"{alternates}{images}</url>")
    out.append("</urlset>")
    return "\n".join(out)


# Роботы поиска и AI-ответов, которым сайт открыт явно: тот же «Allow: /», что
# и всем, но отдельной группой — чтобы правка общего правила их не задела.
AI_CRAWLERS = ("GPTBot", "OAI-SearchBot", "ChatGPT-User", "PerplexityBot", "Perplexity-User",
               "ClaudeBot", "Claude-User", "Claude-SearchBot", "Google-Extended", "Applebot-Extended", "bingbot")


def robots_txt(request: Optional[Request] = None) -> str:
    # /go/* — счётчик клика и редирект в бота или App Store, а не страница:
    # без запрета робот обходил их и записывал в «Страница с переадресацией».
    named = "".join(f"User-agent: {name}\n" for name in AI_CRAWLERS)
    rules = "Disallow: /go/\nAllow: /\n"
    return (f"User-agent: *\n{rules}\n{named}{rules}\n"
            f"Sitemap: {base_url(request)}/sitemap.xml\n")


def llms_txt(request: Optional[Request] = None) -> str:
    """/llms.txt — короткая справка о продукте для поиска внутри AI-ассистентов:
    что это, что умеет, сколько стоит и куда вести. Только факты из каталога
    и config; ссылки — на оба языка."""
    base = base_url(request)
    out: list[str] = []
    for lang in ("en", "ru"):
        t = _t(lang)
        p = _article_params(lang)
        out.append(f"# {t('seo.brand')}" if lang == "en" else f"## {t('seo.brand')}")
        out += ["", f"> {_plain(t('seo.llms.summary'))}", "", f"### {t('seo.llms.features')}"]
        out += [f"- {_plain(t(f'seo.llms.f.{k}', **p))}" for k in ("log", "coach", "video", "progress", "programs", "import", "platforms")]
        out += ["", f"### {t('seo.llms.pricing')}", f"- {_plain(t('seo.llms.price', **p))}", "", f"### {t('seo.llms.links')}"]
        store = app_store_link(LLMS_BLOCK)
        if store:
            out.append(f"- [{t('seo.cta.app_store')}]({store})")
        out.append(f"- [{t('seo.cta.bot')}](https://t.me/{bot_username()}?start={bot_payload(LLMS_BLOCK)})")
        main = [Page("home"), *[Page(f) for f in FEATURES], *[Page("import", s) for s in IMPORT_SOURCES], Page("tools"),
                *[Page("tool", s) for s in TOOLS], Page("strength"), Page("programs"), Page("splits"), Page("exercises"),
                Page("muscles"), Page("guides"), Page("glossary"), Page("compare"),
                *[Page("template", s) for s in TEMPLATES],
                *[Page("vs", c) for c in COMPETITORS], *[Page("alternatives", c) for c in ALTERNATIVES], Page("about")]
        for pg in main:
            out.append(f"- [{_strip_emoji(page_h1(pg, lang))}]({base}{url_for(pg, lang)})")
        out.append("")
    out.append(f"Full version: {base}/llms-full.txt")
    out.append(f"Sitemap: {base}/sitemap.xml")
    return "\n".join(out) + "\n"


def llms_full_txt(request: Optional[Request] = None) -> str:
    """/llms-full.txt — подробная справка: факты о продукте (только из
    каталога и config), потолки, платформы, переезд, и ВСЕ страницы сайта
    с заголовками — на обоих языках."""
    base = base_url(request)
    out: list[str] = []
    for lang in ("en", "ru"):
        t = _t(lang)
        p = _article_params(lang)
        out.append(f"# {t('seo.brand')} — {t('seo.llms.full')}" if lang == "en" else f"## {t('seo.brand')} — {t('seo.llms.full')}")
        out += ["", f"> {_plain(t('seo.llms.summary'))}", "", f"### {t('seo.llms.features')}"]
        out += [f"- {_plain(t(f'seo.llms.f.{k}', **p))}" for k in ("log", "coach", "video", "progress", "programs", "import", "platforms")]
        out += [f"- {_plain(t(f'seo.llms.ff.{k}', **p))}" for k in ("history", "limits", "privacy", "units", "export", "developer")]
        out += ["", f"### {t('seo.llms.pricing')}", f"- {_plain(t('seo.llms.price', **p))}", ""]
        out.append(f"### {t('seo.llms.links')}")
        store = app_store_link(LLMS_BLOCK)
        if store:
            out.append(f"- [{t('seo.cta.app_store')}]({store})")
        out.append(f"- [{t('seo.cta.bot')}](https://t.me/{bot_username()}?start={bot_payload(LLMS_BLOCK)})")
        out += ["", f"### {t('seo.llms.pages')}"]
        for pg in pages():
            out.append(f"- [{_strip_emoji(page_h1(pg, lang))}]({base}{url_for(pg, lang)})")
        out.append("")
    out.append(f"Sitemap: {base}/sitemap.xml")
    return "\n".join(out) + "\n"


_SECURITY = {"X-Content-Type-Options": "nosniff", "Referrer-Policy": "strict-origin-when-cross-origin"}
_NO_FRAME = {"X-Frame-Options": "DENY", "Content-Security-Policy": "frame-ancestors 'none'"}


def _html(content: str, lang: str) -> HTMLResponse:
    return HTMLResponse(content, headers={
        "Cache-Control": "public, max-age=3600", "Content-Language": hreflang(lang), "Vary": "User-Agent",
        **_SECURITY, **_NO_FRAME,
    })


async def _count(kind: str, page_key: str, lang: Optional[str], request: Request, target: Optional[str] = None) -> None:
    await site_stats.record(
        kind, page_key, lang, user_agent=request.headers.get("user-agent"), referer=request.headers.get("referer"),
        own_hosts=_own_hosts(request), utm=request.query_params.get("utm_source"), target=target,
        headers=request.headers,
    )


async def seen_route(request: Request) -> Response:
    """`POST /go/seen` — маячок страницы (site_stats.beacon_script): браузер
    показал страницу человеку. Ключ страницы — тот же, что у серверного
    `view`; источник — домен из `document.referrer`. Чужой сайт маячок не
    подделает из браузера (Sec-Fetch-Site), робот не засчитывается
    (site_stats.record_seen). Ответ всегда 204 — странице он не нужен."""
    params = request.query_params
    page = params.get("p", "")
    fetch_site = request.headers.get("sec-fetch-site")
    if site_stats.valid_page_key(page, (acquisition.REFERRAL_WEB_PAGE_KEY,)) and fetch_site in (None, "same-origin"):
        lang = lang_of_path(page) or lang_from_code(params.get("l", ""))
        await site_stats.record_seen(
            page, lang, user_agent=request.headers.get("user-agent"), referer=params.get("r") or None,
            own_hosts=_own_hosts(request), utm=params.get("utm_source"),
        )
    return Response(status_code=204, headers={"Cache-Control": "no-store", **_SECURITY})


def _page_route(page: Page, lang: str):
    async def route(request: Request) -> HTMLResponse:
        if page.kind == "home":
            import site_numbers

            await site_numbers.refresh()
        html = render(page, lang, request)
        await _count("view", url_for(page, lang), lang, request)
        return _html(html, lang)

    return route


def _embed_route(tool: str, lang: str):
    async def route(request: Request) -> HTMLResponse:
        html = render_embed(tool, lang, request)
        await _count("embed", embed_url(tool, lang), lang, request)
        return HTMLResponse(html, headers={
            "Cache-Control": "public, max-age=3600", "Content-Language": hreflang(lang), **_SECURITY,
            "Content-Security-Policy": "frame-ancestors *",
        })

    return route


def _click_page(request: Request, block: str) -> str:
    """Откуда клик: путь нашей страницы из Referer, без него — метка блока."""
    referer = request.headers.get("referer") or ""
    try:
        parts = urlsplit(referer)
        host = (parts.hostname or "").removeprefix("www.")
    except ValueError:
        return f"block:{block}"
    if host and host in _own_hosts(request) and parts.path:
        return parts.path[:120]
    return f"block:{block}"


def _block_arg(request: Request) -> str:
    block = request.query_params.get("f", "")
    return block if block in _KNOWN_BLOCKS else _BLOCKS["home"]


async def go_bot_route(request: Request) -> Response:
    block = _block_arg(request)
    await _count("click", _click_page(request, block), None, request, target="bot")
    return RedirectResponse(bot_link(block), status_code=302, headers={"Cache-Control": "no-store", **_SECURITY})


async def go_diary_route(request: Request) -> Response:
    """«Запиши в дневник» под калькулятором: куда — решает устройство (как
    главная кнопка страницы), язык — по странице, с которой пришли."""
    referer_path = urlsplit(request.headers.get("referer") or "").path
    lang = lang_of_path(referer_path) or "ru"
    target = diary_target(lang, device_of(request))
    if target == "appstore":
        return await go_appstore_route(request)
    return await go_bot_route(request)


async def go_appstore_route(request: Request) -> Response:
    block = _block_arg(request)
    store = config.app_store_url()
    if not store:
        return RedirectResponse(go_url("bot", block), status_code=302, headers={"Cache-Control": "no-store"})
    await _count("click", _click_page(request, block), None, request, target="appstore")
    return RedirectResponse(app_store_link(block) or store, status_code=302, headers={"Cache-Control": "no-store", **_SECURITY})


async def root_route(request: Request) -> Response:
    lang = legal_pages.page_lang(request)
    return RedirectResponse(url_for(Page("home"), lang), status_code=302, headers={"Vary": "Accept-Language"})


async def sitemap_route(request: Request) -> Response:
    return Response(sitemap_xml(request), media_type="application/xml",
                    headers={"Cache-Control": "public, max-age=3600"})


async def robots_route(request: Request) -> Response:
    return PlainTextResponse(robots_txt(request), headers={"Cache-Control": "public, max-age=3600"})


async def llms_route(request: Request) -> Response:
    return PlainTextResponse(llms_txt(request), media_type="text/markdown; charset=utf-8",
                             headers={"Cache-Control": "public, max-age=3600"})


async def llms_full_route(request: Request) -> Response:
    return PlainTextResponse(llms_full_txt(request), media_type="text/markdown; charset=utf-8",
                             headers={"Cache-Control": "public, max-age=3600"})


def _template_file_route(lang: str, name: str):
    import seo_template

    media_type, build = seo_template.FILES[name]

    async def route(request: Request) -> Response:
        data = build(lang)
        # Скачивание — клик с целью «template»: /growth видит его рядом с кликами в бота.
        await _count("click", _click_page(request, _BLOCKS["template"]), None, request, target="template")
        return Response(data, media_type=media_type, headers={
            "Cache-Control": "public, max-age=86400", "Content-Disposition": f'attachment; filename="{name}"',
            "X-Content-Type-Options": "nosniff",
        })

    return route


async def og_image_route(request: Request) -> Response:
    return FileResponse(_OG_IMAGE_FILE, media_type="image/jpeg",
                        headers={"Cache-Control": "public, max-age=86400"})


def page_by_key(key: str) -> Optional[Page]:
    for page in pages():
        if page.key == key:
            return page
    return None


def og_png(page: Page, lang: str) -> bytes:
    t = _t(lang)
    title = _strip_emoji(page_h1(page, lang))
    eyebrow = t("bot.name")
    footer = urlsplit(config.site_base_url() or "").hostname or t("seo.brand")
    digest = hashlib.sha1(f"{title}|{eyebrow}|{footer}".encode()).hexdigest()[:10]
    return seo_static.og_png(f"{lang}-{page.key}-{digest}", title, eyebrow, footer)


async def og_page_route(request: Request) -> Response:
    lang = lang_from_segment(request.path_params.get("lang", ""))
    page = page_by_key(request.path_params.get("key", ""))
    if lang is None or page is None:
        return PlainTextResponse("Not found", status_code=404)
    data = await asyncio.to_thread(og_png, page, lang)
    return Response(data, media_type="image/png", headers={"Cache-Control": "public, max-age=86400"})


async def manifest_route(request: Request) -> Response:
    lang = lang_from_code(request.query_params.get("lang", "")) or X_DEFAULT_LANG
    t = _t(lang)
    body = seo_static.webmanifest(hreflang(lang), t("seo.brand"), t("seo.brand"), t("seo.home.description"),
                                  start_url=url_for(Page("home"), lang))
    return Response(body, media_type="application/manifest+json", headers={"Cache-Control": "public, max-age=86400"})


def verification_file_name() -> Optional[str]:
    """`google<токен>.html` из GOOGLE_SITE_VERIFICATION_FILE — только такое имя:
    переменная не должна открывать произвольный путь."""
    name = (config.GOOGLE_SITE_VERIFICATION_FILE or "").strip().lstrip("/")
    return name if re.fullmatch(r"google[0-9a-zA-Z]+\.html", name) else None


def _moved(target: str):
    async def route(request: Request) -> Response:
        return RedirectResponse(target, status_code=301, headers={"Cache-Control": "public, max-age=86400"})

    return route


def register_routes(server: Any) -> None:
    for page in pages():
        for lang in LANGS:
            server.custom_route(url_for(page, lang), methods=["GET"])(_page_route(page, lang))
    for tool in EMBEDS:
        for lang in LANGS:
            server.custom_route(embed_url(tool, lang), methods=["GET"])(_embed_route(tool, lang))
    for lang in LANGS:
        for tool, target in REMOVED_TOOLS.items():
            server.custom_route(f"/{lang_segment(lang)}/tools/{tool}", methods=["GET"])(_moved(f"/{lang_segment(lang)}{target}"))
        for tool, target in REMOVED_EMBEDS.items():
            server.custom_route(embed_url(tool, lang), methods=["GET"])(_moved(embed_url(target, lang)))
    server.custom_route("/", methods=["GET"])(root_route)
    server.custom_route("/go/bot", methods=["GET"])(go_bot_route)
    server.custom_route("/go/appstore", methods=["GET"])(go_appstore_route)
    server.custom_route("/go/diary", methods=["GET"])(go_diary_route)
    server.custom_route(site_stats.SEEN_PATH, methods=["POST"])(seen_route)
    server.custom_route("/sitemap.xml", methods=["GET"])(sitemap_route)
    server.custom_route("/robots.txt", methods=["GET"])(robots_route)
    server.custom_route("/llms.txt", methods=["GET"])(llms_route)
    server.custom_route("/llms-full.txt", methods=["GET"])(llms_full_route)
    import seo_template

    for lang in LANGS:
        for name in seo_template.FILES:
            server.custom_route(template_file_url(name, lang), methods=["GET"])(_template_file_route(lang, name))
    import seo_library

    seo_library.register_routes(server)
    server.custom_route(OG_IMAGE_PATH, methods=["GET"])(og_image_route)
    server.custom_route("/og/{lang}/{key}.png", methods=["GET"])(og_page_route)
    server.custom_route("/site/{path:path}", methods=["GET"])(seo_static.site_route)
    server.custom_route("/favicon.ico", methods=["GET"])(seo_static.root_file("icons/favicon.ico"))
    server.custom_route("/apple-touch-icon.png", methods=["GET"])(seo_static.root_file("icons/apple-touch-icon.png"))
    server.custom_route("/site.webmanifest", methods=["GET"])(manifest_route)
    name = verification_file_name()
    if name:
        async def verification_route(request: Request) -> Response:
            return PlainTextResponse(f"google-site-verification: {name}")

        server.custom_route(f"/{name}", methods=["GET"])(verification_route)
    # IndexNow: ключ — файл по адресу `/<ключ>.txt` (scripts/indexnow_ping.py),
    # только когда ключ задан.
    key = seo_static.indexnow_key()
    if key:
        async def indexnow_route(request: Request) -> Response:
            return PlainTextResponse(key, headers={"Cache-Control": "public, max-age=86400"})

        server.custom_route(f"/{key}.txt", methods=["GET"])(indexnow_route)
    # Последними: любой другой адрес под языком сайта (/ru/, /pt-pt/…) — своя страница 404.
    for lang in LANGS:
        server.custom_route(f"/{lang_segment(lang)}/{{rest:path}}", methods=["GET"])(_not_found_route(lang))
