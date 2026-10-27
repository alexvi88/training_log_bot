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
# payload'ом нельзя: всё, что не `src_`/`ref_`/`coach_`, схлопывается в UNKNOWN.
SOURCE_ORGANIC = "organic"  # пришёл сам: поиск, каталог ботов, ссылка без метки
SOURCE_REFERRAL = "referral"  # по ссылке из карточки тренировки другого атлета
SOURCE_SHARED_CARD = "shared_card"  # по визитке программы/упражнения (sh_-ссылка)
SOURCE_COACH_INVITE = "coach_invite"  # по ссылке-приглашению живого тренера (coach_<код>)
SOURCE_UNKNOWN = "unknown"  # payload был, но не разобрался
# Те, кто уже был в боте до появления атрибуции. Стоит в базе с миграции, из
# воронки исключается: считать их «органикой» — врать самим себе в отчёте.
SOURCE_LEGACY = "legacy"

REFERRAL_PREFIX = "ref_"
CHANNEL_PREFIX = "src_"
# Приглашение тренера: `?start=coach_<код>` (handlers/coach.py). Тот же
# «пришёл по приглашению живого человека», что и ref_, но своей строкой в
# воронке: так видно, сколько людей приводит сервис для тренеров. Автора
# (тренера) по коду находит хендлер — здесь, без БД, он неизвестен.
COACH_PREFIX = "coach_"
# id автора в `ref_<id>`: только ASCII-цифры, с необязательным минусом —
# см. parse_start_payload.
_REFERRER_ID = re.compile(r"-?[0-9]+")

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
        # Минус впереди законен: у аккаунта из приложения без Telegram
        # (db.create_app_only_user) id синтетический и отрицательный, и его
        # ссылка из приложения (InviteView) — `ref_-5`. `isdigit()` минус не
        # пропускал, и такие приглашения теряли автора; к тому же он
        # пропускал не-ASCII цифры вроде «²», на которых `int()` падал.
        return Attribution(SOURCE_REFERRAL, int(rest) if _REFERRER_ID.fullmatch(rest) else None)
    if raw.startswith(COACH_PREFIX):
        return Attribution(SOURCE_COACH_INVITE)
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


# Та же метка `ref_<id>` — веб-страницей, для тех, кому Telegram не нужен:
# `<config.site_base_url()>/i/ref_<id>` (referral_page.py). С приложением на
# iPhone она открывает его Universal Link'ом, без — страницу с App Store
# (кампания `ct=ref_<id>`), приложением по схеме и, третьей, ботом. Приложение
# отдаёт код в `POST /v1/me/referral`, и источник ложится тем же
# db.set_user_source, что у /start: первое касание побеждает.
REFERRAL_WEB_PATH = "/i/"
REFERRAL_APP_LINK_PREFIX = "trainingdiary://i/"
# Ключ страницы в счётчике сайта (site_events): одна строка на все `/i/ref_<id>`,
# без id автора — счётчик не связывает визит с аккаунтом.
REFERRAL_WEB_PAGE_KEY = "/i/ref"


def referral_web_link(base_url: Optional[str], telegram_id: int) -> Optional[str]:
    """https-ссылка на страницу приглашения или None без публичного адреса."""
    if not base_url:
        return None
    return f"{base_url}{REFERRAL_WEB_PATH}{referral_payload(telegram_id)}"


def referrer_from_code(code: Optional[str]) -> Optional[int]:
    """id автора из кода `ref_<id>` — строго, без догадок: None, если это не он.

    Тот же разбор, что у `?start=` (parse_start_payload): минус законен
    (синтетический id аккаунта без Telegram), не-ASCII цифры — нет.
    """
    raw = (code or "").strip()
    if not raw.startswith(REFERRAL_PREFIX):
        return None
    rest = raw[len(REFERRAL_PREFIX):]
    return int(rest) if _REFERRER_ID.fullmatch(rest) else None


def with_campaign(store_url: str, telegram_id: int) -> str:
    """Ссылка App Store с кампанией `ct=ref_<id>` (App Store Connect →
    Analytics → Campaigns считает её вместе с `pt`, см. config.APP_STORE_URL)."""
    joiner = "&" if "?" in store_url else "?"
    return f"{store_url}{joiner}ct={referral_payload(telegram_id)}"


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
    if source == SOURCE_COACH_INVITE:
        return "🧑‍🏫 По коду тренера"
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


# Шаги воронки приложения до входа (api_v1_funnel.STEPS) — в том порядке, в
# каком их проходит человек, и с подписью для админа.
APP_FUNNEL_STEPS: tuple[tuple[str, str], ...] = (
    ("onboarding_slide_1", "слайд 1 «ПРИВЕТ АТЛЕТ!»"),
    ("onboarding_slide_2", "слайд 2 «Рекорды…»"),
    ("onboarding_slide_3", "слайд 3 «Не знаешь, что делать в зале?»"),
    ("onboarding_slide_4", "слайд 4 «Давай посмотрю.»"),
    ("onboarding_slide_5", "слайд 5 «Это ТЫ.»"),
    ("onboarding_lock_screen", "экран блокировки (только iOS 17+)"),
    ("onboarding_import", "«Вёл дневник раньше? Перенесу.»"),
    ("onboarding_gender", "«Твой пол»"),
    ("onboarding_gender_male", "↳ «Начать», пол: мужской"),
    ("onboarding_gender_female", "↳ «Начать», пол: женский"),
    ("onboarding_gender_none", "↳ «Начать», пол: не указан"),
    ("push_priming", "экран уведомлений"),
    ("signin_shown", "экран входа"),
    ("signin_tapped", "нажал «Войти с Apple»"),
    ("signin_ok", "вошёл"),
    ("signin_failed", "вход сорвался"),
)


def format_app_funnel(rows: Iterable[Any], days: int, day: Optional[str] = None) -> str:
    """Воронка приложения ДО входа (db.funnel_events): сколько установок
    дошло до каждого шага, с разрезом по языку. Процент — от первого слайда:
    это и есть «открыли приложение»."""
    head = f"📱 <b>ПРИЛОЖЕНИЕ ДО ВХОДА · {period_label(days, day)}</b>"
    by_step: dict[str, dict[str, int]] = {}
    for r in rows:
        by_step.setdefault(r["step"], {})[r["lang"]] = r["installs"]
    if not by_step:
        return f"{head}\n\nУстановок за этот срок не было."
    first = sum(by_step.get("onboarding_slide_1", {}).values())
    lines = [head]
    for step, title in APP_FUNNEL_STEPS:
        langs = by_step.get(step, {})
        total = sum(langs.values())
        split = ", ".join(f"{lang} {n}" for lang, n in sorted(langs.items()))
        tail = f" ({split})" if split else ""
        lines.append(f"{title}: {total} ({_percent(total, first)}%){tail}")
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


def format_referrers(rows: Iterable[Any], days: Optional[int] = None, day: Optional[str] = None) -> str:
    """Кто приводит людей за окно /growth — и сколько из них дошли до первой
    тренировки, а не просто открыли бота. Без окна — за всё время."""
    rows = list(rows)
    head = "🤝 <b>КТО ПРИВОДИТ</b>"
    if days or day:
        head = f"🤝 <b>КТО ПРИВОДИТ · {period_label(days or 0, day)}</b>"
    if not rows:
        return (
            f"{head}\n\nПока никто. Входы сюда — экран «Пригласить» в приложении, "
            f"кнопка на картинке тренировки, визитка и код тренера."
        )
    lines = [head]
    for row in rows:
        who = f"@{row['username']}" if row["username"] else f"id {row['referrer_id']}"
        lines.append(f"• {who} — привёл {row['invited']}, из них начали тренироваться {row['activated']}")
    return "\n".join(lines)
