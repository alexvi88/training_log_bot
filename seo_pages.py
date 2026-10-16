"""Публичные страницы для поиска: переезд из Hevy/Strong, калькуляторы,
готовые программы, техника упражнений, рассказ о боте и сравнения.

Зачем: первое касание человека, который ищет «как перенести тренировки из
Hevy» или «калькулятор 1ПМ», — поиск, а не каталог ботов. Каждая страница
ведёт в бота ссылкой со своей меткой `?start=src_seo_<блок>` (acquisition.py),
так что `/growth` показывает, какой раздел приводит людей, которые доходят до
первой тренировки.

Язык — в пути: `/ru/...` и `/en/...`. У каждой страницы ровно один адрес на
язык, его и называет `canonical`, а пары связаны `hreflang` (x-default —
английская). Это чище для поисковика, чем `?lang=` у /privacy: тот выбирает
язык по заголовку, и один адрес отдаёт два текста. Голый `/` — выбор языка
тем же `legal_pages.page_lang` и редирект.

Висят на приложении MCP-сервера через `custom_route`, как /privacy
(legal_pages.py): все пути, кроме `/v1`, уходят туда. Картинки упражнений —
уже публичный `/v1/media/exercises/<файл>` (api_v1_media.get_media_file),
своего раздатчика здесь нет.

Тексты — каталог (`seo.*` в locales/ru.json и en.json), данные каталога —
через те же локализаторы, что у бота и /v1 (seed_data.localized_*,
exercise_descriptions.get_description): русское имя упражнения здесь только
идентичность для поиска картинки и описания, на экран идёт локализованное.
Строк, вычисленных на импорте, нет — всё собирается в момент запроса.

Ключи с суффиксом `_html` — доверенная разметка из каталога (списки, ссылки,
жирный) и подставляются как есть; остальные экранируются.

Аналитики на страницах нет: ни счётчиков, ни cookies, ни сторонних скриптов —
политику конфиденциальности они не трогают.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from html import escape
from typing import Any, Callable, Optional

from starlette.requests import Request
from starlette.responses import FileResponse, HTMLResponse, PlainTextResponse, RedirectResponse, Response

import config
import exercise_descriptions
import exercise_media
import i18n
import legal_pages
import seed_data
import seo_tools

LANGS = ("ru", "en")
X_DEFAULT_LANG = "en"
BOT_TAG_PREFIX = "src_seo_"

# Упражнения с отдельной страницей техники: адрес → слаг картинок
# (exercise_media.EXERCISE_IMAGE_SLUGS). Только те, у которых есть пара
# кадров и описание на обоих языках — это держит тест.
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
)
_EXERCISE_BY_URL = dict(EXERCISES)

IMPORT_SOURCES = ("hevy", "strong", "csv")
TOOLS = ("1rm", "plates", "wilks")
COMPETITORS = ("hevy", "strong")
FEATURES = ("ai-coach", "video-review", "telegram")

# Метка у ссылки в бота — по разделу, не по странице: слаг acquisition режет
# до 32 символов, а ссылка `t.me/<бот>?start=src_seo_<блок>` целиком должна
# влезть в 48 — так же, как у каталогов ботов (GROWTH.md).
_BLOCKS = {
    "home": "home",
    "ai-coach": "ai",
    "video-review": "video",
    "telegram": "tg",
    "import:hevy": "imp_hevy",
    "import:strong": "imp_strng",
    "import:csv": "imp_csv",
    "tool:1rm": "rm1",
    "tool:plates": "plates",
    "tool:wilks": "wilks",
    "programs": "progs",
    "program": "prog",
    "exercises": "exs",
    "exercise": "ex",
    "vs:hevy": "vs_hevy",
    "vs:strong": "vs_strng",
}

# Сколько смысловых блоков у статьи-страницы (ключи seo.<id>.s<n>.h / .p_html).
_ARTICLE_SECTIONS = {"ai-coach": 4, "video-review": 4, "telegram": 6}

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

OG_IMAGE_PATH = "/og/coach.jpg"
_OG_IMAGE_FILE = os.path.join(os.path.dirname(__file__), "media", "push", "coach_incoming_call.jpg")
_APP_ID_RE = re.compile(r"/id(\d+)")


@dataclass(frozen=True)
class Page:
    kind: str
    arg: str = ""

    @property
    def path(self) -> str:
        """Путь без языка: "" у главной, "/tools/1rm" и т.д."""
        if self.kind == "home":
            return ""
        if self.kind in FEATURES:
            return f"/{self.kind}"
        if self.kind == "import":
            return f"/import/{self.arg}"
        if self.kind == "tool":
            return f"/tools/{self.arg}"
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
    def block(self) -> str:
        key = f"{self.kind}:{self.arg}" if self.kind in ("import", "tool", "vs") else self.kind
        return _BLOCKS[key]


def pages() -> list[Page]:
    result = [Page("home")]
    result += [Page(f) for f in FEATURES]
    result += [Page("import", s) for s in IMPORT_SOURCES]
    result += [Page("tool", t) for t in TOOLS]
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


# ---------- разметка ----------

_STYLE = """
:root { color-scheme: dark; --bg:#1a1210; --fg:#f3e6d8; --muted:#c9ab94; --accent:#f2a541;
  --card:#2a1b16; --line:#5b3426; --btn:#3a251e; --btn-line:#6b3d2c; }
* { box-sizing: border-box; }
html { -webkit-text-size-adjust: 100%; }
body { margin: 0; min-height: 100vh; color: var(--fg);
  font: 17px/1.55 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
  background: var(--bg) radial-gradient(circle at 50% -10%, #5a2a1c 0%, var(--bg) 55%) no-repeat; }
.wrap { max-width: 760px; margin: 0 auto; padding: 0 16px; }
header { padding: 18px 0 8px; }
.top { display: flex; align-items: center; justify-content: space-between; gap: 12px; }
.brand { font-size: 13px; letter-spacing: .12em; font-weight: 700; color: var(--accent); text-decoration: none; }
.lang { font-size: 14px; color: var(--muted); text-decoration: none; border: 1px solid var(--line);
  border-radius: 8px; padding: 3px 9px; }
nav { display: flex; flex-wrap: wrap; gap: 6px 16px; margin-top: 10px; font-size: 15px; }
nav a { color: var(--muted); text-decoration: none; }
nav a:hover, nav a[aria-current] { color: var(--fg); }
main { padding: 16px 0 24px; }
h1 { font-size: 30px; line-height: 1.2; margin: 8px 0 12px; overflow-wrap: anywhere; }
h2 { font-size: 21px; line-height: 1.3; margin: 32px 0 10px; }
h3 { font-size: 18px; margin: 18px 0 6px; }
p, li { overflow-wrap: anywhere; }
a { color: var(--accent); }
.lead { font-size: 19px; color: #f8ece0; }
.muted { color: var(--muted); font-size: 15px; }
ul, ol { padding-left: 22px; }
li { margin: 6px 0; }
code { font: 15px ui-monospace, SFMono-Regular, Menlo, monospace; background: var(--card);
  border: 1px solid var(--line); border-radius: 6px; padding: 1px 6px; }
.cta { margin: 24px 0; padding: 18px 16px; background: var(--card); border: 1px solid var(--line); border-radius: 16px; }
.cta p { margin: 0 0 12px; }
.btn { display: block; width: 100%; margin: 0 0 10px; padding: 14px 16px; text-align: center;
  font: 600 17px/1.2 inherit; font-family: inherit; text-decoration: none; cursor: pointer;
  color: var(--fg); background: var(--btn); border: 1px solid var(--btn-line); border-radius: 12px; }
.btn.primary { color: var(--bg); background: var(--accent); border-color: var(--accent); }
.btn:last-child { margin-bottom: 0; }
.grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(220px, 1fr)); gap: 12px; margin: 16px 0; }
.card { display: block; padding: 14px; background: var(--card); border: 1px solid var(--line);
  border-radius: 14px; color: var(--fg); text-decoration: none; }
.card b { display: block; margin-bottom: 4px; }
.card span { color: var(--muted); font-size: 15px; }
.card img { display: block; width: 100%; aspect-ratio: 4/3; object-fit: cover; border-radius: 10px; margin-bottom: 8px; background: #000; }
.frames { display: grid; grid-template-columns: 1fr 1fr; gap: 8px; margin: 12px 0; }
.frames img { width: 100%; height: auto; border-radius: 12px; background: #000; }
table { width: 100%; border-collapse: collapse; margin: 12px 0; font-size: 15px; }
th, td { text-align: left; vertical-align: top; padding: 8px 6px; border-bottom: 1px solid var(--line); }
th { color: var(--muted); font-weight: 600; }
.scroll { overflow-x: auto; }
form.calc { display: grid; gap: 12px; padding: 16px; background: var(--card); border: 1px solid var(--line); border-radius: 16px; }
label { display: grid; gap: 4px; font-size: 15px; color: var(--muted); }
input, select { font: inherit; color: var(--fg); background: var(--bg); border: 1px solid var(--btn-line);
  border-radius: 10px; padding: 10px 12px; width: 100%; }
.row { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; }
.checks { display: flex; flex-wrap: wrap; gap: 8px 14px; color: var(--fg); }
.checks label { display: flex; align-items: center; gap: 6px; color: var(--fg); }
.checks input { width: auto; }
.result { margin: 16px 0; padding: 16px; border: 1px solid var(--accent); border-radius: 16px; }
.big { font-size: 32px; font-weight: 700; color: var(--accent); }
.plate { display: inline-block; margin: 3px; padding: 4px 9px; border-radius: 8px; background: var(--btn);
  border: 1px solid var(--btn-line); font-weight: 600; }
footer { padding: 20px 0 40px; border-top: 1px solid var(--line); font-size: 14px; color: var(--muted); }
footer a { color: var(--muted); }
footer p { margin: 6px 0; }
"""


def _t(lang: str) -> Callable[..., str]:
    return lambda key, **kw: i18n.t_in(lang, key, **kw)


def _e(text: str) -> str:
    return escape(text, quote=True)


def _strip_emoji(text: str) -> str:
    """«🌿 Всё тело — 2 дня» → «Всё тело — 2 дня» для <title>."""
    return re.sub(r"^[^\w(]+", "", text).strip()


def _cta(lang: str, block: str, text: str) -> str:
    t = _t(lang)
    parts = [f"<div class=cta><p>{text}</p>",
             f'<a class="btn primary" href="{_e(bot_link(block))}">{_e(t("seo.cta.bot"))}</a>']
    store = config.app_store_url()
    if store:
        parts.append(f'<a class=btn href="{_e(store)}">{_e(t("seo.cta.app_store"))}</a>')
    parts.append("</div>")
    return "".join(parts)


def _nav(lang: str, current: Page) -> str:
    t = _t(lang)
    items = (
        (Page("import", "hevy"), "seo.nav.import"),
        (Page("tool", "1rm"), "seo.nav.tools"),
        (Page("programs"), "seo.nav.programs"),
        (Page("exercises"), "seo.nav.exercises"),
        (Page("ai-coach"), "seo.nav.ai"),
    )
    links = []
    for page, key in items:
        here = " aria-current=page" if page.path.split("/")[1:2] == current.path.split("/")[1:2] else ""
        links.append(f'<a href="{url_for(page, lang)}"{here}>{_e(t(key))}</a>')
    return "<nav>" + "".join(links) + "</nav>"


def _layout(lang: str, page: Page, request: Optional[Request], *, title: str, description: str,
            body: str, og_image: Optional[str] = None) -> str:
    t = _t(lang)
    base = base_url(request)
    canonical = base + url_for(page, lang)
    other = "en" if lang == "ru" else "ru"
    head = [
        f"<!doctype html><html lang={lang}><head><meta charset=utf-8>",
        '<meta name=viewport content="width=device-width, initial-scale=1">',
        f"<title>{_e(title)}</title>",
        f'<meta name=description content="{_e(description)}">',
        f'<link rel=canonical href="{_e(canonical)}">',
    ]
    for code in LANGS:
        head.append(f'<link rel=alternate hreflang={code} href="{_e(base + url_for(page, code))}">')
    head.append(f'<link rel=alternate hreflang=x-default href="{_e(base + url_for(page, X_DEFAULT_LANG))}">')
    image = og_image or (base + OG_IMAGE_PATH)
    head += [
        '<meta property="og:type" content="website">',
        f'<meta property="og:site_name" content="{_e(t("seo.brand"))}">',
        f'<meta property="og:title" content="{_e(title)}">',
        f'<meta property="og:description" content="{_e(description)}">',
        f'<meta property="og:url" content="{_e(canonical)}">',
        f'<meta property="og:locale" content="{"ru_RU" if lang == "ru" else "en_US"}">',
        f'<meta property="og:image" content="{_e(image)}">',
        '<meta name="twitter:card" content="summary">',
    ]
    app_id = app_store_id()
    if app_id:
        head.append(f'<meta name="apple-itunes-app" content="app-id={app_id}">')
    verification = config.GOOGLE_SITE_VERIFICATION
    if verification:
        head.append(f'<meta name="google-site-verification" content="{_e(verification)}">')
    head.append(f"<style>{_STYLE}</style></head>")
    header = (
        '<header><div class=wrap><div class=top>'
        f'<a class=brand href="{url_for(Page("home"), lang)}">{_e(t("bot.name"))}</a>'
        f'<a class=lang href="{url_for(page, other)}" hreflang={other} lang={other}>{other.upper()}</a>'
        f"</div>{_nav(lang, page)}</div></header>"
    )
    footer = (
        "<footer><div class=wrap>"
        f"<p>{t('seo.footer.about_html', bot=_e(bot_link(page.block)))}</p>"
        f'<p><a href="{url_for(Page("import", "hevy"), lang)}">{_e(t("seo.import.hevy.short"))}</a> · '
        f'<a href="{url_for(Page("import", "strong"), lang)}">{_e(t("seo.import.strong.short"))}</a> · '
        f'<a href="{url_for(Page("import", "csv"), lang)}">{_e(t("seo.import.csv.short"))}</a> · '
        f'<a href="{url_for(Page("tool", "1rm"), lang)}">{_e(t("seo.tool.1rm.short"))}</a> · '
        f'<a href="{url_for(Page("tool", "plates"), lang)}">{_e(t("seo.tool.plates.short"))}</a> · '
        f'<a href="{url_for(Page("tool", "wilks"), lang)}">{_e(t("seo.tool.wilks.short"))}</a> · '
        f'<a href="{url_for(Page("vs", "hevy"), lang)}">{_e(t("seo.vs.hevy.short"))}</a> · '
        f'<a href="{url_for(Page("vs", "strong"), lang)}">{_e(t("seo.vs.strong.short"))}</a></p>'
        f'<p><a href="/privacy?lang={lang}">{_e(t("seo.footer.privacy"))}</a> · '
        f'<a href="/terms?lang={lang}">{_e(t("seo.footer.terms"))}</a> · '
        f'<a href="/support?lang={lang}">{_e(t("seo.footer.support"))}</a></p>'
        "</div></footer>"
    )
    return "".join(head) + f"<body>{header}<main><div class=wrap>{body}</div></main>{footer}</body></html>"


# ---------- страницы ----------


def _cards(items: list[tuple[str, str, str]]) -> str:
    """(href, заголовок, подпись) → сетка карточек."""
    return "<div class=grid>" + "".join(
        f'<a class=card href="{href}"><b>{_e(title)}</b><span>{_e(text)}</span></a>'
        for href, title, text in items
    ) + "</div>"


def _render_home(lang: str, page: Page, request) -> str:
    t = _t(lang)
    features = [
        (url_for(Page(f), lang), t(f"seo.{f}.card.title"), t(f"seo.{f}.card.text")) for f in FEATURES
    ]
    imports = [
        (url_for(Page("import", s), lang), t(f"seo.import.{s}.short"), t(f"seo.import.{s}.card")) for s in IMPORT_SOURCES
    ]
    tools = [
        (url_for(Page("tool", s), lang), t(f"seo.tool.{s}.short"), t(f"seo.tool.{s}.card")) for s in TOOLS
    ]
    library = [
        (url_for(Page("programs"), lang), t("seo.programs.h1"), t("seo.programs.card")),
        (url_for(Page("exercises"), lang), t("seo.exercises.h1"), t("seo.exercises.card")),
    ]
    body = (
        f"<h1>{_e(t('seo.home.h1'))}</h1><p class=lead>{t('seo.home.lead_html')}</p>"
        + _cta(lang, page.block, _e(t("seo.home.cta")))
        + f"<h2>{_e(t('seo.home.how.h'))}</h2>{t('seo.home.how.p_html')}"
        + f"<h2>{_e(t('seo.home.features.h'))}</h2>" + _cards(features)
        + f"<h2>{_e(t('seo.home.import.h'))}</h2><p>{_e(t('seo.home.import.p'))}</p>" + _cards(imports)
        + f"<h2>{_e(t('seo.home.tools.h'))}</h2>" + _cards(tools)
        + f"<h2>{_e(t('seo.home.library.h'))}</h2>" + _cards(library)
        + f"<h2>{_e(t('seo.home.free.h'))}</h2><p>{_e(t('seo.home.free.p', questions=config.AI_QUESTION_DAILY_LIMIT, videos=config.AI_VIDEO_DAILY_LIMIT))}</p>"
    )
    return _layout(lang, page, request, title=t("seo.home.title"), description=t("seo.home.description"), body=body)


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


def _render_feature(lang: str, page: Page, request) -> str:
    t = _t(lang)
    key = page.kind
    params = _article_params(lang)
    sections = "".join(
        f"<h2>{_e(t(f'seo.{key}.s{i}.h'))}</h2>{t(f'seo.{key}.s{i}.p_html', **params)}"
        for i in range(1, _ARTICLE_SECTIONS[key] + 1)
    )
    related = [(url_for(Page(f), lang), t(f"seo.{f}.card.title"), t(f"seo.{f}.card.text")) for f in FEATURES if f != key]
    body = (
        f"<h1>{_e(t(f'seo.{key}.h1'))}</h1><p class=lead>{_e(t(f'seo.{key}.lead'))}</p>"
        + sections
        + _cta(lang, page.block, _e(t(f"seo.{key}.cta")))
        + _cards(related)
    )
    return _layout(lang, page, request, title=t(f"seo.{key}.title"), description=t(f"seo.{key}.description"), body=body)


def _render_import(lang: str, page: Page, request) -> str:
    t = _t(lang)
    src = page.arg
    params = _article_params(lang)
    others = [
        (url_for(Page("import", s), lang), t(f"seo.import.{s}.short"), t(f"seo.import.{s}.card"))
        for s in IMPORT_SOURCES if s != src
    ]
    body = (
        f"<h1>{_e(t(f'seo.import.{src}.h1'))}</h1><p class=lead>{_e(t(f'seo.import.{src}.lead'))}</p>"
        f"<h2>{_e(t(f'seo.import.{src}.export.h'))}</h2>{t(f'seo.import.{src}.export_html')}"
        f"<h2>{_e(t('seo.import.bot.h'))}</h2>{t('seo.import.bot_html', **params)}"
        f"<h2>{_e(t('seo.import.app.h'))}</h2>{t('seo.import.app_html')}"
        f"<h2>{_e(t('seo.import.what.h'))}</h2>{t('seo.import.what_html', **params)}"
        + _cta(lang, page.block, _e(t("seo.import.cta")))
        + f"<h2>{_e(t('seo.import.more.h'))}</h2>" + _cards(others)
        + f'<p><a href="{url_for(Page("home"), lang)}">{_e(t("seo.back_home"))}</a></p>'
    )
    return _layout(lang, page, request, title=t(f"seo.import.{src}.title"),
                   description=t(f"seo.import.{src}.description"), body=body)


def _unit_select(lang: str, name: str = "unit") -> str:
    t = _t(lang)
    return (f'<label>{_e(t("seo.tool.unit"))}<select id={name}>'
            f'<option value=kg>{_e(t("seo.tool.unit_kg"))}</option>'
            f'<option value=lb>{_e(t("seo.tool.unit_lb"))}</option></select></label>')


def _tool_1rm(lang: str) -> tuple[str, str]:
    t = _t(lang)
    form = (
        "<form class=calc onsubmit='return false'>"
        f'<div class=row><label>{_e(t("seo.tool.1rm.weight"))}<input id=w type=number inputmode=decimal min=0 step=0.25 value=100></label>'
        f'<label>{_e(t("seo.tool.1rm.reps"))}<input id=r type=number inputmode=numeric min=1 max=50 step=1 value=5></label></div>'
        f'<div class=row><label>{_e(t("seo.tool.1rm.rpe"))}<select id=rpe><option value="">—</option>'
        + "".join(f"<option>{v}</option>" for v in ("10", "9.5", "9", "8.5", "8", "7.5", "7", "6"))
        + "</select></label>"
        f'<label>{_e(t("seo.tool.1rm.formula"))}<select id=f><option value=epley>Epley</option>'
        "<option value=brzycki>Brzycki</option></select></label></div>"
        + _unit_select(lang)
        + "</form>"
        f'<div class=result aria-live=polite><div class=muted>{_e(t("seo.tool.1rm.result"))}</div>'
        '<div class=big id=out>—</div><div class=muted id=note></div></div>'
        f'<h2>{_e(t("seo.tool.1rm.table"))}</h2><div class=scroll><table><thead><tr>'
        f'<th>{_e(t("seo.tool.1rm.col_pct"))}</th><th>{_e(t("seo.tool.1rm.col_weight"))}</th></tr></thead>'
        "<tbody id=tbl></tbody></table></div>"
    )
    script = (
        "var too = " + _js_str(t("seo.tool.1rm.too_many", max=config.E1RM_MAX_REPS)) + ";"
        "function run(){var w=parseFloat(document.getElementById('w').value),r=parseInt(document.getElementById('r').value,10),"
        "p=document.getElementById('rpe').value,rpe=p===''?null:parseFloat(p),f=document.getElementById('f').value,"
        "u=document.getElementById('unit').value,out=document.getElementById('out'),note=document.getElementById('note'),"
        "tb=document.getElementById('tbl');tb.innerHTML='';note.textContent='';"
        "if(!(w>0)||!(r>0)){out.textContent='—';return;}var m=e1rm(w,r,f,rpe);"
        "if(!m){out.textContent='—';note.textContent=too;return;}out.textContent=fmt(m)+UNITS[u];"
        "C.percents.forEach(function(pc){var tr=document.createElement('tr');tr.innerHTML='<td>'+pc+'%</td><td>'+fmt(m*pc/100)+UNITS[u]+'</td>';tb.appendChild(tr);});}"
        "['w','r','rpe','f','unit'].forEach(function(id){var el=document.getElementById(id);el.addEventListener('input',run);el.addEventListener('change',run);});run();"
    )
    return form, script


def _tool_plates(lang: str) -> tuple[str, str]:
    t = _t(lang)
    checks = "".join(
        f'<span class=checks data-unit={unit}>' + "".join(
            f'<label><input type=checkbox value="{_num(p)}"'
            f'{" checked" if p in seo_tools.DEFAULT_PLATES[unit] else ""}>{_num(p)}</label>'
            for p in seo_tools.PLATES[unit]
        ) + "</span>"
        for unit in ("kg", "lb")
    )
    form = (
        "<form class=calc onsubmit='return false'>"
        + _unit_select(lang)
        + f'<div class=row><label>{_e(t("seo.tool.plates.target"))}<input id=target type=number inputmode=decimal min=0 step=0.25 value=100></label>'
        f'<label>{_e(t("seo.tool.plates.bar"))}<input id=bar type=number inputmode=decimal min=0 step=0.5 value=20></label></div>'
        f'<div><div class=muted>{_e(t("seo.tool.plates.available"))}</div>{checks}</div>'
        "</form>"
        f'<div class=result aria-live=polite><div class=muted>{_e(t("seo.tool.plates.per_side"))}</div>'
        '<div id=plates></div><div class=muted id=note></div></div>'
    )
    bars = {u: seo_tools.BARS[u][0] for u in ("kg", "lb")}
    script = (
        f"var BARS={bars!r};".replace("'", '"')
        + "var EMPTY=" + _js_str(t("seo.tool.plates.empty")) + ",GOT=" + _js_str(t("seo.tool.plates.got", total="{total}")) + ";"
        "function shown(){var u=document.getElementById('unit').value;document.querySelectorAll('.checks').forEach(function(s){s.style.display=s.dataset.unit===u?'flex':'none';});return u;}"
        "function run(){var u=shown(),tg=parseFloat(document.getElementById('target').value),b=parseFloat(document.getElementById('bar').value)||0,"
        "list=[],box=document.getElementById('plates'),note=document.getElementById('note');"
        "document.querySelectorAll('.checks[data-unit=\"'+u+'\"] input:checked').forEach(function(i){list.push(parseFloat(i.value));});"
        "box.innerHTML='';note.textContent='';if(!(tg>0))return;var res=platesPerSide(tg,b,list);"
        "if(!res.plates.length){box.textContent=EMPTY;}else{res.plates.forEach(function(p){var s=document.createElement('span');s.className='plate';s.textContent=fmt(p)+UNITS[u];box.appendChild(s);});}"
        "if(Math.abs(res.total-tg)>1e-9){note.textContent=GOT.replace('{total}',fmt(res.total)+UNITS[u]);}}"
        "document.getElementById('unit').addEventListener('change',function(){document.getElementById('bar').value=BARS[this.value];"
        "document.getElementById('target').value=this.value==='lb'?225:100;run();});"
        "document.querySelectorAll('input').forEach(function(el){el.addEventListener('input',run);el.addEventListener('change',run);});run();"
    )
    return form, script


def _num(value: float) -> str:
    return f"{value:g}"


def _tool_wilks(lang: str) -> tuple[str, str]:
    t = _t(lang)
    form = (
        "<form class=calc onsubmit='return false'>"
        f'<div class=row><label>{_e(t("seo.tool.wilks.total"))}<input id=total type=number inputmode=decimal min=0 step=0.5 value=500></label>'
        f'<label>{_e(t("seo.tool.wilks.bw"))}<input id=bw type=number inputmode=decimal min=0 step=0.1 value=80></label></div>'
        f'<div class=row><label>{_e(t("seo.tool.wilks.sex"))}<select id=sex>'
        f'<option value=m>{_e(t("seo.tool.wilks.male"))}</option><option value=f>{_e(t("seo.tool.wilks.female"))}</option></select></label>'
        + _unit_select(lang) + "</div></form>"
        '<div class=result aria-live=polite><div class=row>'
        '<div><div class=muted>DOTS</div><div class=big id=dots>—</div></div>'
        '<div><div class=muted>Wilks</div><div class=big id=wilks>—</div></div></div></div>'
    )
    script = (
        "function run(){var tot=parseFloat(document.getElementById('total').value),bw=parseFloat(document.getElementById('bw').value),"
        "s=document.getElementById('sex').value,u=document.getElementById('unit').value,k=u==='lb'?1/C.lbPerKg:1;"
        "var d=document.getElementById('dots'),wk=document.getElementById('wilks');"
        "if(!(tot>0)||!(bw>0)){d.textContent='—';wk.textContent='—';return;}"
        "d.textContent=dots(tot*k,bw*k,s).toFixed(2);wk.textContent=wilks(tot*k,bw*k,s).toFixed(2);}"
        "['total','bw','sex','unit'].forEach(function(id){var el=document.getElementById(id);el.addEventListener('input',run);el.addEventListener('change',run);});run();"
    )
    return form, script


def _units_js(lang: str) -> str:
    t = _t(lang)
    return "{kg:" + _js_str(t("seo.tool.unit_kg")) + ",lb:" + _js_str(t("seo.tool.unit_lb")) + "}"


def _js_str(text: str) -> str:
    """Строка каталога → JS-литерал; `</` не закрывает <script> раньше времени."""
    return json.dumps(text, ensure_ascii=False).replace("</", "<\\/")


_TOOLS: dict[str, Callable[[str], tuple[str, str]]] = {"1rm": _tool_1rm, "plates": _tool_plates, "wilks": _tool_wilks}


def _render_tool(lang: str, page: Page, request) -> str:
    t = _t(lang)
    tool = page.arg
    form, script = _TOOLS[tool](lang)
    others = [(url_for(Page("tool", s), lang), t(f"seo.tool.{s}.short"), t(f"seo.tool.{s}.card")) for s in TOOLS if s != tool]
    body = (
        f"<h1>{_e(t(f'seo.tool.{tool}.h1'))}</h1><p class=lead>{_e(t(f'seo.tool.{tool}.lead'))}</p>"
        + form
        + f"<noscript><p class=muted>{_e(t('seo.tool.noscript'))}</p></noscript>"
        + f"<h2>{_e(t(f'seo.tool.{tool}.how.h'))}</h2>{t(f'seo.tool.{tool}.how_html', max=config.E1RM_MAX_REPS, **_article_params(lang))}"
        + _cta(lang, page.block, _e(t(f"seo.tool.{tool}.cta")))
        + _cards(others)
        + f"<script>{seo_tools.calc_js()}var UNITS={_units_js(lang)};{script}</script>"
    )
    return _layout(lang, page, request, title=t(f"seo.tool.{tool}.title"),
                   description=t(f"seo.tool.{tool}.description"), body=body)


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
        (url_for(Page("program", p["key"]), lang), seed_data.localized_program_name(p["key"], lang),
         seed_data.localized_program_meta(p["key"], lang))
        for p in seed_data.WORKOUT_PROGRAMS
    ]
    body = (
        f"<h1>{_e(t('seo.programs.h1'))}</h1><p class=lead>{_e(t('seo.programs.lead'))}</p>"
        + _cards(cards)
        + f"<h2>{_e(t('seo.programs.take.h'))}</h2>{t('seo.programs.take_html', **_article_params(lang))}"
        + _cta(lang, page.block, _e(t("seo.programs.cta")))
    )
    return _layout(lang, page, request, title=t("seo.programs.title"), description=t("seo.programs.description"), body=body)


def _render_program(lang: str, page: Page, request) -> str:
    t = _t(lang)
    key = page.arg
    program = seed_data.PROGRAM_BY_KEY[key]
    name = seed_data.localized_program_name(key, lang)
    days = []
    for i, (_day_name, exercises) in enumerate(program["days"]):
        rows = []
        for identity, target in exercises:
            shown = _e(seed_data.localized_exercise_name(identity, lang))
            url = _exercise_url_for_identity(identity)
            if url:
                shown = f'<a href="{url_for(Page("exercise", url), lang)}">{shown}</a>'
            rows.append(f"<tr><td>{shown}</td><td>{_e(seed_data.localized_target(target, lang) or '')}</td></tr>")
        days.append(
            f"<h3>{_e(seed_data.localized_program_day_name(key, i, lang))}</h3>"
            f"<div class=scroll><table><thead><tr><th>{_e(t('seo.program.col_exercise'))}</th>"
            f"<th>{_e(t('seo.program.col_target'))}</th></tr></thead><tbody>{''.join(rows)}</tbody></table></div>"
        )
    others = [
        (url_for(Page("program", p["key"]), lang), seed_data.localized_program_name(p["key"], lang),
         seed_data.localized_program_meta(p["key"], lang))
        for p in seed_data.WORKOUT_PROGRAMS if p["key"] != key
    ]
    description = seed_data.localized_program_description(key, lang)
    body = (
        f"<h1>{_e(name)}</h1><p class=muted>{_e(seed_data.localized_program_meta(key, lang))}</p>"
        f"<p class=lead>{_e(description)}</p>"
        f"<h2>{_e(t('seo.program.days.h'))}</h2>{''.join(days)}"
        f"<p class=muted>{_e(t('seo.program.target_note'))}</p>"
        + _cta(lang, page.block, t("seo.program.cta_html", **_article_params(lang), name=_e(name)))
        + f"<h2>{_e(t('seo.program.more.h'))}</h2>" + _cards(others)
    )
    title = t("seo.program.title", name=_strip_emoji(name))
    return _layout(lang, page, request, title=title, description=description, body=body)


def _media_url(filename: str, thumb: bool = False) -> str:
    return f"/v1/media/exercises/{'thumbs/' if thumb else ''}{filename}"


def _render_exercises(lang: str, page: Page, request) -> str:
    t = _t(lang)
    cards = []
    for url, slug in EXERCISES:
        identity = _identity_for_media_slug(slug)
        name = seed_data.localized_exercise_name(identity, lang)
        thumb = os.path.join(exercise_media.THUMBS_DIR, f"{slug}_1.jpg")
        src = _media_url(f"{slug}_1.jpg", thumb=os.path.isfile(thumb))
        group = _group_for(identity, lang)
        cards.append(
            f'<a class=card href="{url_for(Page("exercise", url), lang)}">'
            f'<img src="{src}" alt="{_e(name)}" loading=lazy width=240 height=180>'
            f"<b>{_e(name)}</b><span>{_e(group)}</span></a>"
        )
    body = (
        f"<h1>{_e(t('seo.exercises.h1'))}</h1><p class=lead>{_e(t('seo.exercises.lead'))}</p>"
        f"<div class=grid>{''.join(cards)}</div>"
        + _cta(lang, page.block, _e(t("seo.exercise.cta")))
        + f"<p class=muted>{t('seo.exercise.credit_html')}</p>"
    )
    return _layout(lang, page, request, title=t("seo.exercises.title"), description=t("seo.exercises.description"), body=body)


def _group_for(identity: str, lang: str) -> str:
    for group, name in seed_data.EXERCISE_TEMPLATES:
        if name == identity:
            return seed_data.localized_muscle_group_name(group, lang)
    return ""


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
        f'<img src="{_media_url(f"{slug}_1.jpg")}" alt="{_e(t("seo.exercise.frame_start", name=name))}" width=400 height=300>'
        f'<img src="{_media_url(f"{slug}_2.jpg")}" alt="{_e(t("seo.exercise.frame_end", name=name))}" width=400 height=300 loading=lazy>'
        "</div>"
    )
    steps = "".join(f"<li>{_e(s)}</li>" for s in _steps(identity, lang))
    mistakes = "".join(f"<li>{_e(m)}</li>" for m in t(f"seo.ex.{slug}.mistakes").split("\n") if m.strip())
    programs = _program_pages_for(identity)
    if programs:
        links = "".join(
            f'<li><a href="{url_for(Page("program", k), lang)}">{_e(seed_data.localized_program_name(k, lang))}</a></li>'
            for k in programs
        )
        in_programs = f"<h2>{_e(t('seo.exercise.programs.h'))}</h2><ul>{links}</ul>"
    else:
        in_programs = ""
    lead = t(f"seo.ex.{slug}.lead")
    body = (
        f"<h1>{_e(name)}</h1><p class=muted>{_e(_group_for(identity, lang))}</p>"
        f"<p class=lead>{_e(lead)}</p>{frames}"
        f"<h2>{_e(t('seo.exercise.setup.h'))}</h2><p>{_e(t(f'seo.ex.{slug}.setup'))}</p>"
        f"<h2>{_e(t('seo.exercise.steps.h'))}</h2><ol>{steps}</ol>"
        f"<h2>{_e(t('seo.exercise.mistakes.h'))}</h2><ul>{mistakes}</ul>"
        + in_programs
        + _cta(lang, page.block, _e(t("seo.exercise.cta")))
        + f'<p><a href="{url_for(Page("exercises"), lang)}">{_e(t("seo.exercise.all"))}</a></p>'
        + f"<p class=muted>{t('seo.exercise.credit_html')}</p>"
    )
    return _layout(lang, page, request, title=t("seo.exercise.title", name=name), description=lead,
                   body=body, og_image=base + _media_url(f"{slug}_1.jpg"))


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
    body = (
        f"<h1>{_e(t(f'seo.vs.{rival}.h1'))}</h1><p class=lead>{_e(t(f'seo.vs.{rival}.lead'))}</p>"
        f"<p class=muted>{_e(t('seo.vs.asof'))}</p>"
        f"<div class=scroll><table><thead><tr><th></th><th>{_e(t('bot.name'))}</th>"
        f"<th>{_e(t(f'seo.vs.{rival}.name'))}</th></tr></thead><tbody>{rows}</tbody></table></div>"
        f"<h2>{_e(t('seo.vs.choose.h'))}</h2>{t(f'seo.vs.{rival}.choose_html')}"
        f"<h2>{_e(t('seo.vs.move.h'))}</h2>"
        f'<p>{t("seo.vs.move_html", href=url_for(Page("import", rival), lang), name=_e(t(f"seo.vs.{rival}.name")))}</p>'
        + _cta(lang, page.block, _e(t("seo.vs.cta")))
        + f"<h2>{_e(t('seo.vs.sources.h'))}</h2><ul class=muted>{sources}</ul>"
        f"<p class=muted>{_e(t('seo.vs.disclaimer'))}</p>"
    )
    return _layout(lang, page, request, title=t(f"seo.vs.{rival}.title"),
                   description=t(f"seo.vs.{rival}.description"), body=body)


_RENDERERS: dict[str, Callable[[str, Page, Optional[Request]], str]] = {
    "home": _render_home,
    "ai-coach": _render_feature,
    "video-review": _render_feature,
    "telegram": _render_feature,
    "import": _render_import,
    "tool": _render_tool,
    "programs": _render_programs,
    "program": _render_program,
    "exercises": _render_exercises,
    "exercise": _render_exercise,
    "vs": _render_vs,
}


def render(page: Page, lang: str, request: Optional[Request] = None) -> str:
    return _RENDERERS[page.kind](lang, page, request)


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


def robots_txt(request: Optional[Request] = None) -> str:
    return f"User-agent: *\nAllow: /\n\nSitemap: {base_url(request)}/sitemap.xml\n"


def _html(content: str, lang: str) -> HTMLResponse:
    return HTMLResponse(content, headers={"Cache-Control": "public, max-age=3600", "Content-Language": lang})


def _page_route(page: Page, lang: str):
    async def route(request: Request) -> HTMLResponse:
        return _html(render(page, lang, request), lang)

    return route


async def root_route(request: Request) -> Response:
    lang = legal_pages.page_lang(request)
    return RedirectResponse(url_for(Page("home"), lang), status_code=302, headers={"Vary": "Accept-Language"})


async def sitemap_route(request: Request) -> Response:
    return Response(sitemap_xml(request), media_type="application/xml",
                    headers={"Cache-Control": "public, max-age=3600"})


async def robots_route(request: Request) -> Response:
    return PlainTextResponse(robots_txt(request), headers={"Cache-Control": "public, max-age=3600"})


async def og_image_route(request: Request) -> Response:
    return FileResponse(_OG_IMAGE_FILE, media_type="image/jpeg",
                        headers={"Cache-Control": "public, max-age=86400"})


def verification_file_name() -> Optional[str]:
    """`google<токен>.html` из GOOGLE_SITE_VERIFICATION_FILE — только такое имя:
    переменная не должна открывать произвольный путь."""
    name = (config.GOOGLE_SITE_VERIFICATION_FILE or "").strip().lstrip("/")
    return name if re.fullmatch(r"google[0-9a-zA-Z]+\.html", name) else None


def register_routes(server: Any) -> None:
    for page in pages():
        for lang in LANGS:
            server.custom_route(url_for(page, lang), methods=["GET"])(_page_route(page, lang))
    server.custom_route("/", methods=["GET"])(root_route)
    server.custom_route("/sitemap.xml", methods=["GET"])(sitemap_route)
    server.custom_route("/robots.txt", methods=["GET"])(robots_route)
    server.custom_route(OG_IMAGE_PATH, methods=["GET"])(og_image_route)
    name = verification_file_name()
    if name:
        async def verification_route(request: Request) -> Response:
            return PlainTextResponse(f"google-site-verification: {name}")

        server.custom_route(f"/{name}", methods=["GET"])(verification_route)
