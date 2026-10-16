"""Счётчик посещений публичного сайта (seo_pages.py) — без cookies, без
скриптов и без адреса клиента.

Что пишется (`db.site_events`): вид события (`view` — открыл страницу,
`click` — нажал кнопку «в бота» или «в App Store» через `/go/*`, `embed` —
показ встроенного калькулятора на чужом сайте), ключ страницы (путь без
домена), язык, домен источника (`google`, `t.me`… или `direct` — без полного
адреса, по которому пришли), вид устройства (ios, android, desktop, bot),
имя поискового робота, `utm_source` и куда вёл клик. IP, User-Agent целиком и
что-либо, что связывает визит с человеком или аккаунтом, не пишется — поэтому
политика конфиденциальности описывает счётчик одной строкой.

Роботы (Googlebot, bingbot, GPTBot, OAI-SearchBot, PerplexityBot, ClaudeBot…)
считаются отдельно: обход — это не посетитель, но видеть, ходит ли к нам
поиск ChatGPT, полезно.

Запись не должна ронять страницу: база не открыта (тест, скрипт) или занята
— просмотр просто не посчитан, страница отдаётся как обычно.

Админские сводки здесь же (`format_growth_block`, `format_feed_line`) — по-русски,
как весь админский экран.
"""

from __future__ import annotations

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
_REFERRER_NAMES: tuple[tuple[str, str], ...] = (
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


async def record(kind: str, page: str, lang: Optional[str], *, user_agent: Optional[str],
                 referer: Optional[str], own_hosts: tuple[str, ...] = (), utm: Optional[str] = None,
                 target: Optional[str] = None) -> tuple[str, Optional[str], str]:
    """Пишет событие и строку в лог; возвращает (устройство, робот, источник)."""
    device, crawler = classify_ua(user_agent)
    source = referrer_name(referer, own_hosts)
    utm_clean = clean_utm(utm)
    logger.info(
        "site %s %s lang=%s ref=%s device=%s%s%s%s", kind, page, lang or "-", source, device,
        f" crawler={crawler}" if crawler else "", f" utm={utm_clean}" if utm_clean else "",
        f" to={target}" if target else "",
    )
    try:
        await db.record_site_event(kind, page, lang, source, device, crawler, utm_clean, target)
    except Exception:  # noqa: BLE001 — счётчик не должен ронять страницу
        logger.debug("site event not stored", exc_info=True)
    return device, crawler, source


# ---------- админские сводки ----------

_DEVICE_LABELS = {"ios": "iOS", "android": "Android", "desktop": "компьютер", "bot": "робот"}


def device_label(device: str) -> str:
    return _DEVICE_LABELS.get(device, device)


def format_feed_line(row: Any) -> str:
    """«🌐 15.10 14:20 · /ru/tools/1rm · google · iOS ×3» — время в UTC, окно 10 минут."""
    bucket = str(row["bucket"])
    when = f"{bucket[8:10]}.{bucket[5:7]} {bucket[11:13]}:{bucket[14:15]}0"
    times = f" ×{row['n']}" if row["n"] > 1 else ""
    return f"🌐 {when} · {row['page']} · {row['referrer']} · {device_label(row['device'])}{times}"


def _share(part: int, whole: int) -> str:
    return f"{part * 100 / whole:.1f}%" if whole else "—"


def format_growth_block(stats: dict[str, Any], period: str, seo_signups: list[Any]) -> str:
    """Блок «🌐 САЙТ» для /growth: просмотры, источники, устройства, роботы,
    клики и что из этого дошло до бота (`/start` с меткой src_seo_*)."""
    from html import escape

    views = stats["views"]
    clicks = stats["clicks"]
    to_bot, to_store = clicks.get("bot", 0), clicks.get("appstore", 0)
    lines = [f"🌐 <b>САЙТ за {period}</b>", ""]
    if not views and not stats["crawlers"]:
        lines.append("Посещений пока нет.")
        return "\n".join(lines)
    lines.append(f"Просмотров людьми: {views}")
    if stats["pages"]:
        lines.append("Страницы: " + ", ".join(f"{escape(p)} {n}" for p, n in stats["pages"]))
    if stats["referrers"]:
        lines.append("Откуда: " + ", ".join(f"{escape(r)} {n}" for r, n in stats["referrers"]))
    if stats["devices"]:
        lines.append("Устройства: " + ", ".join(f"{device_label(d)} {n}" for d, n in stats["devices"]))
    lines.append(
        f"Клики: в бота {to_bot}, в App Store {to_store} — {_share(to_bot + to_store, views)} от просмотров"
    )
    if stats["embeds"]:
        lines.append(f"Показы калькулятора на чужих сайтах: {stats['embeds']}")
    if stats["crawlers"]:
        lines.append("Роботы: " + ", ".join(f"{escape(str(c))} {n}" for c, n in stats["crawlers"]))
    if seo_signups:
        users = sum(r["users"] for r in seo_signups)
        activated = sum(r["activated"] for r in seo_signups)
        detail = ", ".join(f"{escape(r['source'].removeprefix('src_seo_'))} {r['users']}" for r in seo_signups)
        lines.append(f"Пришли в бота по ссылкам сайта: {users}, из них первую тренировку закрыли {activated} ({detail})")
    else:
        lines.append("Пришли в бота по ссылкам сайта: 0")
    return "\n".join(lines)
