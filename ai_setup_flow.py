"""Опросник тренера перед сборкой программы (ai_trainer.ask_setup_questions) —
общая для бота (handlers/ai_trainer.py) и REST `/v1` (api_v1_ai.py) логика:
что задать первым вопросом (цель), сколько кругов уточнений терпеть и как
собрать ответы в одно сообщение модели. Экранная часть (отправка вопроса
отдельным сообщением в Telegram, кнопки, правка старого вопроса) у каждого
интерфейса своя — здесь только решение, а не показ (тот же приём, что у
ai_program_actions.py и progression_data.py).
"""

from __future__ import annotations

import json
from typing import Any, Optional

import db
import formatting
import i18n

# Сколько кругов уточнений подряд разрешаем одной просьбе. Второй круг нужен по
# делу: увидев в ответах встречный вопрос или «хз», тренер вправе ответить и
# переспросить то, что осталось открытым. А вот без потолка он способен гонять
# уточнения по кругу, и человек не увидит программу никогда — поэтому на третий
# заход опросник уже не показывается, а тренеру уходит прямое «собирай на
# дефолтах».
SETUP_MAX_ROUNDS = 2

# Ядро опросника бот/приложение задают сами, а не полагаются на модель.
# Промпт просил «2-5 вопросов, дни всегда, по одной вещи в вопросе», а в проде
# модель с богатой историей прислала ОДИН вопрос «сколько дней и сколько
# времени на тренировку» — человек нажал «3 дня», время потерялось, а про
# травмы, место и опыт никто не спросил. Промпт — не гарантия. Поэтому шесть
# вводных, без которых программа собирается наугад, бот ставит сам, всегда и
# в одном порядке (одинаково у любой модели): цель, дни, время, место и
# инвентарь, опыт, травмы. От модели — максимум SETUP_MAX_EXTRA_QUESTIONS
# своих вопросов про этого атлета (приоритеты, что оставить из текущих
# упражнений), и только после ядра.
CORE_SLOTS = ("goal", "days", "time", "place", "experience", "injuries")

# Своих вопросов модели после ядра — не больше стольких. Их место — то, чего
# ядро не покрывает: приоритеты, любимые и нелюбимые упражнения.
SETUP_MAX_EXTRA_QUESTIONS = 2

# По этим кускам узнаём вопрос модели, который повторяет слот ядра (или
# склеивает два слота в один — «сколько дней и сколько времени»: кнопки
# отвечают только на одно, второе теряется). Такой вопрос выбрасываем целиком:
# ядро его и так задаст, по одной вещи в вопросе. Маркеры смешивают оба языка
# (см. running_texts._TOPIC_STEMS — тот же приём): язык ответа модели зависит
# от языка пользователя, а не от языка этого файла. Ложное срабатывание стоит
# одного вопроса модели, поэтому маркеры — фразы, а не голые слова: голое
# "goal" ловило "What's your rep goal for this exercise?", голое "минут" —
# вопрос про отдых между подходами.
SETUP_SLOT_MARKERS: dict[str, tuple[str, ...]] = {
    "goal": (
        "цел", "чего хочешь", "чего ждёшь", "зачем тренир", "какой результат",
        "your goal", "training goal", "what do you want", "what are you after",
        "why train", "why do you train", "what result",
    ),
    "days": (
        "дней в неделю", "дня в неделю", "раз в неделю", "сколько дней",
        "days a week", "days per week", "times a week", "times per week", "how many days",
    ),
    "time": (
        "сколько времени", "времени на трениров", "минут на трениров", "длительность трениров",
        "how much time", "how long do you have", "how long can you", "how long per",
        "how long is a session", "how long are your", "session length", "minutes per",
    ),
    "place": (
        "где трен", "где занима", "оборудован", "инвентар", "зал или дом",
        "where do you train", "where do you work out", "equipment", "gym or home",
    ),
    "experience": (
        # Голое "опыт" сидит внутри «попытка».
        "какой опыт", "какой у тебя опыт", "опыт трен", "опыт в зал", "стаж", "давно трен",
        "experience", "how long have you been", "how long have you trained",
    ),
    "injuries": (
        "травм", "болит", "огранич", "injur", "pain", "hurt", "limitation",
    ),
}
SETUP_GOAL_MARKERS = SETUP_SLOT_MARKERS["goal"]

# Варианты ответа слотов ядра — те же, что у чистого листа.
_SLOT_CHOICES = {
    "days": ("2", "3", "4", "5"),
    "time": ("45", "60", "90"),
    "place": ("gym", "home_weights", "home_bare", "bar"),
    "experience": ("new", "year", "years", "veteran"),
    "injuries": ("none", "back", "knees", "shoulders"),
}


def covered_slots(question_text: str) -> set[str]:
    """Какие слоты ядра задевает вопрос модели (по SETUP_SLOT_MARKERS)."""
    text = (question_text or "").lower()
    return {slot for slot, markers in SETUP_SLOT_MARKERS.items() if any(m in text for m in markers)}


def setup_goal_question(*, for_client: bool = False) -> dict[str, Any]:
    """`for_client` — тренер собирает программу своему атлету
    (coach_program_ai): тот же вопрос, только про атлета."""
    return {
        "question": i18n.t("coach.ai_chat.setup_goal" if for_client else "ai.screen.setup_goal.question"),
        "choices": [
            i18n.t("ai.screen.setup_goal.choice_mass"),
            i18n.t("ai.screen.setup_goal.choice_strength"),
            i18n.t("ai.screen.setup_goal.choice_lose"),
            i18n.t("ai.screen.setup_goal.choice_comeback"),
        ],
    }


def _saved_profile_value(user: Any, slot: str) -> Optional[str]:
    """То, что атлет уже рассказывал (экран «Обо мне»), для слота ядра."""
    if user is None:
        return None
    if slot == "goal":
        return (user["goal"] or "").strip() or None
    if slot == "experience":
        return (user["experience"] or "").strip() or None
    if slot == "place":
        raw = (user["equipment"] or "").strip()
        if not raw:
            return None
        try:
            items = json.loads(raw)
        except ValueError:
            return raw
        if isinstance(items, list):
            return ", ".join(str(item).strip() for item in items if str(item).strip()) or None
        return raw
    return None


def _with_saved_first(question: dict[str, Any], saved: Optional[str]) -> dict[str, Any]:
    """Сохранённое значение — первой кнопкой «Как раньше: …». Переспросить надо
    (цель и зал меняются, а программа собирается под сегодняшние), но вопрос
    без прошлого ответа под рукой читается как «он забыл»."""
    if not saved:
        return question
    import ai_trainer  # локально — цикл импортов (нужны только потолки)

    label = formatting.shorten(i18n.t("ai.screen.setup_core.as_before", value=saved), ai_trainer.SETUP_CHOICE_LIMIT)
    rest = [choice for choice in question["choices"] if choice != label]
    return {**question, "choices": [label, *rest][: ai_trainer.SETUP_MAX_CHOICES]}


def core_questions(user: Any = None, *, for_client: bool = False) -> list[dict[str, Any]]:
    """Шесть вопросов ядра на языке текущего контекста, в порядке CORE_SLOTS.

    `user` — строка users атлета: его сохранённые цель, опыт и инвентарь встают
    первой кнопкой. `for_client` — тренер отвечает за своего атлета: вопросы
    про атлета, а профиль тренера тут ни при чём (его не передают). Каждый
    вопрос несёт `slot` — по нему повторный проход узнаёт, что ядро уже
    поставлено (см. questions_with_core)."""
    prefix = "coach.ai_chat.setup_fresh" if for_client else "ai.screen.setup_fresh"
    goal = setup_goal_question(for_client=for_client)
    questions = [_with_saved_first({**goal, "slot": "goal"}, _saved_profile_value(user, "goal"))]
    for slot in CORE_SLOTS[1:]:
        question = {
            "question": i18n.t(f"{prefix}.{slot}.question"),
            "choices": [i18n.t(f"ai.screen.setup_fresh.{slot}.choice_{choice}") for choice in _SLOT_CHOICES[slot]],
            "slot": slot,
        }
        questions.append(_with_saved_first(question, _saved_profile_value(user, slot)))
    return questions


async def questions_with_core(
    user_id: int, questions: list[dict[str, Any]], previous: dict[str, Any], *, for_client: bool = False,
) -> tuple[list[dict[str, Any]], bool]:
    """Опросник целиком: ядро (CORE_SLOTS) первым, потом не больше
    SETUP_MAX_EXTRA_QUESTIONS своих вопросов модели — тех, что не повторяют и
    не склеивают слоты ядра.

    Возвращает (вопросы, «ядро задано»). Второе переживает круги уточнений
    (в состоянии лежит под старым именем `goal_asked`): на втором круге модель
    переспрашивает то, что осталось открытым, и ядро второй раз не ставится.

    Идемпотентна: список, где ядро уже стоит (вопросы со `slot`), возвращается
    как есть. Это важно — её зовут дважды: в момент вызова инструмента (чтобы
    ответ инструмента модели перечислял ровно заданное, см.
    ai_trainer.sync_setup_payload) и при раскладке опросника на экран.
    """
    import ai_trainer  # локально — иначе цикл импортов (нужна только константа)

    # `slot` есть у каждого вопроса ядра; `slot: None` ставит сам инструмент на
    # уточнении перед тренировкой на сегодня (today_workout) — там ядра нет.
    if previous.get("goal_asked") or any("slot" in question for question in questions):
        return questions[: ai_trainer.SETUP_MAX_QUESTIONS], True
    # Программа атлету: профиль тренера тут ни при чём, а профиля атлета
    # тренер не видит — кнопок «как раньше» там нет.
    user = None if for_client else await db.get_user(user_id)
    extras = [
        question for question in questions
        if not covered_slots(question.get("question") or "")
    ][:SETUP_MAX_EXTRA_QUESTIONS]
    return (core_questions(user, for_client=for_client) + extras)[: ai_trainer.SETUP_MAX_QUESTIONS], True


def setup_answers_text(setup: dict[str, Any]) -> str:
    """Одно сообщение модели со всеми ответами разом — и исходной задачей.

    Пропущенные («⏭ Собирай так» на середине) названы прямо: иначе тренер
    решит, что вопрос просто потерялся, и переспросит его ещё раз.
    """
    questions = setup.get("questions") or []
    answers = setup.get("answers") or []
    lines = [i18n.t("ai.screen.setup_answers_header")]
    skipped = False
    for idx, question in enumerate(questions):
        if idx < len(answers) and answers[idx] is not None:
            lines.append(
                i18n.t("ai.screen.setup_line_answered", question=question["question"], answer=answers[idx])
            )
        else:
            skipped = True
            lines.append(i18n.t("ai.screen.setup_line_skipped", question=question["question"]))
    goal = setup.get("goal")
    if goal:
        lines.append(i18n.t("ai.screen.setup_original_goal", goal=goal))
    if skipped:
        lines.append(i18n.t("ai.screen.setup_skipped_note"))
    lines.append(i18n.t("ai.screen.setup_answers_frame"))
    return "\n".join(lines)


def setup_enough_text(previous_goal: Optional[str]) -> str:
    """Уходит модели вместо третьего круга уточнений подряд — прямое «хватит
    спрашивать, собирай на дефолтах», а не ещё один опросник."""
    text = i18n.t("ai.screen.setup_enough_frame")
    if previous_goal:
        text = text + i18n.t("ai.screen.setup_original_goal", goal=previous_goal)
    return text


# --- Короткий путь для чистого листа ------------------------------------------
# У человека без тренировок, программ и цели в профиле смотреть модели не на что:
# полный платный ход ради «задам пару вопросов» только держал на экране ожидание.
# Вопросы тогда фиксированные — те же, что модель обычно задаёт, — а модель
# подключается один раз, уже за программой, когда ответы собраны.

def fresh_start_questions(*, for_client: bool = False) -> list[dict[str, Any]]:
    """Вопросы чистого листа — то же ядро, что ставится в обычном пути
    (`core_questions`): цель, дни, время, место и инвентарь, опыт, травмы.

    `for_client` — тренер отвечает за своего атлета: те же вопросы и варианты,
    только сами вопросы — про атлета, а не про того, кто отвечает."""
    return core_questions(for_client=for_client)


def fresh_start_reply(*, for_client: bool = False) -> str:
    return i18n.t("coach.ai_chat.fresh_reply" if for_client else "ai.screen.setup_fresh.reply")


def is_build_program_seed(text: str) -> bool:
    """Это ровно текст кнопки «Составь мне программу» — на любом из двух
    языков, а не только на языке аккаунта: приложение могло отправить seed
    своей локали, пока язык в профиле другой."""
    stripped = (text or "").strip()
    return bool(stripped) and any(
        stripped == i18n.t_in(lang, "ai.screen.build_program_seed").strip() for lang in i18n.SUPPORTED
    )


async def is_fresh_start(user_id: int) -> bool:
    """Ни одной законченной тренировки, ни одной сохранённой программы и пустая
    цель в профиле — смотреть модели не на что."""
    user = await db.get_user(user_id)
    if user is not None and (user["goal"] or "").strip():
        return False
    return await db.count_workouts(user_id) == 0 and await db.count_routines(user_id) == 0


# --- Что из служебной реплики видит человек -----------------------------------
# Модели уходит полный текст (`setup_answers_text`, `setup_enough_text`) и он же
# лежит в wire-снимке разговора — переписывать его нельзя: историю с изменённым
# префиксом кэш провайдера считает промахом. Поэтому фильтр стоит только на
# выдаче клиенту (/v1 history, архив разговоров, заголовок архива).


def _all_langs(key: str, **params: Any) -> list[str]:
    return [i18n.t_in(lang, key, **params) for lang in i18n.SUPPORTED]


def visible_user_text(text: str) -> str:
    """Реплика пользователя в том виде, в каком её показывают клиенту.

    Ответы на опросник — только заголовок и строки «вопрос — ответ» (с пометкой
    про пропущенные); исходная задача и служебная рамка для модели отрезаются.
    «Хватит спрашивать» — не слова человека вовсе, поэтому вместо
    инструкции модели показываем короткую реплику (скрыть ход нельзя: пара
    «вопрос — ответ» в ленте развалилась бы). Всё прочее возвращается как есть.
    """
    if not text:
        return text
    for lang in i18n.SUPPORTED:
        enough = i18n.t_in(lang, "ai.screen.setup_enough_frame")
        if text.startswith(enough):
            return i18n.t_in(lang, "ai.screen.setup_enough_visible")
        header = i18n.t_in(lang, "ai.screen.setup_answers_header")
        frame = i18n.t_in(lang, "ai.screen.setup_answers_frame")
        if not (text.startswith(header + "\n") and text.endswith(frame)):
            continue
        body = text[: len(text) - len(frame)].rstrip("\n")
        note = i18n.t_in(lang, "ai.screen.setup_skipped_note")
        has_note = body.endswith("\n" + note)
        if has_note:
            body = body[: len(body) - len(note) - 1]
        goal_marker = i18n.t_in(lang, "ai.screen.setup_original_goal", goal="\0").split("\0")[0]
        cut = body.find(goal_marker)
        if cut != -1:
            body = body[:cut]
        body = body.rstrip("\n")
        return body + ("\n" + note if has_note else "")
    return text
