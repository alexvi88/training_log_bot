"""Публичные страницы для поиска: переезд из Hevy/Strong, калькуляторы,
нормативы силы, готовые программы, техника упражнений, рассказ о боте и
сравнения.

Зачем: первое касание человека, который ищет «как перенести тренировки из
Hevy» или «калькулятор 1ПМ», — поиск (и поиск внутри ChatGPT/Perplexity), а
не каталог ботов. Кнопки ведут через `/go/bot?f=<блок>` и
`/go/appstore?f=<блок>`: там клик считается (site_stats) и уходит
редиректом на `t.me/<бот>?start=src_seo_<блок>` (acquisition.py) или в App
Store — так `/growth` видит и клики, и тех, кто дошёл до первой тренировки.

Язык — в пути: `/ru/...` и `/en/...`. У каждой страницы ровно один адрес на
язык, его и называет `canonical`, а пары связаны `hreflang` (x-default —
английская). Голый `/` — выбор языка тем же `legal_pages.page_lang` и
редирект.

Кнопка «скачать» зависит от устройства (по User-Agent, на сервере, поэтому
`Vary: User-Agent`): iPhone и iPad — App Store первой, Android — бот, на
компьютере — QR-код в App Store и кнопки. На английских страницах App Store
главный везде, кроме Android. Обе ссылки есть в любом варианте — робот,
который пришёл без браузерного User-Agent, видит обе.

Висят на приложении MCP-сервера через `custom_route`, как /privacy
(legal_pages.py). Картинки упражнений — уже публичный
`/v1/media/exercises/<файл>`; своя статика — `/site/*` (seo_static.py).

Тексты — каталог (`seo.*` в locales/ru.json и en.json), данные каталога —
через те же локализаторы, что у бота и /v1 (seed_data.localized_*,
exercise_descriptions.get_description): русское имя упражнения здесь только
идентичность для поиска картинки и описания, на экран идёт локализованное.
Строк, вычисленных на импорте, нет — всё собирается в момент запроса.

Ключи с суффиксом `_html` — доверенная разметка из каталога (списки, ссылки,
жирный) и подставляются как есть; остальные экранируются.

Аналитики в браузере нет: ни счётчиков, ни cookies, ни сторонних скриптов.
Посещение считает сервер (site_stats.py) — без адреса клиента и без связи с
аккаунтом; политика конфиденциальности это описывает.

Встраиваемые калькуляторы (`/embed/<язык>/1rm`, `/embed/<язык>/plates`) —
единственные адреса, которые разрешено показывать в чужом iframe
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

import config
import exercise_descriptions
import exercise_media
import i18n
import legal_pages
import qr_code
import seed_data
import seo_design
import seo_static
import seo_tools
import site_stats

LANGS = ("ru", "en")
X_DEFAULT_LANG = "en"
BOT_TAG_PREFIX = "src_seo_"

# Упражнения с отдельной страницей техники: адрес → слаг картинок
# (exercise_media.EXERCISE_IMAGE_SLUGS). Только те, у которых есть пара
# кадров, описание на обоих языках и свой текст (вступление, постановка,
# ошибки) — это держит тест. Не больше 80: страница без своего текста —
# тонкая, и поиск за такие наказывает.
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
)
_EXERCISE_BY_URL = dict(EXERCISES)
# Упражнение в демо «подход строкой» на главной — по слагу картинок, не по имени.
_DEMO_SLUG = "barbell_bench_press_medium_grip"

IMPORT_SOURCES = ("hevy", "strong", "csv")
TOOLS = ("1rm", "plates", "warmup", "rpe", "wilks")
EMBEDS = ("1rm", "plates")
COMPETITORS = ("hevy", "strong")
FEATURES = ("ai-coach", "video-review", "telegram")
# Нормативы: адрес → ключ упражнения в seo_tools.LIFT_SHARE и адрес техники.
STRENGTH_LIFTS = (("bench-press", "bench", "bench-press"), ("squat", "squat", "back-squat"),
                  ("deadlift", "deadlift", "deadlift"))
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
    "tool:plates": "plates",
    "tool:wilks": "wilks",
    "tool:warmup": "warmup",
    "tool:rpe": "rpe",
    "log:1rm": "rm1_log",
    "log:plates": "plt_log",
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
}
# Метка для ссылки из llms.txt: страницы у неё нет.
LLMS_BLOCK = "llms"
_KNOWN_BLOCKS = frozenset(_BLOCKS.values()) | {LLMS_BLOCK}

# Сколько смысловых блоков у статьи-страницы (ключи seo.<id>.s<n>.h / .p_html).
_ARTICLE_SECTIONS = {"ai-coach": 4, "video-review": 4, "telegram": 6}

# Вопросы FAQ по страницам (ключи seo.faq.<id>.q / .a_html). Ответы — только
# то, что делает код: числа потолков — из config через _article_params.
FAQ = {
    "home": ("what", "free", "alt_hevy", "move", "video", "platforms"),
    "ai-coach": ("what", "history", "free", "video", "platforms"),
    "import": ("move", "dupes", "undo", "free"),
    "vs:hevy": ("alt_hevy", "move", "free", "video"),
    "vs:strong": ("move", "free", "video", "platforms"),
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
        if self.kind == "import":
            return f"/import/{self.arg}"
        if self.kind == "tools":
            return "/tools"
        if self.kind == "tool":
            return f"/tools/{self.arg}"
        if self.kind == "strength":
            return "/strength"
        if self.kind == "strength_lift":
            return f"/strength/{self.arg}/{self.arg2}"
        if self.kind == "programs":
            return "/programs"
        if self.kind == "program":
            return f"/programs/{self.arg}"
        if self.kind == "exercises":
            return "/exercises"
        if self.kind == "exercise":
            return f"/exercises/{self.arg}"
        if self.kind == "vs":
            return f"/vs/{self.arg}"
        raise ValueError(self.kind)

    @property
    def key(self) -> str:
        """Ключ страницы без языка: `home`, `tools-1rm` — для картинки превью."""
        return self.path.strip("/").replace("/", "-") or "home"

    @property
    def block(self) -> str:
        if self.kind in ("import", "tool", "vs"):
            return _BLOCKS[f"{self.kind}:{self.arg}"]
        if self.kind == "strength_lift":
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
    result.append(Page("programs"))
    result += [Page("program", p["key"]) for p in seed_data.WORKOUT_PROGRAMS]
    result.append(Page("exercises"))
    result += [Page("exercise", url) for url, _slug in EXERCISES]
    result += [Page("vs", c) for c in COMPETITORS]
    return result


def url_for(page: Page, lang: str) -> str:
    return f"/{lang}{page.path}"


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
    """Вес для таблицы: число вплотную к единице (TONE_OF_VOICE), зазор — стилем."""
    return f"{_num(round(value, 1))}<span class=u>{_e(unit_label)}</span>"


def _primary_store(lang: str, device: str) -> bool:
    """Главная кнопка — App Store? iPhone — да; Android — нет; остальное —
    по языку: на английских страницах главный App Store, на русских — бот."""
    if device == "ios":
        return True
    if device == "android":
        return False
    return lang == "en"


def _cta_buttons(lang: str, block: str, device: str) -> str:
    t = _t(lang)
    bot = (f'<a class="btn{{cls}}" href="{_e(go_url("bot", block))}">{seo_design.icon("telegram")}'
           f'{_e(t("seo.cta.bot"))}</a>')
    store_url = config.app_store_url()
    if not store_url:
        return '<div class=btn-row>' + bot.replace("{cls}", " primary") + "</div>"
    store = (f'<a class="btn{{cls}}" href="{_e(go_url("appstore", block))}">{seo_design.icon("apple")}'
             f'{_e(t("seo.cta.app_store"))}</a>')
    if device == "ios":
        return ('<div class=btn-row>' + store.replace("{cls}", " primary") + "</div>"
                f'<p class=alt-link>{t("seo.cta.or_bot_html", href=_e(go_url("bot", block)))}</p>')
    if device == "android":
        return ('<div class=btn-row>' + bot.replace("{cls}", " primary") + "</div>"
                f'<p class=alt-link>{t("seo.cta.or_store_html", href=_e(go_url("appstore", block)))}</p>')
    if _primary_store(lang, device):
        return '<div class=btn-row>' + store.replace("{cls}", " primary") + bot.replace("{cls}", "") + "</div>"
    return '<div class=btn-row>' + bot.replace("{cls}", " primary") + store.replace("{cls}", "") + "</div>"


def _qr(lang: str, block: str, request: Optional[Request]) -> str:
    t = _t(lang)
    target = base_url(request) + go_url("appstore", block)
    return (f'<div class=qr role=img aria-label="{_e(t("seo.cta.qr_alt"))}">{qr_code.svg(target)}'
            f"<small>{_e(t('seo.cta.qr'))}</small></div>")


def _cta(lang: str, block: str, text: str, request: Optional[Request]) -> str:
    device = device_of(request)
    show_qr = device == "desktop" and bool(config.app_store_url())
    body = (_qr(lang, block, request) if show_qr else "") + f"<div>{_cta_buttons(lang, block, device)}</div>"
    return (f'<div class="cta reveal"><p class=cta-text>{text}</p>'
            f'<div class="cta-body{" has-qr" if show_qr else ""}">{body}</div></div>')


def _head_cta(lang: str, block: str, device: str) -> str:
    t = _t(lang)
    if config.app_store_url() and _primary_store(lang, device):
        return (f'<a class="btn small primary head-cta" href="{_e(go_url("appstore", block))}">'
                f'{_e(t("seo.cta.head_store"))}</a>')
    return f'<a class="btn small primary head-cta" href="{_e(go_url("bot", block))}">{_e(t("seo.cta.head_bot"))}</a>'


def _nav_pages() -> tuple[tuple[Page, str], ...]:
    return (
        (Page("import", "hevy"), "seo.nav.import"),
        (Page("tools"), "seo.nav.tools"),
        (Page("strength"), "seo.nav.strength"),
        (Page("programs"), "seo.nav.programs"),
        (Page("exercises"), "seo.nav.exercises"),
        (Page("ai-coach"), "seo.nav.ai"),
    )


def _nav_links(lang: str, current: Page) -> str:
    t = _t(lang)
    section = current.path.split("/")[1:2]
    links = []
    for page, key in _nav_pages():
        here = " aria-current=page" if page.path.split("/")[1:2] == section else ""
        links.append(f'<a href="{url_for(page, lang)}"{here}>{_e(t(key))}</a>')
    return "".join(links)


def _crumbs(lang: str, page: Page) -> list[tuple[str, Page]]:
    """Хлебные крошки: (подпись, страница) от главной до текущей."""
    t = _t(lang)
    home = (t("seo.crumb.home"), Page("home"))
    if page.kind == "home":
        return [home]
    trail = [home]
    if page.kind == "tool":
        trail.append((t("seo.tools.h1"), Page("tools")))
    elif page.kind == "strength_lift":
        trail.append((t("seo.strength.h1"), Page("strength")))
    elif page.kind == "program":
        trail.append((t("seo.programs.h1"), Page("programs")))
    elif page.kind == "exercise":
        trail.append((t("seo.exercises.h1"), Page("exercises")))
    trail.append((_strip_emoji(page_h1(page, lang)), page))
    return trail


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


def _footer(lang: str, page: Page, request: Optional[Request]) -> str:
    t = _t(lang)
    other = "en" if lang == "ru" else "ru"

    def links(items: list[tuple[Page, str]]) -> str:
        return "<ul>" + "".join(f'<li><a href="{url_for(p, lang)}">{_e(label)}</a></li>' for p, label in items) + "</ul>"

    product = [(Page(f), _strip_emoji(t(f"seo.{f}.card.title"))) for f in FEATURES]
    product += [(Page("vs", c), t(f"seo.vs.{c}.short")) for c in COMPETITORS]
    tools = [(Page("tool", s), t(f"seo.tool.{s}.short")) for s in TOOLS] + [(Page("strength"), t("seo.strength.short"))]
    library = [(Page("programs"), t("seo.programs.h1")), (Page("exercises"), t("seo.exercises.h1")),
               (Page("strength"), t("seo.strength.h1"))]
    moving = [(Page("import", s), t(f"seo.import.{s}.short")) for s in IMPORT_SOURCES]
    return (
        '<footer class=site-foot><div class=wrap><div class=foot-grid>'
        f'<div class=foot-about><a class=brand href="{url_for(Page("home"), lang)}">'
        f'<img src="{seo_static.asset_url("icons/favicon-32.png")}" alt="" width=30 height=30><span>{_e(t("bot.name"))}</span></a>'
        f"<p>{_e(t('seo.footer.about'))}</p>{_cta_buttons(lang, page.block, device_of(request))}</div>"
        f"<div><h4>{_e(t('seo.footer.product'))}</h4>{links(product)}</div>"
        f"<div><h4>{_e(t('seo.footer.tools'))}</h4>{links(tools)}</div>"
        f"<div><h4>{_e(t('seo.footer.library'))}</h4>{links(library)}</div>"
        f"<div><h4>{_e(t('seo.footer.moving'))}</h4>{links(moving)}</div>"
        "</div><div class=foot-bottom>"
        f'<span>{_e(t("seo.brand"))} · <a href="{url_for(page, other)}" hreflang={other} lang={other}>{_e(t("seo.footer.other_lang"))}</a></span>'
        f'<span><a href="/privacy?lang={lang}">{_e(t("seo.footer.privacy"))}</a> · '
        f'<a href="/terms?lang={lang}">{_e(t("seo.footer.terms"))}</a> · '
        f'<a href="/support?lang={lang}">{_e(t("seo.footer.support"))}</a></span>'
        "</div></div></footer>"
    )


def _css() -> str:
    return seo_design.CSS.replace("__PIXEL__", seo_static.asset_url("fonts/pixel.woff"))


def _layout(lang: str, page: Page, request: Optional[Request], *, title: str, description: str,
            body: str, ld: Optional[list[dict]] = None, wide: bool = False) -> str:
    t = _t(lang)
    base = base_url(request)
    canonical = base + url_for(page, lang)
    other = "en" if lang == "ru" else "ru"
    device = device_of(request)
    head = [
        f"<!doctype html><html lang={lang}><head><meta charset=utf-8>",
        '<meta name=viewport content="width=device-width, initial-scale=1, viewport-fit=cover">',
        f"<title>{_e(title)}</title>",
        f'<meta name=description content="{_e(description)}">',
        f'<link rel=canonical href="{_e(canonical)}">',
    ]
    for code in LANGS:
        head.append(f'<link rel=alternate hreflang={code} href="{_e(base + url_for(page, code))}">')
    head.append(f'<link rel=alternate hreflang=x-default href="{_e(base + url_for(page, X_DEFAULT_LANG))}">')
    image = f"{base}/og/{lang}/{page.key}.png"
    head += [
        '<meta property="og:type" content="website">',
        f'<meta property="og:site_name" content="{_e(t("seo.brand"))}">',
        f'<meta property="og:title" content="{_e(title)}">',
        f'<meta property="og:description" content="{_e(description)}">',
        f'<meta property="og:url" content="{_e(canonical)}">',
        f'<meta property="og:locale" content="{"ru_RU" if lang == "ru" else "en_US"}">',
        f'<meta property="og:image" content="{_e(image)}">',
        '<meta property="og:image:width" content="1200">',
        '<meta property="og:image:height" content="630">',
        '<meta name="twitter:card" content="summary_large_image">',
        f'<meta name="theme-color" content="{seo_static.THEME_COLOR}">',
        '<link rel=icon href="/favicon.ico" sizes="48x48">',
        f'<link rel=icon type="image/png" sizes="32x32" href="{seo_static.asset_url("icons/favicon-32.png")}">',
        '<link rel=apple-touch-icon href="/apple-touch-icon.png">',
        f'<link rel=manifest href="/site.webmanifest?lang={lang}">',
        f'<link rel=preload href="{seo_static.asset_url("fonts/pixel.woff")}" as=font type="font/woff" crossorigin>',
    ]
    app_id = app_store_id()
    if app_id:
        head.append(f'<meta name="apple-itunes-app" content="app-id={app_id}">')
    verification = config.GOOGLE_SITE_VERIFICATION
    if verification:
        head.append(f'<meta name="google-site-verification" content="{_e(verification)}">')
    head.append(f"<style>{_css()}</style>")
    crumbs = _breadcrumb_ld(lang, page, base)
    for block in ([crumbs] if crumbs else []) + list(ld or []):
        head.append(_json_ld(block))
    head.append("</head>")
    header = (
        f'<a class=skip href="#main">{_e(t("seo.skip"))}</a>'
        '<header class=site-head><div class=wrap><div class=head-row>'
        f'<a class=brand href="{url_for(Page("home"), lang)}"><img src="{seo_static.asset_url("icons/favicon-32.png")}" alt="" width=30 height=30>'
        f"<span>{_e(t('bot.name'))}</span></a>"
        f"<nav class=head-nav aria-label=\"{_e(t('seo.nav.label'))}\">{_nav_links(lang, page)}</nav>"
        f'<div class=head-end><a class=lang href="{url_for(page, other)}" hreflang={other} lang={other}>{other.upper()}</a>'
        f"{_head_cta(lang, page.block, device)}</div></div>"
        f'<nav class=sub-nav aria-label="{_e(t("seo.nav.label"))}">{_nav_links(lang, page)}</nav></div></header>'
    )
    if page.kind == "home":
        inner = body
    elif wide:
        inner = _crumbs_html(lang, page) + body
    else:
        inner = f"<div class=prose>{_crumbs_html(lang, page)}{body}</div>"
    cls = "" if page.kind == "home" else " class=page"
    return ("".join(head) + f"<body{cls}>{header}<main id=main><div class=wrap>{inner}</div></main>"
            f"{_footer(lang, page, request)}</body></html>")


def _cards(items: list[tuple[str, str, str]]) -> str:
    """(href, заголовок, подпись) → сетка карточек."""
    return "<div class=grid>" + "".join(
        f'<a class="card reveal" href="{href}"><span class=arrow>{seo_design.icon("arrow")}</span>'
        f"<b>{_e(title)}</b><span>{_e(text)}</span></a>"
        for href, title, text in items
    ) + "</div>"


def _icon_cards(items: list[tuple[str, str, str, str]]) -> str:
    """(href, значок, заголовок, подпись) → карточки со своим значком у каждой."""
    return "<div class=grid>" + "".join(
        f'<a class="card reveal" href="{href}"><span class=ic>{seo_design.icon(ic)}</span>'
        f"<b>{_e(title)}</b><span>{_e(text)}</span></a>"
        for href, ic, title, text in items
    ) + "</div>"


def _article_params(lang: str) -> dict[str, Any]:
    t = _t(lang)
    return {
        "ai": _e(t("btn.ai_coach")),
        "video": _e(t("ai.screen.preset.video_hint.label")),
        "programs": _e(t("btn.programs")),
        "ready": _e(t("btn.ready_programs")),
        "settings": _e(t("btn.settings")),
        "import_btn": _e(t("btn.import_csv")),
        "seconds": config.MAX_VIDEO_SECONDS,
        "videos": config.AI_VIDEO_DAILY_LIMIT,
        "questions": config.AI_QUESTION_DAILY_LIMIT,
        "days": config.IMPORT_UNDO_DAYS,
        "import_history": _e(t("btn.import_history")),
        "upload": _e(t("btn.upload_all")),
        "add": _e(t("btn.add_for_myself_plus")),
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
    return (f'<section class=reveal><h2>{_e(t("seo.faq.h"))}</h2><div class=faq>'
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
        "url": base + url_for(page, lang), "inLanguage": lang,
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
    raise ValueError(page.kind)


# ---------- главная ----------


def _hero(lang: str, page: Page, request) -> str:
    t = _t(lang)
    screen, w, h = seo_static.SCREENS[lang][0]
    return (
        "<div class=hero>"
        f'<div class=hero-in><span class=eyebrow>{_e(t("seo.home.eyebrow"))}</span>'
        f"<h1>{_e(t('seo.home.h1'))}</h1><p class=lead>{t('seo.home.lead_html')}</p>"
        f"<div style=\"margin-top:28px\">{_cta_buttons(lang, page.block, device_of(request))}</div>"
        "<ul class=trust>"
        + "".join(f"<li>{seo_design.icon('check')}{_e(t(f'seo.home.trust.{k}'))}</li>" for k in ("free", "platforms", "import"))
        + "</ul></div>"
        '<div class="stage hero-in d2"><div class=glow></div>'
        f'<div class=coach><img src="{seo_static.asset_url("coach.jpg")}" alt="{_e(t("seo.home.coach_alt"))}" width=720 height=720 fetchpriority=high></div>'
        f'<div class=phone><img src="{seo_static.asset_url(f"screens/{lang}-{screen}.jpg")}" alt="{_e(t(f"seo.home.shot.{screen}.alt"))}" width={w} height={h} fetchpriority=high></div>'
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
        f"<div><span>80{unit} × 8</span><span>✓</span></div>"
        f"<div><span>90{unit} × 6</span><span>✓</span></div>"
        f"<div class=new><span>100{unit} × 8</span><span>🔥 {_e(t('seo.home.demo_record'))}</span></div></div>"
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
        more = f'<a class=more href="{href}">{_e(t("seo.home.more"))} →</a>' if href else ""
        return (f'<div class="tile reveal {cls}"><span class=ic>{seo_design.icon(ic)}</span>'
                f"<h3>{_e(t(f'seo.home.b.{key}.h'))}</h3><p>{_e(t(f'seo.home.b.{key}.p'))}</p>{more}{extra}</div>")

    return (
        "<div class=bento>"
        + tile("b-wide hot", "type", "log", _demo(lang), url_for(Page("telegram"), lang))
        + tile("b-tall", "coach", "coach", _chat(lang), url_for(Page("ai-coach"), lang))
        + tile("b-third", "video", "video", "", url_for(Page("video-review"), lang))
        + tile("b-third", "trophy", "records", "", url_for(Page("tool", "1rm"), lang))
        + tile("b-third", "program", "programs", "", url_for(Page("programs"), lang))
        + tile("b-third", "import", "import", "", url_for(Page("import", "hevy"), lang))
        + tile("b-third", "phone", "everywhere", "", url_for(Page("telegram"), lang))
        + "</div>"
    )


def _shots(lang: str) -> str:
    t = _t(lang)
    figures = "".join(
        f'<figure class=reveal><div class=phone><img src="{seo_static.asset_url(f"screens/{lang}-{name}.jpg")}" '
        f'alt="{_e(t(f"seo.home.shot.{name}.alt"))}" width={w} height={h} loading=lazy decoding=async></div>'
        f"<figcaption>{_e(t(f'seo.home.shot.{name}.cap'))}</figcaption></figure>"
        for name, w, h in seo_static.SCREENS[lang]
    )
    return f"<div class=shots>{figures}</div>"


def sprite(name: str, scale: int = 3, label: str = "") -> str:
    """Пиксельный спрайт из ленты кадров: шаги CSS, без сглаживания; при
    «уменьшении движения» стоит первый кадр."""
    rel, w, h, frames = seo_static.SPRITES[name]
    role = f' role=img aria-label="{_e(label)}"' if label else " aria-hidden=true"
    anim = f"animation-duration:{0.6 * frames:.1f}s;--sx:-{w * scale * frames}px;" if frames > 1 else ""
    return (f'<span class="sprite{" anim" if frames > 1 else ""}"{role} style="width:{w * scale}px;height:{h * scale}px;'
            f"background-image:url({seo_static.asset_url(rel)});background-size:{w * scale * frames}px {h * scale}px;"
            f'{anim}--frames:{frames}"></span>')


def _gym(lang: str) -> str:
    """Подвал с вкладки «Меню» — только как есть в приложении для iPhone."""
    t = _t(lang)
    w, h = seo_static.SCENE_SIZE
    return (
        "<section class='gym-sec reveal'><div class=gym>"
        "<div class=gym-text><span class=eyebrow>" + _e(t("seo.home.gym.eyebrow")) + "</span>"
        f"<h2>{_e(t('seo.home.gym.h'))}</h2><p>{_e(t('seo.home.gym.p'))}</p>"
        f"<div class=you>{sprite('stand', 2)}<span class=you-tag>{_e(t('seo.home.gym.you_tag'))}</span>"
        f"<p>{_e(t('seo.home.gym.you'))}</p></div>"
        f"<p class=muted>{_e(t('seo.home.gym.note'))}</p></div>"
        f'<figure class=scene><img src="{seo_static.asset_url(f"gym/scene-{lang}.png")}" alt="{_e(t("seo.home.gym.alt"))}" '
        f"width={w} height={h} loading=lazy decoding=async>"
        f"<span class=scene-dog>{sprite('dog', 2)}</span></figure>"
        "</div></section>"
    )


def _render_home(lang: str, page: Page, request) -> str:
    t = _t(lang)
    imports = [(url_for(Page("import", s), lang), "import", t(f"seo.import.{s}.short"), t(f"seo.import.{s}.card"))
               for s in IMPORT_SOURCES]
    tools = [(url_for(Page("tool", s), lang), _TOOL_ICONS[s], t(f"seo.tool.{s}.short"), t(f"seo.tool.{s}.card")) for s in TOOLS]
    tools.append((url_for(Page("strength"), lang), "scale", t("seo.strength.short"), t("seo.strength.card")))
    library = [
        (url_for(Page("programs"), lang), "program", t("seo.programs.h1"), t("seo.programs.card")),
        (url_for(Page("exercises"), lang), "dumbbell", t("seo.exercises.h1"), t("seo.exercises.card", count=len(EXERCISES))),
    ]
    faq = _faq_items(lang, FAQ["home"])

    def head(key: str) -> str:
        accent = sprite("curl", 1) if key == "features" else ""
        return (f"<div class='sec-head reveal'>{accent}<span class=eyebrow>{_e(t(f'seo.home.{key}.eyebrow'))}</span>"
                f"<h2>{_e(t(f'seo.home.{key}.h'))}</h2><p>{_e(t(f'seo.home.{key}.p'))}</p></div>")

    body = (
        _hero(lang, page, request)
        + f"<section>{head('features')}{_bento(lang)}</section>"
        + f"<section>{head('shots')}{_shots(lang)}</section>"
        + _gym(lang)
        + f"<section>{head('import')}{_icon_cards(imports)}</section>"
        + f"<section>{head('tools')}{_icon_cards(tools)}</section>"
        + f"<section>{head('library')}{_icon_cards(library)}</section>"
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
        "inLanguage": ["ru", "en"],
        "url": base + url_for(page, lang),
        "image": base + seo_static.asset_url("icons/icon-512.png"),
        "screenshot": [base + seo_static.asset_url(f"screens/{lang}-{n}.jpg") for n, _w, _h in seo_static.SCREENS[lang]],
        "offers": {"@type": "Offer", "price": "0", "priceCurrency": "USD"},
        "sameAs": [u for u in (config.app_store_url(), f"https://t.me/{bot_username()}") if u],
    }
    site = {"@context": "https://schema.org", "@type": "WebSite", "name": t("seo.brand"),
            "url": base + url_for(page, lang), "inLanguage": lang}
    return _layout(lang, page, request, title=t("seo.home.title"), description=t("seo.home.description"),
                   body=body, ld=[app, site, _faq_ld(faq)])


# ---------- статьи и переезд ----------


def _render_feature(lang: str, page: Page, request) -> str:
    t = _t(lang)
    key = page.kind
    params = _article_params(lang)
    sections = "".join(
        f"<section class=reveal><h2>{_e(t(f'seo.{key}.s{i}.h'))}</h2>{t(f'seo.{key}.s{i}.p_html', **params)}</section>"
        for i in range(1, _ARTICLE_SECTIONS[key] + 1)
    )
    related = [(url_for(Page(f), lang), _strip_emoji(t(f"seo.{f}.card.title")), t(f"seo.{f}.card.text")) for f in FEATURES if f != key]
    faq = _faq_items(lang, FAQ[key]) if key in FAQ else []
    title, description = t(f"seo.{key}.title"), t(f"seo.{key}.description")
    body = (
        f"<span class=eyebrow>{_e(t('seo.eyebrow.feature'))}</span>"
        f"<h1>{_e(t(f'seo.{key}.h1'))}</h1><p class=lead>{_e(t(f'seo.{key}.lead'))}</p>"
        + (_chat(lang) if key == "ai-coach" else "")
        + sections
        + (_faq_html(lang, faq) if faq else "")
        + _cta(lang, page.block, _e(t(f"seo.{key}.cta")), request)
        + _cards(related)
    )
    ld = [_webpage_ld(lang, page, request, title, description)] + ([_faq_ld(faq)] if faq else [])
    return _layout(lang, page, request, title=title, description=description, body=body, ld=ld)


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
    body = (
        f"<span class=eyebrow>{_e(t('seo.eyebrow.import'))}</span>"
        f"<h1>{_e(t(f'seo.import.{src}.h1'))}</h1><p class=lead>{_e(t(f'seo.import.{src}.lead'))}</p>"
        f"<section class=reveal><h2>{_e(t(f'seo.import.{src}.export.h'))}</h2>{t(f'seo.import.{src}.export_html')}</section>"
        f"<section class=reveal><h2>{_e(t('seo.import.bot.h'))}</h2>{t('seo.import.bot_html', **params)}</section>"
        f"<section class=reveal><h2>{_e(t('seo.import.app.h'))}</h2>{t('seo.import.app_html')}</section>"
        f"<section class=reveal><h2>{_e(t('seo.import.what.h'))}</h2>{t('seo.import.what_html', **params)}</section>"
        + _faq_html(lang, faq)
        + _cta(lang, page.block, _e(t("seo.import.cta")), request)
        + f"<h2>{_e(t('seo.import.more.h'))}</h2>" + _cards(others)
    )
    ld = [_webpage_ld(lang, page, request, title, description), _faq_ld(faq)]
    return _layout(lang, page, request, title=title, description=description, body=body, ld=ld)


# ---------- калькуляторы ----------

_TOOL_ICONS = {"1rm": "calc", "plates": "plates", "warmup": "warmup", "rpe": "gauge", "wilks": "trophy"}


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


def _rpe_select(ident: str, label: str, blank: bool = True, default: str = "") -> str:
    opts = ('<option value="">—</option>' if blank else "") + "".join(
        f'<option{" selected" if v == default else ""}>{v}</option>' for v in ("10", "9.5", "9", "8.5", "8", "7.5", "7", "6.5", "6")
    )
    return f"<label>{_e(label)}<select id={ident}>{opts}</select></label>"


def _num_input(ident: str, label: str, value: str, step: str = "0.5", mode: str = "decimal", max_: str = "") -> str:
    mx = f" max={max_}" if max_ else ""
    return (f"<label>{_e(label)}<input id={ident} type=number inputmode={mode} min=0{mx} step={step} "
            f"value={value}></label>")


def _log_cta(lang: str, tool: str, text_key: str = "seo.tool.log_cta") -> str:
    t = _t(lang)
    return (f'<div class="log-cta btn-row"><a class="btn primary" href="{_e(go_url("bot", _BLOCKS[f"log:{tool}"]))}">'
            f'{seo_design.icon("telegram")}{_e(t(text_key))}</a></div>')


# Общие функции страницы калькулятора: значение поля или выбранной кнопки,
# число с единицей (единица — отдельным span, зазор даёт стиль).
_UI_JS = (
    "function $(i){return document.getElementById(i);}"
    "function val(n){var c=document.querySelector('[name=\"'+n+'\"]:checked');if(c)return c.value;var e=$(n);return e?e.value:'';}"
    "function fu(x,u){return fmt(x)+'<span class=u>'+UNITS[u]+'</span>';}"
    "function bind(f){document.querySelectorAll('form.calc input,form.calc select').forEach(function(el){el.addEventListener('input',f);el.addEventListener('change',f);});f();}"
    "function done(ok){var r=document.querySelector('.result');if(r)r.classList.toggle('ok',!!ok);}"
)


def _tool_1rm(lang: str, embed: bool = False) -> tuple[str, str, str]:
    t = _t(lang)
    form = (
        "<form class=calc onsubmit='return false'>"
        "<div class=row>" + _num_input("w", t("seo.tool.1rm.weight"), "100", "0.25")
        + _num_input("r", t("seo.tool.1rm.reps"), "5", "1", "numeric", "50") + "</div>"
        "<div class=row>" + _rpe_select("rpe", t("seo.tool.1rm.rpe")) + _formula_seg(lang) + "</div>"
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
        "if(!m){out.textContent='—';note.textContent=too;tb.innerHTML='';done(0);return;}out.innerHTML=fu(m,u);done(1);"
        "tb.innerHTML=C.percents.map(function(pc){return '<tr><td>'+pc+'%</td><td class=num>'+fu(m*pc/100,u)+'</td></tr>';}).join('');}"
        "bind(run);"
    )
    return form, result, script


def _tool_plates(lang: str, embed: bool = False) -> tuple[str, str, str]:
    t = _t(lang)
    checks = "".join(
        f'<div class=checks data-unit={unit}{"" if unit == "kg" else " hidden"}>' + "".join(
            f'<label><input type=checkbox value="{_num(p)}"'
            f'{" checked" if p in seo_tools.DEFAULT_PLATES[unit] else ""}>{_num(p)}</label>'
            for p in seo_tools.PLATES[unit]
        ) + "</div>"
        for unit in ("kg", "lb")
    )
    form = (
        "<form class=calc onsubmit='return false'>" + _unit_seg(lang)
        + "<div class=row>" + _num_input("target", t("seo.tool.plates.target"), "100", "0.25")
        + _num_input("bar", t("seo.tool.plates.bar"), "20", "0.5") + "</div>"
        f'<fieldset><legend>{_e(t("seo.tool.plates.available"))}</legend>{checks}</fieldset>'
        "</form>"
    )
    result = (
        f'<div class=result aria-live=polite><div class=k>{_e(t("seo.tool.plates.per_side"))}</div>'
        '<div class=bar-viz id=viz></div><div class=plates id=plates></div><div class=note id=note></div>'
        + ("" if embed else _log_cta(lang, "plates")) + "</div>"
    )
    bars = {u: seo_tools.BARS[u][0] for u in ("kg", "lb")}
    colors = {u: {_num(k): v for k, v in c.items()} for u, c in seo_design.PLATE_COLORS.items()}
    script = (
        f"var BARS={json.dumps(bars)},COLORS={json.dumps(colors)};"
        "var EMPTY=" + _js_str(t("seo.tool.plates.empty")) + ",GOT=" + _js_str(t("seo.tool.plates.got", total="{total}")) + ";"
        "function pkg(p,u){return u==='lb'?p/C.lbPerKg:p;}"
        "function col(p,u){return (COLORS[u]||{})[fmt(p)]||'#bbb';}"
        "function barSvg(pl,u){var W=640,H=170,cy=85,s='<svg viewBox=\"0 0 '+W+' '+H+'\" aria-hidden=true>';"
        "s+='<rect x=8 y='+(cy-5)+' width='+(W-16)+' height=10 rx=5 fill=\"#8d8d8d\"/>';"
        "s+='<rect x=150 y='+(cy-9)+' width=14 height=18 rx=3 fill=\"#b5b5b5\"/><rect x='+(W-164)+' y='+(cy-9)+' width=14 height=18 rx=3 fill=\"#b5b5b5\"/>';"
        "var xr=W-150,xl=150;pl.forEach(function(p){var k=pkg(p,u),h=k>=10?150:k>=5?104:k>=2.5?80:k>=1.25?62:48,"
        "th=k>=25?24:k>=20?21:k>=15?18:k>=10?15:k>=5?12:9,c=col(p,u);"
        "s+='<rect x='+xr+' y='+(cy-h/2)+' width='+th+' height='+h+' rx=3 fill=\"'+c+'\" stroke=\"rgba(0,0,0,.45)\"/>';"
        "s+='<rect x='+(xl-th)+' y='+(cy-h/2)+' width='+th+' height='+h+' rx=3 fill=\"'+c+'\" stroke=\"rgba(0,0,0,.45)\"/>';xr+=th+2;xl-=th+2;});"
        "return s+'</svg>';}"
        "function shown(){var u=val('unit');document.querySelectorAll('.checks').forEach(function(s){s.hidden=s.dataset.unit!==u;});return u;}"
        "function run(){var u=shown(),tg=parseFloat($('target').value),b=parseFloat($('bar').value)||0,list=[],box=$('plates'),note=$('note');"
        "document.querySelectorAll('.checks[data-unit=\"'+u+'\"] input:checked').forEach(function(i){list.push(parseFloat(i.value));});"
        "box.innerHTML='';note.textContent='';$('viz').innerHTML='';if(!(tg>0)){done(0);return;}var res=platesPerSide(tg,b,list);"
        "$('viz').innerHTML=barSvg(res.plates,u);"
        "if(!res.plates.length){box.textContent=EMPTY;}else{box.innerHTML=res.plates.map(function(p){return '<span class=plate><i style=\"background:'+col(p,u)+'\"></i>'+fu(p,u)+'</span>';}).join('');}"
        "if(Math.abs(res.total-tg)>1e-9){note.innerHTML=GOT.replace('{total}',fu(res.total,u));}done(res.plates.length>0);}"
        "document.querySelectorAll('[name=unit]').forEach(function(el){el.addEventListener('change',function(){$('bar').value=BARS[this.value];"
        "$('target').value=this.value==='lb'?225:100;});});bind(run);"
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
        "d.textContent=dots(tot*k,bw*k,s).toFixed(2);wk.textContent=wilks(tot*k,bw*k,s).toFixed(2);"
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
    "1rm": _tool_1rm, "plates": _tool_plates, "wilks": _tool_wilks, "warmup": _tool_warmup, "rpe": _tool_rpe,
}


def _units_js(lang: str) -> str:
    t = _t(lang)
    return "{kg:" + _js_str(t("seo.tool.unit_kg")) + ",lb:" + _js_str(t("seo.tool.unit_lb")) + "}"


def _calc_script(lang: str, script: str) -> str:
    return f"<script>{seo_tools.calc_js()}var UNITS={_units_js(lang)};{_UI_JS}{script}</script>"


def _embed_snippet(lang: str, tool: str, request) -> str:
    t = _t(lang)
    src = f"{base_url(request)}/embed/{lang}/{tool}"
    height = 640 if tool == "1rm" else 600
    code = (f'<iframe src="{src}" title="{t(f"seo.tool.{tool}.h1")}" width="100%" height="{height}" '
            'style="border:0;border-radius:16px;max-width:560px" loading="lazy"></iframe>')
    return (
        f'<div class="embed-box reveal"><h2>{_e(t("seo.embed.h"))}</h2><p class=muted>{_e(t("seo.embed.p"))}</p>'
        f'<textarea readonly id=embed-code aria-label="{_e(t("seo.embed.h"))}">{_e(code)}</textarea>'
        f'<div class=btn-row style="margin-top:12px"><button class="btn small" type=button id=embed-copy>{_e(t("seo.embed.copy"))}</button></div>'
        "<script>document.getElementById('embed-copy').addEventListener('click',function(){var a=document.getElementById('embed-code');a.select();"
        "if(navigator.clipboard){navigator.clipboard.writeText(a.value);}else{document.execCommand('copy');}this.textContent="
        + _js_str(t("seo.embed.copied")) + ";});</script></div>"
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
    form, result, script = _TOOLS[tool](lang)
    others = [(url_for(Page("tool", s), lang), t(f"seo.tool.{s}.short"), t(f"seo.tool.{s}.card")) for s in TOOLS if s != tool]
    chart = (f"<section class=reveal><h2>{_e(t('seo.tool.rpe.chart_h'))}</h2>{_rpe_table_html(lang)}</section>"
             if tool == "rpe" else "")
    how_params = dict(_article_params(lang), max=config.E1RM_MAX_REPS, gl_source=GL_SOURCE,
                      dots_href=url_for(Page("tool", "wilks"), lang), plates_href=url_for(Page("tool", "plates"), lang),
                      strength_href=url_for(Page("strength"), lang), rpe_href=url_for(Page("tool", "rpe"), lang))
    title, description = t(f"seo.tool.{tool}.title"), t(f"seo.tool.{tool}.description")
    body = (
        f"<span class=eyebrow>{_e(t('seo.eyebrow.tool'))}</span>"
        f"<h1>{_e(t(f'seo.tool.{tool}.h1'))}</h1><p class=lead>{_e(t(f'seo.tool.{tool}.lead'))}</p>"
        f"<div class=tool>{form}{result}</div>"
        + f"<noscript><p class=muted>{_e(t('seo.tool.noscript'))}</p></noscript>"
        + chart
        + f"<div class=prose><section class=reveal><h2>{_e(t(f'seo.tool.{tool}.how.h'))}</h2>"
        f"{t(f'seo.tool.{tool}.how_html', **how_params)}</section>"
        + (_embed_snippet(lang, tool, request) if tool in EMBEDS else "")
        + "</div>"
        + _cta(lang, page.block, _e(t(f"seo.tool.{tool}.cta")), request)
        + _cards(others)
        + _calc_script(lang, script)
    )
    return _layout(lang, page, request, title=title, description=description, body=body, wide=True,
                   ld=[_tool_ld(lang, page, request, title, description)])


def _render_tools(lang: str, page: Page, request) -> str:
    t = _t(lang)
    items = [(url_for(Page("tool", s), lang), _TOOL_ICONS[s], t(f"seo.tool.{s}.short"), t(f"seo.tool.{s}.card")) for s in TOOLS]
    items.append((url_for(Page("strength"), lang), "scale", t("seo.strength.short"), t("seo.strength.card")))
    title, description = t("seo.tools.title"), t("seo.tools.description")
    body = (
        f"<span class=eyebrow>{_e(t('seo.eyebrow.tool'))}</span>"
        f"<h1>{_e(t('seo.tools.h1'))}</h1><p class=lead>{_e(t('seo.tools.lead'))}</p>"
        + _icon_cards(items)
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
    return (f'<div class=table-card><table><thead><tr><th>{_e(t("seo.strength.col_bw"))}</th>{head}</tr></thead>'
            f"<tbody>{rows}</tbody></table></div>")


def _strength_tool(lang: str, lift: str = "bench", sex: str = "m") -> tuple[str, str, str]:
    t = _t(lang)
    form = (
        "<form class=calc onsubmit='return false'>"
        + _seg("lift", t("seo.strength.tool.lift"), [(k, t(f"seo.strength.lift.{k}")) for k in seo_tools.STRENGTH_LIFTS], lift)
        + "<div class=row>" + _seg("sex", t("seo.tool.wilks.sex"), [("m", t("seo.tool.wilks.male")), ("f", t("seo.tool.wilks.female"))], sex)
        + _unit_seg(lang) + "</div>"
        "<div class=row>" + _num_input("bw", t("seo.tool.wilks.bw"), "80" if sex == "m" else "60", "0.1")
        + _num_input("lw", t("seo.tool.1rm.weight"), "100" if sex == "m" else "50", "0.25") + "</div>"
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
        "var sc=liftScore(m*k,lift,bw,s),i=levelIndex(sc);out.textContent=i<0?BELOW:NAMES[i];"
        "var txt=SCORE.replace('{score}',Math.round(sc));if(i<C.levels.length-1){var nw=levelWeight(C.levels[i+1],lift,bw,s)/k;"
        "txt+=' '+NEXT.replace('{weight}',fmt(nw)+UNITS[u]).replace('{level}',NAMES[i+1]);}else{txt+=' '+TOP;}note.textContent=txt;done(1);}"
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
        "thresholds": " · ".join(f"{n} {int(th)}" for n, (_k, th) in zip(_level_names(lang), seo_tools.LEVELS)),
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
        + f"<section class=reveal style=\"margin-top:24px\"><h2>{_e(t('seo.strength.table.h'))}</h2>{tables}"
        f"<p class=muted>{_e(t('seo.strength.table_note'))}</p></section>"
        + f"<section class=reveal><h2>{_e(t('seo.strength.tool.h'))}</h2><div class=tool>{form}{result}</div></section>"
        + _strength_method(lang)
        + f'<p><a href="{url_for(Page("exercise", ex_url), lang)}">{_e(t("seo.strength.technique", lift=lift_name))} →</a></p>'
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
        + _cards(cards)
        + f"<section class='prose reveal'><h2>{_e(t('seo.programs.take.h'))}</h2>{t('seo.programs.take_html', **_article_params(lang))}</section>"
        + _cta(lang, page.block, _e(t("seo.programs.cta")), request)
    )
    ld = _webpage_ld(lang, page, request, title, description, "CollectionPage", {
        "hasPart": [{"@type": "ExercisePlan", "name": name, "url": base_url(request) + href} for href, name, _m in cards],
    })
    return _layout(lang, page, request, title=title, description=description, body=body, wide=True, ld=[ld])


def _render_program(lang: str, page: Page, request) -> str:
    t = _t(lang)
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
                shown = f'<a href="{url_for(Page("exercise", url), lang)}">{shown}</a>'
            rows.append(f"<li><span>{shown}</span><span>{_e(seed_data.localized_target(target, lang) or '')}</span></li>")
        days.append(
            f'<div class="day reveal"><h3><small>{_e(t("seo.program.day", n=i + 1))}</small>'
            f"{_e(seed_data.localized_program_day_name(key, i, lang))}</h3><ul>{''.join(rows)}</ul></div>"
        )
    others = [
        (url_for(Page("program", p["key"]), lang), _strip_emoji(seed_data.localized_program_name(p["key"], lang)),
         seed_data.localized_program_meta(p["key"], lang))
        for p in seed_data.WORKOUT_PROGRAMS if p["key"] != key
    ]
    description = seed_data.localized_program_description(key, lang)
    body = (
        f"<span class=eyebrow>{_e(seed_data.localized_program_meta(key, lang))}</span>"
        f"<h1>{_e(name)}</h1><p class=lead>{_e(description)}</p>"
        f"<section><h2>{_e(t('seo.program.days.h'))}</h2><div class=days>{''.join(days)}</div>"
        f"<p class=muted style=\"margin-top:16px\">{_e(t('seo.program.target_note'))}</p></section>"
        + _cta(lang, page.block, t("seo.program.cta_html", **_article_params(lang), name=_e(name)), request)
        + f"<h2>{_e(t('seo.program.more.h'))}</h2>" + _cards(others)
    )
    title = t("seo.program.title", name=name)
    plan = {
        "@context": "https://schema.org", "@type": "ExercisePlan", "name": name, "description": description,
        "url": base_url(request) + url_for(page, lang), "inLanguage": lang,
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


def _exercise_card(url: str, slug: str, lang: str) -> str:
    identity = _identity_for_media_slug(slug)
    name = seed_data.localized_exercise_name(identity, lang)
    thumb = os.path.join(exercise_media.THUMBS_DIR, f"{slug}_1.jpg")
    src = _media_url(f"{slug}_1.jpg", thumb=os.path.isfile(thumb))
    return (
        f'<a class="card thumb-card reveal" href="{url_for(Page("exercise", url), lang)}">'
        f'<img src="{src}" alt="{_e(name)}" loading=lazy decoding=async width=240 height=180>'
        f"<b>{_e(name)}</b><span>{_e(_group_for(identity, lang))}</span></a>"
    )


def _render_exercises(lang: str, page: Page, request) -> str:
    t = _t(lang)
    groups: dict[str, list[str]] = {}
    for url, slug in EXERCISES:
        groups.setdefault(_group_of(_identity_for_media_slug(slug)), []).append(_exercise_card(url, slug, lang))
    order = list(dict.fromkeys(g for g, _n in seed_data.EXERCISE_TEMPLATES))
    sections = "".join(
        f"<h2 class=group-title>{_e(seed_data.localized_muscle_group_name(g, lang))}</h2><div class=grid>{''.join(groups[g])}</div>"
        for g in order if g in groups
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


def _steps(identity: str, lang: str) -> list[str]:
    text = exercise_descriptions.get_description(identity, lang) or ""
    return [re.sub(r"^\d+\.\s*", "", line).strip() for line in text.splitlines() if line.strip()]


def _render_exercise(lang: str, page: Page, request) -> str:
    t = _t(lang)
    slug = _EXERCISE_BY_URL[page.arg]
    identity = _identity_for_media_slug(slug)
    name = seed_data.localized_exercise_name(identity, lang)
    base = base_url(request)
    frames = (
        "<div class=frames>"
        f'<figure><img src="{_media_url(f"{slug}_1.jpg")}" alt="{_e(t("seo.exercise.frame_start", name=name))}" width=400 height=300>'
        f'<figcaption><b>1</b>{_e(t("seo.exercise.cap_start"))}</figcaption></figure>'
        f'<figure><img src="{_media_url(f"{slug}_2.jpg")}" alt="{_e(t("seo.exercise.frame_end", name=name))}" width=400 height=300 loading=lazy>'
        f'<figcaption><b>2</b>{_e(t("seo.exercise.cap_end"))}</figcaption></figure>'
        "</div>"
    )
    steps = "".join(f"<li>{_e(s)}</li>" for s in _steps(identity, lang))
    mistakes = "".join(f"<li>{_e(m)}</li>" for m in t(f"seo.ex.{slug}.mistakes").split("\n") if m.strip())
    programs = _program_pages_for(identity)
    in_programs = ""
    if programs:
        cards = [(url_for(Page("program", k), lang), _strip_emoji(seed_data.localized_program_name(k, lang)),
                  seed_data.localized_program_meta(k, lang)) for k in programs]
        in_programs = f"<section class=reveal><h2>{_e(t('seo.exercise.programs.h'))}</h2>{_cards(cards)}</section>"
    group = _group_of(identity)
    related = [(u, s) for u, s in EXERCISES if u != page.arg and _group_of(_identity_for_media_slug(s)) == group][:6]
    related_html = ""
    if related:
        related_html = (f"<section class=reveal><h2>{_e(t('seo.exercise.related.h', group=_group_for(identity, lang)))}</h2>"
                        f"<div class=grid>{''.join(_exercise_card(u, s, lang) for u, s in related)}</div></section>")
    lead = t(f"seo.ex.{slug}.lead")
    body = (
        f"<span class=eyebrow>{_e(_group_for(identity, lang))}</span>"
        f"<h1>{_e(name)}</h1><p class=lead>{_e(lead)}</p>{frames}"
        f"<section class=reveal><h2>{_e(t('seo.exercise.setup.h'))}</h2><p>{_e(t(f'seo.ex.{slug}.setup'))}</p></section>"
        f"<section class=reveal><h2>{_e(t('seo.exercise.steps.h'))}</h2><ol class=steps>{steps}</ol></section>"
        f"<section class='mistakes reveal'><h2>{_e(t('seo.exercise.mistakes.h'))}</h2><ul>{mistakes}</ul></section>"
        + in_programs
        + _cta(lang, page.block, _e(t("seo.exercise.cta")), request)
        + related_html
        + f'<p style="margin-top:32px"><a href="{url_for(Page("exercises"), lang)}">{_e(t("seo.exercise.all"))}</a></p>'
        + f"<p class=muted>{t('seo.exercise.credit_html')}</p>"
    )
    home = base + url_for(Page("home"), lang)
    article = {
        "@context": "https://schema.org", "@type": "Article",
        "headline": t("seo.exercise.title", name=name)[:110], "description": lead, "inLanguage": lang,
        "image": [base + _media_url(f"{slug}_1.jpg"), base + _media_url(f"{slug}_2.jpg")],
        "mainEntityOfPage": base + url_for(page, lang),
        "author": {"@type": "Organization", "name": t("seo.brand"), "url": home},
        "publisher": {"@type": "Organization", "name": t("seo.brand"), "url": home},
        "about": {"@type": "Thing", "name": name},
    }
    return _layout(lang, page, request, title=t("seo.exercise.title", name=name), description=lead, body=body,
                   ld=[article])


def _render_vs(lang: str, page: Page, request) -> str:
    t = _t(lang)
    rival = page.arg
    params = _article_params(lang)
    rows = "".join(
        f"<tr><th scope=row>{_e(t(f'seo.vs.row.{row}'))}</th>"
        f"<td>{t(f'seo.vs.us.{row}_html', **params)}</td><td>{t(f'seo.vs.{rival}.{row}_html')}</td></tr>"
        for row in _VS_ROWS
    )
    sources = "".join(f'<li><a href="{_e(u)}" rel=nofollow>{_e(u)}</a></li>' for u in _VS_SOURCES[rival])
    faq = _faq_items(lang, FAQ[f"vs:{rival}"])
    title, description = t(f"seo.vs.{rival}.title"), t(f"seo.vs.{rival}.description")
    body = (
        f"<span class=eyebrow>{_e(t('seo.eyebrow.vs'))}</span>"
        f"<h1>{_e(t(f'seo.vs.{rival}.h1'))}</h1><p class=lead>{_e(t(f'seo.vs.{rival}.lead'))}</p>"
        f"<p class=muted>{_e(t('seo.vs.asof'))}</p>"
        f"<div class='table-card vs'><table><thead><tr><th></th><th>{_e(t('seo.brand'))}</th>"
        f"<th>{_e(t(f'seo.vs.{rival}.name'))}</th></tr></thead><tbody>{rows}</tbody></table></div>"
        f"<section class=reveal><h2>{_e(t('seo.vs.choose.h'))}</h2>{t(f'seo.vs.{rival}.choose_html')}</section>"
        f"<section class=reveal><h2>{_e(t('seo.vs.move.h'))}</h2>"
        f'<p>{t("seo.vs.move_html", href=url_for(Page("import", rival), lang), name=_e(t(f"seo.vs.{rival}.name")))}</p></section>'
        + _faq_html(lang, faq)
        + _cta(lang, page.block, _e(t("seo.vs.cta")), request)
        + f"<section><h2>{_e(t('seo.vs.sources.h'))}</h2><ul class=muted>{sources}</ul>"
        f"<p class=muted>{_e(t('seo.vs.disclaimer'))}</p></section>"
    )
    ld = [_webpage_ld(lang, page, request, title, description), _faq_ld(faq)]
    return _layout(lang, page, request, title=title, description=description, body=body, ld=ld)


_RENDERERS: dict[str, Callable[[str, Page, Optional[Request]], str]] = {
    "home": _render_home,
    "ai-coach": _render_feature,
    "video-review": _render_feature,
    "telegram": _render_feature,
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


def render(page: Page, lang: str, request: Optional[Request] = None) -> str:
    return _RENDERERS[page.kind](lang, page, request)


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
    # Без canonical и hreflang: у несуществующей страницы их нет.
    html = re.sub(r"<link rel=(?:canonical|alternate)[^>]*>", "", html)
    return html.replace("<head><meta charset=utf-8>", "<head><meta charset=utf-8><meta name=robots content=noindex>", 1)


def _not_found_route(lang: str):
    async def route(request: Request) -> HTMLResponse:
        return HTMLResponse(render_not_found(lang, request), status_code=404,
                            headers={"Cache-Control": "no-store", "Content-Language": lang, **_SECURITY, **_NO_FRAME})

    return route


# ---------- встраиваемые калькуляторы ----------


def render_embed(tool: str, lang: str, request: Optional[Request] = None) -> str:
    t = _t(lang)
    form, result, script = _TOOLS[tool](lang, embed=True)
    site = base_url(request) + url_for(Page("tool", tool), lang)
    return (
        f"<!doctype html><html lang={lang}><head><meta charset=utf-8>"
        '<meta name=viewport content="width=device-width, initial-scale=1">'
        f"<title>{_e(t(f'seo.tool.{tool}.h1'))}</title><meta name=robots content=noindex>"
        f'<link rel=canonical href="{_e(site)}"><style>{_css()}</style></head>'
        f"<body class=embed><main><div class=wrap><div class=tool>{form}{result}</div>"
        f'<span class=powered>{t("seo.embed.powered_html", href=_e(site))}</span></div></main>'
        f"{_calc_script(lang, script)}</body></html>"
    )


# ---------- служебные адреса ----------


def sitemap_xml(request: Optional[Request] = None) -> str:
    base = base_url(request)
    out = ['<?xml version="1.0" encoding="UTF-8"?>',
           '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9" xmlns:xhtml="http://www.w3.org/1999/xhtml">']
    for page in pages():
        alternates = "".join(
            f'<xhtml:link rel="alternate" hreflang="{code}" href="{escape(base + url_for(page, code))}"/>'
            for code in LANGS
        ) + f'<xhtml:link rel="alternate" hreflang="x-default" href="{escape(base + url_for(page, X_DEFAULT_LANG))}"/>'
        for lang in LANGS:
            out.append(f"<url><loc>{escape(base + url_for(page, lang))}</loc>{alternates}</url>")
    out.append("</urlset>")
    return "\n".join(out)


# Роботы поиска и AI-ответов, которым сайт открыт явно: тот же «Allow: /», что
# и всем, но отдельной группой — чтобы правка общего правила их не задела.
AI_CRAWLERS = ("GPTBot", "OAI-SearchBot", "ChatGPT-User", "PerplexityBot", "Perplexity-User",
               "ClaudeBot", "Claude-User", "Claude-SearchBot", "Google-Extended", "Applebot-Extended", "bingbot")


def robots_txt(request: Optional[Request] = None) -> str:
    named = "".join(f"User-agent: {name}\n" for name in AI_CRAWLERS)
    return (f"User-agent: *\nAllow: /\n\n{named}Allow: /\n\n"
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
                *[Page("tool", s) for s in TOOLS], Page("strength"), Page("programs"), Page("exercises"),
                *[Page("vs", c) for c in COMPETITORS]]
        for pg in main:
            out.append(f"- [{_strip_emoji(page_h1(pg, lang))}]({base}{url_for(pg, lang)})")
        out.append("")
    out.append(f"Sitemap: {base}/sitemap.xml")
    return "\n".join(out) + "\n"


_SECURITY = {"X-Content-Type-Options": "nosniff", "Referrer-Policy": "strict-origin-when-cross-origin"}
_NO_FRAME = {"X-Frame-Options": "DENY", "Content-Security-Policy": "frame-ancestors 'none'"}


def _html(content: str, lang: str) -> HTMLResponse:
    return HTMLResponse(content, headers={
        "Cache-Control": "public, max-age=3600", "Content-Language": lang, "Vary": "User-Agent",
        **_SECURITY, **_NO_FRAME,
    })


async def _count(kind: str, page_key: str, lang: Optional[str], request: Request, target: Optional[str] = None) -> None:
    await site_stats.record(
        kind, page_key, lang, user_agent=request.headers.get("user-agent"), referer=request.headers.get("referer"),
        own_hosts=_own_hosts(request), utm=request.query_params.get("utm_source"), target=target,
    )


def _page_route(page: Page, lang: str):
    async def route(request: Request) -> HTMLResponse:
        html = render(page, lang, request)
        await _count("view", url_for(page, lang), lang, request)
        return _html(html, lang)

    return route


def _embed_route(tool: str, lang: str):
    async def route(request: Request) -> HTMLResponse:
        html = render_embed(tool, lang, request)
        await _count("embed", f"/embed/{lang}/{tool}", lang, request)
        return HTMLResponse(html, headers={
            "Cache-Control": "public, max-age=3600", "Content-Language": lang, **_SECURITY,
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
    lang = request.path_params.get("lang", "")
    page = page_by_key(request.path_params.get("key", ""))
    if lang not in LANGS or page is None:
        return PlainTextResponse("Not found", status_code=404)
    data = await asyncio.to_thread(og_png, page, lang)
    return Response(data, media_type="image/png", headers={"Cache-Control": "public, max-age=86400"})


async def manifest_route(request: Request) -> Response:
    lang = request.query_params.get("lang", X_DEFAULT_LANG)
    lang = lang if lang in LANGS else X_DEFAULT_LANG
    t = _t(lang)
    body = seo_static.webmanifest(lang, t("seo.brand"), t("seo.brand"), t("seo.home.description"))
    return Response(body, media_type="application/manifest+json", headers={"Cache-Control": "public, max-age=86400"})


def verification_file_name() -> Optional[str]:
    """`google<токен>.html` из GOOGLE_SITE_VERIFICATION_FILE — только такое имя:
    переменная не должна открывать произвольный путь."""
    name = (config.GOOGLE_SITE_VERIFICATION_FILE or "").strip().lstrip("/")
    return name if re.fullmatch(r"google[0-9a-zA-Z]+\.html", name) else None


def register_routes(server: Any) -> None:
    for page in pages():
        for lang in LANGS:
            server.custom_route(url_for(page, lang), methods=["GET"])(_page_route(page, lang))
    for tool in EMBEDS:
        for lang in LANGS:
            server.custom_route(f"/embed/{lang}/{tool}", methods=["GET"])(_embed_route(tool, lang))
    server.custom_route("/", methods=["GET"])(root_route)
    server.custom_route("/go/bot", methods=["GET"])(go_bot_route)
    server.custom_route("/go/appstore", methods=["GET"])(go_appstore_route)
    server.custom_route("/sitemap.xml", methods=["GET"])(sitemap_route)
    server.custom_route("/robots.txt", methods=["GET"])(robots_route)
    server.custom_route("/llms.txt", methods=["GET"])(llms_route)
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
    # Последними: любой другой адрес под /ru/ и /en/ — своя страница 404.
    for lang in LANGS:
        server.custom_route(f"/{lang}/{{rest:path}}", methods=["GET"])(_not_found_route(lang))
