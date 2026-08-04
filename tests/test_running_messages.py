"""Placeholder-фразы «тренер думает» — должны попадать в тему вопроса.

До этого пул был один общий на всё: вопрос про питание крутил "гружу знания,
как штангу", вопрос про программу — "остываю от подхода мысли". running_texts.py
раскладывает фразы по темам и подбирает нужную по ключевым словам-стемам —
проверяем здесь, что подбор реально работает на характерных запросах, не
спотыкается о "ё"/"е" и регистр (SQLite и наивные сравнения фолдят только
ASCII, а живые вопросы приходят как есть), не разваливает ротацию и не роняет
handlers/ai_trainer.py, который теперь берёт пул именно отсюда, а не из
собственного плоского списка.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage

import running_texts
from handlers import ai_trainer

# asyncio_mode=auto (pytest.ini); часть проверок ниже — чисто по running_texts,
# без event loop вообще.


# ---------- подбор темы по характерным запросам ----------

# Каждый вопрос ниже написан так, как реально спросил бы человек — не голым
# ключевым словом, а обычной фразой, — чтобы проверка не превратилась в тест
# самого себя (стем внутри стема).
_CHARACTERISTIC_QUESTIONS = {
    running_texts.PROGRAM: "Составь мне программу тренировок на месяц, пожалуйста",
    running_texts.EXERCISE_PROGRESS: "Как там прогресс в жиме лёжа? Растёт ли рабочий вес?",
    running_texts.WEEKLY_VOLUME: "Какой у меня недельный объём на грудь, не перегружаю ли эту группу?",
    running_texts.NUTRITION: "Сколько белка мне нужно есть, чтобы расти? Как с калориями?",
    running_texts.BODYWEIGHT: "Вешу сейчас 82 кг, хочу похудеть к лету",
    running_texts.TECHNIQUE: "Расскажи про технику приседа, не ошибаюсь ли я в движении?",
    running_texts.RECOVERY: "У меня болит плечо после жима, как восстановиться?",
    running_texts.MOTIVATION: "Что-то лень тренироваться, пропала мотивация",
    running_texts.HISTORY: "Покажи историю моих тренировок за прошлый месяц",
    running_texts.TODAY: "Что делать сегодня на тренировке?",
}


@pytest.mark.parametrize("topic, question", list(_CHARACTERISTIC_QUESTIONS.items()))
def test_characteristic_question_picks_its_own_topic(topic, question):
    assert running_texts.classify(question) == topic
    # И пул для этой темы реально существует и не путается с чужим.
    assert running_texts.pool_for(question) is running_texts.POOLS[topic]


def test_unrelated_question_falls_back_to_default():
    """Вопрос без единого совпадения по темам — это не баг, а обычный трёп с
    тренером ("привет, как дела?"), для него и держим общий пул."""
    assert running_texts.classify("Привет! Как у тебя дела, тренер?") == running_texts.DEFAULT_TOPIC


def test_empty_question_falls_back_to_default():
    """Пустой текст (например, фото без подписи) не должен падать с ошибкой —
    просто дефолтный пул, там ведь и правда непонятно, про что вопрос."""
    assert running_texts.classify("") == running_texts.DEFAULT_TOPIC


def test_yo_and_ye_do_not_split_the_same_topic():
    """«объём»/«объем» — одно и то же слово для человека, и должно остаться одним
    и тем же словом для классификатора: LOWER() в SQLite и наивные сравнения
    регистра фолдят только ASCII, а вот .lower() и ё→е — уже наш собственный
    код, и его легко забыть в одном из двух написаний."""
    with_yo = "Какой у меня недельный объём на спину?"
    with_ye = "Какой у меня недельный объем на спину?"
    assert running_texts.classify(with_yo) == running_texts.classify(with_ye) == running_texts.WEEKLY_VOLUME


def test_upper_case_question_classifies_the_same_as_lower_case():
    question = "сколько белка есть, чтобы расти"
    assert running_texts.classify(question.upper()) == running_texts.classify(question) == running_texts.NUTRITION


# ---------- сами пулы ----------


def test_every_pool_is_non_empty_with_no_blank_or_duplicate_phrases():
    for topic, pool in running_texts.POOLS.items():
        assert len(pool) > 1, f"пул {topic} слишком мал для честной ротации"
        assert all(phrase.strip() for phrase in pool), f"пустая фраза в пуле {topic}"
        assert len(set(pool)) == len(pool), f"повторяющаяся фраза внутри пула {topic}"


def test_pool_count_grew_well_past_the_original_flat_list():
    """Раньше был один список на двадцать фраз — с темами их должно стать
    заметно больше, иначе "разнообразие" осталось на бумаге."""
    total = sum(len(pool) for pool in running_texts.POOLS.values())
    assert total > 100


# ---------- ротация ----------


def test_rotation_never_repeats_the_same_phrase_twice_in_a_row():
    pool = running_texts.PROGRAM_POOL
    previous = running_texts.pick(pool)
    for _ in range(200):
        current = running_texts.pick_different(pool, previous)
        assert current != previous
        previous = current


def test_rotation_does_not_hang_on_a_single_phrase_pool():
    """Пул из одной фразы не должен уйти в бесконечный while — раньше это было
    гарантией на большом пуле, но не на вырожденном."""
    only = ["🏋️ единственная фраза..."]
    assert running_texts.pick_different(only, only[0]) == only[0]


# ---------- вживую через handlers.ai_trainer._handle_question ----------


async def _make_state(user_id: int) -> FSMContext:
    key = StorageKey(bot_id=1, chat_id=user_id, user_id=user_id)
    return FSMContext(storage=MemoryStorage(), key=key)


def _make_chat_message(user_id: int, text: str):
    message = MagicMock()
    message.text = text
    message.from_user = SimpleNamespace(id=user_id, username="tester")
    message.reply = AsyncMock()
    placeholder = MagicMock()
    placeholder.edit_text = AsyncMock()
    placeholder.chat = SimpleNamespace(id=user_id)
    placeholder.message_id = 9
    message.answer = AsyncMock(return_value=placeholder)
    message.bot = MagicMock()
    return message


async def test_handle_question_shows_a_topic_relevant_placeholder_first(fresh_db, user_id, monkeypatch):
    """Тот самый первый placeholder, который видно ещё до единого tool-call —
    он и есть самая важная фраза (см. модульный докстринг running_texts.py), и
    именно он должен реально попасть в тему запроса, а не в дефолтный пул."""
    monkeypatch.setattr(ai_trainer.ai_trainer, "ask", AsyncMock(return_value="ешь больше белка"))

    state = await _make_state(user_id)
    await state.set_state("AITrainerFlow:chatting")
    message = _make_chat_message(user_id, "Сколько белка мне нужно есть, чтобы расти?")

    await ai_trainer.ai_question(message, state)

    first_call_text = message.answer.await_args_list[0].args[0]
    assert first_call_text in running_texts.NUTRITION_POOL
