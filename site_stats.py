"""Счётчик посещений публичного сайта (seo_pages.py) — без cookies, без
сторонних скриптов и без адреса клиента.

Что пишется (`db.site_events`): вид события (`view` — открыл страницу,
`click` — нажал кнопку «в бота» или «в App Store» через `/go/*`, `embed` —
показ встроенного калькулятора на чужом сайте), ключ страницы (путь без
домена), язык, домен источника (`google`, `t.me`… или `direct` — без полного
адреса, по которому пришли), вид устройства (ios, android, desktop, bot),
имя поискового робота, `utm_source` и куда вёл клик. IP, User-Agent целиком и
что-либо, что связывает визит с человеком или аккаунтом, не пишется — поэтому
политика конфиденциальности описывает счётчик одной строкой.

**Человек — только подтверждённый браузером.** Серверный `view` — это запрос
страницы, и его шлёт кто угодно: скрейпер с браузерным User-Agent, который
обходит `/<язык>/exercises/*/alternatives` раз в полминуты, выглядел
«компьютером» и надувал «Просмотров людьми». Поэтому страница, показанная
на экране, сама отправляет маячок (`beacon_script`: `navigator.sendBeacon`
на `POST /go/seen`, без cookies и без идентификатора — только ключ
страницы, домен источника и utm_source) — это событие `seen`. Маячок не
уходит из предзагрузки (`document.prerendering`), из скрытой вкладки и из
браузера под автоматизацией (`navigator.webdriver`); робот, который
исполняет JS (рендер Googlebot), отсекается тем же `classify_ua` на
приёме, а `/go/` закрыт в robots.txt. Людьми в /growth и /activity
считаются `seen`; серверные `view` с человеческим устройством после
первого `seen` — «без подтверждения браузером». История до появления
маячка подтвердиться не может: до даты первого `seen` людьми считаются
старые серверные `view` (`db.SITE_HUMAN_SQL`), и /growth пишет дату
переключения.

**Кто такой «other».** Для робота без имени (общий шаблон «bot», curl,
headless…) и для «компьютера» в строку пишется семейство User-Agent
(`ua_family`: слово робота, `python-requests`, `Chrome/120 Windows`) — не
сам UA; сырой UA (первые 200 символов) — только в лог процесса, и только
у безымянных роботов и у «компьютеров», похожих на автомат (нет
Accept-Language или Sec-Fetch-Mode — их шлёт любой живой браузер).

Роботы (Googlebot, bingbot, GPTBot, OAI-SearchBot, PerplexityBot, ClaudeBot…)
считаются отдельно: обход — это не посетитель, но видеть, ходит ли к нам
поиск ChatGPT, полезно.

Запись не должна ронять страницу: база не открыта (тест, скрипт) или занята
— просмотр просто не посчитан, страница отдаётся как обычно.

Админские сводки здесь же (`format_growth_block`, `format_feed_line`) — по-русски,
как весь админский экран.
"""

from __future__ import annotations

import datetime as dt
import logging
import re
from typing import Any, Optional
from urllib.parse import urlsplit

import db

logger = logging.getLogger(__name__)

# (подстрока UA в нижнем регистре, имя робота). Порядок важен: OAI-SearchBot
# и ChatGPT-User раньше общего «bot».
CRAWLERS: tuple[tuple[str, str], ...] = (
    ("googlebot", "Googlebot"),
    ("google-inspectiontool", "Googlebot"),
    ("googleother", "GoogleOther"),
    ("adsbot-google", "Googlebot"),
    ("bingbot", "bingbot"),
    ("bingpreview", "bingbot"),
    ("yandex", "YandexBot"),
    ("oai-searchbot", "OAI-SearchBot"),
    ("chatgpt-user", "ChatGPT-User"),
    ("gptbot", "GPTBot"),
    ("perplexitybot", "PerplexityBot"),
    ("perplexity-user", "Perplexity-User"),
    ("claudebot", "ClaudeBot"),
    ("claude-user", "Claude-User"),
    ("claude-searchbot", "Claude-SearchBot"),
    ("mistralai-user", "MistralAI-User"),
    ("duckassistbot", "DuckAssistBot"),
    ("meta-externalfetcher", "Meta-ExternalFetcher"),
    ("meta-externalagent", "Meta-ExternalAgent"),
    ("applebot", "Applebot"),
    ("duckduckbot", "DuckDuckBot"),
    ("telegrambot", "TelegramBot"),
    ("twitterbot", "Twitterbot"),
    ("facebookexternalhit", "facebook"),
    ("slackbot", "Slackbot"),
    ("discordbot", "Discordbot"),
    ("whatsapp", "WhatsApp"),
    ("petalbot", "PetalBot"),
    ("ahrefsbot", "AhrefsBot"),
    ("semrushbot", "SemrushBot"),
    ("mj12bot", "MJ12bot"),
    ("bytespider", "Bytespider"),
    ("ccbot", "CCBot"),
    ("amazonbot", "Amazonbot"),
)
_GENERIC_BOT = re.compile(r"bot\b|crawler|spider|crawl|headless|python-requests|curl/|wget|httpx|go-http-client|lighthouse")

# Домен → короткое имя источника. Остальные домены пишутся как есть (без www).
# AI-ассистенты — раньше поисковиков: gemini.google.com иначе ушёл бы в «google»,
# copilot.microsoft.com — мимо «bing».
_REFERRER_NAMES: tuple[tuple[str, str], ...] = (
    ("gemini.google.com", "gemini"),
    ("bard.google.com", "gemini"),
    ("copilot.microsoft.com", "copilot"),
    ("copilot.cloud.microsoft", "copilot"),
    ("grok.com", "grok"),
    ("chat.deepseek.com", "deepseek"),
    ("you.com", "you"),
    ("chat.mistral.ai", "mistral"),
    ("meta.ai", "meta-ai"),
    ("phind.com", "phind"),
    ("google.", "google"),
    ("yandex.", "yandex"),
    ("ya.ru", "yandex"),
    ("bing.com", "bing"),
    ("duckduckgo.com", "duckduckgo"),
    ("chatgpt.com", "chatgpt"),
    ("chat.openai.com", "chatgpt"),
    ("perplexity.ai", "perplexity"),
    ("claude.ai", "claude"),
    ("t.me", "telegram"),
    ("telegram.org", "telegram"),
    ("web.telegram.org", "telegram"),
    ("vk.com", "vk"),
    ("reddit.com", "reddit"),
    ("habr.com", "habr"),
)
# Источники-ассистенты: переход человека из ответа AI. Для GEO это главная
# цифра — нас процитировали, и человек пошёл по ссылке.
AI_REFERRERS = frozenset({
    "chatgpt", "perplexity", "claude", "gemini", "copilot", "grok", "deepseek", "you", "mistral", "meta-ai", "phind",
})
# utm_source, которые ассистенты сами дописывают к ссылкам (ChatGPT —
# `utm_source=chatgpt.com`): переход без Referer всё равно засчитывается.
_AI_UTM = (
    ("chatgpt", "chatgpt"),
    ("openai", "chatgpt"),
    ("perplexity", "perplexity"),
    ("claude", "claude"),
    ("gemini", "gemini"),
    ("copilot", "copilot"),
    ("grok", "grok"),
    ("deepseek", "deepseek"),
)
# Роботы, которые приходят за страницей по вопросу человека прямо сейчас
# (а не обходом для индекса или обучения): ассистент процитирует ответ.
AI_ON_DEMAND = frozenset({
    "ChatGPT-User", "Perplexity-User", "Claude-User", "MistralAI-User", "DuckAssistBot", "Meta-ExternalFetcher",
})


def ai_source(referrer: Optional[str], utm: Optional[str]) -> Optional[str]:
    """Имя ассистента, из которого пришёл человек, — по источнику или по
    utm_source; `None` — не из AI."""
    if referrer in AI_REFERRERS:
        return referrer
    for needle, name in _AI_UTM:
        if utm and needle in utm:
            return name
    return None


_HOST_RE = re.compile(r"^[a-z0-9.-]{1,80}$")
_UTM_RE = re.compile(r"[^a-z0-9_.-]+")


def classify_ua(user_agent: Optional[str]) -> tuple[str, Optional[str]]:
    """(устройство, робот): устройство — ios | android | desktop | bot."""
    ua = (user_agent or "").lower()
    if not ua:
        return "bot", "unknown"
    for needle, name in CRAWLERS:
        if needle in ua:
            return "bot", name
    if _GENERIC_BOT.search(ua):
        return "bot", "other"
    if "iphone" in ua or "ipad" in ua or "ipod" in ua:
        return "ios", None
    if "android" in ua:
        return "android", None
    return "desktop", None


_BOT_WORD = re.compile(r"[a-z0-9][a-z0-9._-]*(?:bot|crawler|spider|crawl)[a-z0-9._-]*", re.I)
_PRODUCT = re.compile(r"([A-Za-z][A-Za-z0-9._-]{0,39})/")
# Общие слова браузерного UA — не имя клиента.
_UA_NOISE = frozenset({"mozilla", "applewebkit", "gecko", "khtml", "safari", "version", "mobile", "chrome", "firefox"})
_BROWSERS: tuple[tuple[str, str], ...] = (
    ("Edg/", "Edge"), ("OPR/", "Opera"), ("YaBrowser/", "Yandex"), ("Firefox/", "Firefox"), ("Chrome/", "Chrome"),
)
_OSES: tuple[tuple[str, str], ...] = (
    ("windows", "Windows"), ("cros", "ChromeOS"), ("macintosh", "Mac"), ("mac os x", "Mac"), ("linux", "Linux"),
)
_FAMILY_RE = re.compile(r"[^A-Za-z0-9._/ -]+")
RAW_UA_LOG_CHARS = 200


def ua_family(user_agent: Optional[str]) -> Optional[str]:
    """Семейство User-Agent для строки site_events — не сам UA: у безымянного
    робота — его слово («SeznamBot») или продукт («python-requests», «curl»,
    «HeadlessChrome»), у «компьютера» — браузер, мажорная версия и система
    («Chrome/120 Windows»). У телефонов и роботов с именем — None: их и так
    видно."""
    ua = (user_agent or "").strip()
    if not ua:
        return None
    device, crawler = classify_ua(ua)
    if device == "bot" and crawler == "other":
        hit = _BOT_WORD.search(ua)
        if hit:
            return _clean_family(hit.group(0))
        return _clean_family(_product_token(ua))
    if device != "desktop":
        return None
    low = ua.lower()
    os_name = next((name for needle, name in _OSES if needle in low), "?")
    for needle, name in _BROWSERS:
        at = ua.find(needle)
        if at >= 0:
            major = re.match(r"\d+", ua[at + len(needle):])
            return _clean_family(f"{name}/{major.group(0) if major else '?'} {os_name}")
    if "Safari/" in ua and "Version/" in ua:
        major = re.search(r"Version/(\d+)", ua)
        return _clean_family(f"Safari/{major.group(1) if major else '?'} {os_name}")
    return _clean_family(_product_token(ua))


def _product_token(ua: str) -> str:
    """Первый «продукт/версия», который не общее слово браузера; иначе — первое слово."""
    for token in _PRODUCT.findall(ua):
        if token.lower() not in _UA_NOISE:
            return token
    return ua.split()[0].split("/")[0]


def _clean_family(value: str) -> Optional[str]:
    cleaned = _FAMILY_RE.sub("", value).strip()[:40]
    return cleaned or None


def looks_automated(headers: Any) -> Optional[str]:
    """Чего не хватает «компьютеру» из того, что шлёт любой живой браузер при
    переходе на страницу: `no-al` — нет Accept-Language, `no-sf` — нет
    Sec-Fetch-Mode. None — заголовки как у браузера (или их не дали)."""
    if headers is None:
        return None
    missing = [tag for tag, name in (("no-al", "accept-language"), ("no-sf", "sec-fetch-mode")) if not headers.get(name)]
    return ",".join(missing) or None


def _raw_ua(user_agent: Optional[str]) -> str:
    return (user_agent or "").replace('"', "'").replace("\n", " ")[:RAW_UA_LOG_CHARS]


def referrer_name(referer: Optional[str], own_hosts: tuple[str, ...] = ()) -> str:
    """Домен источника без полного адреса: `google`, `telegram`, `internal`, `direct`…"""
    if not referer:
        return "direct"
    try:
        host = (urlsplit(referer).hostname or "").lower()
    except ValueError:
        return "other"
    if not host:
        return "direct"
    host = host.removeprefix("www.")
    if host in own_hosts:
        return "internal"
    for needle, name in _REFERRER_NAMES:
        if needle.endswith("."):
            hit = host.startswith(needle) or f".{needle}" in host
        else:
            hit = host == needle or host.endswith("." + needle)
        if hit:
            return name
    return host if _HOST_RE.match(host) else "other"


def clean_utm(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    cleaned = _UTM_RE.sub("", value.strip().lower())[:32]
    return cleaned or None


# Роботы заходят тысячами в сутки, и строка на каждый заход топила живой лог.
# В базу пишется всё, а в лог INFO: люди и «компьютеры», похожие на автомат,
# как раньше; названный робот (Googlebot, Amazonbot…) — только DEBUG, его и
# так видно в /growth; безымянный — одна строка с сырым UA на семейство в сутки,
# чтобы его можно было опознать и занести в CRAWLERS.
_unnamed_seen: set[tuple[str, str]] = set()


def _log_level(crawler: Optional[str], family: Optional[str]):
    if not crawler:
        return logger.info
    if crawler not in ("other", "unknown"):
        return logger.debug
    key = (dt.date.today().isoformat(), family or "-")
    if key in _unnamed_seen:
        return logger.debug
    if len(_unnamed_seen) > 5000:
        _unnamed_seen.clear()
    _unnamed_seen.add(key)
    return logger.info


async def record(kind: str, page: str, lang: Optional[str], *, user_agent: Optional[str],
                 referer: Optional[str], own_hosts: tuple[str, ...] = (), utm: Optional[str] = None,
                 target: Optional[str] = None, headers: Any = None) -> tuple[str, Optional[str], str]:
    """Пишет событие и строку в лог; возвращает (устройство, робот, источник).

    `headers` — заголовки запроса страницы: по ним «компьютер», похожий на
    автомат, попадает в лог с сырым UA (в базу — только семейство)."""
    device, crawler = classify_ua(user_agent)
    source = referrer_name(referer, own_hosts)
    utm_clean = clean_utm(utm)
    family = ua_family(user_agent)
    suspect = looks_automated(headers) if device == "desktop" else None
    raw = ""
    if crawler in ("other", "unknown") or suspect:
        raw = f' ua="{_raw_ua(user_agent)}"'
    _log_level(crawler, family)(
        "site %s %s lang=%s ref=%s device=%s%s%s%s%s%s", kind, page, lang or "-", source, device,
        f" crawler={crawler}" if crawler else "", f" utm={utm_clean}" if utm_clean else "",
        f" to={target}" if target else "", f" suspect={suspect}" if suspect else "", raw,
    )
    try:
        await db.record_site_event(kind, page, lang, source, device, crawler, utm_clean, target, family)
    except Exception:  # noqa: BLE001 — счётчик не должен ронять страницу
        logger.debug("site event not stored", exc_info=True)
    return device, crawler, source


# ---------- маячок «страницу видел браузер» ----------

SEEN_PATH = "/go/seen"
_PAGE_KEY_RE = re.compile(r"^/[a-z0-9/_.-]{1,120}$")


def beacon_script(page_key: str) -> str:
    """Тег <script> маячка для страницы с ключом `page_key` (тот же ключ, что
    у серверного `view`). Шлёт один `POST /go/seen`, когда страница впервые
    видна на экране: не из предзагрузки, не из фоновой вкладки, не под
    webdriver. Без cookies и без идентификатора; адрес источника браузер
    отдаёт целиком, но сервер оставляет от него только домен."""
    import json

    key = json.dumps(page_key).replace("</", "<\\/")
    return (
        "<script>(function(){try{var n=navigator,d=document,s=0;if(n.webdriver||!n.sendBeacon)return;"
        "function go(){if(s||d.visibilityState!=='visible'||d.prerendering)return;s=1;"
        "var u=new URLSearchParams(location.search).get('utm_source'),"
        f"q='?p='+encodeURIComponent({key})+'&l='+encodeURIComponent(d.documentElement.lang||'')"
        "+'&r='+encodeURIComponent(d.referrer||'')+(u?'&utm_source='+encodeURIComponent(u):'');"
        f"n.sendBeacon('{SEEN_PATH}'+q);}}"
        "go();d.addEventListener('visibilitychange',go);d.addEventListener('prerenderingchange',go);}catch(e){}})();</script>"
    )


def valid_page_key(page: Optional[str], allowed: tuple[str, ...] = ()) -> bool:
    """Ключ из маячка — путь сайта (`/<язык>/…`) или один из `allowed`; мусор не пишется."""
    if not page or not _PAGE_KEY_RE.match(page) or ".." in page:
        return False
    if page in allowed:
        return True
    import seo_pages

    return seo_pages.lang_of_path(page) is not None


async def record_seen(page: str, lang: Optional[str], *, user_agent: Optional[str], referer: Optional[str],
                      own_hosts: tuple[str, ...] = (), utm: Optional[str] = None) -> bool:
    """Маячок браузера → событие `seen`. Робот (в том числе рендер Googlebot,
    который исполняет JS) не пишется вовсе. True — засчитано."""
    device, crawler = classify_ua(user_agent)
    if device == "bot":
        logger.debug("site seen-dropped %s device=bot crawler=%s", page, crawler)
        return False
    await record("seen", page, lang, user_agent=user_agent, referer=referer, own_hosts=own_hosts, utm=utm)
    return True


# ---------- админские сводки ----------

_DEVICE_LABELS = {"ios": "iOS", "android": "Android", "desktop": "компьютер", "bot": "робот"}


def device_label(device: str) -> str:
    return _DEVICE_LABELS.get(device, device)


def format_feed_line(row: Any) -> str:
    """«🌐 15.10 14:20 · /ru/tools/1rm · google · iOS ×3» — время в UTC, окно 10 минут.
    Робот — одной строкой на окно: «🤖 15.10 14:20 · ClaudeBot · 37 стр.»."""
    bucket = str(row["bucket"])
    when = f"{bucket[8:10]}.{bucket[5:7]} {bucket[11:13]}:{bucket[14:15]}0"
    if row["crawler"] is not None:
        return f"🤖 {when} · {row['crawler']} · {row['n']} стр."
    times = f" ×{row['n']}" if row["n"] > 1 else ""
    return f"🌐 {when} · {row['page']} · {row['referrer']} · {device_label(row['device'])}{times}"


def _share(part: int, whole: int) -> str:
    return f"{part * 100 / whole:.1f}%" if whole else "—"


def _day_label(iso: str) -> str:
    return f"{iso[8:10]}.{iso[5:7]}"


def _confirmed_note(stats: dict[str, Any]) -> str:
    """Подпись к «Просмотров людьми»: с какой даты счёт — только подтверждённые
    браузером (маячок `seen`), и сколько в окне ещё старого серверного счёта."""
    since = stats.get("confirmed_since")
    legacy = stats.get("legacy_views") or 0
    if not since:
        return " (серверный счёт: подтверждений браузером ещё не было)"
    note = f"с {_day_label(since)} — только подтверждённые браузером"
    if legacy:
        note += f"; {legacy} из них — раньше, серверный счёт без проверки"
    return f" ({note})"


def format_growth_block(stats: dict[str, Any], period: str, seo_signups: list[Any]) -> str:
    """Блок «🌐 САЙТ» для /growth: просмотры, источники, устройства, роботы,
    клики и что из этого дошло до бота (`/start` с меткой src_seo_*)."""
    from html import escape

    views = stats["views"]
    clicks = stats["clicks"]
    to_bot, to_store = clicks.get("bot", 0), clicks.get("appstore", 0)
    lines = [f"🌐 <b>САЙТ за {period}</b>", ""]
    if not views and not stats["crawlers"] and not stats.get("unconfirmed"):
        lines.append("Посещений пока нет.")
        return "\n".join(lines)
    lines.append(f"Просмотров людьми: {views}{_confirmed_note(stats)}")
    if stats.get("unconfirmed"):
        families = stats.get("unconfirmed_families") or []
        lines.append(
            f"Без подтверждения браузером: {stats['unconfirmed']}"
            + (" (компьютер: " + ", ".join(f"{escape(str(f))} {n}" for f, n in families) + ")" if families else "")
        )
    if stats["pages"]:
        lines.append("Страницы: " + ", ".join(f"{escape(p)} {n}" for p, n in stats["pages"]))
    if stats["referrers"]:
        lines.append("Откуда: " + ", ".join(f"{escape(r)} {n}" for r, n in stats["referrers"]))
    ai = stats.get("ai_sources") or []
    ai_total = sum(n for _, n in ai)
    lines.append(
        f"Из AI-ассистентов: {ai_total}"
        + (" (" + ", ".join(f"{escape(r)} {n}" for r, n in ai) + ")" if ai else "")
    )
    if stats["devices"]:
        lines.append("Устройства: " + ", ".join(f"{device_label(d)} {n}" for d, n in stats["devices"]))
    lines.append(
        f"Клики: в бота {to_bot}, в App Store {to_store} — {_share(to_bot + to_store, views)} от просмотров"
    )
    if stats["embeds"]:
        lines.append(f"Показы калькулятора на чужих сайтах: {stats['embeds']}")
    if stats["crawlers"]:
        lines.append("Роботы: " + ", ".join(f"{escape(str(c))} {n}" for c, n in stats["crawlers"]))
        others = stats.get("other_bots") or []
        if others:
            lines.append("Роботы без имени (other) по UA: " + ", ".join(f"{escape(str(f))} {n}" for f, n in others))
        on_demand = [(c, n) for c, n in stats["crawlers"] if c in AI_ON_DEMAND]
        if on_demand:
            lines.append(
                "AI открыл страницу по вопросу человека: " + ", ".join(f"{escape(str(c))} {n}" for c, n in on_demand)
            )
    if seo_signups:
        users = sum(r["users"] for r in seo_signups)
        activated = sum(r["activated"] for r in seo_signups)
        detail = ", ".join(f"{escape(r['source'].removeprefix('src_seo_'))} {r['users']}" for r in seo_signups)
        lines.append(f"Пришли в бота по ссылкам сайта: {users}, из них первую тренировку закрыли {activated} ({detail})")
    else:
        lines.append("Пришли в бота по ссылкам сайта: 0")
    return "\n".join(lines)
