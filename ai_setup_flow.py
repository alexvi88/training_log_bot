"""Опросник тренера перед сборкой программы (ai_trainer.ask_setup_questions) —
общая для бота (handlers/ai_trainer.py) и REST `/v1` (api_v1_ai.py) логика:
что задать первым вопросом (цель), сколько кругов уточнений терпеть и как
собрать ответы в одно сообщение модели. Экранная часть (отправка вопроса
отдельным сообщением в Telegram, кнопки, правка старого вопроса) у каждого
интерфейса своя — здесь только решение, а не показ (тот же приём, что у
ai_program_actions.py и progression_data.py).
"""

from __future__ import annotations

from typing import Any, Optional

import db
import i18n

# Сколько кругов уточнений подряд разрешаем одной просьбе. Второй круг нужен по
# делу: увидев в ответах встречный вопрос или «хз», тренер вправе ответить и
# переспросить то, что осталось открытым. А вот без потолка он способен гонять
# уточнения по кругу, и человек не увидит программу никогда — поэтому на третий
# заход опросник уже не показывается, а тренеру уходит прямое «собирай на
# дефолтах».
SETUP_MAX_ROUNDS = 2

# По этим кускам узнаём вопрос про цель, который модель всё-таки задала сама
# (промпт запрещает, но запрет — не гарантия). Совпало — свой не подставляем:
# два вопроса про одно подряд читаются как поломка. Смешивает оба языка сразу
# (см. running_texts._TOPIC_STEMS — тот же приём): язык ответа модели зависит
# от языка пользователя, а не от языка этого файла, так что маркер обязан
# ловить обе версии вопроса о цели.
SETUP_GOAL_MARKERS = (
    "цел", "чего хочешь", "чего ждёшь", "зачем тренир", "какой результат",
    # Голое "goal" ловило обычные тренерские вопросы не про цель тренировок
    # вовсе ("What's your rep goal for this exercise?", "any weight goal for
    # this month?") — и защёлкивало "цель уже спросили" раньше, чем реальный
    # вопрос вообще звучал. Фразы ниже — так же специфичны, как русские выше.
    "your goal", "training goal", "what do you want", "what are you after",
    "why train", "why do you train", "what result",
)


# Вопрос про цель бот/приложение задают сами, а не полагаются на модель. Цель
# была одним пунктом промпта среди пяти, а слотов в опроснике меньше, чем тем,
# — и она регулярно проигрывала дням, времени, травмам и сплиту: человек
# отвечал на четыре вопроса и получал программу, ни разу не сказав, ЗАЧЕМ он
# тренируется. Это единственная вводная, без которой программа собирается
# наугад, поэтому она идёт первой и не зависит от того, вспомнит ли о ней
# модель.
def setup_goal_question() -> dict[str, Any]:
    return {
        "question": i18n.t("ai.screen.setup_goal.question"),
        "choices": [
            i18n.t("ai.screen.setup_goal.choice_mass"),
            i18n.t("ai.screen.setup_goal.choice_strength"),
            i18n.t("ai.screen.setup_goal.choice_lose"),
            i18n.t("ai.screen.setup_goal.choice_comeback"),
        ],
    }


async def questions_with_goal(
    user_id: int, questions: list[dict[str, Any]], previous: dict[str, Any]
) -> tuple[list[dict[str, Any]], bool]:
    """Поставить вопрос про цель первым — если её ещё никто не спросил.

    Возвращает (вопросы, «цель закрыта»). Второе переживает круги уточнений:
    профиль между кругами не меняется (модель сохранит цель только в финальной
    сборке), так что без флага второй круг задал бы тот же вопрос ещё раз.

    `SETUP_MAX_QUESTIONS` — потолок на число вопросов в опроснике — держится в
    ai_trainer.py (там же, где остальные потолки propose_program/ask_setup_questions);
    импортируем его отсюда, а не дублируем числом.
    """
    import ai_trainer  # локально — иначе цикл импортов (ai_trainer тут не нужен нигде, кроме этой константы)

    if previous.get("goal_asked"):
        return questions, True
    asked_by_model = any(
        marker in (question.get("question") or "").lower()
        for question in questions
        for marker in SETUP_GOAL_MARKERS
    )
    if asked_by_model:
        return questions, True
    user = await db.get_user(user_id)
    if user is not None and (user["goal"] or "").strip():
        # Цель уже записана с его слов — переспрашивать то, что бот показывает
        # на экране «Обо мне», значит признаваться, что он этого не помнит.
        return questions, True
    goal_question = {
        "question": setup_goal_question()["question"],
        "choices": list(setup_goal_question()["choices"]),
    }
    # Срезаем с хвоста: свой вопрос идёт первым, а лишним оказывается последний
    # вопрос модели — он же и наименее важный, вопросы она ставит по убыванию.
    return ([goal_question] + questions)[: ai_trainer.SETUP_MAX_QUESTIONS], True


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

_FRESH_QUESTIONS = (
    ("days", ("2", "3", "4", "5")),
    ("time", ("45", "60", "90")),
    ("place", ("gym", "home_weights", "home_bare", "bar")),
    ("experience", ("new", "year", "years", "veteran")),
    ("injuries", ("none", "back", "knees", "shoulders")),
)


def fresh_start_questions() -> list[dict[str, Any]]:
    """Шесть вопросов чистого листа на языке текущего контекста: цель — первой
    (тот же `setup_goal_question`, что подставляется в обычном пути), дальше
    дни, время, место и инвентарь, опыт, травмы. Не больше
    `ai_trainer.SETUP_MAX_QUESTIONS` — иначе потолок молча срезал бы последний."""
    import ai_trainer  # локально — цикл импортов, как в questions_with_goal

    questions = [setup_goal_question()]
    for slot, choices in _FRESH_QUESTIONS:
        questions.append({
            "question": i18n.t(f"ai.screen.setup_fresh.{slot}.question"),
            "choices": [i18n.t(f"ai.screen.setup_fresh.{slot}.choice_{choice}") for choice in choices],
        })
    return questions[: ai_trainer.SETUP_MAX_QUESTIONS]


def fresh_start_reply() -> str:
    return i18n.t("ai.screen.setup_fresh.reply")


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
