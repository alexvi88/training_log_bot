"""Откуда пришёл человек: разбор deep link'ов на /start и воронка по источникам.

Один канал закупки — одна ссылка `t.me/<bot>?start=src_<слаг>`; приглашение от
живого человека — `?start=ref_<telegram_id>`. Источник пишется в `users.source`
**один раз**, на первом /start (см. db.set_user_source): человек мог прийти из
канала, а потом ещё десять раз открыть бота по чьей-то ссылке — засчитываем
первое касание, иначе последний перешедший переписывал бы историю.

Модуль чистый, как analytics.py: разбор payload'а и сборка текстов, без БД.
Запросы к базе — db.acquisition_funnel / db.top_referrers, вызовы — в хендлерах.
"""

import re
from dataclasses import dataclass
from typing import Any, Iterable, Optional

# Зарезервированные источники — у них нет префикса, поэтому подделать их
# payload'ом нельзя: всё, что не `src_`/`ref_`, схлопывается в UNKNOWN.
SOURCE_ORGANIC = "organic"  # пришёл сам: поиск, каталог ботов, ссылка без метки
SOURCE_REFERRAL = "referral"  # по ссылке из карточки тренировки другого атлета
SOURCE_SHARED_CARD = "shared_card"  # по визитке программы/упражнения (sh_-ссылка)
SOURCE_UNKNOWN = "unknown"  # payload был, но не разобрался
# Те, кто уже был в боте до появления атрибуции. Стоит в базе с миграции, из
# воронки исключается: считать их «органикой» — врать самим себе в отчёте.
SOURCE_LEGACY = "legacy"

REFERRAL_PREFIX = "ref_"
CHANNEL_PREFIX = "src_"

# Telegram и сам пропускает в start-параметре только A-Za-z0-9_- и 64 символа,
# но слаг приезжает из ссылки, которую руками собирал человек, — режем сами.
MAX_SLUG_LEN = 32
_SLUG_RE = re.compile(r"[^a-z0-9_-]+")

# Столько дней без завершённой тренировки — и человек считается отвалившимся.
ALIVE_WINDOW_DAYS = 7


@dataclass(frozen=True)
class Attribution:
    source: str
    referrer_id: Optional[int] = None


def _slug(raw: str) -> str:
    return _SLUG_RE.sub("", raw.lower())[:MAX_SLUG_LEN].strip("-_")


def parse_start_payload(payload: Optional[str]) -> Attribution:
    """Что означает `?start=<payload>`.

    Пустой payload — органика: человек нажал Start в поиске или пришёл из
    каталога ботов, где своей метки не поставить.
    """
    raw = (payload or "").strip()
    if not raw:
        return Attribution(SOURCE_ORGANIC)
    if raw.startswith(REFERRAL_PREFIX):
        rest = raw[len(REFERRAL_PREFIX):]
        # Не цифры в id — либо опечатка в пересланной ссылке, либо чья-то
        # самодеятельность: приглашение остаётся приглашением, но без автора.
        return Attribution(SOURCE_REFERRAL, int(rest) if rest.isdigit() else None)
    if raw.startswith(CHANNEL_PREFIX):
        slug = _slug(raw[len(CHANNEL_PREFIX):])
        return Attribution(f"{CHANNEL_PREFIX}{slug}" if slug else SOURCE_UNKNOWN)
    return Attribution(SOURCE_UNKNOWN)


def attribution_for(payload: Optional[str], telegram_id: int) -> Attribution:
    """То же, но с поправкой на переход по собственной ссылке.

    Своя же карточка, открытая с другого аккаунта, — не приглашение; а с того
    же аккаунта человек и вовсе не новый и до записи источника не дойдёт.
    Считаем такой заход органикой, чтобы «пригласил сам себя» не попадало ни в
    воронку рефералов, ни в топ пригласивших.
    """
    attribution = parse_start_payload(payload)
    if attribution.referrer_id is not None and attribution.referrer_id == telegram_id:
        return Attribution(SOURCE_ORGANIC)
    return attribution


def referral_payload(telegram_id: int) -> str:
    return f"{REFERRAL_PREFIX}{telegram_id}"


def referral_link(bot_username: str, telegram_id: int) -> str:
    """Ссылка, которая уезжает в чужой чат на кнопке карточки тренировки."""
    return f"https://t.me/{bot_username}?start={referral_payload(telegram_id)}"


def channel_link(bot_username: str, slug: str) -> str:
    """Ссылка под конкретный рекламный канал — её и отдаём в закупку."""
    return f"https://t.me/{bot_username}?start={CHANNEL_PREFIX}{_slug(slug)}"


# ---------- отчёт для админки ----------


def _percent(part: int, whole: int) -> int:
    return round(part * 100 / whole) if whole else 0


def _source_title(source: str) -> str:
    if source == SOURCE_ORGANIC:
        return "🔎 Сами пришли"
    if source == SOURCE_REFERRAL:
        return "🤝 По приглашению"
    if source == SOURCE_SHARED_CARD:
        return "📤 По визитке"
    if source == SOURCE_UNKNOWN:
        return "❓ Метка не разобралась"
    return f"📣 {source[len(CHANNEL_PREFIX):]}" if source.startswith(CHANNEL_PREFIX) else source


def _funnel_line(row: Any) -> str:
    users = row["users"]
    activated, engaged, alive = row["activated"], row["engaged"], row["alive"]
    return (
        f"{_source_title(row['source'])} — <b>{users}</b>\n"
        f"   записали первую: {activated} ({_percent(activated, users)}%) · "
        f"от трёх: {engaged} · живы: {alive}"
    )


def format_funnel(rows: Iterable[Any], days: int) -> str:
    """Кто пришёл и дошёл ли до первой тренировки, по источникам.

    Ключевая цифра — не «пришли», а «записали первую»: канал с дешёвыми
    переходами, которые не доходят до первой записи, — выброшенные деньги, и
    видно это только здесь.
    """
    rows = list(rows)
    head = f"📈 <b>ОТКУДА ЛЮДИ · {days} дн.</b>"
    if not rows:
        return (
            f"{head}\n\nЗа этот срок новых не было. Ссылка под канал — "
            f"<code>?start=src_имя</code>, и заходы по ней я посчитаю здесь."
        )
    total = sum(r["users"] for r in rows)
    total_activated = sum(r["activated"] for r in rows)
    lines = [
        head,
        f"Всего {total}, записали первую тренировку {total_activated} "
        f"({_percent(total_activated, total)}%).",
        "",
    ]
    lines += [_funnel_line(r) for r in rows]
    lines.append("")
    lines.append(f"«Живы» — тренировались за последние {ALIVE_WINDOW_DAYS} дн.")
    return "\n".join(lines)


def format_referrers(rows: Iterable[Any]) -> str:
    """Кто приводит людей — тех, кто дошёл до первой тренировки, а не просто открыл бота."""
    rows = list(rows)
    head = "🤝 <b>КТО ПРИВОДИТ</b>"
    if not rows:
        return f"{head}\n\nПока никто. Кнопка на картинке тренировки — единственный вход сюда."
    lines = [head]
    for row in rows:
        who = f"@{row['username']}" if row["username"] else f"id {row['referrer_id']}"
        lines.append(f"• {who} — привёл {row['invited']}, из них записали первую {row['activated']}")
    return "\n".join(lines)
