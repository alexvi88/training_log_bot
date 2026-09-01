from aiogram.fsm.state import State, StatesGroup


class WorkoutFlow(StatesGroup):
    idle = State()
    picking_group = State()
    picking_exercise = State()
    creating_exercise_name = State()
    logging_set = State()
    logging_exercise_note = State()
    confirming_finish = State()
    confirming_finish_date = State()
    awaiting_finish_date = State()
    editing_finished_note = State()


class ExerciseManage(StatesGroup):
    picking_group = State()
    picking_exercise = State()
    editing_name = State()
    editing_group = State()
    editing_description = State()
    new_group_name = State()
    creating_exercise_name = State()
    new_exercise_group = State()  # name typed from "📋 Все", group still to pick
    awaiting_photo = State()
    picking_merge_target = State()


class HistoryFlow(StatesGroup):
    browsing = State()


class ProgressFlow(StatesGroup):
    picking_group = State()
    picking_exercise = State()


class SettingsFlow(StatesGroup):
    menu = State()
    # Экран «Что тренер про тебя знает». Отдельное состояние нужно ровно затем,
    # чтобы у обещания «просто скажи мне, как правильно» был слушатель: в
    # SettingsFlow.menu набранный текст уходил в никуда и получал «Не понял 🤔».
    profile = State()


class BackfillFlow(StatesGroup):
    awaiting_date = State()


class ResolveFlow(StatesGroup):
    """Shared sub-flow for mapping a free-typed exercise name to an exercise row."""
    picking = State()
    picking_new_group = State()


class EditWorkoutFlow(StatesGroup):
    viewing = State()          # the workout's exercise list
    viewing_exercise = State()  # one exercise's sets
    awaiting_date = State()
    editing_set = State()
    adding_set = State()
    adding_exercise_group = State()
    adding_exercise_pick = State()


class AdminFlow(StatesGroup):
    browsing_users = State()
    browsing_history = State()
    browsing_pushes = State()
    browsing_ai_users = State()
    browsing_activity_users = State()
    browsing_activity = State()
    browsing_activity_all = State()
    broadcast_choosing_lang = State()
    broadcast_awaiting_message = State()
    broadcast_awaiting_message_en = State()
    broadcast_confirming = State()


class AITrainerFlow(StatesGroup):
    chatting = State()


class BodyweightFlow(StatesGroup):
    viewing = State()
    browsing = State()  # экран «✏️ Записи» — список с удалением любой записи


class RoutineFlow(StatesGroup):
    naming = State()
    naming_program = State()   # многодневка из нескольких прошлых тренировок
    naming_day = State()       # новый день внутри уже существующей программы
    renaming = State()
    renaming_program = State()
    adding_exercise_group = State()
    adding_exercise_pick = State()
    adding_exercise_target = State()
    editing_exercise_target = State()  # схема подходов у уже добавленного упражнения


class ImportFlow(StatesGroup):
    awaiting_file = State()
    mapping_columns = State()
    confirming = State()


class FeedbackFlow(StatesGroup):
    awaiting_message = State()


class FoodDiaryFlow(StatesGroup):
    viewing = State()          # one day's diary — typing/sending a photo here logs food
    confirming = State()       # model's guess is on screen, awaiting подтверждение/правку
    correcting = State()       # user is typing what the model got wrong
    browsing_history = State()
