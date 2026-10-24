"""Откат того, что тренер сделал сам, — общий для бота и для приложения.

Инструменты тренера, которые пишут в базу (вес, создание и
переименование упражнения, копия программы, правка профиля — см.
`_UNDO_NOTE` в ai_trainer.py), возвращают вторым значением описание отката:
`{"label": ..., "undo": {"kind": ..., ...}}`. Модель в тексте ответа честно
обещает кнопку («Если мимо — отменишь кнопкой ниже»), и обещание это должно
выполняться в обоих клиентах, а не только в Telegram.

Раньше и хранение описаний, и само применение жили в handlers/ai_trainer.py,
то есть были заперты в aiogram: HTTP-клиент (`api_v1_ai.py`) собирал те же
описания в `collect_action` и выбрасывал, поэтому в приложении кнопки не было
вовсе — текст про неё был, а кнопки нет. Здесь — только применение,
одинаковое для обоих: бот берёт описание из FSM, HTTP — из
`ai_undo_actions` (см. db.take_ai_undo_action), а дальше оба зовут `apply`.

Хранение сознательно осталось у вызывающих: у бота это FSM с ключом в
callback_data (64 байта, в которые описание не влезает), у HTTP — таблица с
ключом в JSON. Общее у них только то, что делать по этому ключу.
"""

from typing import Optional

import ai_trainer
import db
import i18n


async def apply(user_id: int, undo: dict) -> Optional[str]:
    """Вернуть как было. Возвращает текст для пользователя либо None, если не вышло.

    Владельца проверяем на каждом шаге заново: между записью и тапом по кнопке
    проходит сколько угодно времени, а id приезжает из описания, которое
    положил прошлый ход, — но сама строка в базе к этому моменту могла и
    смениться.
    """
    kind = undo.get("kind")

    if kind == "batch":
        # В обратном порядке: ход мог сначала создать упражнение, а потом
        # записать в него подход — снимать надо с конца, иначе откат упрётся
        # в то, что ещё на нём висит.
        items = list(undo.get("items") or [])
        done = 0
        for item in reversed(items):
            if await apply(user_id, item) is not None:
                done += 1
        if done == 0:
            return None
        if done < len(items):
            # Часть уже не откатывалась — молчать об этом нельзя: человек
            # решит, что вернулось всё.
            return i18n.t("ai.screen.undo.batch_partial", done=done, total=len(items))
        return i18n.t("ai.screen.undo.batch_all", done=done)

    # Не откаты, а предложения тренера, которые в приложении выполняет та же
    # кнопка под ответом (api_v1_ai._store_undo_actions): «🗄 В архив» и
    # «📬 Передать разработчику». В Telegram у них свои экраны с «точно?»;
    # в приложении подпись кнопки сама называет действие, а архив обратим.
    if kind == "archive_exercises":
        archived = 0
        for ex_id in undo.get("ids") or []:
            ex = await db.get_exercise(int(ex_id))
            if ex is None or ex["user_id"] != user_id or ex["is_template"] or ex["is_archived"]:
                continue
            await db.archive_exercise(int(ex_id))
            archived += 1
        return i18n.t("ai.screen.archived_count", n=archived) if archived else i18n.t("ai.screen.already_archived")

    if kind == "feedback":
        import api_v1_feedback
        from api_v1_common import ApiError

        # Шапка — админу, по-русски при любом языке атлета (как в боте,
        # handlers/ai_trainer.ai_send_feedback).
        with i18n.use_lang("ru"):
            label = undo.get("label") or i18n.t("ai.screen.feedback.default_label")
            header = i18n.t("ai.screen.feedback_header", label=label, who=str(user_id), id=user_id)
        try:
            await api_v1_feedback.store_and_forward(user_id, f"{header}\n\n{undo.get('text') or ''}", None)
        except ApiError:
            return None
        return i18n.t("ai.screen.feedback.sent")

    if kind == "bodyweight":
        if await db.delete_bodyweight_log(int(undo["id"]), user_id):
            return i18n.t("ai.screen.undo.bodyweight")
        return None

    if kind == "bodyweight_restore":
        await db.add_bodyweight_log(user_id, undo["weight"], logged_at=undo.get("logged_at"))
        return i18n.t("ai.screen.undo.bodyweight_restore", weight=f"{undo['weight']:g}")

    if kind == "exercise_new":
        if await db.delete_exercise_if_unused(int(undo["id"]), user_id):
            name = undo.get("name") or i18n.t("ai.screen.undo.generic_exercise")
            return i18n.t("ai.screen.undo.exercise_removed", name=name)
        # По нему уже успели что-то записать — сносить нельзя, чужие данные
        # уедут вместе с ним. Честнее сказать, чем сделать вид, что откатили.
        return None

    if kind == "exercise_name":
        exercise = await db.get_exercise(int(undo["id"]))
        if exercise is None or exercise["user_id"] != user_id:
            return None
        if not await db.update_exercise_name(exercise["id"], undo["name"]):
            return None
        return i18n.t("ai.screen.undo.name_restored", name=undo["name"])

    if kind == "exercise_group":
        exercise = await db.get_exercise(int(undo["id"]))
        if exercise is None or exercise["user_id"] != user_id:
            return None
        await db.update_exercise_group(exercise["id"], int(undo["group_id"]))
        if undo.get("name"):
            return i18n.t("ai.screen.undo.group_named", name=undo["name"])
        return i18n.t("ai.screen.undo.group_generic")

    if kind == "program_name":
        program = await db.get_program(int(undo["id"]))
        if program is None or program["user_id"] != user_id:
            return None
        if not await db.rename_program_by_id(program["id"], undo["name"]):
            return None
        return i18n.t("ai.screen.undo.name_restored", name=undo["name"])

    if kind == "routine_name":
        routine = await db.get_routine(int(undo["id"]))
        if routine is None or routine["user_id"] != user_id:
            return None
        await db.rename_routine(routine["id"], undo["name"])
        return i18n.t("ai.screen.undo.name_restored", name=undo["name"])

    if kind == "program_new":
        program = await db.get_program(int(undo["id"]))
        if program is None or program["user_id"] != user_id:
            return None
        await db.delete_program_by_id(program["id"])
        return i18n.t("ai.screen.undo.program_copy_removed", name=(undo.get("name") or program["name"]))

    if kind == "profile":
        before = undo.get("before") or {}
        fields = {k: v for k, v in before.items() if k in ai_trainer.PROFILE_FIELDS}
        if not fields:
            return None
        await db.update_user(user_id, **fields)
        names = ", ".join(ai_trainer.PROFILE_LABELS.get(k, k) for k in fields)
        return i18n.t("ai.screen.undo.profile_restored", names=names)

    return None
