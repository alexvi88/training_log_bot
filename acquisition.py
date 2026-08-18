"""Откуда пришёл человек: разбор deep link'ов на /start и воронка по источникам.

Один канал закупки — одна ссылка `t.me/<bot>?start=src_<слаг>`; приглашение от
живого человека — `?start=ref_<telegram_id>`. Источник пишется в `users.source`
**один раз**, на первом /start (см. db.set_user_source): человек мог прийти из
канала, а потом ещё десять раз открыть бота по чьей-то ссылке — засчитываем
первое касание, иначе последний перешедший переписывал бы историю.

Модуль чистый, как analytics.py: разбор payload'а и сборка текстов, без БД.
Запросы к базе — db.acquisition_funnel / db.top_referrers, вызовы — в хендлерах.
"""

import datetime as dt
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


# Подпись окна в шапке отчёта: «30 дн.» или конкретный день («сегодня, 18.08»).
# Одна на оба отчёта — они всегда показываются вместе, и разъехавшиеся шапки
# читались бы как разные периоды.
def period_label(days: int, day: Optional[str] = None) -> str:
    if not day:
        return f"{days} дн."
    date = dt.date.fromisoformat(day)
    stamp = date.strftime("%d.%m")
    today = dt.date.today()
    if date == today:
        return f"сегодня, {stamp}"
    if date == today - dt.timedelta(days=1):
        return f"вчера, {stamp}"
    return stamp


def format_funnel(rows: Iterable[Any], days: int, day: Optional[str] = None) -> str:
    """Кто пришёл и дошёл ли до первой тренировки, по источникам.

    Ключевая цифра — не «пришли», а «записали первую»: канал с дешёвыми
    переходами, которые не доходят до первой записи, — выброшенные деньги, и
    видно это только здесь.
    """
    rows = list(rows)
    head = f"📈 <b>ОТКУДА ЛЮДИ · {period_label(days, day)}</b>"
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


def _onboarding_line(row: Any) -> str:
    users = row["users"]
    started, logged_set, finished = row["started"], row["logged_set"], row["finished"]
    return (
        f"{_source_title(row['source'])} — <b>{users}</b>\n"
        f"   начали тренировку: {started} ({_percent(started, users)}%) · "
        f"записали подход: {logged_set} ({_percent(logged_set, users)}%) · "
        f"завершили первую: {finished} ({_percent(finished, users)}%)"
    )


def format_onboarding_funnel(rows: Iterable[Any], days: int, day: Optional[str] = None) -> str:
    """Воронка новичка по шагам: пришёл → начал тренировку → записал подход →
    закрыл первую. Разрез по источникам — тот же, что и у format_funnel: видно
    не только «сколько дошло до первой», но и НА КАКОМ шаге теряются —
    открыли тренировку и не тронули снаряд, или дожали подход и не закрыли.
    """
    rows = list(rows)
    head = f"🧭 <b>ВОРОНКА НОВИЧКА · {period_label(days, day)}</b>"
    if not rows:
        return f"{head}\n\nЗа этот срок новых не было."
    total = sum(r["users"] for r in rows)
    total_started = sum(r["started"] for r in rows)
    total_logged_set = sum(r["logged_set"] for r in rows)
    total_finished = sum(r["finished"] for r in rows)
    lines = [
        head,
        f"Всего {total}: начали тренировку {total_started} ({_percent(total_started, total)}%), "
        f"записали подход {total_logged_set} ({_percent(total_logged_set, total)}%), "
        f"завершили первую {total_finished} ({_percent(total_finished, total)}%).",
        "",
    ]
    lines += [_onboarding_line(r) for r in rows]
    return "\n".join(lines)


# Ниже скольких новичков источник не называем «хуже всех» — 1-2 человека
# дают 0% или 100% на ровном месте, и такая «диагностика» — шум, а не сигнал
# (см. engagement._maybe_send_admin_funnel_digest).
WEEKLY_DIGEST_MIN_SOURCE_SAMPLE = 3


def build_weekly_funnel_digest(rows: Iterable[Any], days: int) -> str:
    """Еженедельная сводка воронки новичка для админа — от лица тренера, не
    телеметрией: та же арифметика, что у format_onboarding_funnel, но с
    прицелом на одно число, которое реально требует внимания — какой источник
    хуже всех доводит до конца.

    Ноль новых за неделю — не молчание, а честная строка: тишина каждый
    понедельник неотличима от того, что джоба вообще не сработала, а «новых не
    было» сразу видно, что цикл живой и просто нечего разбирать.
    """
    rows = list(rows)
    head = f"🧭 <b>ВОРОНКА ЗА НЕДЕЛЮ · {days} дн.</b>"
    total = sum(r["users"] for r in rows)
    if total == 0:
        return f"{head}\n\nЗа неделю новых атлетов не пришло — посмотрю ещё раз через неделю."
    started = sum(r["started"] for r in rows)
    logged_set = sum(r["logged_set"] for r in rows)
    finished = sum(r["finished"] for r in rows)
    lines = [
        head,
        "",
        f"За неделю {total} новых: {started} начали тренировку "
        f"({_percent(started, total)}%), {logged_set} записали подход "
        f"({_percent(logged_set, total)}%), {finished} завершили первую "
        f"({_percent(finished, total)}%).",
    ]
    candidates = [r for r in rows if r["users"] >= WEEKLY_DIGEST_MIN_SOURCE_SAMPLE]
    if candidates:
        worst = min(candidates, key=lambda r: r["finished"] / r["users"])
        lines.append(
            f"\nХуже всех конвертит источник {_source_title(worst['source'])} — "
            f"{worst['finished']} из {worst['users']} дошли до конца "
            f"({_percent(worst['finished'], worst['users'])}%)."
        )
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
