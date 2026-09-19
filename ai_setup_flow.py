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
