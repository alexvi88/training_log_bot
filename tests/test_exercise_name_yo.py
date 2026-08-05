"""Ё и Е в названиях упражнений — одна буква для поиска.

.lower() «ё» не сворачивает, а люди (и модель AI-тренера) пишут «Жим лёжа» и
«Жим лежа» вперемешку: без нормализации совпадающее по сути имя не находилось,
упражнение уходило в unresolved и стоило модели лишнего раунда уточнений.
"""


async def _exercise(db, user_id, name):
    group_id = await db.create_muscle_group(user_id, "Грудь")
    return await db.create_exercise(user_id, name, group_id)


async def test_users_exercise_with_yo_is_found_by_a_query_with_e(fresh_db, user_id):
    db = fresh_db
    ex_id = await _exercise(db, user_id, "Жим лёжа")
    found = await db.find_exercise_by_name(user_id, "жим лежа")
    assert found is not None and found["id"] == ex_id


async def test_users_exercise_with_e_is_found_by_a_query_with_yo(fresh_db, user_id):
    db = fresh_db
    ex_id = await _exercise(db, user_id, "Тяга к поясу лежа")
    found = await db.find_exercise_by_name(user_id, "Тяга к поясу лёжа")
    assert found is not None and found["id"] == ex_id


async def test_global_template_with_yo_matches_a_query_with_e(fresh_db, user_id):
    # «Жим штанги лёжа» — посевной глобальный шаблон (seed_data), в нём есть «ё».
    db = fresh_db
    template = await db._find_global_template_by_name("Жим штанги лежа")
    assert template is not None and template["name"] == "Жим штанги лёжа"

    # И резолвер имён программ (own → template) через него тоже находит.
    kind, display_name = await db.resolve_exercise_name(user_id, "жим штанги лежа")
    assert kind == "template"
    assert display_name.startswith("Жим штанги лёжа")
