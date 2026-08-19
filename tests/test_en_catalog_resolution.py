"""Английское имя из НАШЕГО же каталога должно резолвиться обратно.

Разбор живого диалога с англоязычным атлетом (18.08): тренер собрал программу,
назвал упражнения по-английски — и два движения завёл с русскими именами
(«Приседания с собственным весом»), а недельный объём посчитал только по ногам.
Обе странности от одной причины: идентичность шаблона и группы в базе русская
навсегда, человеку бот показывает перевод, а обратного хода не было — показанное
имя не принималось назад ни как упражнение, ни как группа.
"""

import ai_trainer
import i18n
import seed_data

TEMPLATES = (
    ("Отжимания от пола", "Push-Ups"),
    ("Подтягивания", "Pull-Ups"),
    ("Планка", "Plank"),
)


def test_localized_names_map_back_to_identity():
    for canonical, shown in TEMPLATES:
        assert seed_data.localized_exercise_name(canonical, "en") == shown
        assert seed_data.canonical_exercise_name(shown) == canonical
    # Регистр не важен, выдуманное имя не подставляется.
    assert seed_data.canonical_exercise_name("push-ups") == "Отжимания от пола"
    assert seed_data.canonical_exercise_name("Пельмени со штангой") is None
    assert seed_data.canonical_exercise_name("") is None


def test_localized_group_names_map_back_to_identity():
    assert seed_data.canonical_muscle_group_name("Chest") == "Грудь"
    assert seed_data.canonical_muscle_group_name("legs") == "Ноги"
    # Русское имя — тоже валидный вход: это и есть идентичность.
    assert seed_data.canonical_muscle_group_name("Спина") == "Спина"
    assert seed_data.canonical_muscle_group_name("Печень") is None


async def test_english_exercise_name_resolves_to_the_template(fresh_db, user_id):
    await fresh_db.update_user(user_id, lang="en")

    for _canonical, shown in TEMPLATES:
        kind, name = await fresh_db.resolve_exercise_name(user_id, shown)
        assert kind == "template", f"{shown} не нашёлся в каталоге"
        assert name == shown


async def test_english_exercise_name_finds_its_muscle_group(fresh_db, user_id):
    """Без этого недельный объём предложенной программы считался только по своим
    упражнениям: у англоязычного тренер называл атлету заниженное число."""
    await fresh_db.update_user(user_id, lang="en")

    assert await fresh_db.exercise_group_name(user_id, "Push-Ups") == "Грудь"
    assert await fresh_db.exercise_group_name(user_id, "Pull-Ups") == "Спина"


async def test_english_group_name_resolves_for_create_exercise(fresh_db, user_id):
    """Тренер зовёт create_exercise с group="Chest" — той группой, которую бот
    ему сам и показал. Раньше это была ошибка «такой группы нет»."""
    await fresh_db.update_user(user_id, lang="en")

    with i18n.use_lang("en"):
        payload, _undo = await ai_trainer._create_exercise(
            user_id, {"name": "Sandbag Carry", "group": "Chest"}
        )

    assert payload.get("ok") is True, payload
    assert payload["created"]["name"] == "Sandbag Carry"


async def test_weekly_volume_of_a_proposal_counts_the_catalog(fresh_db, user_id):
    """Тот самый подсчёт: пять подходов груди и четыре спины из каталога."""
    await fresh_db.update_user(user_id, lang="en")
    days = [
        {
            "items": [
                {"name": "Push-Ups", "sets": 5},
                {"name": "Pull-Ups", "sets": 4},
                {"name": "Не существует такого", "sets": 3},
                {"name": "Plank", "sets": None},
            ]
        }
    ]

    with i18n.use_lang("en"):
        totals = await ai_trainer._weekly_sets_by_group(user_id, days)

    # Ключи — на языке атлета: числа отсюда тренер называет ему в ответе.
    assert totals == {"Chest": 5, "Back": 4}


async def test_weekly_volume_groups_speak_the_users_language(fresh_db, user_id):
    await fresh_db.update_user(user_id, lang="en")

    with i18n.use_lang("en"):
        payload = await ai_trainer._weekly_volume(user_id)

    shown = {row["group"] for row in payload["groups"]}
    assert "Chest" in shown and "Legs" in shown
    assert "Грудь" not in shown
