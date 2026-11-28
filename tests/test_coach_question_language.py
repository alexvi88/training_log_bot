"""Тренер отвечает на языке вопроса, если атлет спросил не на языке профиля
(владелец: вопрос по-русски у атлета с en — ответ пришёл по-английски).
Оговорка — в самой языковой директиве, на всех языках."""

import i18n

EXCEPTION = {
    "ru": "на языке вопроса",
    "en": "in the language of that question",
    "es": "en el idioma de esa pregunta",
    "pt": "na língua dessa pergunta",
    "pt-PT": "na língua dessa pergunta",
    "de": "in der Sprache dieser Frage",
    "fr": "dans la langue de cette question",
    "tr": "o sorunun dilinde",
}


def test_language_tail_lets_the_question_language_win():
    for lang, phrase in EXCEPTION.items():
        assert phrase in i18n.t_in(lang, "ai.language_tail"), lang
